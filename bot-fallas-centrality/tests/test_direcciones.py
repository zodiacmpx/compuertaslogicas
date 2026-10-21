"""Tests de direcciones.py: los cuatro formatos medidos y el nombre de calle sin número."""

from __future__ import annotations

import unittest

from bot_fallas.direcciones import Direccion, clave_calle, nombre_calle, parse_direccion


class TestNombreCalle(unittest.TestCase):
    # Todos los ejemplos del SPEC §4.1, más variantes vistas en reclamos.
    CASOS = [
        ("AV. MATTA 1234", "AV. MATTA"),
        ("PJE LOS AROMOS 45 DEPTO 3", "PJE. LOS AROMOS"),
        ("LOS CARRERA S/N", "LOS CARRERA"),
        ("CAMINO A PUERTO OCTAY KM 5", "CAMINO A PUERTO OCTAY"),
        ("CAUPOLICAN Y O'HIGGINS", "CAUPOLICAN Y O'HIGGINS"),
        ("N° 123", ""),
        ("21 DE MAYO 450", "21 DE MAYO"),
        ("CALLE 5 NORTE 123", "5 NORTE"),
        ("Avenida Matta", "AV. MATTA"),
        ("avda. matta 1250 depto 3", "AV. MATTA"),
        ("AV.MATTA #455", "AV. MATTA"),
        ("AV. MATTA N° 1234", "AV. MATTA"),
        ("AV MATTA 1234 B", "AV. MATTA"),
        ("LOS CARRERA 1234-B", "LOS CARRERA"),
        ("LOS CARRERA S/Nº", "LOS CARRERA"),
        ("LOS CARRERA  s / n", "LOS CARRERA"),
        ("PASAJE LOS AROMOS 45 CASA 2", "PJE. LOS AROMOS"),
        ("POBLACION LOS VOLCANES", "POB. LOS VOLCANES"),
        ("Pobl. Los Volcanes 12", "POB. LOS VOLCANES"),
        ("POB LOS VOLCANES BLOCK B", "POB. LOS VOLCANES"),
        ("VILLA LOS ALAMOS 300 LOTE 4", "VILLA LOS ALAMOS"),
        ("LOS CARRERA 980 VILLA X", "LOS CARRERA"),
        ("TORRE B DEPTO 302 AV ALEMANIA 600", "AV. ALEMANIA"),
        ("RUTA 215 KM 12,5", "RUTA 215"),
        ("PASAJE 5", "PJE. 5"),          # el número ES el nombre
        ("PJE 5 123", "PJE. 5"),
        ("CALLE 46", "CALLE 46"),
        ("CASA BLANCA 55", "CASA BLANCA"),   # "CASA" sin valor no es marcador
        ("1234", ""),
        ("SIN NUMERO", ""),
        ("", ""),
        ("  los   carrera.  ", "LOS CARRERA"),
    ]

    def test_ejemplos(self):
        for entrada, esperado in self.CASOS:
            with self.subTest(entrada=entrada):
                self.assertEqual(nombre_calle(entrada), esperado)

    def test_idempotente(self):
        # El agregador la vuelve a aplicar sobre Direccion.calle: no debe cambiar nada.
        for entrada, _ in self.CASOS:
            with self.subTest(entrada=entrada):
                una = nombre_calle(entrada)
                self.assertEqual(nombre_calle(una), una)

    def test_nunca_deja_numero_de_casa_al_final(self):
        for entrada in ("AV. MATTA 1234", "LOS CARRERA 980", "BULNES 77 DEPTO 12",
                        "O'HIGGINS 300 OF 5", "PJE LOS AROMOS 45 DEPTO 3"):
            with self.subTest(entrada=entrada):
                self.assertNotRegex(nombre_calle(entrada), r"\d")


class TestClaveCalle(unittest.TestCase):
    def test_agrupa_variantes(self):
        k = clave_calle("AV. MATTA")
        for v in ("Avenida Matta", "AVDA MATTA 120", "av matta", "Av.Matta 1250 depto 3"):
            with self.subTest(v=v):
                self.assertEqual(clave_calle(v), k)

    def test_apostrofe_y_tildes(self):
        self.assertEqual(clave_calle("O'HIGGINS"), clave_calle("OHIGGINS 45"))
        self.assertEqual(clave_calle("Pasaje Los Álamos"), clave_calle("PJE LOS ALAMOS 3"))

    def test_tipos_distintos_no_se_mezclan(self):
        # "PJE. LOS AROMOS" y "LOS AROMOS" suelen ser calles distintas en la misma población.
        self.assertNotEqual(clave_calle("PJE LOS AROMOS"), clave_calle("LOS AROMOS"))

    def test_vacia(self):
        self.assertEqual(clave_calle("N° 12"), "")


class TestParseDireccion(unittest.TestCase):
    def test_formato_coma_ultimo_campo_comuna(self):
        d = parse_direccion("Avenida Matta 1250 Depto 3, Rahue, Osorno, OSORNO", "PURRANQUE")
        self.assertEqual(d.calle, "AV. MATTA")
        self.assertEqual(d.localidad, "Rahue")
        self.assertEqual(d.comuna, "OSORNO")
        self.assertTrue(d.tiene_calle())

    def test_formato_slash_localidad_comuna(self):
        d = parse_direccion("LOS CARRERA 49 / RAHUE-OSORNO / CL")
        self.assertEqual((d.calle, d.localidad, d.comuna), ("LOS CARRERA", "RAHUE", "OSORNO"))

    def test_sn_protegido(self):
        # Sin proteger S/N, el split por "/" dejaba la comuna en "N".
        d = parse_direccion("LOS CARRERA S/N / RAHUE-OSORNO / CL", "X")
        self.assertEqual((d.calle, d.comuna), ("LOS CARRERA", "OSORNO"))
        d = parse_direccion("LOS CARRERA S/N", "OSORNO")
        self.assertEqual((d.calle, d.comuna), ("LOS CARRERA", "OSORNO"))
        d = parse_direccion("LOS CARRERA S/N, PUERTO OCTAY", "OSORNO")
        self.assertEqual((d.calle, d.comuna), ("LOS CARRERA", "PUERTO OCTAY"))

    def test_lat_lon_sin_calle(self):
        d = parse_direccion("Lat: -40.5731, Lon: -73.1330", "OSORNO")
        self.assertFalse(d.tiene_calle())
        self.assertEqual(d.calle, "")
        self.assertAlmostEqual(d.lat, -40.5731)
        self.assertAlmostEqual(d.lon, -73.1330)
        self.assertEqual(d.comuna, "OSORNO")

    def test_lat_lon_con_coma_decimal(self):
        d = parse_direccion("lat: -37,9000; lon: -72,5649")
        self.assertAlmostEqual(d.lat, -37.9)
        self.assertAlmostEqual(d.lon, -72.5649)

    def test_sin_comuna_cae_al_area(self):
        d = parse_direccion("Caupolican y O'Higgins", "OSORNO")
        self.assertEqual(d.calle, "CAUPOLICAN Y O'HIGGINS")
        self.assertEqual(d.comuna, "OSORNO")

    def test_comuna_pegada_al_final_sin_separador(self):
        # Si no se separa, el número de casa queda al medio y no se puede quitar.
        d = parse_direccion("AV. MATTA 1234 OSORNO", "OSORNO")
        self.assertEqual((d.calle, d.comuna), ("AV. MATTA", "OSORNO"))

    def test_ultimo_campo_no_comuna(self):
        # Con dígitos -> no es comuna -> Área.
        d = parse_direccion("LOS CARRERA 12, 1234", "OSORNO")
        self.assertEqual((d.calle, d.comuna), ("LOS CARRERA", "OSORNO"))
        # Muy corto (resto de parsing) -> Área.
        d = parse_direccion("LOS CARRERA 12, N", "OSORNO")
        self.assertEqual(d.comuna, "OSORNO")
        # El país al final se ignora.
        d = parse_direccion("AV MATTA 12, OSORNO, CHILE", "X")
        self.assertEqual(d.comuna, "OSORNO")
        d = parse_direccion("AV MATTA 12, CL", "PURRANQUE")
        self.assertEqual(d.comuna, "PURRANQUE")

    def test_un_solo_campo_con_coma_final(self):
        d = parse_direccion("LOS CARRERA 45,", "OSORNO")
        self.assertEqual((d.calle, d.comuna), ("LOS CARRERA", "OSORNO"))

    def test_primer_campo_vacio(self):
        d = parse_direccion(", OSORNO", "X")
        self.assertEqual((d.calle, d.comuna), ("", "OSORNO"))
        self.assertFalse(d.tiene_calle())

    def test_vacia(self):
        d = parse_direccion("", "OSORNO")
        self.assertEqual(d, Direccion(cruda="", comuna="OSORNO"))
        d = parse_direccion(None)
        self.assertEqual((d.calle, d.comuna), ("", ""))

    def test_cola_cero(self):
        d = parse_direccion("LOS CARRERA 12, OSORNO 0", "X")
        self.assertEqual(d.comuna, "OSORNO")

    def test_calle_nunca_trae_numero_de_casa(self):
        for cruda in ("AV. MATTA 1234, OSORNO", "PJE LOS AROMOS 45 DEPTO 3, PUERTO OCTAY",
                      "BULNES 77 / RAHUE-OSORNO / CL", "21 DE MAYO 450, COYHAIQUE"):
            with self.subTest(cruda=cruda):
                d = parse_direccion(cruda)
                self.assertTrue(d.tiene_calle())
                self.assertNotIn("1234", d.calle)
                self.assertNotRegex(d.calle, r"\b(45|77|450)\b")


if __name__ == "__main__":
    unittest.main()
