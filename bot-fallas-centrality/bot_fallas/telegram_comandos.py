"""Comandos de Telegram: /fallas /caso /estado /ayuda /id (long-poll getUpdates).

Por qué long-poll y no webhook: el bot corre en un PC corporativo detrás de
NAT y proxy; Telegram no puede entrar, pero el PC sí puede salir. getUpdates
con `timeout=25` deja una conexión abierta que Telegram responde apenas
llega un mensaje: latencia de ~1 s sin abrir puertos.

Privacidad: sólo los chats de `telegram.chats_comandos` (o `chats` si está
vacío) reciben datos de fallas. Un chat desconocido recibe ÚNICAMENTE su
chat_id, para que el administrador lo pueda autorizar: el bot puede quedar
en un grupo equivocado, o alguien puede encontrarlo por su @usuario.

`procesar_update` es pura (sin red): recibe un update y devuelve las
respuestas. Lo de red queda en `ciclo_una_vez`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .notificadores import Abrir, ErrorEnvio, TelegramNotificador, redactar
from .texto import escape_html, normalizar_caso

log = logging.getLogger(__name__)

COMANDOS = ("start", "ayuda", "help", "fallas", "caso", "estado", "id")

AYUDA = (
    "🤖 <b>Bot de fallas Centrality</b>\n"
    "/fallas — fallas activas, de más a menos clientes\n"
    "/caso &lt;número&gt; — detalle de un caso (hasta 40 calles), p. ej. /caso 40808515\n"
    "/estado — fuentes de Centrality, extensión y canales de aviso\n"
    "/id — el chat_id de este chat"
)

# Telegram guarda los updates no leídos 24 h. Si el PC estuvo apagado, al
# volver respondería un "/fallas" de anoche con datos de ahora: confunde más
# de lo que ayuda. Lo más viejo que esto se descarta (y se avanza el offset).
MAX_EDAD_COMANDO = timedelta(minutes=10)

_RE_CASO = re.compile(r"(\d{5,12})(?:-(?:SD|D|R)\d+)?")

ESPERA_MIN_S = 5
ESPERA_MAX_S = 60


def _no_autorizado(chat: Any) -> str:
    return (f"⛔ Chat no autorizado. Tu chat_id es <code>{escape_html(chat)}</code>; pídele al "
            "administrador que lo agregue en telegram.chats_comandos.")


class ComandosTelegram:
    def __init__(self, token: str, chats_permitidos: list, servicio,
                 estado_offset_get: Callable[[], int], estado_offset_set: Callable[[int], None],
                 timeout_s: float = 25, proxy: str = "", ca_bundle: str = "",
                 abrir: Abrir | None = None, dormir: Callable[[float], None] = time.sleep,
                 reloj: Callable[[], datetime] = datetime.now):
        self._token = str(token or "")
        self.permitidos = {str(c).strip() for c in (chats_permitidos or []) if str(c).strip()}
        self.servicio = servicio
        self._get_offset = estado_offset_get
        self._set_offset = estado_offset_set
        self.timeout_s = float(timeout_s)
        self._dormir = dormir
        self._reloj = reloj
        # Mismo cliente que los avisos (429, HTML -> texto plano, 1,1 s por chat).
        self._tx = TelegramNotificador(self._token, [], timeout_s=20, proxy=proxy, ca_bundle=ca_bundle,
                                       abrir=abrir, dormir=dormir)
        self.usuario_bot = ""        # @usuario del bot, vía getMe (best-effort)
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    def __repr__(self) -> str:
        return f"ComandosTelegram(chats_permitidos={len(self.permitidos)})"

    # ------------------------------------------------------------ lógica pura

    def autorizado(self, chat: Any) -> bool:
        return str(chat).strip() in self.permitidos

    def procesar_update(self, upd: dict) -> list[tuple[Any, str]]:
        """Update de Telegram -> [(chat_id, html)]. Sin red; nunca levanta excepción."""
        msg = upd.get("message") if isinstance(upd, dict) else None
        if not isinstance(msg, dict):
            return []
        texto = msg.get("text")
        chat_info = msg.get("chat") if isinstance(msg.get("chat"), dict) else {}
        chat = chat_info.get("id")
        if not isinstance(texto, str) or not texto.startswith("/") or chat is None:
            return []
        if isinstance(msg.get("from"), dict) and msg["from"].get("is_bot"):
            return []
        fecha = msg.get("date")
        if isinstance(fecha, (int, float)) and not isinstance(fecha, bool):
            try:
                if self._reloj() - datetime.fromtimestamp(fecha) > MAX_EDAD_COMANDO:
                    log.info("Comando de hace más de %s ignorado (chat %s)", MAX_EDAD_COMANDO, chat)
                    return []
            except (OverflowError, OSError, ValueError):
                pass

        partes = texto.strip().split(maxsplit=1)
        cmd, _, destinatario = partes[0][1:].partition("@")
        arg = partes[1].strip() if len(partes) > 1 else ""
        # En un grupo con varios bots, "/fallas@OtroBot" es para el otro.
        if destinatario and self.usuario_bot and destinatario.lower() != self.usuario_bot.lower():
            return []
        cmd = cmd.lower()

        if cmd == "id":
            return [(chat, f"Tu chat_id es <code>{escape_html(chat)}</code>")]
        if cmd not in COMANDOS:
            # En grupos llegan comandos de otros bots: sólo se contesta en privado.
            if chat_info.get("type") == "private" and self.autorizado(chat):
                return [(chat, "No conozco ese comando.\n\n" + AYUDA)]
            return []
        if not self.autorizado(chat):
            log.warning("Comando /%s desde un chat NO autorizado: %s", cmd, chat)
            return [(chat, _no_autorizado(chat))]

        log.info("Comando /%s desde el chat %s", cmd, chat)
        try:
            return [(chat, self._responder(cmd, arg))]
        except Exception:       # noqa: BLE001 — un bug en un comando no debe tumbar el hilo
            log.exception("Error respondiendo /%s", cmd)
            return [(chat, "⚠️ Error interno al consultar el bot; revisa datos/bot.log.")]

    def _responder(self, cmd: str, arg: str) -> str:
        if cmd in ("start", "ayuda", "help"):
            return AYUDA
        if cmd == "fallas":
            return self.servicio.texto_resumen() or "Sin datos."
        if cmd == "estado":
            return self.servicio.texto_estado() or "Sin datos."
        # /caso
        token = arg.split()[0] if arg else ""
        if not token:
            return "Uso: /caso &lt;número de caso&gt;, p. ej. <code>/caso 40808515</code>"
        m = _RE_CASO.fullmatch(normalizar_caso(token))
        if not m:
            return (f"No entiendo «{escape_html(token[:30])}» como número de caso. "
                    "Uso: <code>/caso 40808515</code>")
        caso = m.group(1)
        # Si el servicio no lo conoce puede devolver "" / None: el texto de
        # "no encontrado" se arma aquí para no depender de cómo lo resuelva.
        return self.servicio.texto_caso(caso) or f"No encuentro el caso <code>{escape_html(caso)}</code>"

    # ------------------------------------------------------------ red

    def _identificar(self) -> None:
        if self.usuario_bot:
            return
        try:
            status, r = self._tx.llamar("getMe", {})
        except ErrorEnvio:
            return
        if status == 200 and isinstance(r.get("result"), dict):
            self.usuario_bot = str(r["result"].get("username") or "")

    def ciclo_una_vez(self) -> int:
        """Un getUpdates (long-poll) + respuestas. Devuelve cuántos updates llegaron.

        Errores de red / HTTP -> ErrorEnvio (redactado); el bucle aplica backoff.
        """
        self._identificar()
        ultimo = int(self._get_offset() or 0)
        payload: dict = {"timeout": int(self.timeout_s), "allowed_updates": ["message"]}
        if ultimo:
            payload["offset"] = ultimo + 1
        status, r = self._tx.llamar("getUpdates", payload, timeout=self.timeout_s + 10)
        if status != 200 or not r.get("ok", False):
            desc = redactar(str(r.get("description") or "")[:200], self._token)
            pista = {401: " (token inválido)",
                     409: " (otro proceso lee este mismo bot —¿dos bots abiertos?— o tiene webhook)",
                     }.get(status, "")
            raise ErrorEnvio(f"Telegram getUpdates: HTTP {status}: {desc or 'sin descripción'}{pista}",
                             status=status)
        updates = r.get("result") if isinstance(r.get("result"), list) else []
        mayor = ultimo
        try:
            for upd in updates:
                uid = upd.get("update_id") if isinstance(upd, dict) else None
                if isinstance(uid, int) and uid <= ultimo:
                    continue
                for chat, html in self.procesar_update(upd if isinstance(upd, dict) else {}):
                    try:
                        self._tx.enviar_a(chat, html)
                    except ErrorEnvio as e:
                        log.warning("No se pudo responder al chat %s: %s", chat, e)
                if isinstance(uid, int):
                    mayor = max(mayor, uid)
        finally:
            # El offset se guarda aunque una respuesta falle: si no, un update
            # "venenoso" se re-procesaría para siempre.
            if mayor != ultimo:
                self._set_offset(mayor)
        return len(updates)

    def _esperar(self, s: float) -> None:
        if self._dormir is time.sleep:
            self._parar.wait(s)      # interrumpible por detener()
        else:
            self._dormir(s)

    def _bucle(self) -> None:
        espera = ESPERA_MIN_S
        while not self._parar.is_set():
            try:
                self.ciclo_una_vez()
                espera = ESPERA_MIN_S
            except Exception as e:      # noqa: BLE001 — la red corporativa falla; el hilo sigue
                if self._parar.is_set():
                    break
                log.warning("Comandos de Telegram: %s; reintento en %d s",
                            redactar(e, self._token), espera)
                self._esperar(espera)
                espera = min(espera * 2, ESPERA_MAX_S)

    def iniciar(self) -> None:
        if self._hilo and self._hilo.is_alive():
            return
        self._parar.clear()
        self._hilo = threading.Thread(target=self._bucle, name="telegram-comandos", daemon=True)
        self._hilo.start()

    def detener(self, timeout: float = 2) -> None:
        # El long-poll en curso puede tardar hasta timeout_s en volver; el hilo
        # es daemon, así que no se le espera más que `timeout`.
        self._parar.set()
        if self._hilo:
            self._hilo.join(timeout)
