"""Orquestador del bot: recibe las fotos de cada fuente, recalcula y avisa.

Es lo único que tiene estado vivo en memoria (las snapshots) y lo único que
toca a la vez el contrato, el agregador, el detector y los avisos:

    servidor / extensión ──► ingest_csv / ingest_grilla / latido
                                     │  (lock)
                                     ▼
          snapshots frescas ─► construir_fallas ─► Detector.evaluar ─► Eventos
                                     │                                   │
                              estado.json (atómico)            despachador.encolar

Por qué un solo `RLock` para todo: la extensión postea las fuentes una tras
otra, el timer de recálculo corre cada 2 min y los comandos de Telegram leen
desde otro hilo. Si dos recálculos se cruzaran, el detector vería el estado
a medio actualizar y podría avisar dos veces la misma falla. Los envíos no
bloquean el lock: el despachador sólo encola (cada canal tiene su hilo).

Decisiones que vale la pena saber:

- Una ingesta con `ErrorContrato` NO reemplaza la snapshot previa: un reporte
  que una vez vino como página de login o sin columnas no puede "vaciar" las
  fallas (sería una normalización falsa de todo). Queda registrado en
  /estado y se re-lanza para que el servidor responda 422.
- Primer arranque (estado sin `iniciado`): la extensión postea las fuentes de
  un ciclo en serie (casos, reclamos, 432, grilla), separadas por hasta ~75 s.
  Si la primera evaluación corriera con la primera fuente, las fallas que
  sólo trae la segunda se avisarían una por una como "nuevas" en vez de ir
  en el resumen de inicio. Por eso, mientras el bot no se ha iniciado, la
  evaluación espera el latido de fin de ciclo de la extensión (o, como
  respaldo, GRACIA_INICIO desde la primera ingesta). Con estado guardado no
  hay espera: lo ya avisado está en el estado.
- Salud: "sin datos" se mide desde la última ingesta OK, pero nunca desde
  antes del arranque del proceso ni desde antes de una suspensión del PC
  (el reloj salta sin que corra el timer). Si no, cada mañana al prender el
  PC llegaría un "sin datos" falso seguido de un "datos recuperados".

Privacidad: aquí pasan los Registros con NIS y direcciones (dentro de las
snapshots, en memoria). Hacia afuera (estado, /estado, /fallas, avisos,
log) sólo sale lo que ya trae `Falla` / `Snapshot.resumen()`: conteos,
calles sin número, comunas, nombres de columnas.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import estado as estado_mod
from .agregador import construir_fallas
from .config import advertencias as advertencias_config
from .contrato import ErrorContrato, caso_de_tarea, parse_csv, parse_grilla
from .detector import Detector
from .maestros import Maestros
from .mensajes import mensaje_detalle, mensaje_evento, mensaje_resumen
from .modelo import FUENTES, FUENTES_CSV, Evento, Falla, Mensaje, Snapshot
from .texto import (es_nulo, escape_html, fmt_duracion, fmt_hora, fmt_miles, limpio,
                    normalizar_caso, parse_fecha)

log = logging.getLogger(__name__)

__all__ = ["ServicioFallas", "ventana_de_params", "GRACIA_INICIO"]

# Respaldo del latido de fin de ciclo en el primer arranque (ver docstring).
# Un ciclo normal de la extensión (3 reportes + grilla) dura 1-3 min.
GRACIA_INICIO = timedelta(minutes=5)

_FRESCURA_DEF_MIN = 30
_LARGO_ERROR = 500

# Campos del latido que se conservan. El resto se bota: el latido llega de
# la red local y /estado no debe reflejar lo que mande cualquiera.
_CAMPOS_LATIDO = ("nivel", "mensaje", "ciclo", "ultima", "ultimoError", "intervaloMin",
                  "activo", "version", "proximo_en_min", "disparo")

_NOMBRE_FUENTE = {"casos": "informe de casos", "reclamos": "reclamos",
                  "abiertas": "tareas abiertas", "grilla": "bandeja capturada"}


def _iso(d: datetime | None) -> str | None:
    return d.isoformat(timespec="seconds") if isinstance(d, datetime) else None


def _fecha(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _minutos(td: timedelta) -> float:
    return round(td.total_seconds() / 60, 1)


def _corto(s: Any, n: int) -> str:
    s = limpio(s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def ventana_de_params(params: dict[str, str] | None) -> tuple[datetime | None, datetime | None]:
    """Query string del POST (`desde`, `hasta`) -> (ventana_desde, ventana_hasta).

    Acepta ISO ('2026-10-18T15:00:00') o 'YYYY-MM-DD HH:MM:SS' (lo que manda
    la extensión). Un valor que no se entiende es ValueError (-> 400) y NO se
    ignora: si se perdiera la ventana, un caso viejo que sale del periodo del
    reporte se daría por normalizado.
    """
    out: list[datetime | None] = []
    for k in ("desde", "hasta"):
        v = (params or {}).get(k)
        if v is None or es_nulo(v):
            out.append(None)
            continue
        d = parse_fecha(v)
        if d is None:
            raise ValueError(f"parámetro '{k}' inválido: {str(v)[:40]!r} "
                             "(se espera 'YYYY-MM-DD HH:MM:SS' o ISO)")
        out.append(d)
    desde, hasta = out
    if desde and hasta and desde > hasta:
        raise ValueError("parámetros de ventana invertidos: 'desde' es posterior a 'hasta'")
    return desde, hasta


class ServicioFallas:
    """Estado vivo del bot. Todos los métodos públicos son seguros entre hilos.

    `ruta_estado=None` = estado sólo en memoria (tests); `__main__` pasa
    `<datos_dir>/estado.json`, y `--simular` un archivo en una carpeta temporal.
    `gracia_inicio_s` (extra al SPEC): espera del primer arranque; 0 la apaga.
    """

    def __init__(self, cfg: dict, despachador, maestros: Maestros | None,
                 reloj: Callable[[], datetime] = datetime.now, ruta_estado: Path | None = None,
                 gracia_inicio_s: float | None = None):
        self.cfg = cfg
        self.despachador = despachador
        self.maestros = maestros if maestros is not None else Maestros()
        self._reloj = reloj
        self.ruta_estado = Path(ruta_estado) if ruta_estado else None
        self.estado = estado_mod.cargar(self.ruta_estado) if self.ruta_estado else estado_mod.EstadoBot()
        self.detector = Detector(self.estado, cfg)
        self._lock = threading.RLock()
        self._gracia = (GRACIA_INICIO if gracia_inicio_s is None
                        else timedelta(seconds=max(0.0, float(gracia_inicio_s))))

        self.snapshots: dict[str, Snapshot] = {}      # última foto VÁLIDA por fuente
        self._fallas: dict[str, Falla] = {}           # último cálculo, para /fallas
        self._latido: dict = {}
        self._latido_ts: datetime | None = None
        self._primera_ingesta: datetime | None = None  # de ESTE proceso
        self._fin_ciclo: datetime | None = None        # último latido de fin de ciclo
        self._ultimo_tick: datetime | None = None      # último recálculo
        self._error_estado: str | None = None
        # Desde cuándo se le da plazo a la extensión para mandar datos: el
        # arranque del proceso o la última reanudación del PC.
        self._referencia_salud = reloj()

        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Ingesta
    # ------------------------------------------------------------------

    def ingest_csv(self, fuente: str, datos: bytes, params: dict[str, str] | None = None) -> dict:
        """CSV crudo de una fuente -> reemplaza su snapshot y recalcula."""
        if fuente not in FUENTES_CSV:
            raise ValueError("fuente desconocida")
        desde, hasta = ventana_de_params(params)
        recibido = self._reloj()
        # El parseo (hasta 50 MB) va FUERA del lock: /estado y los comandos de
        # Telegram no tienen por qué esperar a que termine.
        try:
            snap = parse_csv(datos, fuente, recibido, desde, hasta)
        except ErrorContrato as e:
            self._registrar_error(fuente, e, recibido)
            raise
        return self._aceptar(snap)

    def ingest_grilla(self, payload: dict) -> dict:
        """Grillas ExtJS capturadas por la extensión -> snapshot "grilla"."""
        recibido = self._reloj()
        try:
            snap = parse_grilla(payload, recibido)
        except ErrorContrato as e:
            # Es normal (la pestaña no está en la bandeja): se registra igual,
            # pero sin alarma en el log.
            self._registrar_error("grilla", e, recibido, nivel=logging.INFO)
            raise
        return self._aceptar(snap)

    def _registrar_error(self, fuente: str, e: Exception, ts: datetime,
                         nivel: int = logging.WARNING) -> None:
        with self._lock:
            # Se conserva lo de la última foto válida (recibido, registros):
            # /estado muestra el error Y de cuándo es el dato que se sigue usando.
            previo = dict(self.estado.fuentes.get(fuente) or {})
            previo.update({"fuente": fuente, "ok": False, "error": str(e)[:_LARGO_ERROR],
                           "ts": _iso(ts)})
            self.estado.fuentes[fuente] = previo
            self._guardar()
        log.log(nivel, "Ingesta de %s rechazada (se conserva la foto anterior): %s",
                fuente, str(e)[:300])

    def _aceptar(self, snap: Snapshot) -> dict:
        with self._lock:
            ahora = self._reloj()
            fuente = snap.fuente
            self.snapshots[fuente] = snap
            self.estado.fuentes[fuente] = {**snap.resumen(), "ok": True, "error": "",
                                           "ts": _iso(snap.recibido),
                                           "ultimo_ok": _iso(snap.recibido)}
            if self._primera_ingesta is None:
                self._primera_ingesta = snap.recibido

            previos: list[Evento] = []
            if "sin_datos" in self.estado.alertas:
                desde = _fecha(self.estado.alertas.pop("sin_datos"))
                motivo = f"Llegó {_NOMBRE_FUENTE.get(fuente, fuente)}"
                if desde:
                    motivo += f"; el aviso de falta de datos fue a las {desde:%H:%M}"
                previos.append(Evento(tipo="datos_recuperados", motivo=motivo + ".", ts=ahora))

            eventos = self._recalcular(ahora, previos)
            activas = self._activas()
            log.info("Ingesta %s: %d registros (%d filas, %d descartadas) -> %d fallas activas, "
                     "%d avisos", fuente, len(snap.registros), snap.filas_leidas,
                     snap.filas_descartadas, len(activas), len(eventos))
            for a in snap.advertencias:
                log.info("Ingesta %s: %s", fuente, a)
            if snap.faltantes:
                log.info("Ingesta %s: columnas útiles que no vienen: %s", fuente,
                         ", ".join(snap.faltantes))
            return {
                "ok": True,
                "fuente": fuente,
                "registros": len(snap.registros),
                "filas_leidas": snap.filas_leidas,
                "filas_descartadas": snap.filas_descartadas,
                "fallas_activas": len(activas),
                "eventos": [{"tipo": ev.tipo, "caso": ev.caso} for ev in eventos],
                "columnas_mapeadas": dict(snap.columnas_mapeadas),
                "faltantes": list(snap.faltantes),
                "advertencias": list(snap.advertencias),
            }

    def latido(self, payload: dict) -> dict:
        """Latido de la extensión: estado del ciclo y último error."""
        if not isinstance(payload, dict):
            raise ValueError("el latido debe ser un objeto JSON")
        with self._lock:
            ahora = self._reloj()
            lat: dict = {}
            for k in _CAMPOS_LATIDO:
                if k not in payload:
                    continue
                v = payload[k]
                if isinstance(v, str):
                    v = v[:_LARGO_ERROR]
                elif not (v is None or isinstance(v, (bool, int, float))):
                    continue
                lat[k] = v
            lat["recibido"] = _iso(ahora)
            self._latido, self._latido_ts = lat, ahora

            eventos: list[Evento] = []
            error = limpio(payload.get("ultimoError") or "")
            alertas = self.estado.alertas
            cambio = False
            if "SESION_CADUCADA" in error.upper():
                if "sesion_caducada" not in alertas:
                    alertas["sesion_caducada"] = _iso(ahora)
                    cambio = True
                    eventos.append(Evento(
                        tipo="sesion_caducada", ts=ahora,
                        motivo=f"La extensión lo informó a las {ahora:%H:%M}; mientras tanto no "
                               "llegan reportes nuevos."))
            elif not error and "sesion_caducada" in alertas:
                # Sólo un latido SIN error rearma el aviso: otro error distinto
                # (p. ej. un reporte pesado) no prueba que la sesión volvió.
                alertas.pop("sesion_caducada")
                cambio = True

            # Fin de ciclo: la extensión ya posteó todas sus fuentes activas.
            nivel = str(payload.get("nivel") or "").lower()
            if "proximo_en_min" in payload or nivel in ("ok", "error"):
                self._fin_ciclo = ahora
                if not self.estado.iniciado and self._primera_ingesta is not None:
                    eventos = self._recalcular(ahora, eventos)
                    return {"ok": True, "eventos": [{"tipo": e.tipo, "caso": e.caso} for e in eventos]}

            if eventos:
                self._emitir(eventos, ahora)
            if cambio:
                self._guardar()
            return {"ok": True, "eventos": [{"tipo": e.tipo, "caso": e.caso} for e in eventos]}

    # ------------------------------------------------------------------
    # Recálculo
    # ------------------------------------------------------------------

    def recalcular(self) -> list[Evento]:
        """Recalcula con las snapshots frescas y avisa. Lo llama también el timer."""
        with self._lock:
            return self._recalcular(self._reloj(), [])

    def _frescura(self, fuente: str) -> timedelta:
        f = (self.cfg.get("fuentes") or {}).get(fuente) or {}
        return timedelta(minutes=f.get("frescura_min", _FRESCURA_DEF_MIN))

    def _cuenta_ausencia(self, fuente: str) -> bool:
        f = (self.cfg.get("fuentes") or {}).get(fuente) or {}
        return bool(f.get("cuenta_ausencia", fuente != "grilla"))

    def _es_fresca(self, snap: Snapshot, ahora: datetime) -> bool:
        return ahora - snap.recibido <= self._frescura(snap.fuente)

    def _frescas(self, ahora: datetime) -> dict[str, Snapshot]:
        return {f: s for f, s in self.snapshots.items() if self._es_fresca(s, ahora)}

    def _limite_sin_datos(self) -> timedelta:
        return timedelta(minutes=(self.cfg.get("avisos") or {}).get("alerta_sin_datos_min", 30))

    def _calentando(self, ahora: datetime) -> bool:
        """¿Primer arranque esperando que llegue el ciclo completo de la extensión?"""
        if self.estado.iniciado or not self._gracia or self._primera_ingesta is None:
            return False
        if self._fin_ciclo is not None and self._fin_ciclo >= self._primera_ingesta:
            return False
        if all(f in self.snapshots for f in FUENTES_CSV):
            return False
        return ahora - self._primera_ingesta < self._gracia

    def _recalcular(self, ahora: datetime, previos: list[Evento]) -> list[Evento]:
        # Un hueco entre recálculos más largo que el plazo de "sin datos" no es
        # la extensión caída: el timer no corrió (PC suspendido o hibernado).
        if self._ultimo_tick is not None and ahora - self._ultimo_tick > self._limite_sin_datos():
            log.info("El reloj avanzó %s sin recálculos (¿PC suspendido?): se reinicia el plazo "
                     "de 'sin datos'", fmt_duracion(ahora - self._ultimo_tick))
            self._referencia_salud = ahora
        self._ultimo_tick = ahora

        frescas = self._frescas(ahora)
        fallas = construir_fallas(frescas, self.maestros, self.cfg)
        self._fallas = fallas
        eventos = list(previos) + self._alertas_salud(ahora)
        if self._calentando(ahora):
            log.debug("Primer arranque: se espera el fin del ciclo de la extensión antes de evaluar")
        else:
            completas = {f: s for f, s in frescas.items() if s.completa and self._cuenta_ausencia(f)}
            eventos += self.detector.evaluar(fallas, completas, ahora)
        self._guardar()
        self._emitir(eventos, ahora)
        return eventos

    def _ultimo_ok(self) -> datetime | None:
        fechas = [_fecha(d.get("ultimo_ok")) for d in self.estado.fuentes.values() if isinstance(d, dict)]
        fechas = [f for f in fechas if f]
        return max(fechas) if fechas else None

    def _alertas_salud(self, ahora: datetime) -> list[Evento]:
        ultimo = self._ultimo_ok()
        # Sin ninguna ingesta OK nunca, el bot está recién instalado: lo que
        # falta es configurar la extensión, y eso se ve en /estado.
        if ultimo is None or "sin_datos" in self.estado.alertas:
            return []
        referencia = max(ultimo, self._referencia_salud)
        if ahora - referencia <= self._limite_sin_datos():
            return []
        self.estado.alertas["sin_datos"] = _iso(ahora)
        return [Evento(tipo="sin_datos", ts=ahora,
                       motivo=f"Último reporte recibido: {fmt_hora(ultimo)} "
                              f"(hace {fmt_duracion(ahora - ultimo)}). Revisa que el PC tenga "
                              "Chrome abierto con Centrality y la extensión activa.")]

    def _emitir(self, eventos: list[Evento], ahora: datetime) -> None:
        for ev in eventos:
            m = self._mensaje(ev, ahora)
            log.info("Aviso %s: %s", ev.tipo, m.titulo)
            try:
                self.despachador.encolar(m)
            except Exception:       # noqa: BLE001 — un canal roto no detiene el recálculo
                log.exception("No se pudo encolar el aviso %s", m.titulo)

    def _mensaje(self, ev: Evento, ahora: datetime) -> Mensaje:
        try:
            return mensaje_evento(ev, self.cfg, ahora)
        except Exception:           # noqa: BLE001 — el evento ya quedó como avisado en el estado
            log.exception("No se pudo redactar el aviso %s del caso %s", ev.tipo, ev.caso)
            txt = f"Aviso {ev.tipo}" + (f" · Caso {ev.caso}" if ev.caso else "")
            return Mensaje(titulo=txt, html=escape_html(txt), texto=txt, tipo=ev.tipo, caso=ev.caso)

    def _guardar(self) -> None:
        if self.ruta_estado is None:
            return
        try:
            estado_mod.guardar(self.estado, self.ruta_estado)
            self._error_estado = None
        except OSError as e:
            # Disco lleno / permisos: los avisos siguen saliendo; sólo se
            # arriesga re-avisar tras un reinicio. Se muestra en /estado.
            if self._error_estado is None:
                log.error("No se pudo guardar el estado en %s: %s", self.ruta_estado.name, e)
            self._error_estado = f"{type(e).__name__}: {e.strerror or e}"

    # ------------------------------------------------------------------
    # Timer de recálculo
    # ------------------------------------------------------------------

    def iniciar_recalculo(self, intervalo_s: float | None = None) -> threading.Thread:
        """Hilo daemon que llama `recalcular()` cada `recalculo_periodico_s`.

        Sin él, una fuente que deja de llegar nunca se vería "vieja" ni una
        falla ausente cumpliría sus 15 minutos: sólo se evalúa cuando hay ingesta.
        """
        if intervalo_s is None:
            # Piso de 1 s: un 0.001 mal escrito en config.json no debe dejar
            # al bot recalculando sin parar.
            intervalo = max(1.0, float((self.cfg.get("avisos") or {}).get("recalculo_periodico_s", 120)))
        else:
            intervalo = max(0.01, float(intervalo_s))
        if self._hilo and self._hilo.is_alive():
            return self._hilo
        self._parar.clear()
        self._hilo = threading.Thread(target=self._bucle, args=(intervalo,), name="recalculo",
                                      daemon=True)
        self._hilo.start()
        return self._hilo

    def _bucle(self, intervalo: float) -> None:
        while not self._parar.wait(intervalo):
            try:
                self.recalcular()
            except Exception:       # noqa: BLE001 — el timer no puede morir por un bug puntual
                log.exception("Error en el recálculo periódico")

    def detener(self, timeout: float = 5) -> None:
        self._parar.set()
        if self._hilo:
            self._hilo.join(timeout)
        with self._lock:
            self._guardar()

    # ------------------------------------------------------------------
    # Consultas (GET /estado, /fallas, comandos de Telegram)
    # ------------------------------------------------------------------

    def _activas(self) -> list[Falla]:
        lista = [f for f in self._fallas.values() if f.abierta and self.detector.pasa_filtros(f)]
        lista.sort(key=lambda f: (-(f.clientes if f.clientes is not None else -1), f.caso))
        return lista

    def ultimas_fallas(self) -> dict[str, Falla]:
        """Todas las fallas del último cálculo (incluye cerradas y filtradas)."""
        with self._lock:
            return dict(self._fallas)

    def fallas_activas(self) -> list[dict]:
        with self._lock:
            return [f.to_dict() for f in self._activas()]

    def _buscar(self, caso: str) -> tuple[Falla | None, Any]:
        c = normalizar_caso(caso)
        c = caso_de_tarea(c) or c
        s = self.estado.casos.get(c)
        f = self._fallas.get(c)
        if f is None and s is not None:
            f = s.falla()
        return f, s

    def falla(self, caso: str) -> dict | None:
        with self._lock:
            f, s = self._buscar(caso)
            if f is None:
                return None
            d = f.to_dict()
            if s is not None:
                d["seguimiento"] = {
                    "notificada": s.notificada,
                    "normalizada": s.normalizada,
                    "normalizada_en": _iso(s.normalizada_en),
                    "primera_vista": _iso(s.primera_vista),
                    "ultima_vista": _iso(s.ultima_vista),
                    "ultimo_aviso": _iso(s.ultimo_aviso),
                    "max_clientes": s.max_clientes,
                }
            return d

    def salud(self) -> dict:
        with self._lock:
            ahora = self._reloj()
            fuentes: dict[str, dict] = {}
            nombres = [f for f in FUENTES if f in self.estado.fuentes or f in self.snapshots]
            for nombre in nombres:
                d = dict(self.estado.fuentes.get(nombre) or {})
                snap = self.snapshots.get(nombre)
                recibido = snap.recibido if snap else _fecha(d.get("recibido"))
                d["edad_min"] = _minutos(ahora - recibido) if recibido else None
                # Fresca = se está usando en el cálculo. Tras reiniciar el bot,
                # la foto guardada en el estado no está en memoria: no es fresca.
                d["fresca"] = bool(snap and self._es_fresca(snap, ahora))
                d["frescura_min"] = _minutos(self._frescura(nombre))
                fuentes[nombre] = d
            ext = dict(self._latido)
            if self._latido_ts:
                ext["edad_min"] = _minutos(ahora - self._latido_ts)
            if "_advertencias" in self.cfg:
                avisos_cfg = list(self.cfg.get("_advertencias") or [])
            else:
                avisos_cfg = advertencias_config(self.cfg)
            try:
                notif = self.despachador.estado()
            except Exception:       # noqa: BLE001
                log.exception("despachador.estado() falló")
                notif = {}
            return {
                "version": __version__,
                "ahora": _iso(ahora),
                "iniciado": self.estado.iniciado,
                "calentando": self._calentando(ahora),
                "fuentes": fuentes,
                "fallas_activas": len(self._activas()),
                "casos_seguidos": len(self.estado.casos),
                "extension": ext,
                "notificadores": notif,
                "maestros": self.maestros.diagnostico,
                "alertas": {k: v for k, v in self.estado.alertas.items()
                            if k in ("sin_datos", "sesion_caducada")},
                "error_estado": self._error_estado,
                "config_advertencias": avisos_cfg,
            }

    # ------------------------------------------------------------------
    # Textos para Telegram (HTML ya escapado)
    # ------------------------------------------------------------------

    def texto_resumen(self) -> str:
        with self._lock:
            return mensaje_resumen(self._activas(), self.cfg, self._reloj()).html

    def texto_caso(self, caso: str) -> str:
        with self._lock:
            ahora = self._reloj()
            f, s = self._buscar(caso)
            if f is None:
                horas = (self.cfg.get("avisos") or {}).get("retener_normalizadas_h", 24)
                return (f"No encuentro el caso <code>{escape_html(_corto(caso, 40))}</code> entre las "
                        f"fallas activas ni entre las normalizadas de las últimas {escape_html(horas)} h.")
            if s is not None and s.normalizada and f.abierta:
                # Normalizada por ausencia: la última foto la muestra abierta.
                f = Falla.from_dict(f.to_dict())
                f.abierta = False
                f.cierre = f.cierre or s.normalizada_en
            return mensaje_detalle(f, self.cfg, ahora).html

    def texto_estado(self) -> str:
        sal = self.salud()
        ahora = _fecha(sal["ahora"]) or self._reloj()
        e = escape_html
        lineas = [f"🤖 <b>Bot de fallas</b> · v{e(sal['version'])} · {ahora:%d-%m %H:%M}"]
        if sal.get("calentando"):
            lineas.append("⏳ Primer arranque: esperando el ciclo completo de la extensión.")
        lineas.append(f"🔥 Fallas activas: <b>{fmt_miles(sal['fallas_activas'])}</b> · "
                      f"casos seguidos: {fmt_miles(sal['casos_seguidos'])}")

        lineas.append("📥 <b>Fuentes</b>")
        if not sal["fuentes"]:
            lineas.append("• aún no llega ningún reporte")
        for nombre, d in sal["fuentes"].items():
            edad = d.get("edad_min")
            hace = f"hace {fmt_duracion(timedelta(minutes=edad))}" if edad is not None else "sin dato"
            if d.get("ok") is False:
                marca = f"❌ error: {e(_corto(d.get('error'), 120))} · último dato válido {hace}"
            elif d.get("fresca"):
                marca = f"✅ {hace} · {fmt_miles(d.get('registros') or 0)} registros"
            else:
                marca = f"⚠️ vieja ({hace}): no se usa"
            lineas.append(f"• {e(nombre)}: {marca}")

        ext = sal.get("extension") or {}
        if not ext:
            lineas.append("🧩 Extensión: sin latidos desde que partió el bot")
        else:
            txt = f"🧩 Extensión: último latido hace {fmt_duracion(timedelta(minutes=ext.get('edad_min') or 0))}"
            if ext.get("ciclo") is not None:
                txt += f" · ciclo {e(ext.get('ciclo'))}"
            if ext.get("ultimoError"):
                txt += f" · ⚠️ {e(_corto(ext.get('ultimoError'), 160))}"
            lineas.append(txt)

        canales = sal.get("notificadores") or {}
        if canales:
            partes = []
            for nombre, c in canales.items():
                c = c if isinstance(c, dict) else {}
                p = f"{e(nombre)} {fmt_miles(c.get('enviados') or 0)} ✅"
                if c.get("fallidos"):
                    p += f" {fmt_miles(c.get('fallidos'))} ❌"
                partes.append(p)
            lineas.append("📣 Canales: " + " · ".join(partes))
        else:
            lineas.append("📣 Canales: ninguno activo")

        maestros = sal.get("maestros") or {}
        partes = []
        for nombre, d in maestros.items():
            if not isinstance(d, dict) or not d.get("configurado"):
                continue
            partes.append(f"{e(nombre)} " + (f"✅ {fmt_miles(d.get('filas') or 0)} filas" if d.get("ok")
                                             else f"❌ {e(_corto(d.get('error'), 60))}"))
        if partes:
            lineas.append("🗂️ Maestros: " + " · ".join(partes))
        if sal.get("error_estado"):
            lineas.append(f"💾 ⚠️ No se pudo guardar el estado: {e(_corto(sal['error_estado'], 120))}")
        for a in (sal.get("config_advertencias") or [])[:5]:
            lineas.append(f"⚠️ {e(_corto(a, 200))}")

        # Telegram corta en 4096: se paran las líneas antes (cada una cierra
        # sus etiquetas, así que cortar entre líneas nunca deja HTML roto).
        salida: list[str] = []
        largo = 0
        for ln in lineas:
            if largo + len(ln) + 1 > 3900:
                salida.append("…")
                break
            salida.append(ln)
            largo += len(ln) + 1
        return "\n".join(salida)

    # ------------------------------------------------------------------
    # Offset de Telegram (lo persiste el estado)
    # ------------------------------------------------------------------

    def telegram_offset(self) -> int:
        with self._lock:
            return int(self.estado.telegram_offset or 0)

    def guardar_telegram_offset(self, offset: int) -> None:
        with self._lock:
            self.estado.telegram_offset = int(offset)
            self._guardar()
