"""Servidor HTTP local (stdlib) que recibe los reportes que manda la extensión.

Escucha SÓLO en loopback. Aun así, cualquier página web abierta en el mismo
Chrome puede intentar hablarle a 127.0.0.1, así que la seguridad no se
apoya en "nadie lo ve":

- Host: tiene que ser 127.0.0.1 / localhost / [::1]. Un ataque de DNS
  rebinding ("evil.example" que resuelve a 127.0.0.1) llega con
  `Host: evil.example` -> 421, y no puede leer /estado ni postear.
- Origin en POST: un <form> o un fetch "simple" (text/plain) de otra web
  NO dispara preflight CORS y llegaría igual al handler: podría inventar
  fallas y mandarlas al grupo de Telegram. Por eso TODO POST con Origin no
  permitido -> 403, aunque el navegador nunca deje leer la respuesta. Sin
  Origin (curl, scripts locales) se acepta: no es un navegador.
- Chrome (Private Network Access / Local Network Access) exige que el
  preflight hacia la red local responda
  `Access-Control-Allow-Private-Network: true`.
- `token_compartido` opcional (`X-Bot-Token`) para quien quiera una
  segunda llave; se compara con `hmac.compare_digest`.

Decisión sobre ErrorContrato (-> 422): se importa de `contrato` si se puede
y, además, se reconoce por NOMBRE de clase en la jerarquía. Así el servidor
no depende de que `contrato` cargue para atender /estado, y un servicio que
envuelva o re-declare la excepción igual produce 422 y no 400/500.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import re
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__
from .modelo import FUENTES_CSV
from .texto import fmt_miles

try:
    from .contrato import ErrorContrato as _ErrorContrato
except Exception:   # noqa: BLE001 — ver docstring: se reconoce también por nombre
    _ErrorContrato = None

log = logging.getLogger(__name__)

HOSTS_LOOPBACK = ("127.0.0.1", "localhost", "::1")

_RE_HOST = re.compile(r"^(?:127\.0\.0\.1|localhost|\[::1\])(?::\d{1,5})?$", re.IGNORECASE)
_RE_EXTENSION = re.compile(r"^chrome-extension://[a-p]{32}$")

_CABECERAS_PREFLIGHT = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Bot-Token",
    "Access-Control-Max-Age": "600",
}

# La página / es estática (sin JS): se prohíbe todo salvo el CSS en línea.
_CSP = ("default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'")

_TROZO = 1 << 16


def es_error_contrato(e: BaseException) -> bool:
    if _ErrorContrato is not None and isinstance(e, _ErrorContrato):
        return True
    return any(c.__name__ == "ErrorContrato" for c in type(e).__mro__)


def host_permitido(host: str | None) -> bool:
    return bool(host) and bool(_RE_HOST.match(host.strip()))


def origen_permitido(origen: str | None, cfg: dict, puerto: int) -> bool:
    """Lista exacta de config, extensiones de Chrome (opcional) o la propia página /."""
    if not origen:
        return False
    o = origen.strip().rstrip("/").lower()
    srv = cfg.get("servidor") or {}
    lista = {str(x).strip().rstrip("/").lower()
             for x in (srv.get("origenes_permitidos") or []) if isinstance(x, str) and x.strip()}
    if o in lista:
        return True
    if srv.get("permitir_extensiones", True) and _RE_EXTENSION.match(o):
        return True
    return o in (f"http://127.0.0.1:{puerto}", f"http://localhost:{puerto}", f"http://[::1]:{puerto}")


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


# --------------------------------------------------------------------------
# Página /
# --------------------------------------------------------------------------


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _clientes_txt(f: dict) -> str:
    n = f.get("clientes")
    if n is None:
        return "s/d"
    txt = fmt_miles(n) if isinstance(n, int) else str(n)
    return f"≥ {txt}" if f.get("clientes_fuente") == "nis_reclamos" else txt


def _calles_txt(f: dict, maximo: int = 5) -> str:
    calles = [c for c in (f.get("calles") or []) if isinstance(c, dict)]
    nombres = [str(c.get("nombre") or "") for c in calles[:maximo]]
    resto = len(calles) - len(nombres)
    return ", ".join(n for n in nombres if n) + (f" (+{resto})" if resto > 0 else "")


def pagina_inicio(salud: dict, fallas: list[dict]) -> str:
    """HTML mínimo para mirar el estado desde el navegador. Todo valor se escapa."""
    salud = salud if isinstance(salud, dict) else {}
    filas_f = []
    for f in fallas or []:
        if not isinstance(f, dict):
            continue
        filas_f.append(
            "<tr>"
            f"<td class='n-{_e(f.get('nivel') or 'sd')}'>{_e((f.get('nivel') or 's/d').upper())}</td>"
            f"<td><code>{_e(f.get('caso'))}</code></td>"
            f"<td class='num'>{_e(_clientes_txt(f))}</td>"
            f"<td>{_e(', '.join(map(str, f.get('comunas') or [])))}</td>"
            f"<td>{_e(_calles_txt(f))}</td>"
            f"<td>{_e(', '.join(map(str, f.get('alimentadores') or [])))}</td>"
            f"<td>{_e(str(f.get('inicio') or '').replace('T', ' ')[:16])}</td>"
            f"<td>{_e(', '.join(map(str, f.get('fuentes') or [])))}</td>"
            "</tr>")
    tabla_f = ("<table><tr><th>Nivel</th><th>Caso</th><th>Clientes</th><th>Comunas</th>"
               "<th>Calles</th><th>Alimentadores</th><th>Inicio</th><th>Fuentes</th></tr>"
               + "".join(filas_f) + "</table>") if filas_f else "<p>✅ Sin fallas activas que cumplan los filtros.</p>"

    filas_s = []
    fuentes = salud.get("fuentes") if isinstance(salud.get("fuentes"), dict) else {}
    for nombre, d in fuentes.items():
        d = d if isinstance(d, dict) else {}
        fresca = d.get("fresca")
        estado = "—" if fresca is None else ("fresca" if fresca else "VIEJA")
        if d.get("ok") is False:
            estado = "ERROR"
        notas = list(map(str, d.get("advertencias") or []))
        if d.get("error"):
            notas.insert(0, str(d["error"]))
        filas_s.append(
            "<tr>"
            f"<td>{_e(nombre)}</td><td>{_e(estado)}</td>"
            f"<td>{_e(str(d.get('recibido') or d.get('ts') or '').replace('T', ' '))}</td>"
            f"<td class='num'>{_e(d.get('edad_min', ''))}</td>"
            f"<td class='num'>{_e(d.get('registros', ''))}</td>"
            f"<td>{_e(', '.join(map(str, d.get('faltantes') or [])))}</td>"
            f"<td>{_e(' · '.join(notas))}</td>"
            "</tr>")
    tabla_s = ("<table><tr><th>Fuente</th><th>Estado</th><th>Último dato</th><th>Edad (min)</th>"
               "<th>Registros</th><th>Columnas faltantes</th><th>Notas</th></tr>"
               + "".join(filas_s) + "</table>") if filas_s else "<p>Aún no llega ningún reporte.</p>"

    canales = salud.get("notificadores") if isinstance(salud.get("notificadores"), dict) else {}
    filas_c = "".join(
        f"<tr><td>{_e(k)}</td><td class='num'>{_e((v or {}).get('enviados', ''))}</td>"
        f"<td class='num'>{_e((v or {}).get('fallidos', ''))}</td>"
        f"<td>{_e((v or {}).get('ultimo_error') or '')}</td></tr>"
        for k, v in canales.items() if isinstance(v, dict) or v is None)
    tabla_c = ("<table><tr><th>Canal</th><th>Enviados</th><th>Fallidos</th><th>Último error</th></tr>"
               + filas_c + "</table>") if filas_c else "<p>Sin canales de aviso activos.</p>"

    ext = salud.get("extension") if isinstance(salud.get("extension"), dict) else {}
    linea_ext = ("Extensión: sin latidos aún." if not ext else
                 f"Extensión: último latido hace {_e(ext.get('edad_min', '?'))} min"
                 + (f" · último error: {_e(ext.get('ultimoError'))}" if ext.get("ultimoError") else ""))
    avisos = "".join(f"<li>{_e(a)}</li>" for a in (salud.get("config_advertencias") or []))

    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta http-equiv='refresh' content='60'>"
        "<title>Bot de fallas</title><style>"
        ":root{--fondo:#fff;--texto:#1d1d1f;--borde:#d9d9de;--suave:#f4f4f6;--acento:#b45309}"
        "@media (prefers-color-scheme: dark){:root{--fondo:#16161a;--texto:#ececf1;"
        "--borde:#3a3a42;--suave:#202026;--acento:#f59e0b}}"
        "body{font:14px/1.45 system-ui,Segoe UI,sans-serif;background:var(--fondo);color:var(--texto);"
        "margin:0;padding:16px;max-width:1200px}"
        "h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:24px 0 8px;color:var(--acento)}"
        "table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}"
        "th,td{border:1px solid var(--borde);padding:4px 8px;text-align:left;vertical-align:top}"
        "th{background:var(--suave)}td.num{text-align:right;white-space:nowrap}"
        ".n-masivo{color:#7c3aed;font-weight:700}.n-alta{color:#dc2626;font-weight:700}"
        ".n-media{color:#ea580c}.n-baja{color:#ca8a04}small{opacity:.75}"
        "</style></head><body>"
        f"<h1>Bot de fallas Centrality</h1><small>v{_e(salud.get('version') or __version__)} · "
        f"{_e(str(salud.get('ahora') or '').replace('T', ' '))} · se actualiza cada 60 s</small>"
        f"<h2>Fallas activas ({len(filas_f)})</h2>{tabla_f}"
        f"<h2>Fuentes</h2>{tabla_s}<p>{linea_ext}</p>"
        f"<h2>Canales</h2>{tabla_c}"
        + (f"<h2>Advertencias de configuración</h2><ul>{avisos}</ul>" if avisos else "")
        + "</body></html>")


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------


class _Manejador(BaseHTTPRequestHandler):
    server_version = "BotFallas/" + __version__
    sys_version = ""
    # Sin esto, un cliente que abre la conexión y no manda nada (o manda el
    # cuerpo a goteo) retiene un hilo para siempre.
    timeout = 60

    # ------------------------------------------------------------ utilidades

    @property
    def cfg(self) -> dict:
        return self.server.cfg       # type: ignore[attr-defined]

    @property
    def servicio(self):
        return self.server.servicio  # type: ignore[attr-defined]

    @property
    def puerto(self) -> int:
        return int(self.server.server_address[1])

    def log_message(self, fmt: str, *args) -> None:   # noqa: D401 — silenciar stderr
        log.debug("%s %s", self.address_string(), fmt % args)

    def _origen(self) -> str | None:
        return self.headers.get("Origin")

    def _enviar(self, status: int, cuerpo: bytes, tipo: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Vary", "Origin")
        origen = self._origen()
        # GET/POST con Origin no permitido: sin cabeceras CORS -> el navegador
        # no deja que esa página lea la respuesta.
        if origen and origen_permitido(origen, self.cfg, self.puerto):
            self.send_header("Access-Control-Allow-Origin", origen.strip())
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if cuerpo:
            self.wfile.write(cuerpo)

    def _json(self, status: int, obj: Any) -> None:
        self._enviar(status, _json_bytes(obj), "application/json; charset=utf-8")

    def _error(self, status: int, mensaje: str) -> None:
        self._json(status, {"ok": False, "error": mensaje})

    def _host_ok(self) -> bool:
        if host_permitido(self.headers.get("Host")):
            return True
        # No se repite el Host recibido: no hay por qué reflejar lo que mande otro.
        log.warning("Petición con Host no permitido rechazada (421) desde %s", self.address_string())
        self.close_connection = True
        self._error(421, "Host no permitido: usa http://127.0.0.1:<puerto>")
        return False

    def _ruta(self) -> tuple[str, dict[str, str]]:
        partes = urlsplit(self.path)
        params = {k: v[-1] for k, v in parse_qs(partes.query, keep_blank_values=True).items()}
        ruta = partes.path.rstrip("/") or "/"
        return ruta, params

    def _largo(self) -> int | None:
        cl = self.headers.get("Content-Length")
        if cl is None:
            return None
        try:
            n = int(cl.strip())
        except ValueError:
            return -1
        return n

    def _descartar_cuerpo(self) -> None:
        """Lee y bota el cuerpo antes de un 401/403.

        Si se cierra el socket con datos sin leer, el kernel manda RST y el
        cliente (la extensión) ve "failed to fetch" en vez del 401 que le
        dice qué corregir.
        """
        n = self._largo()
        if n is None or n <= 0 or n > int((self.cfg.get("servidor") or {}).get("max_bytes") or 0):
            self.close_connection = True
            return
        while n > 0:
            trozo = self.rfile.read(min(n, _TROZO))
            if not trozo:
                break
            n -= len(trozo)

    def _leer_cuerpo(self) -> bytes | None:
        """Cuerpo del POST o None si ya se respondió un error (411/413/400)."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True
            self._error(411, "se requiere Content-Length (no se acepta chunked)")
            return None
        n = self._largo()
        if n is None:
            self.close_connection = True
            self._error(411, "falta Content-Length")
            return None
        if n < 0:
            self.close_connection = True
            self._error(400, "Content-Length inválido")
            return None
        maximo = int((self.cfg.get("servidor") or {}).get("max_bytes") or 0)
        if n > maximo:
            self.close_connection = True
            self._error(413, f"cuerpo de {n} bytes supera el máximo de {maximo}; "
                             "reduce la ventana del reporte o la OU")
            return None
        partes = []
        falta = n
        while falta > 0:
            trozo = self.rfile.read(min(falta, _TROZO))
            if not trozo:
                break
            partes.append(trozo)
            falta -= len(trozo)
        if falta:
            self.close_connection = True
            self._error(400, "cuerpo incompleto")
            return None
        return b"".join(partes)

    @staticmethod
    def _payload_json(cuerpo: bytes) -> dict:
        try:
            d = json.loads(cuerpo.decode("utf-8-sig") or "null")
        except ValueError as e:     # JSONDecodeError y UnicodeDecodeError son ValueError
            raise ValueError(f"JSON inválido: {e}") from None
        if not isinstance(d, dict):
            raise ValueError("se esperaba un objeto JSON {...}")
        return d

    def _ejecutar(self, fn) -> None:
        try:
            resultado = fn()
        except Exception as e:      # noqa: BLE001 — se traduce a código HTTP
            if es_error_contrato(e):
                self._error(422, str(e)[:500])
            elif isinstance(e, ValueError):
                self._error(400, str(e)[:500])
            else:
                log.exception("Error atendiendo %s %s", self.command, urlsplit(self.path).path)
                self._error(500, "error interno del bot; revisa datos/bot.log")
            return
        self._json(200, resultado)

    # ------------------------------------------------------------ métodos

    def do_OPTIONS(self) -> None:   # noqa: N802
        if not self._host_ok():
            return
        origen = self._origen()
        if not origen_permitido(origen, self.cfg, self.puerto):
            self._error(403, "origen no permitido")
            return
        extra = dict(_CABECERAS_PREFLIGHT)
        if (self.headers.get("Access-Control-Request-Private-Network") or "").strip().lower() == "true":
            extra["Access-Control-Allow-Private-Network"] = "true"
        self._enviar(204, b"", "text/plain; charset=utf-8", extra)

    def do_GET(self) -> None:   # noqa: N802
        if not self._host_ok():
            return
        ruta, _ = self._ruta()
        if ruta == "/":
            try:
                cuerpo = pagina_inicio(self.servicio.salud(), self.servicio.fallas_activas()).encode("utf-8")
            except Exception:       # noqa: BLE001
                log.exception("Error armando la página /")
                self._error(500, "error interno del bot; revisa datos/bot.log")
                return
            self._enviar(200, cuerpo, "text/html; charset=utf-8",
                         {"Content-Security-Policy": _CSP, "X-Frame-Options": "DENY",
                          "Referrer-Policy": "no-referrer"})
        elif ruta == "/estado":
            self._ejecutar(self.servicio.salud)
        elif ruta == "/fallas":
            self._ejecutar(self.servicio.fallas_activas)
        elif ruta.startswith("/fallas/"):
            caso = unquote(ruta[len("/fallas/"):]).strip()
            try:
                f = self.servicio.falla(caso)
            except Exception:       # noqa: BLE001
                log.exception("Error buscando el caso")
                self._error(500, "error interno del bot; revisa datos/bot.log")
                return
            if f is None:
                self._error(404, "caso no encontrado")
            else:
                self._json(200, f)
        elif ruta == "/ping":
            self._json(200, {"ok": True})
        else:
            self._error(404, "ruta no encontrada")

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return
        origen = self._origen()
        if origen is not None and not origen_permitido(origen, self.cfg, self.puerto):
            log.warning("POST desde un origen no permitido rechazado (403): %s", origen[:100])
            self._descartar_cuerpo()
            self._error(403, "origen no permitido")
            return
        token = str((self.cfg.get("servidor") or {}).get("token_compartido") or "")
        if token:
            recibido = self.headers.get("X-Bot-Token") or ""
            if not hmac.compare_digest(recibido.encode("utf-8"), token.encode("utf-8")):
                self._descartar_cuerpo()
                self._error(401, "falta X-Bot-Token o no coincide con servidor.token_compartido")
                return
        cuerpo = self._leer_cuerpo()
        if cuerpo is None:
            return
        ruta, params = self._ruta()

        if ruta == "/ping":
            self._json(200, {"ok": True})
        elif ruta == "/ingest/grilla":
            self._ejecutar(lambda: self.servicio.ingest_grilla(self._payload_json(cuerpo)))
        elif ruta.startswith("/ingest/"):
            fuente = ruta[len("/ingest/"):]
            if fuente not in FUENTES_CSV:
                self._error(404, f"fuente desconocida: {fuente[:40]} (válidas: {', '.join(FUENTES_CSV)}, grilla)")
                return
            self._ejecutar(lambda: self.servicio.ingest_csv(fuente, cuerpo, params))
        elif ruta == "/extension/latido":
            self._ejecutar(lambda: self.servicio.latido(self._payload_json(cuerpo)))
        else:
            self._error(404, "ruta no encontrada")


# --------------------------------------------------------------------------
# Servidor
# --------------------------------------------------------------------------


class _Servidor(ThreadingHTTPServer):
    daemon_threads = True
    # En Windows SO_REUSEADDR deja que OTRO proceso se enlace al mismo puerto
    # ya ocupado (dos bots abiertos "andarían" y la extensión hablaría con
    # cualquiera). Allí se desactiva para que el segundo falle claro.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, direccion, manejador, servicio, cfg: dict, familia: int):
        self.address_family = familia
        self.servicio = servicio
        self.cfg = cfg
        super().__init__(direccion, manejador)


def crear_servidor(servicio, cfg: dict) -> ThreadingHTTPServer:
    """Crea (y enlaza) el servidor en cfg.servidor.host:puerto. Puerto 0 = efímero (tests)."""
    srv = cfg.get("servidor") or {}
    host = str(srv.get("host") or "127.0.0.1").strip()
    if host.lower() not in HOSTS_LOOPBACK:
        # Defensa en profundidad: validar() ya lo marca como error.
        raise ValueError(f"servidor.host debe ser loopback (127.0.0.1, localhost o ::1), no {host!r}")
    puerto = int(srv.get("puerto", 8771))
    familia = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        return _Servidor((host, puerto), _Manejador, servicio, cfg, familia)
    except OSError as e:
        raise OSError(e.errno, f"no se pudo abrir {host}:{puerto} ({e.strerror or e}); "
                               "¿hay otro bot corriendo en ese puerto?") from e


def iniciar_en_hilo(servidor: ThreadingHTTPServer) -> threading.Thread:
    hilo = threading.Thread(target=servidor.serve_forever, kwargs={"poll_interval": 0.5},
                            name="servidor-http", daemon=True)
    hilo.start()
    host, puerto = servidor.server_address[:2]
    log.info("Servidor local escuchando en http://%s:%s/",
             f"[{host}]" if ":" in str(host) else host, puerto)
    return hilo
