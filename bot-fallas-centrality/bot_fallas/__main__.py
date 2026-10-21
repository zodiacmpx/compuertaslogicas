"""Punto de entrada: `python -m bot_fallas [--config RUTA] [--probar] [--simular FUENTE ARCHIVO [--enviar]] [--version]`.

Modos:
  (sin opciones)   Deja el bot corriendo: servidor local para la extensión,
                   timer de recálculo, avisos y comandos de Telegram. Ctrl+C
                   lo detiene limpio (guarda el estado).
  --probar         Manda un mensaje de prueba por cada canal activo y dice
                   cuál funcionó. Es lo primero que hay que correr al
                   configurar Telegram/Teams (proxy, token, chat_id).
  --simular F A    Procesa un archivo exportado de Centrality como si lo
                   mandara la extensión y muestra en consola qué avisaría y
                   qué fallas arma. No envía nada (salvo --enviar) y usa un
                   estado temporal: no toca el estado real del bot.

Códigos de salida: 0 ok · 1 falló algo al correr · 2 error de configuración o de uso.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import signal
import sys
import tempfile
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import cargar_config, chats_comandos, token_telegram, validar
from .contrato import ErrorContrato
from .maestros import Maestros
from .mensajes import mensaje_detalle, mensaje_salud
from .modelo import FUENTES, Mensaje
from .notificadores import Despachador, construir_notificadores
from .servicio import ServicioFallas
from .texto import fmt_miles

log = logging.getLogger("bot_fallas")

_FORMATO_LOG = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MAX_DETALLES_SIMULAR = 20


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------


def _consola_tolerante() -> None:
    # En Windows, con la salida redirigida a un archivo o a un pipe, la consola
    # usa cp1252 y un emoji de los avisos revienta el print. Mejor un '?' que
    # un traceback.
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(errors="replace")      # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _err(*partes) -> None:
    print(*partes, file=sys.stderr)


def configurar_logging(cfg: dict, archivo: bool = True,
                       nivel_consola: int | None = None) -> list[logging.Handler]:
    """Archivo rotativo (2 MB x 5) + consola. Devuelve los handlers agregados."""
    nivel = getattr(logging, str((cfg.get("log") or {}).get("nivel") or "INFO").upper(), logging.INFO)
    raiz = logging.getLogger()
    raiz.setLevel(min(nivel, nivel_consola or nivel))
    fmt = logging.Formatter(_FORMATO_LOG, datefmt="%Y-%m-%d %H:%M:%S")
    nuevos: list[logging.Handler] = []
    consola = logging.StreamHandler(sys.stderr)
    consola.setLevel(nivel_consola or nivel)
    consola.setFormatter(fmt)
    nuevos.append(consola)
    ruta = str((cfg.get("log") or {}).get("ruta") or "").strip()
    if archivo and ruta:
        try:
            Path(ruta).parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(ruta, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
            fh.setLevel(nivel)
            fh.setFormatter(fmt)
            nuevos.append(fh)
        except OSError as e:
            _err(f"No se pudo abrir el log {ruta}: {e}; sigo sólo con la consola")
    for h in nuevos:
        raiz.addHandler(h)
    return nuevos


def _quitar_logging(handlers: list[logging.Handler]) -> None:
    raiz = logging.getLogger()
    for h in handlers:
        raiz.removeHandler(h)
        try:
            h.close()
        except Exception:       # noqa: BLE001
            pass


def _cargar(args) -> dict | None:
    """Config según --config o ./config.json; None si no se pudo (ya se imprimió el motivo)."""
    ruta = args.config
    if ruta is None:
        defecto = Path("config.json")
        ruta = defecto if defecto.is_file() else None
    try:
        cfg = cargar_config(ruta)
    except (FileNotFoundError, ValueError) as e:
        _err(f"Error de configuración: {e}")
        return None
    if ruta is None:
        _err("Aviso: no hay config.json en esta carpeta; se usan los valores por defecto "
             "(Telegram y Teams desactivados). Copia config.example.json como config.json.")
    return cfg


def _imprimir_errores(errores: list[str]) -> None:
    _err("La configuración tiene errores:")
    for e in errores:
        _err(f"  - {e}")


def _resultado_canales(desp: Despachador) -> bool:
    todo_ok = True
    for nombre, d in desp.estado().items():
        if d.get("fallidos") or not d.get("enviados"):
            todo_ok = False
            print(f"  ✗ {nombre}: FALLÓ — {d.get('ultimo_error') or 'sin confirmación de envío'}")
        else:
            print(f"  ✓ {nombre}: enviado")
    return todo_ok


class DespachadorConsola:
    """Despachador de --simular: imprime cada aviso en vez de enviarlo."""

    def __init__(self):
        self.mensajes: list[Mensaje] = []

    @property
    def canales(self) -> list[str]:
        return ["consola"]

    def encolar(self, m: Mensaje) -> None:
        # Se juntan y se imprimen después del resumen de la ingesta, en orden.
        self.mensajes.append(m)

    def imprimir(self) -> None:
        for m in self.mensajes:
            print(f"\n──── AVISO [{m.tipo}] {m.titulo} ────\n{m.texto}")

    def iniciar(self) -> None:
        pass

    def detener(self, timeout: float = 5) -> None:
        pass

    def vaciar(self, timeout: float = 10) -> bool:
        return True

    def estado(self) -> dict:
        return {"consola": {"enviados": len(self.mensajes), "fallidos": 0, "ultimo_error": None,
                            "ultimo_ok": None, "en_cola": 0, "activo": True}}


# --------------------------------------------------------------------------
# Modos
# --------------------------------------------------------------------------


def probar(cfg: dict) -> int:
    errores = validar(cfg)
    if errores:
        _imprimir_errores(errores)
        return 2
    notificadores = construir_notificadores(cfg, Path(cfg["_base_dir"]))
    if not notificadores:
        _err("No hay ningún canal activo (telegram, teams, archivo): no hay a quién mandar la prueba.")
        return 1
    desp = Despachador(notificadores)
    detalle = f"Versión {__version__} · canales: {', '.join(desp.canales)}"
    desp.encolar(mensaje_salud("prueba", detalle, datetime.now()))
    desp.iniciar()
    print(f"Enviando mensaje de prueba por: {', '.join(desp.canales)} …")
    # 3 intentos con timeout de 20 s y esperas de 2 y 5 s: ~70 s en el peor caso.
    terminado = desp.vaciar(timeout=120)
    ok = _resultado_canales(desp) and terminado
    desp.detener(timeout=2)
    if not terminado:
        print("  (se acabó el tiempo de espera; algún canal no respondió)")
    return 0 if ok else 1


def _linea_falla(f, pasa: bool) -> str:
    estado = "ABIERTA" if f.abierta else "CERRADA"
    marcas = []
    if f.programada:
        marcas.append("programada")
    if f.abierta and not pasa:
        marcas.append("no pasa los filtros")
    cl = "s/d" if f.clientes is None else (("≥ " if f.clientes_fuente == "nis_reclamos" else "")
                                          + fmt_miles(f.clientes))
    partes = [f"{f.caso}", estado, f"{cl} clientes ({f.clientes_fuente or 'sin fuente'})",
              (f.nivel or "s/d").upper(), ", ".join(f.comunas[:3]) or "sin comuna",
              f"{len(f.calles)} calles", "fuentes: " + ", ".join(f.fuentes)]
    return "  " + " · ".join(partes) + (f"  [{'; '.join(marcas)}]" if marcas else "")


def simular(cfg: dict, fuente: str, archivo: Path, enviar: bool = False) -> int:
    if fuente not in FUENTES:
        _err(f"Fuente desconocida: {fuente!r} (válidas: {', '.join(FUENTES)})")
        return 2
    try:
        datos = Path(archivo).read_bytes()
    except OSError as e:
        _err(f"No se pudo leer {archivo}: {e.strerror or e}")
        return 2

    # Sin --enviar los canales no importan: un token faltante no impide simular.
    a_validar = cfg
    if not enviar:
        a_validar = copy.deepcopy(cfg)
        for canal in ("telegram", "teams"):
            if isinstance(a_validar.get(canal), dict):
                a_validar[canal]["activo"] = False
    errores = validar(a_validar)
    if errores:
        _imprimir_errores(errores)
        return 2

    base_dir = Path(cfg["_base_dir"])
    maestros = Maestros.cargar(cfg.get("maestros") or {}, base_dir)
    if enviar:
        desp = Despachador(construir_notificadores(cfg, base_dir))
        if not desp.canales:
            _err("--enviar: no hay ningún canal activo en la configuración.")
            return 2
        desp.iniciar()
        try:
            codigo = _simular(cfg, fuente, archivo, datos, maestros, desp)
            if codigo:
                return codigo
            print("\nEnviando avisos …")
            terminado = desp.vaciar(timeout=120)
            ok = _resultado_canales(desp) and terminado
            return 0 if ok else 1
        finally:
            desp.detener(timeout=2)
    consola = DespachadorConsola()
    codigo = _simular(cfg, fuente, archivo, datos, maestros, consola)
    if codigo:
        return codigo
    print("\n(simulación: no se envió nada; usa --enviar para mandar los avisos a los canales)")
    return 0


def _simular(cfg: dict, fuente: str, archivo: Path, datos: bytes, maestros: Maestros, desp) -> int:
    with tempfile.TemporaryDirectory(prefix="bot_fallas_simular_") as tmp:
        # gracia 0: aquí no hay ciclo de extensión que esperar.
        servicio = ServicioFallas(cfg, desp, maestros, ruta_estado=Path(tmp) / "estado.json",
                                  gracia_inicio_s=0)
        try:
            if fuente == "grilla":
                try:
                    payload = json.loads(datos.decode("utf-8-sig"))
                except ValueError as e:
                    _err(f"{archivo} no es un JSON válido: {e}")
                    return 2
                res = servicio.ingest_grilla(payload)
            else:
                res = servicio.ingest_csv(fuente, datos)
        except ErrorContrato as e:
            _err(f"El archivo no sirve como fuente '{fuente}': {e}")
            return 1
        except ValueError as e:
            _err(f"Error: {e}")
            return 2

        print(f"\n==== Ingesta {fuente}: {res['registros']} registros de {res['filas_leidas']} filas "
              f"({res['filas_descartadas']} descartadas)")
        if res["columnas_mapeadas"]:
            print("Columnas reconocidas: " + ", ".join(f"{k} → {v}" for k, v in res["columnas_mapeadas"].items()))
        if res["faltantes"]:
            print("Columnas útiles que NO vienen: " + ", ".join(res["faltantes"]))
        for a in res["advertencias"]:
            print(f"Advertencia: {a}")

        print(f"\n==== Eventos ({len(res['eventos'])})")
        for ev in res["eventos"]:
            print(f"  {ev['tipo']}" + (f" · caso {ev['caso']}" if ev["caso"] else ""))
        if isinstance(desp, DespachadorConsola):
            desp.imprimir()

        fallas = sorted(servicio.ultimas_fallas().values(),
                        key=lambda f: (-(f.clientes if f.clientes is not None else -1), f.caso))
        print(f"\n==== Fallas calculadas ({len(fallas)})")
        activas = []
        for f in fallas:
            pasa = servicio.detector.pasa_filtros(f)
            print(_linea_falla(f, pasa))
            if f.abierta and pasa:
                activas.append(f)
        ahora = datetime.now()
        for f in activas[:_MAX_DETALLES_SIMULAR]:
            print(f"\n---- Detalle caso {f.caso}\n{mensaje_detalle(f, cfg, ahora).texto}")
        if len(activas) > _MAX_DETALLES_SIMULAR:
            print(f"\n…y {len(activas) - _MAX_DETALLES_SIMULAR} fallas activas más sin detalle")
    return 0


def correr(cfg: dict, parar: threading.Event | None = None) -> int:
    """El bot en servicio. Vuelve cuando `parar` se activa o con Ctrl+C."""
    # Importes aquí: --version y --simular no necesitan levantar red.
    from .servidor import crear_servidor, iniciar_en_hilo
    from .telegram_comandos import ComandosTelegram

    errores = validar(cfg)
    if errores:
        _imprimir_errores(errores)
        return 2
    for a in cfg.get("_advertencias") or []:
        log.warning("Config: %s", a)

    base_dir = Path(cfg["_base_dir"])
    maestros = Maestros.cargar(cfg.get("maestros") or {}, base_dir)
    desp = Despachador(construir_notificadores(cfg, base_dir))
    ruta_estado = Path(cfg.get("datos_dir") or base_dir / "datos") / "estado.json"
    try:
        servicio = ServicioFallas(cfg, desp, maestros, ruta_estado=ruta_estado)
    except OSError as e:
        _err(f"No se pudo leer el estado {ruta_estado}: {e}")
        return 1
    try:
        servidor = crear_servidor(servicio, cfg)
    except (OSError, ValueError) as e:
        _err(f"No se pudo abrir el servidor local: {e}")
        return 1

    parar = parar or threading.Event()
    comandos = None
    desp.iniciar()
    iniciar_en_hilo(servidor)
    servicio.iniciar_recalculo()
    tg = cfg.get("telegram") or {}
    if tg.get("activo") and tg.get("comandos"):
        comandos = ComandosTelegram(token_telegram(cfg), chats_comandos(cfg), servicio,
                                    servicio.telegram_offset, servicio.guardar_telegram_offset,
                                    proxy=tg.get("proxy") or "", ca_bundle=tg.get("ca_bundle") or "")
        comandos.iniciar()

    host, puerto = servidor.server_address[:2]
    host = f"[{host}]" if ":" in str(host) else host
    canales = desp.canales
    log.info("Bot de fallas v%s escuchando en http://%s:%s/", __version__, host, puerto)
    log.info("Canales de aviso: %s%s", ", ".join(canales) or "NINGUNO",
             " · comandos de Telegram activos" if comandos else "")
    fuentes = cfg.get("fuentes") or {}
    log.info("Fuentes esperadas: %s", ", ".join(
        f"{f} (frescura {(fuentes.get(f) or {}).get('frescura_min', '?')} min"
        + ("" if (fuentes.get(f) or {}).get("cuenta_ausencia", f != "grilla") else ", no normaliza por ausencia")
        + ")" for f in FUENTES))
    log.info("Estado en %s (%d casos seguidos)", ruta_estado, len(servicio.estado.casos))

    def _senal(signum, _frame):
        log.info("Señal %s recibida: deteniendo", signum)
        parar.set()

    if threading.current_thread() is threading.main_thread():
        for nombre in ("SIGTERM", "SIGBREAK"):     # SIGBREAK: Ctrl+Break en Windows
            if hasattr(signal, nombre):
                try:
                    signal.signal(getattr(signal, nombre), _senal)
                except (OSError, ValueError):
                    pass
    try:
        # wait con timeout: en Windows un wait() sin timeout no deja pasar Ctrl+C.
        while not parar.wait(1.0):
            pass
    except KeyboardInterrupt:
        log.info("Ctrl+C: deteniendo")
    finally:
        if comandos:
            comandos.detener()
        servicio.detener()
        servidor.shutdown()
        servidor.server_close()
        desp.vaciar(timeout=10)
        desp.detener(timeout=5)
        log.info("Bot detenido")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m bot_fallas",
        description="Bot de fallas Centrality: avisa calles y clientes afectados por Telegram/Teams.")
    p.add_argument("--config", type=Path, metavar="RUTA",
                   help="archivo de configuración (por defecto ./config.json si existe)")
    p.add_argument("--probar", action="store_true",
                   help="manda un mensaje de prueba por cada canal activo y termina")
    p.add_argument("--simular", nargs=2, metavar=("FUENTE", "ARCHIVO"),
                   help=f"procesa un archivo exportado ({', '.join(FUENTES)}) y muestra qué avisaría")
    p.add_argument("--enviar", action="store_true",
                   help="con --simular: envía de verdad los avisos a los canales activos")
    p.add_argument("--version", action="store_true", help="muestra la versión y termina")
    return p


def main(argv: list[str] | None = None) -> int:
    _consola_tolerante()
    args = _parser().parse_args(argv)
    if args.version:
        print(f"bot_fallas {__version__}")
        return 0
    if args.enviar and not args.simular:
        _err("--enviar sólo tiene sentido junto con --simular")
        return 2
    cfg = _cargar(args)
    if cfg is None:
        return 2

    if args.simular:
        # Simular no escribe en el log real: no mezclar pruebas con la operación.
        handlers = configurar_logging(cfg, archivo=False, nivel_consola=logging.WARNING)
        try:
            return simular(cfg, args.simular[0], Path(args.simular[1]), enviar=args.enviar)
        finally:
            _quitar_logging(handlers)

    handlers = configurar_logging(cfg)
    try:
        if args.probar:
            return probar(cfg)
        return correr(cfg)
    finally:
        _quitar_logging(handlers)


if __name__ == "__main__":
    sys.exit(main())
