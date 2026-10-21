"""Tests de agregador.py: una Falla por caso, con la semántica de conteo del SPEC §4.4."""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime
from pathlib import Path

from bot_fallas.agregador import construir_fallas, nivel_de
from bot_fallas.contrato import parse_csv, parse_grilla
from bot_fallas.maestros import InfoAlimentador, Maestros
from bot_fallas.modelo import Calle, Registro, Snapshot

FIXTURES = Path(__file__).resolve().parent / "fixtures"
T = datetime(2026, 10, 21, 15, 30)
CFG = {"niveles": {"masivo": 1000, "alta": 300, "media": 50},
       "maestros": {"ratio_alimentador_completo": 0.8}}
CASO = "40808515"


def R(fuente: str, caso: str = CASO, **kw) -> Registro:
    return Registro(fuente=fuente, caso=caso, **kw)


def snaps(*regs: Registro) -> dict[str, Snapshot]:
    out: dict[str, Snapshot] = {}
    for r in regs:
        out.setdefault(r.fuente, Snapshot(fuente=r.fuente, recibido=T,
                                          completa=r.fuente != "grilla")).registros.append(r)
    return out


def falla(*regs: Registro, maestros: Maestros | None = None, cfg: dict | None = None,
          caso: str = CASO):
    return construir_fallas(snaps(*regs), maestros or Maestros(), cfg or CFG)[caso]


def calles(f):
    return [(c.nombre, c.comuna, c.reclamos, c.clientes) for c in f.calles]


class TestClientes(unittest.TestCase):
    def test_suma_por_alimentador_con_dedup_por_maximo(self):
        f = falla(R("casos", alimentador="A260", clientes=1200),
                  R("casos", alimentador="A261", clientes=40),
                  R("casos", alimentador="A260", clientes=1150))
        self.assertEqual((f.clientes, f.clientes_fuente), (1240, "informe_casos"))
        self.assertEqual(f.nivel, "masivo")

    def test_alimentador_se_normaliza_antes_de_deduplicar(self):
        f = falla(R("casos", alimentador="A260", clientes=100),
                  R("casos", alimentador="260", clientes=90))
        self.assertEqual(f.clientes, 100)
        self.assertEqual(f.alimentadores_cod, ["A260"])

    def test_sin_alimentador_es_un_alimentador_vacio(self):
        f = falla(R("casos", clientes=10), R("casos", clientes=30),
                  R("casos", alimentador="A260", clientes=100))
        self.assertEqual(f.clientes, 130)

    def test_varias_filas_un_solo_caso(self):
        fallas = construir_fallas(snaps(R("casos", alimentador="A260", clientes=5),
                                        R("casos", alimentador="A261", clientes=5),
                                        R("casos", caso="40811111", clientes=7)),
                                  Maestros(), CFG)
        self.assertEqual(sorted(fallas), [CASO, "40811111"])
        self.assertEqual(fallas[CASO].caso, CASO)

    def test_prioridad_de_fuentes(self):
        # (1) informe de casos manda aunque la grilla diga otra cosa.
        f = falla(R("casos", alimentador="A260", clientes=80),
                  R("grilla", clientes=500, nis="1", direccion="BULNES 1"))
        self.assertEqual((f.clientes, f.clientes_fuente), (80, "informe_casos"))
        # (2) casos sin conteo -> máximo de la grilla.
        f = falla(R("casos", alimentador="A260"), R("grilla", clientes=120, id="g1"),
                  R("grilla", clientes=95, id="g2"), R("reclamos", nis="1"))
        self.assertEqual((f.clientes, f.clientes_fuente), (120, "grilla"))
        # (3) sólo reclamos -> NIS distintos (un mínimo).
        f = falla(R("reclamos", nis="1", id="a"), R("reclamos", nis="2", id="b"),
                  R("abiertas", nis="2", id="40808515-D1"))
        self.assertEqual((f.clientes, f.clientes_fuente), (2, "nis_reclamos"))
        self.assertEqual(f.nivel, "baja")
        # (4) nada -> None.
        f = falla(R("casos", alimentador="A260"))
        self.assertEqual((f.clientes, f.clientes_fuente, f.nivel), (None, "", ""))

    def test_cero_clientes_del_informe_es_dato(self):
        f = falla(R("casos", alimentador="A260", clientes=0), R("reclamos", nis="1"))
        self.assertEqual((f.clientes, f.clientes_fuente), (0, "informe_casos"))


class TestReclamando(unittest.TestCase):
    def test_nis_distintos_y_sin_nis_por_id(self):
        f = falla(R("reclamos", id="r1", nis="1", direccion="BULNES 1, OSORNO"),
                  R("reclamos", id="r2", nis="1", direccion="BULNES 1, OSORNO"),
                  R("reclamos", id="r3", nis="2"),
                  R("abiertas", id="40808515-SD1", nis="2"),
                  R("grilla", id="g1", nis="3"),
                  R("reclamos", id="r4", direccion="LOS CARRERA 5, OSORNO"),   # sin NIS: 1 por id
                  R("grilla", id="r4", direccion="LOS CARRERA 5, OSORNO"),     # mismo id
                  R("reclamos", direccion="O'HIGGINS 9, OSORNO"),              # sin NIS ni id
                  R("reclamos", id="r6"),                                      # ni NIS ni dirección
                  R("casos", alimentador="A260", clientes=10))
        self.assertEqual(f.clientes_reclamando, 5)      # NIS 1,2,3 + id r4 + fila sin id
        # Registros distintos por (fuente, id); sin id cada fila cuenta.
        self.assertEqual(f.reclamos, 9)

    def test_reclamos_mismo_id_misma_fuente_cuenta_una_vez(self):
        f = falla(R("reclamos", id="r1", nis="1"), R("reclamos", id="r1", nis="1"))
        self.assertEqual((f.reclamos, f.clientes_reclamando), (1, 1))


class TestCalles(unittest.TestCase):
    def test_dedup_por_cliente_y_sin_numeros(self):
        f = falla(R("reclamos", id="1", nis="1", direccion="AV. MATTA 1234, OSORNO"),
                  R("reclamos", id="2", nis="1", direccion="AV. MATTA 1234, OSORNO"),
                  R("abiertas", id="40808515-SD1", nis="1", direccion="AV. MATTA 1234, OSORNO"),
                  R("reclamos", id="3", nis="2", direccion="Avenida Matta 1250 Depto 3, OSORNO"),
                  R("reclamos", id="4", nis="3", direccion="LOS CARRERA S/N / RAHUE-OSORNO / CL"),
                  R("reclamos", id="5", nis="4", direccion="Lat: -40.5, Lon: -73.1", area="OSORNO"),
                  R("reclamos", id="6", nis="5", direccion="BULNES 77", area="OSORNO"))
        self.assertEqual(calles(f), [("AV. MATTA", "Osorno", 2, None),
                                     ("BULNES", "Osorno", 1, None),
                                     ("LOS CARRERA", "Osorno", 1, None)])
        self.assertEqual(f.sin_calle, 1)
        self.assertEqual(f.calles_fuente, "reclamos")
        for c in f.calles:
            self.assertNotRegex(c.nombre, r"\d")
            self.assertEqual(c.fuente, "reclamos")

    def test_cliente_con_dos_direcciones_usa_la_primera_con_calle(self):
        f = falla(R("reclamos", id="1", nis="1", direccion="Lat: -40.5, Lon: -73.1"),
                  R("abiertas", id="40808515-D1", nis="1", direccion="BULNES 12, OSORNO"))
        self.assertEqual(calles(f), [("BULNES", "Osorno", 1, None)])
        self.assertEqual(f.sin_calle, 0)

    def test_variante_mas_frecuente_y_orden(self):
        f = falla(R("reclamos", nis="1", direccion="O'HIGGINS 12, OSORNO"),
                  R("reclamos", nis="2", direccion="O'HIGGINS 40, OSORNO"),
                  R("reclamos", nis="3", direccion="OHIGGINS 5, OSORNO"),
                  R("reclamos", nis="4", direccion="ZENTENO 1, OSORNO"),
                  R("reclamos", nis="5", direccion="BULNES 1, OSORNO"),
                  R("reclamos", nis="6", direccion="LOS CARRERA 3, PURRANQUE"),
                  R("reclamos", nis="7", direccion="LOS CARRERA 3, OSORNO"),
                  R("reclamos", nis="8", direccion="LOS CARRERA 9, OSORNO"))
        self.assertEqual(calles(f), [("O'HIGGINS", "Osorno", 3, None),
                                     ("LOS CARRERA", "Osorno", 2, None),
                                     ("BULNES", "Osorno", 1, None),
                                     ("LOS CARRERA", "Purranque", 1, None),
                                     ("ZENTENO", "Osorno", 1, None)])

    def test_sin_direcciones(self):
        f = falla(R("casos", alimentador="A260", clientes=5))
        self.assertEqual((f.calles, f.calles_fuente, f.sin_calle), ([], "", 0))


def _maestro_calles() -> Maestros:
    return Maestros(
        alimentadores=[InfoAlimentador(codigo="A260", nombre="Ficticio Centro", subestacion="Osorno",
                                       comuna="Osorno", clientes_total=1300, ccd="SUR"),
                       InfoAlimentador(codigo="A261", nombre="Ficticio Rahue", subestacion="Osorno",
                                       comuna="Osorno", clientes_total=2000, ccd="SUR")],
        calles_sed={"12345": [Calle(nombre="AV. MATTA", comuna="Osorno", clientes=50),
                              Calle(nombre="LOS CARRERA", comuna="Osorno", clientes=25)],
                    "22222": [Calle(nombre="AV. MATTA", comuna="Osorno", clientes=10)]},
        calles_alim={"A260": [Calle(nombre="AV. MATTA", comuna="Osorno", clientes=400),
                              Calle(nombre="BULNES", comuna="Osorno", clientes=200),
                              Calle(nombre="ZENTENO", comuna="", clientes=100)]},
    )


class TestOverrideMaestro(unittest.TestCase):
    RECLAMOS = (R("reclamos", nis="1", direccion="AV. MATTA 1234, OSORNO"),
                R("reclamos", nis="2", direccion="AV MATTA 20, OSORNO"),
                R("reclamos", nis="3", direccion="ZENTENO 5, OSORNO"),
                R("reclamos", nis="4", direccion="CHACABUCO 9, OSORNO"))

    def test_por_sed(self):
        f = falla(R("casos", alimentador="A260", clientes=1200),
                  R("reclamos", nis="9", jerarquia="SE OSORNO > A260 > SED 12345"),
                  *self.RECLAMOS, maestros=_maestro_calles())
        self.assertEqual(f.calles_fuente, "maestro_sed")
        self.assertEqual(calles(f), [("AV. MATTA", "Osorno", 2, 50),
                                     ("LOS CARRERA", "Osorno", 0, 25)])
        self.assertTrue(all(c.fuente == "maestro_sed" for c in f.calles))

    def test_dos_sed_suman_clientes_de_la_misma_calle(self):
        f = falla(R("reclamos", nis="1", sed="12345"), R("reclamos", nis="2", sed="SED 22222"),
                  maestros=_maestro_calles())
        self.assertEqual(f.seds, ["12345", "22222"])
        self.assertEqual(calles(f)[:1], [("AV. MATTA", "Osorno", 0, 60)])

    def test_por_alimentador_completo(self):
        f = falla(R("casos", alimentador="A260", clientes=1200), *self.RECLAMOS,
                  maestros=_maestro_calles())
        self.assertEqual(f.calles_fuente, "maestro_alimentador")
        # ZENTENO no trae comuna en el maestro: calza con el reclamo de cualquier comuna.
        self.assertEqual(calles(f), [("AV. MATTA", "Osorno", 2, 400),
                                     ("ZENTENO", "", 1, 100),
                                     ("BULNES", "Osorno", 0, 200)])

    def test_alimentador_parcial_no_reemplaza(self):
        f = falla(R("casos", alimentador="A260", clientes=900), *self.RECLAMOS,
                  maestros=_maestro_calles())
        self.assertEqual(f.calles_fuente, "reclamos")
        self.assertIn(("CHACABUCO", "Osorno", 1, None), calles(f))

    def test_ratio_configurable(self):
        cfg = {**CFG, "maestros": {"ratio_alimentador_completo": 0.5}}
        f = falla(R("casos", alimentador="A260", clientes=900), *self.RECLAMOS,
                  maestros=_maestro_calles(), cfg=cfg)
        self.assertEqual(f.calles_fuente, "maestro_alimentador")

    def test_completo_mas_parcial_conserva_reclamos_sobrantes(self):
        f = falla(R("casos", alimentador="A260", clientes=1250),
                  R("casos", alimentador="A261", clientes=30), *self.RECLAMOS,
                  maestros=_maestro_calles())
        self.assertEqual(f.calles_fuente, "maestro_alimentador")
        c = calles(f)
        self.assertEqual(c[:3], [("AV. MATTA", "Osorno", 2, 400), ("ZENTENO", "", 1, 100),
                                 ("BULNES", "Osorno", 0, 200)])
        self.assertEqual(c[3:], [("CHACABUCO", "Osorno", 1, None)])
        self.assertEqual(f.calles[3].fuente, "reclamos")

    def test_sin_informe_de_casos_no_usa_alimentador(self):
        f = falla(R("grilla", alimentador="A260", clientes=1300, id="g"), *self.RECLAMOS,
                  maestros=_maestro_calles())
        self.assertEqual(f.calles_fuente, "reclamos")

    def test_maestro_no_se_ensucia(self):
        m = _maestro_calles()
        falla(R("reclamos", nis="1", sed="12345", direccion="AV. MATTA 1, OSORNO"), maestros=m)
        self.assertEqual(m.calles_de_sed("12345")[0].reclamos, 0)


class TestEstado(unittest.TestCase):
    def test_casos_abierta_si_alguna_fila_sin_cierre(self):
        f = falla(R("casos", alimentador="A260", cierre=datetime(2026, 10, 21, 16)),
                  R("casos", alimentador="A261", cierre=None))
        self.assertTrue(f.abierta)
        self.assertIsNone(f.cierre)

    def test_casos_cerrada_cierre_maximo(self):
        f = falla(R("casos", alimentador="A260", cierre=datetime(2026, 10, 21, 16), cerrado=True),
                  R("casos", alimentador="A261", cierre=datetime(2026, 10, 21, 17), cerrado=True),
                  R("reclamos", nis="1", cerrado=False))
        # El informe de casos manda: el reclamo abierto no reabre el caso.
        self.assertFalse(f.abierta)
        self.assertEqual(f.cierre, datetime(2026, 10, 21, 17))

    def test_sin_casos_manda_el_resto(self):
        f = falla(R("abiertas", id="t1", cerrado=True), R("reclamos", id="r", cerrado=False))
        self.assertTrue(f.abierta)
        f = falla(R("abiertas", id="t1", cerrado=True),
                  R("reclamos", id="r", cerrado=True, cierre=datetime(2026, 10, 21, 12)))
        self.assertFalse(f.abierta)
        self.assertEqual(f.cierre, datetime(2026, 10, 21, 12))

    def test_inicio_minimo_de_todas_las_fuentes(self):
        f = falla(R("casos", inicio=datetime(2026, 10, 21, 14, 32)),
                  R("reclamos", nis="1", inicio=datetime(2026, 10, 21, 14, 20)),
                  R("grilla", id="g", inicio=None))
        self.assertEqual(f.inicio, datetime(2026, 10, 21, 14, 20))

    def test_programada(self):
        self.assertTrue(falla(R("casos", programada=True), R("casos", alimentador="A1")).programada)
        self.assertFalse(falla(R("reclamos", nis="1", programada=True)).programada)


class TestAtributos(unittest.TestCase):
    def test_comunas_por_frecuencia_title_case(self):
        f = falla(R("reclamos", nis="1", direccion="BULNES 1, PUERTO OCTAY"),
                  R("reclamos", nis="2", direccion="BULNES 2, OSORNO"),
                  R("reclamos", nis="3", direccion="BULNES 3, osorno"),
                  R("reclamos", nis="4", direccion="Lat: -40.5, Lon: -73.1", area="PTO. OCTAY"),
                  R("grilla", id="g", comuna="OSORNO"),
                  R("casos", alimentador="A300"),
                  maestros=Maestros(alimentadores=[InfoAlimentador(codigo="A300",
                                                                   comuna="Purranque")]))
        self.assertEqual(f.comunas, ["Osorno", "Puerto Octay", "Purranque"])

    def test_alimentadores_para_mostrar(self):
        m = _maestro_calles()
        f = falla(R("casos", alimentador="A260"), R("casos", alimentador="0412"), maestros=m)
        self.assertEqual(f.alimentadores_cod, ["A260", "A412"])
        self.assertEqual(f.alimentadores, ["A260 Ficticio Centro (SE Osorno)", "A412"])
        f = falla(R("casos", alimentador="A260"))
        self.assertEqual(f.alimentadores, ["A260"])

    def test_seds_desde_campo_y_jerarquia(self):
        f = falla(R("reclamos", nis="1", sed="12345", jerarquia="SE OSORNO > A260 > SED: 67890"),
                  R("reclamos", nis="2", jerarquia="SED N°12345 / A260"),
                  R("reclamos", nis="3", jerarquia="SE OSORNO > SED 12"))   # <3 dígitos: no
        self.assertEqual(f.seds, ["12345", "67890"])
        self.assertEqual(f.jerarquia, "SE OSORNO > A260 > SED: 67890")

    def test_jerarquia_recortada(self):
        f = falla(R("reclamos", nis="1", jerarquia="X" * 200))
        self.assertEqual(len(f.jerarquia), 80)

    def test_mas_frecuente_y_peligro(self):
        f = falla(R("reclamos", nis="1", motivo="Sin Suministro", empresa="SAESA", peligro=False),
                  R("reclamos", nis="2", motivo="SIN SUMINISTRO", empresa="SAESA", peligro=None),
                  R("reclamos", nis="3", motivo="Líneas Cortadas", empresa="FRONTEL", peligro=True),
                  R("casos", causa="Viento", componente="Aislador",
                    origen="Operacion de  protecciones "),
                  R("casos", alimentador="A2", causa="", componente="Aislador"))
        self.assertEqual(f.motivo, "Sin Suministro")
        self.assertEqual(f.empresa, "SAESA")
        self.assertEqual((f.causa, f.componente), ("Viento", "Aislador"))
        self.assertEqual(f.origen, "Operacion de protecciones")
        self.assertTrue(f.peligro)
        self.assertFalse(falla(R("reclamos", nis="1", peligro=None)).peligro)

    def test_fuentes_en_orden(self):
        f = falla(R("grilla", id="g"), R("abiertas", id="t"), R("casos"))
        self.assertEqual(f.fuentes, ["casos", "abiertas", "grilla"])


class TestCcdZonales(unittest.TestCase):
    ZONAL = {"maestro": [
        {"COMUNA": "OSORNO", "ZONAL": "OSORNO", "CCD": "SUR"},
        {"COMUNA": "CORONEL", "ZONAL": "CONCEPCION COSTA", "CCD": "NORTE"},
        {"COMUNA": "TEMUCO", "ZONAL": "CAUTIN COSTA", "CCD": "NORTE"},
        {"COMUNA": "TEMUCO", "ZONAL": "CAUTIN CORDILLERA", "CCD": "SUR"}]}

    def test_ccd_por_maestro_de_alimentador(self):
        m = Maestros(alimentadores=[InfoAlimentador(codigo="A101", ccd="NORTE", comuna="Coronel")],
                     zonal=self.ZONAL)
        f = falla(R("casos", alimentador="A101"), R("reclamos", nis="1", area="OSORNO"),
                  R("reclamos", nis="2", area="OSORNO"), maestros=m)
        self.assertEqual(f.ccd, "NORTE")           # el alimentador manda sobre la comuna
        self.assertEqual(f.zonales, ["OSORNO", "CONCEPCION COSTA"])

    def test_ccd_por_zonal_de_la_comuna(self):
        f = falla(R("reclamos", nis="1", direccion="BULNES 1, OSORNO"),
                  maestros=Maestros(zonal=self.ZONAL))
        self.assertEqual((f.ccd, f.zonales), ("SUR", ["OSORNO"]))

    def test_comuna_ambigua_devuelve_ambos_zonales_y_no_adivina_ccd(self):
        f = falla(R("reclamos", nis="1", area="TEMUCO"), maestros=Maestros(zonal=self.ZONAL))
        self.assertEqual(f.zonales, ["CAUTIN COSTA", "CAUTIN CORDILLERA"])
        self.assertEqual(f.ccd, "")

    def test_sin_maestro(self):
        f = falla(R("casos", alimentador="A260"), R("reclamos", nis="1", area="OSORNO"))
        self.assertEqual((f.ccd, f.zonales), ("", []))


class TestNivel(unittest.TestCase):
    def test_cortes(self):
        c = CFG["niveles"]
        casos = [(None, ""), (0, "baja"), (49, "baja"), (50, "media"), (299, "media"),
                 (300, "alta"), (999, "alta"), (1000, "masivo"), (25000, "masivo")]
        for n, esperado in casos:
            with self.subTest(n=n):
                self.assertEqual(nivel_de(n, c), esperado)

    def test_cortes_configurables_y_por_defecto(self):
        self.assertEqual(nivel_de(150, {"masivo": 500, "alta": 100, "media": 10}), "alta")
        self.assertEqual(nivel_de(1000, {}), "masivo")
        self.assertEqual(nivel_de(60, None), "media")


class TestIntegracionFixtures(unittest.TestCase):
    """Las cuatro fuentes reales (sintéticas) juntas, con y sin maestros."""

    @classmethod
    def setUpClass(cls):
        cls.snaps = {f: parse_csv((FIXTURES / f"{f}.csv").read_bytes(), f, T)
                     for f in ("casos", "reclamos", "abiertas")}
        cls.snaps["grilla"] = parse_grilla(
            json.loads((FIXTURES / "grilla.json").read_text(encoding="utf-8")), T)
        cls.maestros = Maestros.cargar(
            {"alimentadores": "maestro_alimentadores.csv", "zonal": "maestro_zonal.json",
             "calles": "maestro_calles.csv"}, FIXTURES)
        cls.sin = construir_fallas(cls.snaps, Maestros(), CFG)
        cls.con = construir_fallas(cls.snaps, cls.maestros, CFG)

    def test_casos_y_suma(self):
        self.assertEqual(sorted(self.sin), ["40808515", "40811111", "40822222", "40833333",
                                            "40844444", "40855555", "40866666", "40877777"])
        f = self.sin["40808515"]
        self.assertEqual((f.clientes, f.clientes_fuente, f.nivel), (1240, "informe_casos", "masivo"))
        self.assertEqual(f.clientes_reclamando, 6)
        self.assertEqual(f.reclamos, 10)
        self.assertTrue(f.abierta)
        self.assertTrue(f.peligro)
        self.assertEqual(f.inicio, datetime(2026, 10, 21, 14, 32))
        self.assertEqual(f.fuentes, ["casos", "reclamos", "abiertas", "grilla"])
        self.assertEqual(f.sin_calle, 1)
        self.assertEqual([c.nombre for c in f.calles][:1], ["AV. MATTA"])

    def test_estados_de_los_fixtures(self):
        self.assertFalse(self.sin["40822222"].abierta)
        self.assertEqual(self.sin["40822222"].cierre, datetime(2026, 10, 21, 10, 15))
        self.assertTrue(self.sin["40811111"].abierta)            # -infinity no cierra
        self.assertTrue(self.sin["40833333"].programada)
        self.assertFalse(self.sin["40855555"].abierta)           # sólo tarea Finalizada [X]
        self.assertEqual(self.sin["40877777"].clientes_fuente, "grilla")
        self.assertEqual(self.sin["40844444"].clientes_fuente, "nis_reclamos")

    def test_con_maestros(self):
        f = self.con["40808515"]
        self.assertEqual(f.alimentadores, ["A260 Ficticio Centro (SE Osorno)",
                                           "A261 Ficticio Rahue (SE Osorno)"])
        self.assertEqual((f.ccd, f.zonales), ("SUR", ["OSORNO"]))
        self.assertEqual(f.calles_fuente, "maestro_sed")
        self.assertEqual(calles(f), [("AV. MATTA", "Osorno", 2, 50), ("LOS CARRERA", "Osorno", 1, 25)])
        self.assertEqual(self.con["40822222"].ccd, "NORTE")

    def test_nada_personal_sale_en_la_falla(self):
        prohibidos = ["JUAN PRUEBA", "MARIA PRUEBA", "PEDRO PRUEBA", "PRUEBA,", "+56 9",
                      "1111 1111", "OPERADORA", "ANALISTA", "Depto", "DEPTO", "KM 5", "-40.57",
                      "-73.13", "chispas", "poste caido"]
        # Números de casa del fixture, como palabra (la SED 12345 sí puede salir).
        casas = ["1234", "1250", "1300", "1500", "980", "77", "45", "450", "470", "123", "300"]
        nis = {r.nis for s in self.snaps.values() for r in s.registros if r.nis}
        for fallas in (self.sin, self.con):
            texto = json.dumps([f.to_dict() for f in fallas.values()], ensure_ascii=False)
            for p in prohibidos:
                self.assertNotIn(p, texto, p)
            calles_txt = " | ".join(c.nombre for f in fallas.values() for c in f.calles)
            for n in casas:
                self.assertNotRegex(calles_txt, rf"\b{n}\b", n)
            for n in nis:
                self.assertNotRegex(texto, rf"\b{re.escape(n)}\b", n)
            for f in fallas.values():
                for c in f.calles:
                    self.assertNotRegex(c.nombre, r"\d{3,}", c.nombre)


if __name__ == "__main__":
    unittest.main()
