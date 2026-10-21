"""Canales de aviso (Telegram, Teams, archivo) y el despachador que los alimenta.

Por qué un despachador con cola y un hilo POR CANAL: el recálculo de fallas
corre con el lock del servicio tomado; si enviara directo, un Telegram lento
o un proxy corporativo colgado congelaría la ingesta de la extensión. Con un
hilo por canal, además, Teams caído (20 s de timeout x 3 intentos) no atrasa
el aviso por Telegram.

Reglas de seguridad de este módulo:
- El token de Telegram va en la URL (`/bot<token>/sendMessage`) y el webhook
  de Teams ES un secreto (trae la firma `sig=`). Ninguno aparece en
  `ErrorEnvio`, en `estado()`, en `repr()` ni en el log: los errores se
  redactan y las excepciones de urllib se re-lanzan `from None` para que la
  traza encadenada tampoco los arrastre.
- Sólo se envía lo que trae `Mensaje` (ya sin datos personales, ver mensajes.py).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import token_telegram, webhook_teams
from .modelo import Mensaje

log = logging.getLogger(__name__)

API_TELEGRAM = "https://api.telegram.org"
LIMITE_TELEGRAM = 4096

# Abrir = callable(url, data_bytes, headers, timeout) -> (status, body_bytes).
Abrir = Callable[[str, bytes | None, dict, float], tuple[int, bytes]]

_RE_TOKEN_TG = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")


class ErrorEnvio(Exception):
    """Un canal no pudo entregar un mensaje. El texto NUNCA incluye secretos.

    - `permanente`: reintentar no sirve (token malo, chat inexistente, bot
      expulsado): el despachador no gasta los reintentos.
    - `pendientes`: destinos que fallaron cuando otros sí recibieron (Telegram
      con varios chats). El reintento va sólo a ésos: reenviar a todos
      duplicaría el aviso en el grupo que ya lo tenía.
    """

    def __init__(self, mensaje: str, *, permanente: bool = False,
                 pendientes: list | None = None, status: int | None = None):
        super().__init__(mensaje)
        self.permanente = permanente
        self.pendientes = list(pendientes) if pendientes else None
        self.status = status


def redactar(texto: Any, *secretos: str) -> str:
    """Quita secretos conocidos y cualquier cosa con forma de token de Telegram."""
    s = str(texto)
    for sec in secretos:
        if sec:
            s = s.replace(sec, "***")
    return _RE_TOKEN_TG.sub("bot***", s)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def opener_http(proxy: str, ca_bundle: str, timeout: float) -> urllib.request.OpenerDirector:
    """OpenerDirector de urllib con proxy y CA corporativa opcionales.

    - Sin `proxy` se deja el ProxyHandler por defecto de urllib, que lee
      HTTPS_PROXY y, en Windows, la configuración de proxy del sistema
      (registro): en el PC corporativo suele bastar con eso.
    - `ca_bundle` se AGREGA a las CA del sistema (no las reemplaza): con un
      proxy que inspecciona TLS, la CA de la empresa ya suele estar en el
      almacén de Windows; el archivo es para cuando no.
    - `timeout` no lo guarda OpenerDirector; queda en `opener.timeout_s`.
    """
    ctx = ssl.create_default_context()
    if ca_bundle:
        ctx.load_verify_locations(cafile=ca_bundle)
        # Python 3.13 activa VERIFY_X509_STRICT por defecto y varias CA de
        # inspección TLS corporativa no lo cumplen (falta keyUsage): fallaba
        # con "CA cert does not include key usage extension". Se relaja SÓLO
        # cuando el usuario declaró una CA propia; la cadena se sigue verificando.
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    handlers: list = [urllib.request.HTTPSHandler(context=ctx)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    opener.timeout_s = timeout   # type: ignore[attr-defined]
    return opener


def _abrir_urllib(opener: urllib.request.OpenerDirector) -> Abrir:
    def abrir(url: str, data: bytes | None, headers: dict, timeout: float) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=data, headers=dict(headers),
                                     method="POST" if data is not None else "GET")
        try:
            with opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            # 4xx/5xx traen un cuerpo útil (Telegram explica el error en JSON).
            try:
                cuerpo = e.read()
            except Exception:    # noqa: BLE001
                cuerpo = b""
            return e.code, cuerpo
    return abrir


def _json(body: bytes) -> dict:
    try:
        d = json.loads(body.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _html_a_texto(html: str) -> str:
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", html or ""))


# --------------------------------------------------------------------------
# Canales
# --------------------------------------------------------------------------


class Notificador:
    """Base. `enviar` entrega o levanta ErrorEnvio (nunca otra excepción con secretos)."""

    nombre: str = "base"

    def enviar(self, m: Mensaje) -> None:   # pragma: no cover - interfaz
        raise NotImplementedError


class TelegramNotificador(Notificador):
    """sendMessage a cada chat configurado, con HTML y respaldo en texto plano.

    `dormir` y `reloj` son inyectables para que los tests no esperen de verdad.
    """

    nombre = "telegram"
    # Telegram limita ~1 mensaje/s por chat (y 20/min por grupo): con 1,1 s
    # entre mensajes al mismo chat una ráfaga (resumen + varias nuevas) no
    # dispara 429.
    PAUSA_MISMO_CHAT_S = 1.1
    MAX_RETRY_AFTER_S = 60

    def __init__(self, token: str, chats: list, timeout_s: float = 20, proxy: str = "",
                 ca_bundle: str = "", abrir: Abrir | None = None,
                 dormir: Callable[[float], None] = time.sleep,
                 reloj: Callable[[], float] = time.monotonic):
        self._token = str(token or "")
        self.chats = list(chats or [])
        self.timeout_s = float(timeout_s)
        self._abrir = abrir or _abrir_urllib(opener_http(proxy, ca_bundle, timeout_s))
        self._dormir = dormir
        self._reloj = reloj
        self._ultimo_envio: dict[str, float] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"TelegramNotificador(chats={len(self.chats)})"

    # ------------------------------------------------------------ bajo nivel

    def llamar(self, metodo: str, payload: dict, timeout: float | None = None) -> tuple[int, dict]:
        """POST JSON a la API. Devuelve (status, json). Errores de red -> ErrorEnvio redactado."""
        url = f"{API_TELEGRAM}/bot{self._token}/{metodo}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            status, body = self._abrir(url, data, {"Content-Type": "application/json"},
                                       timeout if timeout is not None else self.timeout_s)
        except ErrorEnvio:
            raise
        except Exception as e:      # noqa: BLE001 — red, DNS, TLS, proxy
            raise ErrorEnvio(f"Telegram {metodo}: error de red: "
                             f"{type(e).__name__}: {redactar(e, self._token)}") from None
        return int(status or 0), _json(body or b"")

    def _post_espaciado(self, chat: Any, payload: dict) -> tuple[int, dict]:
        clave = str(chat)
        with self._lock:
            ultimo = self._ultimo_envio.get(clave)
            if ultimo is not None:
                falta = self.PAUSA_MISMO_CHAT_S - (self._reloj() - ultimo)
                if falta > 0:
                    self._dormir(falta)
            try:
                return self.llamar("sendMessage", payload)
            finally:
                self._ultimo_envio[clave] = self._reloj()

    # ------------------------------------------------------------ envío

    def enviar_a(self, chat: Any, html: str, texto: str | None = None) -> None:
        """Un mensaje a un chat, con los reintentos que Telegram documenta."""
        payload: dict = {"chat_id": chat, "text": html[:LIMITE_TELEGRAM], "parse_mode": "HTML",
                         "disable_web_page_preview": True}
        usados: set[str] = set()
        while True:
            status, r = self._post_espaciado(chat, payload)
            if status == 200 and r.get("ok", True):
                return
            desc = redactar(str(r.get("description") or "")[:200], self._token)
            params = r.get("parameters") if isinstance(r.get("parameters"), dict) else {}

            if status == 429 and "429" not in usados:
                usados.add("429")
                espera = params.get("retry_after")
                espera = espera if isinstance(espera, (int, float)) and espera > 0 else 5
                self._dormir(min(float(espera), self.MAX_RETRY_AFTER_S))
                continue
            if status == 400 and "can't parse entities" in desc.lower() and "plano" not in usados:
                # Un valor que se coló sin escapar rompe TODO el mensaje: mejor
                # llegar en texto plano que no llegar.
                usados.add("plano")
                log.warning("Telegram rechazó el HTML (%s); se reenvía en texto plano", desc)
                plano = texto if texto else _html_a_texto(html)
                payload = {"chat_id": payload["chat_id"], "text": plano[:LIMITE_TELEGRAM],
                           "disable_web_page_preview": True}
                continue
            nuevo = params.get("migrate_to_chat_id")
            if status == 400 and nuevo and "migra" not in usados:
                # Un grupo que pasa a supergrupo cambia de chat_id: se entrega
                # al nuevo y se avisa en el log para corregir la config.
                usados.add("migra")
                log.warning("El chat %s migró a supergrupo %s: actualiza telegram.chats", chat, nuevo)
                payload = dict(payload, chat_id=nuevo)
                continue

            pista = {401: " (token inválido: revisa la variable de entorno del token)",
                     403: " (el bot fue expulsado o bloqueado en ese chat)"}.get(status, "")
            if status == 400 and "chat not found" in desc.lower():
                pista = " (chat_id incorrecto o el bot no está en el grupo)"
            raise ErrorEnvio(f"Telegram chat {chat}: HTTP {status}: {desc or 'sin descripción'}{pista}",
                             permanente=status in (400, 401, 403, 404), status=status)

    def enviar(self, m: Mensaje, destinos: list | None = None) -> None:
        """A todos los chats (o sólo a `destinos`). Si alguno falla -> ErrorEnvio con `pendientes`."""
        objetivos = list(destinos) if destinos is not None else self.chats
        fallidos: list = []
        errores: list[ErrorEnvio] = []
        for chat in objetivos:
            try:
                self.enviar_a(chat, m.html, m.texto)
            except ErrorEnvio as e:
                fallidos.append(chat)
                errores.append(e)
        if errores:
            raise ErrorEnvio("; ".join(str(e) for e in errores),
                             permanente=all(e.permanente for e in errores), pendientes=fallidos,
                             status=errores[-1].status)


class TeamsNotificador(Notificador):
    """Webhook de Workflows (Power Automate) con una Adaptive Card.

    Los "Incoming Webhooks" clásicos de Office 365 se están retirando; el
    flujo de Workflows "Publicar en un canal cuando se recibe una solicitud
    de webhook" espera exactamente este sobre `{"type":"message","attachments":[…]}`.
    """

    nombre = "teams"

    def __init__(self, webhook_url: str, timeout_s: float = 20, proxy: str = "", ca_bundle: str = "",
                 abrir: Abrir | None = None):
        self._url = str(webhook_url or "")
        self.timeout_s = float(timeout_s)
        self._abrir = abrir or _abrir_urllib(opener_http(proxy, ca_bundle, timeout_s))

    def __repr__(self) -> str:
        return "TeamsNotificador()"

    @staticmethod
    def tarjeta(m: Mensaje) -> dict:
        cuerpo: list[dict] = [{"type": "TextBlock", "text": m.titulo, "weight": "Bolder", "wrap": True}]
        for linea in (m.texto or "").splitlines():
            if linea.strip():
                cuerpo.append({"type": "TextBlock", "text": linea, "wrap": True, "spacing": "None"})
        return {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": cuerpo,
                },
            }],
        }

    def enviar(self, m: Mensaje) -> None:
        data = json.dumps(self.tarjeta(m), ensure_ascii=False).encode("utf-8")
        try:
            status, body = self._abrir(self._url, data, {"Content-Type": "application/json"},
                                       self.timeout_s)
        except Exception as e:      # noqa: BLE001
            raise ErrorEnvio(f"Teams: error de red: {type(e).__name__}: "
                             f"{redactar(e, self._url)}") from None
        status = int(status or 0)
        if 200 <= status < 300:
            return
        detalle = redactar((body or b"")[:200].decode("utf-8", errors="replace"), self._url)
        raise ErrorEnvio(f"Teams: HTTP {status}: {detalle.strip() or 'sin detalle'}",
                         permanente=status in (400, 401, 403, 404), status=status)


class ArchivoNotificador(Notificador):
    """Una línea JSON por aviso: auditoría local y respaldo si Telegram cae."""

    nombre = "archivo"

    def __init__(self, ruta: Path, reloj: Callable[[], datetime] = datetime.now):
        self.ruta = Path(ruta)
        self._reloj = reloj
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"ArchivoNotificador({self.ruta.name})"

    def enviar(self, m: Mensaje) -> None:
        fila = {"ts": self._reloj().isoformat(timespec="seconds"), "tipo": m.tipo, "caso": m.caso,
                "titulo": m.titulo, "texto": m.texto}
        try:
            with self._lock:
                self.ruta.parent.mkdir(parents=True, exist_ok=True)
                with open(self.ruta, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(fila, ensure_ascii=False) + "\n")
        except OSError as e:
            raise ErrorEnvio(f"archivo {self.ruta.name}: {type(e).__name__}: {e.strerror or e}") from None


# --------------------------------------------------------------------------
# Despachador
# --------------------------------------------------------------------------

_FIN = object()


class _Canal:
    def __init__(self, notificador: Notificador, nombre: str):
        self.n = notificador
        self.nombre = nombre
        self.cola: queue.Queue = queue.Queue()
        self.hilo: threading.Thread | None = None
        self.pendientes = 0
        self.cond = threading.Condition()
        self.enviados = 0
        self.fallidos = 0
        self.ultimo_error: str | None = None
        self.ultimo_error_ts: datetime | None = None
        self.ultimo_ok: datetime | None = None


class Despachador:
    """Cola FIFO por canal, un hilo daemon por canal, reintentos con backoff.

    Por mensaje y canal: hasta INTENTOS intentos, esperando ESPERAS_S[0] y
    ESPERAS_S[1] entre ellos. Si se agotan y quedan más mensajes en la cola de
    ese canal, el canal descansa ESPERAS_S[2] antes del siguiente: con la red
    caída no tiene sentido martillar mensaje tras mensaje.
    """

    INTENTOS = 3
    ESPERAS_S = (2, 5, 15)

    def __init__(self, notificadores: list[Notificador], dormir: Callable[[float], None] = time.sleep,
                 reloj: Callable[[], datetime] = datetime.now):
        self._dormir = dormir
        self._reloj = reloj
        self._parar = threading.Event()
        self._iniciado = False
        self._canales: list[_Canal] = []
        vistos: dict[str, int] = {}
        for n in notificadores:
            base = getattr(n, "nombre", "") or type(n).__name__.lower()
            vistos[base] = vistos.get(base, 0) + 1
            nombre = base if vistos[base] == 1 else f"{base}-{vistos[base]}"
            self._canales.append(_Canal(n, nombre))

    @property
    def canales(self) -> list[str]:
        return [c.nombre for c in self._canales]

    def _esperar(self, s: float) -> None:
        # time.sleep no se interrumpe con detener(): en producción se espera
        # sobre el Event; un `dormir` inyectado (tests) se respeta tal cual.
        if self._dormir is time.sleep:
            self._parar.wait(s)
        else:
            self._dormir(s)

    # ------------------------------------------------------------ API

    def encolar(self, m: Mensaje) -> None:
        if not self._canales:
            log.debug("Aviso sin canales activos: %s", m.titulo)
        for c in self._canales:
            with c.cond:
                c.pendientes += 1
            c.cola.put(m)

    def iniciar(self) -> None:
        if self._iniciado:
            return
        self._iniciado = True
        self._parar.clear()
        for c in self._canales:
            c.hilo = threading.Thread(target=self._trabajar, args=(c,), name=f"aviso-{c.nombre}",
                                      daemon=True)
            c.hilo.start()

    def detener(self, timeout: float = 5) -> None:
        self._parar.set()
        for c in self._canales:
            c.cola.put(_FIN)
        limite = time.monotonic() + timeout
        for c in self._canales:
            if c.hilo is not None:
                c.hilo.join(max(0.0, limite - time.monotonic()))
            if c.pendientes:
                log.warning("Canal %s detenido con %d aviso(s) sin enviar", c.nombre, c.pendientes)
        self._iniciado = False

    def vaciar(self, timeout: float = 10) -> bool:
        """Espera a que todas las colas queden vacías. False si se acabó el tiempo."""
        limite = time.monotonic() + timeout
        for c in self._canales:
            with c.cond:
                while c.pendientes > 0:
                    resto = limite - time.monotonic()
                    if resto <= 0:
                        return False
                    c.cond.wait(resto)
        return True

    def estado(self) -> dict:
        """Por canal: contadores y último error (ya redactado). Va a /estado."""
        def iso(d: datetime | None) -> str | None:
            return d.isoformat(timespec="seconds") if d else None
        out = {}
        for c in self._canales:
            with c.cond:
                out[c.nombre] = {
                    "enviados": c.enviados,
                    "fallidos": c.fallidos,
                    "ultimo_error": c.ultimo_error,
                    "ultimo_error_ts": iso(c.ultimo_error_ts),
                    "ultimo_ok": iso(c.ultimo_ok),
                    "en_cola": c.pendientes,
                    "activo": bool(c.hilo and c.hilo.is_alive()),
                }
        return out

    # ------------------------------------------------------------ hilo

    def _trabajar(self, c: _Canal) -> None:
        while True:
            m = c.cola.get()
            if m is _FIN:
                return
            fallo = False
            try:
                fallo = not self._entregar(c, m)
            except Exception:       # noqa: BLE001 — el hilo del canal no puede morir
                log.exception("Error inesperado en el canal %s", c.nombre)
                fallo = True
            finally:
                with c.cond:
                    c.pendientes -= 1
                    c.cond.notify_all()
            if fallo and not c.cola.empty() and not self._parar.is_set():
                self._esperar(self.ESPERAS_S[-1])

    def _entregar(self, c: _Canal, m: Mensaje) -> bool:
        destinos: list | None = None
        error: ErrorEnvio | None = None
        for intento in range(1, self.INTENTOS + 1):
            try:
                if destinos is None:
                    c.n.enviar(m)
                else:
                    c.n.enviar(m, destinos=destinos)   # type: ignore[call-arg]
            except ErrorEnvio as e:
                error = e
                if e.pendientes:
                    destinos = e.pendientes
            except Exception as e:      # noqa: BLE001 — un notificador con bug no tumba el canal
                error = ErrorEnvio(f"{type(e).__name__}: {redactar(e)}")
            else:
                with c.cond:
                    c.enviados += 1
                    c.ultimo_ok = self._reloj()
                return True
            if error.permanente or intento == self.INTENTOS or self._parar.is_set():
                break
            self._esperar(self.ESPERAS_S[min(intento - 1, len(self.ESPERAS_S) - 1)])
        texto = str(error)[:500] if error else "error desconocido"
        with c.cond:
            c.fallidos += 1
            c.ultimo_error = texto
            c.ultimo_error_ts = self._reloj()
        log.warning("No se pudo enviar «%s» por %s: %s", m.titulo, c.nombre, texto)
        return False


# --------------------------------------------------------------------------
# Construcción desde la config
# --------------------------------------------------------------------------


def _ruta(valor: Any, base_dir: Path) -> str:
    v = str(valor or "").strip()
    if not v:
        return ""
    p = Path(v).expanduser()
    return str(p if p.is_absolute() else Path(base_dir) / p)


def construir_notificadores(cfg: dict, base_dir: Path,
                            env: Mapping[str, str] = os.environ) -> list[Notificador]:
    """Canales activos según cfg. Uno mal configurado se omite con log.error (no revienta)."""
    out: list[Notificador] = []
    tg = cfg.get("telegram") or {}
    if tg.get("activo"):
        token = token_telegram(cfg, env)
        chats = list(tg.get("chats") or [])
        if token and chats:
            out.append(TelegramNotificador(token, chats, timeout_s=tg.get("timeout_s", 20),
                                           proxy=tg.get("proxy", "") or "",
                                           ca_bundle=_ruta(tg.get("ca_bundle"), base_dir)))
        else:
            log.error("Telegram activo pero sin token o sin chats: el canal queda desactivado")
    tm = cfg.get("teams") or {}
    if tm.get("activo"):
        url = webhook_teams(cfg, env)
        if url:
            out.append(TeamsNotificador(url, timeout_s=tm.get("timeout_s", 20),
                                        proxy=tm.get("proxy", "") or "",
                                        ca_bundle=_ruta(tm.get("ca_bundle"), base_dir)))
        else:
            log.error("Teams activo pero sin webhook: el canal queda desactivado")
    ar = cfg.get("archivo") or {}
    if ar.get("activo") and str(ar.get("ruta") or "").strip():
        out.append(ArchivoNotificador(Path(_ruta(ar["ruta"], base_dir))))
    return out
