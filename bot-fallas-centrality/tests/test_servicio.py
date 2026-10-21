"""Tests de servicio.py (SPEC §4.11) y escenario integrado de punta a punta (SPEC §6).

Todo corre con un reloj falso que se avanza a mano y un despachador falso que
junta los Mensajes: así se ve exactamente qué le llegaría al despachador y
cuándo. Los CSV de cada paso se arman a partir de los fixtures (mismos
encabezados, mismas filas), agregando, quitando o cambiando filas.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import logging
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from bot_fallas.config import DEFAULTS
from bot_fallas.contrato import ErrorContrato
from bot_fallas.maestros import Maestros
from bot_fallas.modelo import Falla
from bot_fallas.servicio import ServicioFallas, ventana_de_params

FIX = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 10, 21, 16, 30, 0)


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


# --------------------------------------------------------------------------
# Dobles
# --------------------------------------------------------------------------


class Reloj:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def ir(self, minutos: float) -> datetime:
        """Pone el reloj en T0 + `minutos`."""
        self.t = T0 + timedelta(minutes=minutos)
        return self.t


class DespachadorFalso:
    def __init__(self):
        self.mensajes = []

    def encolar(self, m) -> None:
        self.mensajes.append(m)

    def estado(self) -> dict:
        return {"falso": {"enviados": len(self.mensajes), "fallidos": 0, "ultimo_error": None}}

    def tipos(self) -> list[str]:
        return [m.tipo for m in self.mensajes]


def cfg_prueba(**secciones) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    for seccion, valores in secciones.items():
        cfg[seccion].update(valores)
    return cfg


# --------------------------------------------------------------------------
# CSV a partir de los fixtures
# --------------------------------------------------------------------------


def _leer(nombre: str, sep: str) -> tuple[list[str], list[list[str]]]:
    texto = (FIX / nombre).read_text(encoding="utf-8-sig")
    filas = list(csv.reader(io.StringIO(texto, newline=""), delimiter=sep))
    return filas[0], filas[1:]


ENC_CASOS, FILAS_CASOS = _leer("casos.csv", ",")
ENC_RECL, FILAS_RECL = _leer("reclamos.csv", ";")
_COL_CIERRE_CASOS = 6


def _csv(enc: list[str], filas: list[list[str]], sep: str) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep, lineterminator="\n")
    w.writerow(enc)
    w.writerows(filas)
    return buf.getvalue().encode("utf-8")


def fila_caso(caso: str, alim: str, clientes: int, inicio: str, cierre: str = "") -> list[str]:
    return [caso, alim, "Arbol sobre linea", "ANALISTA PRUEBA", inicio, "", cierre, str(clientes),
            "Aerea", "Conductor", "Operacion de protecciones",
            "cliente RAUL PRUEBA fono +56 9 1414 1414 indica arbol en O'HIGGINS 455"]


def fila_reclamo(ticket: str, caso: str, direccion: str, nis: str, nombre: str, fono: str,
                 fecha: str) -> list[str]:
    return [ticket, caso, "SAESA", direccion, nis, nombre, fono, "Cliente indica que no tiene luz",
            "OPERADORA PRUEBA", "Sin comentarios", "Sin Suministro", fecha, "BBOO 9001 PRUEBA", "",
            "", "", "Sin peligro", "Verdadero", "Medio", "OSORNO", ""]


def casos_csv(sin: tuple = (), extra: list | None = None, cerrar: dict | None = None) -> bytes:
    filas = []
    for f in FILAS_CASOS:
        if f and f[0] in sin:
            continue
        f = list(f)
        if f and f[0] in (cerrar or {}):
            f[_COL_CIERRE_CASOS] = cerrar[f[0]]
        filas.append(f)
    return _csv(ENC_CASOS, filas + list(extra or []), ",")


def reclamos_csv(sin: tuple = (), extra: list | None = None) -> bytes:
    filas = [list(f) for f in FILAS_RECL if not (len(f) > 1 and f[1] in sin)]
    return _csv(ENC_RECL, filas + list(extra or []), ";")


def grilla(sin_casos: tuple = (), sin_filas: bool = False) -> dict:
    p = json.loads((FIX / "grilla.json").read_text(encoding="utf-8"))
    for g in p["grillas"]:
        if sin_filas:
            g["filas"] = []
        else:
            g["filas"] = [f for f in g["filas"] if str(f.get("Caso", "")) not in sin_casos]
    return p


# Caso nuevo del escenario: 2 alimentadores (600 + 150) y 2 reclamos con
# direcciones con número de casa y depto, nombres y teléfonos inventados.
NUEVO = "40890000"
RECL_NUEVO = [
    fila_reclamo("R-3001", NUEVO, "AV. MATTA 2200, OSORNO", "9100001", "RAUL PRUEBA",
                 "+56 9 1414 1414", "2026-10-21 16:25:00"),
    fila_reclamo("R-3002", NUEVO, "O'HIGGINS 455 DEPTO 2, OSORNO", "9100002", "SOFIA PRUEBA",
                 "+56 9 1515 1515", "2026-10-21 16:27:00"),
]


def filas_nuevo(a300: int) -> list[list[str]]:
    return [fila_caso(NUEVO, "A300", a300, "2026-10-21T16:20:00"),
            fila_caso(NUEVO, "A101", 150, "2026-10-21T16:21:00")]


# --------------------------------------------------------------------------
# Privacidad: lo que NUNCA puede salir en un aviso, /estado, /fallas o estado.json
# --------------------------------------------------------------------------

NIS_FIXTURES = [
    "1000001", "1000002", "1000003", "1000004", "1000005", "1000006", "1000010", "1000011",
    "2000001", "3000001", "4000001", "4000002", "4000003", "5000001", "6000001", "6000002",
    "7000001", "7000002", "7000009", "8000001", "9000001", "9000002", "9000003",
    "9100001", "9100002",
]
# Todos los nombres de persona de los fixtures (clientes, telefonista, analistas,
# brigadas) llevan "PRUEBA": basta con que esa palabra no aparezca.
PROHIBIDOS = ["PRUEBA", "+56", "1111 1111", "1414 1414", "S/N", "DEPTO", "Depto", "KM 5", "Lat:",
              "-40.5731", "MATTA 1", "MATTA 2", "Matta 1", "CARRERA 9", "CARRERA 1", "AROMOS 4",
              "MAYO 4", "NORTE 1", "HIGGINS 4", "HIGGINS 3", "poste", "chispas"] + NIS_FIXTURES
# Calles cuyo nombre lleva números de verdad (no son números de casa).
CALLES_CON_NUMERO = {"21 DE MAYO", "5 NORTE"}


class _Privacidad:
    def assertSinDatosPersonales(self, texto: str, donde: str = ""):
        for p in PROHIBIDOS:
            self.assertNotIn(p, texto, f"{donde}: aparece {p!r}")

    def assertCallesSinNumero(self, fallas):
        for f in fallas:
            for c in f.calles:
                if c.nombre not in CALLES_CON_NUMERO:
                    self.assertIsNone(re.search(r"\d", c.nombre),
                                      f"caso {f.caso}: calle con número {c.nombre!r}")


# --------------------------------------------------------------------------
# Escenario integrado (SPEC §6)
# --------------------------------------------------------------------------


class TestEscenarioIntegrado(unittest.TestCase, _Privacidad):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ruta = Path(self.tmp.name) / "datos" / "estado.json"
        self.reloj = Reloj(T0)
        self.desp = DespachadorFalso()
        self.svc = ServicioFallas(cfg_prueba(), self.desp, Maestros(), reloj=self.reloj,
                                  ruta_estado=self.ruta)
        self.eventos = []       # todos los eventos, para las verificaciones finales

    def tearDown(self):
        self.tmp.cleanup()

    def paso(self, minutos: float, accion: str, *args) -> list[tuple[str, str]]:
        """Avanza el reloj y ejecuta una acción; devuelve [(tipo, caso)] de lo avisado."""
        self.reloj.ir(minutos)
        antes = len(self.desp.mensajes)
        if accion == "recalcular":
            evs = self.svc.recalcular()
            self.eventos += evs
            res = [(e.tipo, e.caso) for e in evs]
        elif accion == "grilla":
            res = [(e["tipo"], e["caso"]) for e in self.svc.ingest_grilla(*args)["eventos"]]
        elif accion == "latido":
            res = [(e["tipo"], e["caso"]) for e in self.svc.latido(*args)["eventos"]]
        else:
            res = [(e["tipo"], e["caso"]) for e in self.svc.ingest_csv(accion, *args)["eventos"]]
        # Lo que devuelve la ingesta es exactamente lo que se encoló.
        self.assertEqual([(m.tipo, m.caso) for m in self.desp.mensajes[antes:]], res)
        return res

    def ultimo(self, tipo: str):
        return [m for m in self.desp.mensajes if m.tipo == tipo][-1]

    def test_escenario_completo(self):
        svc = self.svc

        # ---- 1. Primer arranque: casos + reclamos del mismo ciclo -> UN resumen.
        self.assertEqual(self.paso(0, "casos", casos_csv()), [])
        self.assertEqual(self.paso(1, "reclamos", reclamos_csv()), [],
                         "en el primer arranque se espera el fin del ciclo de la extensión")
        self.assertTrue(svc.salud()["calentando"])
        self.assertEqual(self.paso(2, "latido", {"nivel": "ok", "mensaje": "ciclo 1 terminado",
                                                 "ciclo": 1, "ultimoError": None,
                                                 "proximo_en_min": 5}),
                         [("resumen_inicio", "")])
        resumen = self.ultimo("resumen_inicio")
        # Activas que pasan filtros, de más a menos clientes. 40844444 sólo viene
        # en reclamos (3 clientes con reclamo) y entra igual al resumen.
        orden = [c for c in ("40808515", "40811111", "40844444") if c in resumen.texto]
        self.assertEqual(orden, ["40808515", "40811111", "40844444"])
        self.assertLess(resumen.texto.index("40808515"), resumen.texto.index("40811111"))
        self.assertLess(resumen.texto.index("40811111"), resumen.texto.index("40844444"))
        self.assertNotIn("40833333", resumen.texto)      # programada
        self.assertNotIn("40822222", resumen.texto)      # cerrada en el informe de casos
        self.assertIn("1.240 cl", resumen.texto)         # 1200 (máx. de A260 repetido) + 40 (A261)

        # ---- 2. Caso nuevo con 2 alimentadores. Con 2 reclamos solos no pasa el
        # filtro (≥3); cuando llega el informe de casos se avisa con la suma.
        self.assertEqual(self.paso(5, "reclamos", reclamos_csv(extra=RECL_NUEVO)), [])
        self.assertEqual(self.paso(6, "casos", casos_csv(extra=filas_nuevo(600))), [("nueva", NUEVO)])
        nueva = self.ultimo("nueva")
        self.assertIn("FALLA NUEVA", nueva.html)
        self.assertIn("<b>750 clientes</b>", nueva.html)
        self.assertIn("nivel ALTA", nueva.texto)
        self.assertIn("• AV. MATTA — Osorno (1)", nueva.texto)
        self.assertIn("• O'HIGGINS — Osorno (1)", nueva.texto)
        self.assertIn("750 clientes", nueva.titulo)

        # ---- 3. Crece 750 -> 980 (+230, ≥50 y ≥25 %) pero a 4 min del aviso:
        # se difiere; sale en el primer recálculo pasado el intervalo de 10 min.
        self.assertEqual(self.paso(10, "casos", casos_csv(extra=filas_nuevo(830))), [])
        self.assertEqual(self.paso(14, "recalcular"), [])
        self.assertEqual(self.paso(17, "recalcular"), [("actualizada", NUEVO)])
        act = self.ultimo("actualizada")
        self.assertIn("750 → 980 clientes", act.texto)
        self.assertIn("(+230)", act.texto)
        # Pasa a MASIVO 2 min después: urgente, no espera el intervalo.
        self.assertEqual(self.paso(19, "casos", casos_csv(extra=filas_nuevo(1500))),
                         [("actualizada", NUEVO)])
        self.assertIn("nivel ALTA → MASIVO", self.ultimo("actualizada").texto)
        self.assertIn("980 → 1.650 clientes", self.ultimo("actualizada").texto)

        # ---- 4. Cierre explícito en el informe de casos -> normalizada al tiro.
        cerrar = {"40811111": "2026-10-21T16:45:00"}
        self.assertEqual(self.paso(20, "casos", casos_csv(extra=filas_nuevo(1500), cerrar=cerrar)),
                         [("normalizada", "40811111")])
        norm = self.ultimo("normalizada")
        self.assertIn("FALLA NORMALIZADA", norm.html)
        self.assertIn("cierre informado por informe de casos", norm.texto)

        # ---- 5. 40808515 deja de aparecer (en casos Y en reclamos): normalizada
        # sólo tras ≥15 min y ≥2 evaluaciones de ausencia, nunca antes.
        sin = ("40808515",)
        self.assertEqual(self.paso(25, "reclamos", reclamos_csv(sin=sin, extra=RECL_NUEVO)), [])
        self.assertEqual(self.paso(26, "casos", casos_csv(sin=sin, extra=filas_nuevo(1500),
                                                          cerrar=cerrar)), [])
        self.assertEqual(self.paso(31, "recalcular"), [])
        self.assertEqual(self.paso(36, "reclamos", reclamos_csv(sin=sin, extra=RECL_NUEVO)), [])
        self.assertEqual(self.paso(40, "recalcular"), [], "a los 14 min todavía no")
        self.assertEqual(self.paso(41, "recalcular"), [("normalizada", "40808515")])
        norm = self.ultimo("normalizada")
        self.assertIn("no hay cierre explícito", norm.texto)
        self.assertIn("Máx. clientes: 1.240", norm.texto)

        # ---- 6. Falla que sólo ve la grilla: se avisa, pero su ausencia NUNCA
        # la normaliza (la grilla puede estar filtrada o paginada).
        self.assertEqual(self.paso(42, "grilla", grilla(sin_casos=sin)), [("nueva", "40877777")])
        self.assertIn("120 clientes", self.ultimo("nueva").texto)
        self.assertEqual(self.paso(47, "grilla", grilla(sin_filas=True)), [])
        for k in range(1, 9):                      # 40 min más con las otras fuentes al día
            self.assertEqual(self.paso(47 + 5 * k, "casos", casos_csv(sin=sin, extra=filas_nuevo(1500),
                                                                      cerrar=cerrar)), [])
            self.assertEqual(self.paso(48 + 5 * k, "reclamos", reclamos_csv(sin=sin, extra=RECL_NUEVO)), [])
            self.assertEqual(self.paso(49 + 5 * k, "recalcular"), [])
        s = svc.estado.casos["40877777"]
        self.assertFalse(s.normalizada)
        self.assertTrue(s.notificada)

        # ---- Totales: ningún aviso repetido ni de más.
        self.assertEqual(self.desp.tipos(), ["resumen_inicio", "nueva", "actualizada", "actualizada",
                                             "normalizada", "normalizada", "nueva"])

        # ---- Privacidad en TODO lo que sale del equipo.
        for m in self.desp.mensajes:
            self.assertSinDatosPersonales(m.html, f"html de {m.titulo}")
            self.assertSinDatosPersonales(m.texto, f"texto de {m.titulo}")
            self.assertSinDatosPersonales(m.titulo, "título")
        fallas_eventos = [e.falla for e in self.eventos if e.falla] + \
                         [f for e in self.eventos for f in e.fallas]
        fallas_eventos += [Falla.from_dict(d) for d in svc.fallas_activas()]
        self.assertTrue(fallas_eventos)
        self.assertCallesSinNumero(fallas_eventos)
        self.assertSinDatosPersonales(json.dumps(svc.salud(), ensure_ascii=False), "/estado")
        self.assertSinDatosPersonales(json.dumps(svc.fallas_activas(), ensure_ascii=False), "/fallas")
        self.assertSinDatosPersonales(json.dumps(svc.falla(NUEVO), ensure_ascii=False), "/fallas/caso")
        self.assertSinDatosPersonales(svc.texto_resumen(), "/fallas Telegram")
        self.assertSinDatosPersonales(svc.texto_caso(NUEVO), "/caso Telegram")
        self.assertSinDatosPersonales(svc.texto_estado(), "/estado Telegram")
        self.assertTrue(self.ruta.is_file())
        self.assertSinDatosPersonales(self.ruta.read_text(encoding="utf-8"), "estado.json")

        # ---- /fallas: activas que pasan filtros, de más a menos clientes. 40877777
        # ya no sale (ninguna fuente fresca la muestra) pero se sigue en el estado.
        activas = [d["caso"] for d in svc.fallas_activas()]
        self.assertEqual(activas, [NUEVO, "40844444"])
        self.assertIn("40877777", svc.estado.casos)


# --------------------------------------------------------------------------
# Piezas del servicio
# --------------------------------------------------------------------------


class _Base(unittest.TestCase, _Privacidad):
    def nuevo(self, cfg=None, gracia=0, ruta=None, maestros=None, t=T0) -> ServicioFallas:
        self.reloj = Reloj(t)
        self.desp = DespachadorFalso()
        return ServicioFallas(cfg or cfg_prueba(), self.desp, maestros or Maestros(), reloj=self.reloj,
                              ruta_estado=ruta, gracia_inicio_s=gracia)


class TestIngesta(_Base):
    def test_respuesta_de_ingesta(self):
        svc = self.nuevo()
        r = svc.ingest_csv("casos", casos_csv())
        self.assertEqual(r["ok"], True)
        self.assertEqual(r["fuente"], "casos")
        self.assertEqual(r["registros"], 6)
        self.assertEqual(r["filas_leidas"], 7)
        self.assertEqual(r["filas_descartadas"], 1)
        self.assertEqual(r["fallas_activas"], 2)          # 40808515 y 40811111 (sin programada ni cerrada)
        self.assertEqual(r["eventos"], [{"tipo": "resumen_inicio", "caso": ""}])
        self.assertEqual(r["columnas_mapeadas"]["Caso"], "caso")
        self.assertIsInstance(r["faltantes"], list)
        self.assertIsInstance(r["advertencias"], list)
        json.dumps(r)                                      # serializable tal cual

    def test_fuente_desconocida(self):
        svc = self.nuevo()
        for fuente in ("grilla", "otra", ""):
            with self.assertRaises(ValueError) as cm:
                svc.ingest_csv(fuente, casos_csv())
            self.assertNotIsInstance(cm.exception, ErrorContrato)
            self.assertIn("fuente desconocida", str(cm.exception))
        self.assertEqual(svc.snapshots, {})

    def test_error_contrato_no_reemplaza_la_snapshot_previa(self):
        svc = self.nuevo()
        svc.ingest_csv("casos", casos_csv())
        previa = svc.snapshots["casos"]
        antes = svc.estado.fuentes["casos"]["recibido"]
        self.reloj.ir(5)
        with self.assertRaises(ErrorContrato):
            svc.ingest_csv("casos", b"Hola;Mundo\nx;y\n")
        self.assertIs(svc.snapshots["casos"], previa)
        f = svc.estado.fuentes["casos"]
        self.assertFalse(f["ok"])
        self.assertIn("caso", f["error"])
        self.assertEqual(f["ts"], (T0 + timedelta(minutes=5)).isoformat())
        self.assertEqual(f["recibido"], antes, "se conserva de cuándo es el dato válido")
        self.assertEqual([d["caso"] for d in svc.fallas_activas()], ["40808515", "40811111"])
        sal = svc.salud()["fuentes"]["casos"]
        self.assertFalse(sal["ok"])
        self.assertTrue(sal["fresca"], "la foto previa se sigue usando")
        # Una ingesta OK posterior limpia el error.
        svc.ingest_csv("casos", casos_csv())
        self.assertTrue(svc.estado.fuentes["casos"]["ok"])
        self.assertEqual(svc.estado.fuentes["casos"]["error"], "")

    def test_respuesta_vacia_es_error_contrato(self):
        svc = self.nuevo()
        with self.assertRaises(ErrorContrato):
            svc.ingest_csv("reclamos", b"")
        self.assertNotIn("reclamos", svc.snapshots)
        self.assertFalse(svc.estado.fuentes["reclamos"]["ok"])

    def test_error_contrato_de_grilla(self):
        svc = self.nuevo()
        svc.ingest_grilla(grilla())
        previa = svc.snapshots["grilla"]
        with self.assertRaises(ErrorContrato):
            svc.ingest_grilla({"url": "x", "grillas": []})
        self.assertIs(svc.snapshots["grilla"], previa)
        self.assertFalse(svc.estado.fuentes["grilla"]["ok"])

    def test_csv_valido_sin_filas_vacia_la_fuente(self):
        svc = self.nuevo()
        svc.ingest_csv("casos", casos_csv())
        svc.ingest_csv("casos", _csv(ENC_CASOS, [], ","))
        self.assertEqual(svc.snapshots["casos"].registros, [])
        self.assertEqual(svc.fallas_activas(), [])

    def test_latido_ignora_campos_desconocidos_y_no_primitivos(self):
        svc = self.nuevo()
        r = svc.latido({"nivel": "ok", "ciclo": 3, "ultimoError": None, "x": "otro",
                        "mensaje": "a" * 2000, "version": {"no": "primitivo"}})
        self.assertEqual(r, {"ok": True, "eventos": []})
        ext = svc.salud()["extension"]
        self.assertEqual(ext["ciclo"], 3)
        self.assertNotIn("x", ext)
        self.assertNotIn("version", ext)
        self.assertEqual(len(ext["mensaje"]), 500)
        self.assertEqual(ext["edad_min"], 0)
        with self.assertRaises(ValueError):
            svc.latido(["no", "es", "objeto"])


class TestVentana(_Base):
    def test_params_desde_hasta_van_a_la_snapshot(self):
        svc = self.nuevo()
        svc.ingest_csv("casos", casos_csv(), {"desde": "2026-10-18 16:30:00", "hasta": "2026-10-21T16:30:00"})
        s = svc.snapshots["casos"]
        self.assertEqual(s.ventana_desde, datetime(2026, 10, 18, 16, 30))
        self.assertEqual(s.ventana_hasta, datetime(2026, 10, 21, 16, 30))
        self.assertEqual(svc.estado.fuentes["casos"]["ventana_desde"], "2026-10-18T16:30:00")

    def test_sin_params_no_hay_ventana(self):
        svc = self.nuevo()
        svc.ingest_csv("casos", casos_csv(), {"desde": "", "otro": "x"})
        self.assertIsNone(svc.snapshots["casos"].ventana_desde)
        self.assertIsNone(svc.snapshots["casos"].ventana_hasta)

    def test_ventana_invalida_es_value_error_y_no_toca_nada(self):
        svc = self.nuevo()
        svc.ingest_csv("casos", casos_csv())
        previa = svc.snapshots["casos"]
        for params in ({"desde": "ayer"}, {"desde": "2026-10-21 10:00:00", "hasta": "2026-10-20 10:00:00"}):
            with self.assertRaises(ValueError):
                svc.ingest_csv("casos", casos_csv(sin=("40808515",)), params)
            self.assertIs(svc.snapshots["casos"], previa)

    def test_ventana_de_params(self):
        self.assertEqual(ventana_de_params(None), (None, None))
        self.assertEqual(ventana_de_params({"desde": "2026-10-21T08:00:00-03:00"}),
                         (datetime(2026, 10, 21, 8, 0), None))

    def _ausencia(self, desde: str) -> list[str]:
        """40808515 (inicio 14:32) desaparece de un informe de casos con ventana."""
        svc = self.nuevo()
        params = {"desde": desde, "hasta": "2026-10-21 16:30:00"}
        self.assertEqual([e["tipo"] for e in svc.ingest_csv("casos", casos_csv(), params)["eventos"]],
                         ["resumen_inicio"])
        tipos = []
        for minuto in range(5, 65, 5):
            self.reloj.ir(minuto)
            r = svc.ingest_csv("casos", casos_csv(sin=("40808515",)), params)
            tipos += [e["tipo"] for e in r["eventos"]]
        return tipos

    def test_ventana_respeta_inicio_antiguo(self):
        # Inicio 14:32 < desde + 1 h (15:00): pudo salir por la ventana, no por
        # cierre -> el informe de casos no da fe de su ausencia.
        self.assertEqual(self._ausencia("2026-10-21 14:00:00"), [])

    def test_ventana_holgada_si_normaliza(self):
        self.assertEqual(self._ausencia("2026-10-18 16:30:00"), ["normalizada"])


class TestFrescura(_Base):
    def test_snapshot_vieja_no_cuenta(self):
        svc = self.nuevo(cfg_prueba(avisos={"alerta_sin_datos_min": 240}))
        svc.ingest_csv("casos", casos_csv())
        self.assertEqual(len(svc.fallas_activas()), 2)
        self.reloj.ir(30)
        svc.recalcular()
        self.assertEqual(len(svc.fallas_activas()), 2, "a los 30 min justos sigue fresca")
        self.assertTrue(svc.salud()["fuentes"]["casos"]["fresca"])
        for minuto in (31, 60, 120):
            self.reloj.ir(minuto)
            self.assertEqual(svc.recalcular(), [], "una fuente vieja no normaliza nada")
        self.assertEqual(svc.fallas_activas(), [])
        sal = svc.salud()["fuentes"]["casos"]
        self.assertFalse(sal["fresca"])
        self.assertEqual(sal["edad_min"], 120.0)
        self.assertIn("40808515", svc.estado.casos, "se sigue: no se olvida ni se normaliza")
        self.assertFalse(svc.estado.casos["40808515"].normalizada)
        # Vuelve la fuente: no se re-avisa lo ya avisado.
        self.reloj.ir(125)
        self.assertEqual(svc.ingest_csv("casos", casos_csv())["eventos"], [])

    def test_frescura_por_fuente(self):
        cfg = cfg_prueba()
        cfg["fuentes"]["reclamos"]["frescura_min"] = 5
        svc = self.nuevo(cfg)
        svc.ingest_csv("casos", casos_csv())
        svc.ingest_csv("reclamos", reclamos_csv())
        self.reloj.ir(6)
        svc.recalcular()
        self.assertEqual(sorted(svc.ultimas_fallas()), ["40808515", "40811111", "40822222", "40833333"])
        self.assertTrue(svc.salud()["fuentes"]["casos"]["fresca"])
        self.assertFalse(svc.salud()["fuentes"]["reclamos"]["fresca"])


class TestPrimerArranque(_Base):
    def test_calentamiento_termina_por_tiempo(self):
        svc = self.nuevo(gracia=None)                  # GRACIA_INICIO por defecto (5 min)
        self.assertEqual(svc.ingest_csv("casos", casos_csv())["eventos"], [])
        self.reloj.ir(4)
        self.assertEqual(svc.recalcular(), [])
        self.reloj.ir(5)
        self.assertEqual([e.tipo for e in svc.recalcular()], ["resumen_inicio"])
        self.assertTrue(svc.estado.iniciado)

    def test_calentamiento_termina_con_todas_las_fuentes_csv(self):
        svc = self.nuevo(gracia=None)
        self.assertEqual(svc.ingest_csv("casos", casos_csv())["eventos"], [])
        self.assertEqual(svc.ingest_csv("reclamos", reclamos_csv())["eventos"], [])
        r = svc.ingest_csv("abiertas", (FIX / "abiertas.csv").read_bytes())
        self.assertEqual([e["tipo"] for e in r["eventos"]], ["resumen_inicio"])

    def test_latido_de_fin_de_ciclo_previo_no_cuenta(self):
        svc = self.nuevo(gracia=None)
        svc.latido({"nivel": "ok", "proximo_en_min": 5})
        self.reloj.ir(1)
        self.assertEqual(svc.ingest_csv("casos", casos_csv())["eventos"], [],
                         "ese latido era de un ciclo anterior a la primera ingesta")
        r = svc.latido({"nivel": "info", "mensaje": "ciclo 2 iniciado", "disparo": "alarma"})
        self.assertEqual(r["eventos"], [], "el latido de INICIO de ciclo no termina la espera")
        r = svc.latido({"nivel": "error", "ultimoError": "abiertas: REPORTE_PESADO: 504",
                        "proximo_en_min": 5})
        self.assertEqual([e["tipo"] for e in r["eventos"]], ["resumen_inicio"])

    def test_sin_espera_si_el_estado_ya_estaba_iniciado(self):
        svc = self.nuevo(gracia=None)
        svc.estado.iniciado = True
        r = svc.ingest_csv("casos", casos_csv())
        self.assertEqual(sorted(e["caso"] for e in r["eventos"]), ["40808515", "40811111"])
        self.assertEqual({e["tipo"] for e in r["eventos"]}, {"nueva"})

    def test_reinicio_con_estado_guardado_no_re_avisa(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "estado.json"
            svc = self.nuevo(ruta=ruta)
            svc.ingest_csv("casos", casos_csv())
            svc.ingest_csv("reclamos", reclamos_csv())
            # Sin espera de arranque (gracia 0): los reclamos llegan después del
            # resumen y traen 40844444 (nueva) y el peligro de 40808515 (urgente).
            self.assertEqual(self.desp.tipos(), ["resumen_inicio", "nueva", "actualizada"])
            svc.detener()
            # Otro proceso (reinicio del PC) con el mismo estado.
            svc2 = self.nuevo(ruta=ruta, gracia=None, t=T0 + timedelta(minutes=30))
            self.assertTrue(svc2.estado.iniciado)
            self.assertEqual(svc2.ingest_csv("casos", casos_csv())["eventos"], [])
            self.assertEqual(svc2.ingest_csv("reclamos", reclamos_csv())["eventos"], [])
            self.assertEqual(self.desp.mensajes, [])


class TestSalud(_Base):
    def tipos_salud(self):
        return [t for t in self.desp.tipos() if t in ("sin_datos", "datos_recuperados", "sesion_caducada")]

    def test_sin_datos_y_datos_recuperados_una_sola_vez(self):
        svc = self.nuevo()
        svc.ingest_csv("casos", casos_csv())
        for minuto in (10, 20, 30):
            self.reloj.ir(minuto)
            svc.recalcular()
        self.assertEqual(self.tipos_salud(), [], "a los 30 min justos todavía no")
        for minuto in (31, 40, 50, 59):
            self.reloj.ir(minuto)
            svc.recalcular()
        self.assertEqual(self.tipos_salud(), ["sin_datos"])
        m = [m for m in self.desp.mensajes if m.tipo == "sin_datos"][0]
        self.assertIn("16:30", m.texto)                    # último reporte
        self.assertIn("sin_datos", svc.salud()["alertas"])
        self.reloj.ir(60)
        r = svc.ingest_csv("reclamos", reclamos_csv())
        self.assertEqual(r["eventos"][0], {"tipo": "datos_recuperados", "caso": ""})
        self.reloj.ir(65)
        svc.ingest_csv("casos", casos_csv())
        self.assertEqual(self.tipos_salud(), ["sin_datos", "datos_recuperados"])
        self.assertNotIn("sin_datos", svc.salud()["alertas"])

    def test_sin_ninguna_ingesta_no_alerta(self):
        svc = self.nuevo()
        for minuto in range(10, 130, 10):
            self.reloj.ir(minuto)
            svc.recalcular()
        self.assertEqual(self.desp.mensajes, [])

    def test_error_contrato_no_cuenta_como_dato(self):
        svc = self.nuevo()
        svc.ingest_csv("casos", casos_csv())
        for minuto in (10, 20, 31):
            self.reloj.ir(minuto)
            with self.assertRaises(ErrorContrato):
                svc.ingest_csv("casos", b"no;sirve\n1;2\n")
            svc.recalcular()
        self.assertEqual(self.tipos_salud(), ["sin_datos"])

    def test_tras_reinicio_el_plazo_corre_desde_el_arranque(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "estado.json"
            svc = self.nuevo(ruta=ruta)
            svc.ingest_csv("casos", casos_csv())
            svc.detener()
            # El PC estuvo apagado toda la noche: el último dato es de hace 14 h.
            arranque = T0 + timedelta(hours=14)
            svc2 = self.nuevo(ruta=ruta, t=arranque)
            for minuto in (2, 12, 22, 30):
                self.reloj.t = arranque + timedelta(minutes=minuto)
                svc2.recalcular()
            self.assertEqual(self.tipos_salud(), [], "recién partió: se le da plazo a la extensión")
            self.reloj.t = arranque + timedelta(minutes=31)
            svc2.recalcular()
            self.assertEqual(self.tipos_salud(), ["sin_datos"])
            self.assertIn("hace 14 h", self.desp.mensajes[-1].texto)

    def test_suspension_del_pc_reinicia_el_plazo(self):
        svc = self.nuevo()
        svc.ingest_csv("casos", casos_csv())
        self.reloj.ir(10)
        svc.recalcular()
        self.reloj.ir(130)                  # 2 h sin que corra el timer: PC suspendido
        svc.recalcular()
        for minuto in (140, 150, 160):
            self.reloj.ir(minuto)
            svc.recalcular()
        self.assertEqual(self.tipos_salud(), [])
        self.reloj.ir(161)
        svc.recalcular()
        self.assertEqual(self.tipos_salud(), ["sin_datos"])

    def test_sesion_caducada_una_vez_hasta_latido_sin_error(self):
        svc = self.nuevo()
        err = "casos: SESION_CADUCADA: la respuesta es la página de login"
        r = svc.latido({"nivel": "error", "ultimoError": err, "proximo_en_min": 5})
        self.assertEqual(r["eventos"], [{"tipo": "sesion_caducada", "caso": ""}])
        # Mismo error en el fin de ciclo siguiente, en el latido de inicio y con
        # otro error agregado: no se repite.
        self.assertEqual(svc.latido({"nivel": "error", "ultimoError": err})["eventos"], [])
        self.assertEqual(svc.latido({"nivel": "info", "ultimoError": err, "disparo": "alarma"})["eventos"], [])
        self.assertEqual(svc.latido({"nivel": "error", "ultimoError": "abiertas: REPORTE_PESADO: 504"})["eventos"],
                         [], "otro error no prueba que la sesión volvió")
        self.assertIn("sesion_caducada", svc.salud()["alertas"])
        # Latido sin error: se rearma.
        self.assertEqual(svc.latido({"nivel": "ok", "ultimoError": None, "proximo_en_min": 5})["eventos"], [])
        self.assertNotIn("sesion_caducada", svc.salud()["alertas"])
        self.assertEqual(svc.latido({"nivel": "error", "ultimoError": err})["eventos"],
                         [{"tipo": "sesion_caducada", "caso": ""}])
        self.assertEqual(self.tipos_salud(), ["sesion_caducada", "sesion_caducada"])
        m = self.desp.mensajes[0]
        self.assertIn("Sesión de Centrality caducada", m.texto)
        self.assertNotIn("login", m.texto, "no se reenvía el texto crudo del error")

    def test_flags_de_salud_se_persisten(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "estado.json"
            svc = self.nuevo(ruta=ruta)
            svc.latido({"nivel": "error", "ultimoError": "SESION_CADUCADA: login"})
            svc2 = self.nuevo(ruta=ruta)
            svc2.latido({"nivel": "error", "ultimoError": "SESION_CADUCADA: login"})
            self.assertEqual(self.desp.mensajes, [], "tras reiniciar no se repite el aviso")

    def test_salud_estructura(self):
        svc = self.nuevo(maestros=Maestros.cargar(
            {"alimentadores": str(FIX / "maestro_alimentadores.csv"), "zonal": "",
             "calles": "no_existe.csv"}, FIX))
        svc.ingest_csv("casos", casos_csv())
        svc.ingest_grilla(grilla())
        svc.latido({"nivel": "ok", "ciclo": 7, "ultimoError": None})
        self.reloj.ir(3)
        sal = svc.salud()
        for k in ("version", "ahora", "fuentes", "fallas_activas", "casos_seguidos", "extension",
                  "notificadores", "maestros", "config_advertencias"):
            self.assertIn(k, sal)
        self.assertEqual(sal["version"], "1.0.0")
        self.assertEqual(sal["ahora"], "2026-10-21T16:33:00")
        self.assertEqual(list(sal["fuentes"]), ["casos", "grilla"])
        casos = sal["fuentes"]["casos"]
        self.assertEqual((casos["edad_min"], casos["fresca"], casos["ok"], casos["registros"]),
                         (3.0, True, True, 6))
        self.assertEqual(sal["extension"]["ciclo"], 7)
        self.assertEqual(sal["extension"]["edad_min"], 3.0)
        self.assertEqual(sal["notificadores"],          # resumen_inicio + nueva 40877777 (grilla)
                         {"falso": {"enviados": 2, "fallidos": 0, "ultimo_error": None}})
        self.assertTrue(sal["maestros"]["alimentadores"]["ok"])
        self.assertFalse(sal["maestros"]["calles"]["ok"])
        self.assertEqual(sal["fallas_activas"], 3)          # 40808515, 40811111, 40877777
        self.assertEqual(sal["casos_seguidos"], len(svc.estado.casos))
        self.assertIsInstance(sal["config_advertencias"], list)
        texto = json.dumps(sal, ensure_ascii=False)
        self.assertSinDatosPersonales(texto, "/estado")

    def test_config_advertencias_de_cargar_config(self):
        cfg = cfg_prueba()
        cfg["_advertencias"] = ["algo que revisar"]
        svc = self.nuevo(cfg)
        self.assertEqual(svc.salud()["config_advertencias"], ["algo que revisar"])


class TestConsultas(_Base):
    def preparar(self) -> ServicioFallas:
        svc = self.nuevo()
        svc.ingest_csv("casos", casos_csv())
        svc.ingest_csv("reclamos", reclamos_csv())
        return svc

    def test_fallas_activas(self):
        svc = self.preparar()
        casos = [d["caso"] for d in svc.fallas_activas()]
        self.assertEqual(casos, ["40808515", "40811111", "40844444"])   # clientes desc, filtradas
        d = svc.fallas_activas()[0]
        self.assertEqual(d["clientes"], 1240)
        self.assertEqual(d["clientes_fuente"], "informe_casos")
        self.assertEqual(sorted(d["alimentadores_cod"]), ["A260", "A261"])
        self.assertEqual(Falla.from_dict(d).clientes, 1240)

    def test_falla_por_caso_o_tarea(self):
        svc = self.preparar()
        d = svc.falla("40808515")
        self.assertEqual(d["caso"], "40808515")
        self.assertTrue(d["seguimiento"]["notificada"])
        self.assertEqual(svc.falla(" 40808515-SD19 ")["caso"], "40808515")
        self.assertEqual(svc.falla("40833333")["programada"], True)     # se puede consultar igual
        self.assertIsNone(svc.falla("99999999"))

    def test_falla_normalizada_reciente(self):
        svc = self.preparar()
        self.reloj.ir(5)
        svc.ingest_csv("casos", casos_csv(cerrar={"40811111": "2026-10-21T16:33:00"}))
        d = svc.falla("40811111")
        self.assertFalse(d["abierta"])
        self.assertTrue(d["seguimiento"]["normalizada"])
        self.assertIn("Cerrada", svc.texto_caso("40811111"))

    def test_texto_caso_de_normalizada_por_ausencia(self):
        svc = self.preparar()
        sin = ("40808515",)
        for minuto in (5, 10, 21):
            self.reloj.ir(minuto)
            # Primero sale de reclamos: la última foto que se guarda es la del
            # informe de casos (1.240). En el orden inverso quedaría el mínimo
            # por NIS (≥ 5), que es lo último que se vio.
            svc.ingest_csv("reclamos", reclamos_csv(sin=sin))
            svc.ingest_csv("casos", casos_csv(sin=sin))
        self.assertTrue(svc.estado.casos["40808515"].normalizada)
        html = svc.texto_caso("40808515")
        self.assertIn("DETALLE", html)
        self.assertIn("Cerrada", html)
        self.assertIn("1.240", html)

    def test_textos_telegram(self):
        svc = self.preparar()
        self.assertIn("<code>40808515</code>", svc.texto_resumen())
        html = svc.texto_caso("40808515")
        self.assertIn("DETALLE", html)
        self.assertIn("AV. MATTA", html)
        self.assertIn("No encuentro el caso <code>12345678</code>", svc.texto_caso("12345678"))
        self.assertIn("&lt;b&gt;", svc.texto_caso("<b>"))
        est = svc.texto_estado()
        self.assertIn("Fuentes", est)
        self.assertIn("• casos: ✅", est)
        self.assertIn("Fallas activas: <b>3</b>", est)
        self.assertLess(len(est), 4096)
        svc2 = self.nuevo()
        self.assertIn("Sin fallas activas", svc2.texto_resumen())
        self.assertIn("aún no llega ningún reporte", svc2.texto_estado())

    def test_texto_estado_escapa_errores(self):
        svc = self.nuevo()
        with self.assertRaises(ErrorContrato):
            svc.ingest_csv("casos", b"<b>x</b>;y\n1;2\n")
        svc.latido({"nivel": "error", "ultimoError": "grilla: <script>"})
        est = svc.texto_estado()
        self.assertNotIn("<script>", est)
        self.assertNotIn("<b>x</b>", est)
        self.assertIn("❌ error", est)

    def test_offset_de_telegram_se_persiste(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "estado.json"
            svc = self.nuevo(ruta=ruta)
            self.assertEqual(svc.telegram_offset(), 0)
            svc.guardar_telegram_offset(812345)
            self.assertEqual(self.nuevo(ruta=ruta).telegram_offset(), 812345)


class TestRobustez(_Base):
    def test_falla_al_guardar_no_detiene_los_avisos(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self.nuevo(ruta=Path(tmp) / "estado.json")
            with mock.patch("bot_fallas.servicio.estado_mod.guardar",
                            side_effect=PermissionError(13, "Permiso denegado")):
                r = svc.ingest_csv("casos", casos_csv())
                self.assertEqual(r["eventos"], [{"tipo": "resumen_inicio", "caso": ""}])
                self.assertIn("Permiso denegado", svc.salud()["error_estado"])
                self.assertIn("No se pudo guardar el estado", svc.texto_estado())
            svc.recalcular()
            self.assertIsNone(svc.salud()["error_estado"])

    def test_error_al_redactar_manda_un_aviso_generico(self):
        svc = self.nuevo()
        svc.estado.iniciado = True
        with mock.patch("bot_fallas.servicio.mensaje_evento", side_effect=RuntimeError("bug")):
            svc.ingest_csv("casos", casos_csv())
        self.assertEqual([m.titulo for m in self.desp.mensajes],
                         ["Aviso nueva · Caso 40808515", "Aviso nueva · Caso 40811111"])

    def test_despachador_roto_no_rompe_la_ingesta(self):
        svc = self.nuevo()
        svc.despachador = mock.Mock(encolar=mock.Mock(side_effect=RuntimeError("cola rota")),
                                    estado=mock.Mock(return_value={}))
        r = svc.ingest_csv("casos", casos_csv())
        self.assertTrue(r["ok"])
        self.assertTrue(svc.estado.iniciado)

    def test_ingestas_concurrentes(self):
        svc = self.nuevo()
        errores = []

        def trabajar(fuente, datos):
            try:
                for _ in range(10):
                    svc.ingest_csv(fuente, datos)
                    svc.recalcular()
                    svc.salud()
                    svc.fallas_activas()
            except Exception as e:      # noqa: BLE001
                errores.append(e)

        hilos = [threading.Thread(target=trabajar, args=a) for a in
                 [("casos", casos_csv()), ("reclamos", reclamos_csv()),
                  ("abiertas", (FIX / "abiertas.csv").read_bytes()), ("casos", casos_csv())]]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(30)
        self.assertEqual(errores, [])
        tipos = self.desp.tipos()
        self.assertEqual(tipos.count("resumen_inicio"), 1)
        nuevas = [m.caso for m in self.desp.mensajes if m.tipo == "nueva"]
        self.assertEqual(len(nuevas), len(set(nuevas)), "ninguna falla avisada dos veces como nueva")

    def test_timer_de_recalculo(self):
        svc = self.nuevo()
        llamado = threading.Event()
        original = svc.recalcular

        def espia():
            llamado.set()
            return original()

        svc.recalcular = espia
        svc.iniciar_recalculo(intervalo_s=0.02)
        try:
            self.assertTrue(llamado.wait(5))
        finally:
            svc.detener(timeout=2)
        self.assertFalse(svc._hilo.is_alive())

    def test_timer_sobrevive_a_un_error(self):
        svc = self.nuevo()
        llamadas = []

        def falla():
            llamadas.append(1)
            raise RuntimeError("bug puntual")

        svc.recalcular = falla
        svc.iniciar_recalculo(intervalo_s=0.01)
        try:
            for _ in range(500):
                if len(llamadas) >= 3:
                    break
                threading.Event().wait(0.01)
        finally:
            svc.detener(timeout=2)
        self.assertGreaterEqual(len(llamadas), 3)

    def test_con_maestros_de_fixture(self):
        maestros = Maestros.cargar({"alimentadores": "maestro_alimentadores.csv",
                                    "zonal": "maestro_zonal.json", "calles": "maestro_calles.csv"}, FIX)
        svc = self.nuevo(maestros=maestros)
        svc.ingest_csv("casos", casos_csv())
        svc.ingest_csv("reclamos", reclamos_csv())
        # El peligro que traen los reclamos es urgente: actualizada al tiro.
        act = [m for m in self.desp.mensajes if m.caso == "40808515"][0]
        self.assertEqual(act.tipo, "actualizada")
        self.assertIn("A260 Ficticio Centro (SE Osorno)", act.texto)
        self.assertIn("según maestro de red (SED)", act.texto)
        self.assertIn("• AV. MATTA — Osorno (50 clientes)", act.texto)
        self.assertIn("A260 Ficticio Centro (SE Osorno)", svc.texto_caso("40808515"))
        d = svc.falla("40808515")
        self.assertEqual(d["ccd"], "SUR")
        self.assertEqual(d["calles_fuente"], "maestro_sed")
        for m in self.desp.mensajes:
            self.assertSinDatosPersonales(m.html, m.titulo)


if __name__ == "__main__":
    unittest.main()
