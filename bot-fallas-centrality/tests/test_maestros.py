"""Tests de maestros.py: carga tolerante, normalización de claves y formatos."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bot_fallas.maestros import (InfoAlimentador, Maestros, clave_comuna, normalizar_alimentador,
                                 normalizar_ccd, normalizar_sed)
from bot_fallas.modelo import Calle

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUTAS = {"alimentadores": "maestro_alimentadores.csv", "zonal": "maestro_zonal.json",
         "calles": "maestro_calles.csv", "ratio_alimentador_completo": 0.8}


class TestNormalizadores(unittest.TestCase):
    def test_ccd(self):
        for v in ("ccd_sur", "CCD Sur", "SUR", "sur", " ccd-sur "):
            self.assertEqual(normalizar_ccd(v), "SUR", v)
        for v in ("ccd_norte", "CCD Norte", "NORTE"):
            self.assertEqual(normalizar_ccd(v), "NORTE", v)
        for v in ("", None, "centro", "ccd"):
            self.assertEqual(normalizar_ccd(v), "", v)

    def test_alimentador(self):
        for v in ("260", "A0260", " a260 ", "A260", 260, 260.0, "260.0", "0260"):
            self.assertEqual(normalizar_alimentador(v), "A260", repr(v))
        self.assertEqual(normalizar_alimentador("chi-01"), "CHI-01")   # no numérico: tal cual
        self.assertEqual(normalizar_alimentador(""), "")
        self.assertEqual(normalizar_alimentador(None), "")
        self.assertEqual(normalizar_alimentador("-"), "")

    def test_sed(self):
        for v in ("12345", "SED 12345", "sed-012345", "SED: 12345", 12345):
            self.assertEqual(normalizar_sed(v), "12345", repr(v))
        self.assertEqual(normalizar_sed(""), "")

    def test_clave_comuna(self):
        self.assertEqual(clave_comuna("Pto. Octay"), clave_comuna("PUERTO OCTAY"))
        self.assertEqual(clave_comuna("Coihaique"), clave_comuna("COYHAIQUE"))
        self.assertEqual(clave_comuna("Valdivia"), "VALDIVIA")


class TestCargaFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = Maestros.cargar(RUTAS, FIXTURES)

    def test_diagnostico_ok(self):
        d = self.m.diagnostico
        for k in ("alimentadores", "zonal", "calles"):
            self.assertTrue(d[k]["ok"], d[k])
            self.assertTrue(d[k]["configurado"])
            self.assertEqual(d[k]["error"], "")
        self.assertEqual(d["alimentadores"]["filas"], 4)
        self.assertEqual(d["calles"]["seds"], 2)
        self.assertEqual(d["calles"]["alimentadores"], 2)
        self.assertFalse(self.m.vacio)

    def test_alimentador_con_bom_y_claves(self):
        info = self.m.alimentador("A260")
        self.assertIsNotNone(info)
        self.assertEqual(info.codigo, "A260")
        self.assertEqual(info.nombre, "Ficticio Centro")
        self.assertEqual(info.subestacion, "Osorno")
        self.assertEqual(info.comuna, "Osorno")             # Title Case
        self.assertEqual(info.clientes_total, 1300)         # '1300.0' de pandas
        self.assertEqual(info.ccd, "SUR")
        self.assertEqual(info.empresa, "Saesa")
        for v in ("260", "a0260", " A260 "):
            self.assertIs(self.m.alimentador(v), info, v)
        self.assertEqual(self.m.alimentador("300").codigo, "A300")   # COD '0300'
        self.assertEqual(self.m.alimentador("A101").ccd, "NORTE")
        self.assertIsNone(self.m.alimentador("A999"))
        self.assertEqual(info.para_mostrar(), "A260 Ficticio Centro (SE Osorno)")

    def test_zonal_comuna_en_dos_zonales(self):
        self.assertEqual(self.m.zonales_de_comuna("Temuco"),
                         [("CAUTIN COSTA", "NORTE"), ("CAUTIN CORDILLERA", "SUR")])
        self.assertEqual(self.m.zonales_de_comuna("osorno"), [("OSORNO", "SUR")])
        self.assertEqual(self.m.zonales_de_comuna("Pto. Octay"), [("OSORNO", "SUR")])
        self.assertEqual(self.m.zonales_de_comuna("Valdivia"), [])
        self.assertEqual(self.m.zonales_de_comuna(""), [])

    def test_calles_sed_suma_repetidas(self):
        calles = self.m.calles_de_sed("12345")
        self.assertEqual([(c.nombre, c.comuna, c.clientes) for c in calles],
                         [("AV. MATTA", "Osorno", 50), ("LOS CARRERA", "Osorno", 25)])
        self.assertTrue(all(c.fuente == "maestro_sed" for c in calles))
        self.assertEqual([c.nombre for c in self.m.calles_de_sed("SED 22222")], ["BULNES"])
        self.assertEqual(self.m.calles_de_sed("99999"), [])

    def test_calles_alimentador_con_clave_normalizada(self):
        calles = self.m.calles_de_alimentador("260")
        self.assertEqual([(c.nombre, c.clientes) for c in calles],
                         [("AV. MATTA", 400), ("LOS CARRERA", 300), ("BULNES", 200)])
        self.assertTrue(all(c.fuente == "maestro_alimentador" for c in calles))
        self.assertEqual(self.m.calles_de_alimentador("A0261")[0].nombre, "PJE. LOS AROMOS")

    def test_tipo_desconocido_se_ignora(self):
        todas = [c.nombre for k in ("12345", "22222") for c in self.m.calles_de_sed(k)]
        self.assertNotIn("CALLE IGNORADA", todas)

    def test_devuelve_copias(self):
        self.m.calles_de_sed("12345")[0].reclamos = 99
        self.assertEqual(self.m.calles_de_sed("12345")[0].reclamos, 0)


class TestCargaTolerante(unittest.TestCase):
    def test_vacio_no_carga(self):
        m = Maestros.cargar({"alimentadores": "", "zonal": "", "calles": ""}, FIXTURES)
        self.assertTrue(m.vacio)
        for k in ("alimentadores", "zonal", "calles"):
            self.assertFalse(m.diagnostico[k]["configurado"])
            self.assertTrue(m.diagnostico[k]["ok"])
        self.assertTrue(Maestros.cargar(None, FIXTURES).vacio)

    def test_inexistente_no_revienta(self):
        with self.assertLogs("bot_fallas.maestros", level="WARNING"):
            m = Maestros.cargar({"alimentadores": "no_existe.csv", "zonal": "maestro_zonal.json"},
                                FIXTURES)
        d = m.diagnostico["alimentadores"]
        self.assertFalse(d["ok"])
        self.assertIn("no existe", d["error"])
        self.assertIsNone(m.alimentador("A260"))
        self.assertTrue(m.diagnostico["zonal"]["ok"])      # los otros se cargan igual

    def test_json_corrupto_y_csv_sin_filas(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "zonal.json").write_text("{no es json", encoding="utf-8")
            (base / "alim.csv").write_text("COD,cod_centrality\n", encoding="utf-8")
            with self.assertLogs("bot_fallas.maestros", level="WARNING"):
                m = Maestros.cargar({"zonal": "zonal.json", "alimentadores": "alim.csv"}, base)
            self.assertFalse(m.diagnostico["zonal"]["ok"])
            self.assertTrue(m.diagnostico["zonal"]["error"])
            self.assertFalse(m.diagnostico["alimentadores"]["ok"])
            self.assertEqual(m.diagnostico["alimentadores"]["filas"], 0)

    def test_ruta_absoluta_y_separador_punto_y_coma(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "calles.csv"
            ruta.write_text("tipo;clave;calle;comuna;clientes\n"
                            "sed;777;Pasaje Los Aromos 12;PURRANQUE;\n"
                            "sed;777;PJE LOS AROMOS;Purranque;\n", encoding="cp1252")
            m = Maestros.cargar({"calles": str(ruta)}, FIXTURES)
            (c,) = m.calles_de_sed("777")
            self.assertEqual((c.nombre, c.comuna, c.clientes), ("PJE. LOS AROMOS", "Purranque", None))


class TestConstructor(unittest.TestCase):
    def test_sin_argumentos(self):
        m = Maestros()
        self.assertIsNone(m.alimentador("A260"))
        self.assertEqual(m.zonales_de_comuna("OSORNO"), [])
        self.assertEqual(m.calles_de_sed("1"), [])
        self.assertEqual(m.calles_de_alimentador("A1"), [])
        self.assertIn("alimentadores", m.diagnostico)

    def test_estructuras_en_memoria(self):
        m = Maestros(
            alimentadores={"260": InfoAlimentador(codigo="A260", nombre="X", clientes_total=10)},
            zonal={"maestro": [{"COMUNA": "TOLTEN", "ZONAL": "CAUTIN COSTA", "CCD": "NORTE"},
                               {"COMUNA": "TOLTEN", "ZONAL": "VALDIVIA", "CCD": "ccd_sur"}]},
            calles_sed={"SED 1": [Calle(nombre="BULNES", comuna="Osorno", clientes=3)]},
            calles_alim={"260": [Calle(nombre="AV. MATTA", comuna="Osorno")]},
        )
        self.assertEqual(m.alimentador("a0260").nombre, "X")
        self.assertEqual(m.zonales_de_comuna("Toltén"),
                         [("CAUTIN COSTA", "NORTE"), ("VALDIVIA", "SUR")])
        self.assertEqual(m.calles_de_sed("1")[0].fuente, "maestro_sed")
        self.assertEqual(m.calles_de_alimentador("A260")[0].fuente, "maestro_alimentador")
        # Lista de InfoAlimentador y zonal ya indexado.
        m2 = Maestros(alimentadores=[InfoAlimentador(codigo="101")],
                      zonal={"OSORNO": [("OSORNO", "SUR")]})
        self.assertEqual(m2.alimentador("A101").codigo, "101")
        self.assertEqual(m2.zonales_de_comuna("Osorno"), [("OSORNO", "SUR")])


if __name__ == "__main__":
    unittest.main()
