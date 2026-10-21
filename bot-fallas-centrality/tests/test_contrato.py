"""Tests de contrato.py contra fixtures sintéticos con los defectos reales de §1."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from bot_fallas.contrato import (CAMPOS_UTILES, ErrorContrato, caso_de_tarea, mapear_encabezados,
                                 motivo_de_descripcion, parse_csv, parse_grilla, peligro_de)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
T = datetime(2026, 10, 21, 15, 30)

# Datos personales inventados de los fixtures: nunca deben llegar a un Registro.
PERSONALES = ("JUAN PRUEBA", "MARIA PRUEBA", "PEDRO PRUEBA", "ANA PRUEBA", "OPERADORA PRUEBA",
              "ANALISTA PRUEBA", "+56 9 1111 1111", "+56 9 2222 2222", "+56 9 3333 3333",
              "Sin comentarios", "chispas", "Llama de nuevo", "poste caido")


def leer(nombre: str) -> bytes:
    return (FIXTURES / nombre).read_bytes()


def por_caso(snap):
    out = {}
    for r in snap.registros:
        out.setdefault(r.caso, []).append(r)
    return out


class _SinPersonales:
    def assertSinPersonales(self, snap):
        texto = json.dumps([asdict(r) for r in snap.registros], default=str, ensure_ascii=False)
        for p in PERSONALES:
            self.assertNotIn(p, texto)


class TestCasos(unittest.TestCase, _SinPersonales):
    @classmethod
    def setUpClass(cls):
        cls.snap = parse_csv(leer("casos.csv"), "casos", T)
        cls.casos = por_caso(cls.snap)

    def test_encabezado_real_con_doble_espacio(self):
        m = self.snap.columnas_mapeadas
        self.assertEqual(m["Fecha de  Creacion"], "inicio")
        self.assertEqual(m["Fecha de  Cierre"], "cierre")
        self.assertEqual(m["Codigo de alimentador"], "alimentador")
        self.assertEqual(m["Clientes Interrumpidos"], "clientes")
        self.assertEqual(m["Origen interrupcion"], "origen")
        self.assertEqual(self.snap.faltantes, [])
        self.assertTrue(self.snap.completa)

    def test_nunca_mapea_analistas_ni_descripcion_despacho(self):
        m = self.snap.columnas_mapeadas
        for h in ("Analista Inicial", "Analista Final", "Descripcion despacho"):
            self.assertNotIn(h, m)
        self.assertSinPersonales(self.snap)

    def test_conteos(self):
        # 7 filas con datos (la totalmente vacía no cuenta), 1 sin caso.
        self.assertEqual(self.snap.filas_leidas, 7)
        self.assertEqual(self.snap.filas_descartadas, 1)
        self.assertEqual(len(self.snap.registros), 6)

    def test_caso_con_varias_filas_y_alimentadores(self):
        regs = self.casos["40808515"]
        self.assertEqual(len(regs), 3)        # el contrato NO deduplica: eso es del agregador
        self.assertEqual(sorted({r.alimentador for r in regs}), ["A260", "A261"])
        self.assertEqual(sorted(r.clientes for r in regs), [40, 1150, 1200])

    def test_timestamps_con_T(self):
        r = self.casos["40808515"][0]
        self.assertEqual(r.inicio, datetime(2026, 10, 21, 14, 32))
        self.assertIsNone(r.cierre)
        self.assertFalse(r.cerrado)

    def test_menos_infinito_no_descarta_la_fila(self):
        (r,) = self.casos["40811111"]
        self.assertIsNone(r.inicio)
        self.assertIsNone(r.cierre)
        self.assertFalse(r.cerrado)
        self.assertEqual(r.clientes, 80)
        self.assertEqual(r.alimentador, "A300")      # 'a0300' normalizado

    def test_cerrado_por_fecha_de_cierre(self):
        (r,) = self.casos["40822222"]
        self.assertTrue(r.cerrado)
        self.assertEqual(r.cierre, datetime(2026, 10, 21, 10, 15))
        self.assertEqual(r.alimentador, "A101")      # sólo dígitos -> 'A' + dígitos

    def test_programada_con_espacio_final(self):
        (r,) = self.casos["40833333"]
        self.assertTrue(r.programada)
        self.assertEqual(r.origen, "Programada")     # espacios normalizados
        self.assertEqual(r.alimentador, "A412")      # '0412' -> 'A412'
        self.assertFalse(self.casos["40808515"][0].programada)

    def test_origen_normalizado(self):
        origenes = {r.origen for r in self.casos["40808515"]}
        self.assertEqual(origenes, {"Operacion de protecciones"})


class TestReclamos(unittest.TestCase, _SinPersonales):
    @classmethod
    def setUpClass(cls):
        cls.snap = parse_csv(leer("reclamos.csv"), "reclamos", T)
        cls.casos = por_caso(cls.snap)
        cls.por_id = {r.id: r for r in cls.snap.registros}

    def test_mapeo_por_nombre(self):
        m = self.snap.columnas_mapeadas
        self.assertEqual(m["Ticket"], "id")
        self.assertEqual(m["Empresa afectada"], "empresa")
        self.assertEqual(m["Dirección"], "direccion")
        self.assertEqual(m["NIS"], "nis")
        self.assertEqual(m["Nivel de peligro"], "peligro")
        self.assertEqual(m["En interrupción"], "en_interrupcion")
        self.assertEqual(m["Jerarquía de red"], "jerarquia")
        self.assertEqual(m["Área"], "area")
        self.assertEqual(m["Fecha"], "inicio")
        self.assertEqual(m["Fecha cierre"], "cierre")
        self.assertEqual(self.snap.faltantes, [])
        self.assertNotIn("encabezado no reconocido: se usó el orden posicional de 21 columnas",
                         self.snap.advertencias)

    def test_columnas_personales_nunca_se_mapean(self):
        m = self.snap.columnas_mapeadas
        for h in ("Cliente", "Teléfono", "Telefonista", "Comentarios", "Brigadas", "Resolución",
                  "Descripción", "Motivo cierre"):
            self.assertNotIn(h, m, h)
        self.assertSinPersonales(self.snap)

    def test_conteos(self):
        self.assertEqual(self.snap.filas_leidas, 12)       # la fila de ';' vacíos no cuenta
        self.assertEqual(self.snap.filas_descartadas, 1)   # R-1012 sin caso
        self.assertEqual(len(self.casos["40808515"]), 6)

    def test_valores(self):
        r = self.por_id["R-1001"]
        self.assertEqual((r.nis, r.empresa, r.area, r.motivo), ("1000001", "SAESA", "OSORNO",
                                                               "Sin Suministro"))
        self.assertIs(r.peligro, False)
        self.assertIs(r.en_interrupcion, True)
        self.assertEqual(r.inicio, datetime(2026, 10, 21, 14, 40))
        self.assertEqual(r.direccion, "AV. MATTA 1234, OSORNO")   # sólo en memoria
        self.assertIs(self.por_id["R-1002"].peligro, True)
        self.assertIsNone(self.por_id["R-1003"].peligro)         # Requiere inspección
        self.assertIs(self.por_id["R-1007"].en_interrupcion, False)

    def test_comillas_con_salto_de_linea(self):
        # R-1002 trae la Descripción entre comillas con ';' y salto de línea: no
        # debe partir la fila ni correr las columnas.
        r = self.por_id["R-1002"]
        self.assertEqual(r.caso, "40808515")
        self.assertEqual(r.area, "OSORNO")
        self.assertEqual(r.jerarquia, "SE OSORNO > A260 > SED 12345")

    def test_cerrado_por_fecha_de_cierre(self):
        self.assertTrue(self.por_id["R-1008"].cerrado)
        self.assertFalse(self.por_id["R-1001"].cerrado)

    def test_cerrado_por_estado(self):
        csv_ = "Caso;Estado;NIS\n40808515;Cerrado;1\n40808515;Anulada;2\n40808515;Pendiente;3\n"
        snap = parse_csv(csv_, "reclamos", T)
        self.assertEqual([r.cerrado for r in snap.registros], [True, True, False])


class TestReclamosPosicional(unittest.TestCase, _SinPersonales):
    @classmethod
    def setUpClass(cls):
        cls.snap = parse_csv(leer("reclamos_posicional.csv"), "reclamos", T)
        cls.por_id = {r.id: r for r in cls.snap.registros}

    def test_advertencia_y_orden(self):
        self.assertIn("encabezado no reconocido: se usó el orden posicional de 21 columnas",
                      self.snap.advertencias)
        self.assertEqual(len(self.snap.registros), 3)
        r = self.por_id["R-2001"]
        self.assertEqual((r.caso, r.empresa, r.nis, r.motivo, r.area),
                         ("40808515", "SAESA", "1000010", "Sin Suministro", "OSORNO"))
        self.assertEqual(r.direccion, "AV. MATTA 1300, OSORNO")
        self.assertIs(r.peligro, True)
        self.assertEqual(r.inicio, datetime(2026, 10, 21, 14, 41))

    def test_separador_coma_y_comillas(self):
        r = self.por_id["R-2002"]
        self.assertEqual(r.direccion, "BULNES 77 / RAHUE-OSORNO / CL")
        self.assertIs(r.en_interrupcion, False)
        self.assertEqual(r.jerarquia, "")

    def test_cierre_posicional(self):
        self.assertTrue(self.por_id["R-2003"].cerrado)

    def test_posicional_no_copia_personales(self):
        canon = set(self.snap.columnas_mapeadas.values())
        self.assertFalse(canon & {"cliente", "telefono", "telefonista", "comentarios"})
        self.assertSinPersonales(self.snap)

    def test_posicional_sin_encabezado(self):
        # Si la primera fila ya es un dato (caso numérico en la columna 2), no se pierde.
        fila = ["R-1", "40808515", "SAESA", "BULNES 1", "1", "JUAN PRUEBA"] + [""] * 15
        snap = parse_csv(";".join(fila) + "\n" + ";".join(fila).replace("R-1", "R-2") + "\n",
                         "reclamos", T)
        self.assertEqual([r.id for r in snap.registros], ["R-1", "R-2"])

    def test_posicional_solo_para_reclamos(self):
        texto = leer("reclamos_posicional.csv")
        for fuente in ("casos", "abiertas"):
            with self.assertRaises(ErrorContrato):
                parse_csv(texto, fuente, T)

    def test_21_columnas_exactas(self):
        enc = ",".join(f"x{i}" for i in range(20))
        with self.assertRaises(ErrorContrato):
            parse_csv(enc + "\n" + ",".join(["1"] * 20) + "\n", "reclamos", T)


class TestAbiertas(unittest.TestCase, _SinPersonales):
    @classmethod
    def setUpClass(cls):
        cls.snap = parse_csv(leer("abiertas.csv"), "abiertas", T)
        cls.por_id = {r.id: r for r in cls.snap.registros}

    def test_solo_familia_de_emergencia(self):
        self.assertEqual(self.snap.filas_leidas, 10)
        self.assertEqual(self.snap.filas_descartadas, 5)
        self.assertEqual(sorted(self.por_id), ["40808515-D17", "40808515-R2", "40808515-SD19",
                                               "40855555-SD0", "40866666-D0"])
        self.assertEqual({r.caso for r in self.snap.registros},
                         {"40808515", "40855555", "40866666"})

    def test_mapeo_432(self):
        m = self.snap.columnas_mapeadas
        self.assertEqual(m["Tarea"], "tarea")
        self.assertEqual(m["Número Servicio"], "nis")
        self.assertEqual(m["Fecha Creación"], "inicio")
        self.assertEqual(m["Descripción"], "descripcion")
        # 'Ingreso a Centrality' sólo hace de inicio si no hay otra fecha de creación.
        self.assertNotIn("Ingreso a Centrality", m)
        self.assertNotIn("Inicio Programado", m)
        self.assertNotIn("Responsable", m)

    def test_estado_sin_sufijo_y_cerrado(self):
        self.assertEqual(self.por_id["40808515-SD19"].estado, "En ejecución")
        self.assertFalse(self.por_id["40808515-SD19"].cerrado)
        self.assertTrue(self.por_id["40808515-D17"].cerrado)        # Cancelada
        self.assertTrue(self.por_id["40855555-SD0"].cerrado)        # 'Finalizada [X]'
        self.assertFalse(self.por_id["40808515-R2"].cerrado)

    def test_motivo_desde_descripcion_sin_datos_personales(self):
        self.assertEqual(self.por_id["40808515-SD19"].motivo, "Sin Suministro")
        self.assertEqual(self.por_id["40866666-D0"].motivo, "Líneas Cortadas")
        self.assertSinPersonales(self.snap)

    def test_inicio_y_nis(self):
        r = self.por_id["40808515-SD19"]
        self.assertEqual(r.inicio, datetime(2026, 10, 21, 14, 53))
        self.assertEqual(r.nis, "1000003")
        self.assertEqual(r.area, "OSORNO")

    def test_ingreso_a_centrality_como_respaldo(self):
        snap = parse_csv("Tarea,Ingreso a Centrality\n40808515-D1,2026-10-21 10:00:00\n",
                         "abiertas", T)
        self.assertEqual(snap.columnas_mapeadas["Ingreso a Centrality"], "inicio")
        self.assertEqual(snap.registros[0].inicio, datetime(2026, 10, 21, 10))

    def test_requiere_columna_tarea(self):
        with self.assertRaises(ErrorContrato) as cm:
            parse_csv("Caso,Estado\n40808515,En ejecución\n", "abiertas", T)
        self.assertIn("tarea", str(cm.exception))

    def test_estados_cerrados(self):
        filas = ["Finalizada", "Cancelada", "Cancelada por otra brigada",
                 "Finalizada por otra brigada [X]", "Rechazada", "Detenida", "En ejecución"]
        texto = "Tarea;Estado\n" + "".join(f"40808515-D{i};{e}\n" for i, e in enumerate(filas))
        snap = parse_csv(texto, "abiertas", T)
        self.assertEqual([r.cerrado for r in snap.registros], [True] * 5 + [False] * 2)


class TestCasoDeTarea(unittest.TestCase):
    def test_familias(self):
        casos = {
            "40808515-SD19": "40808515", "40808515-D0": "40808515", "40808515-R2": "40808515",
            "40808515-d17": "40808515", " 40808515-SD1 ": "40808515",
            "1234567-IERD": None, "1234567-IMISD": None, "1234567890-01-EnlaC": None,
            "mb-1a2b3c-1760000000": None, "T1234567": None, "L-1-2": None, "M-3-4": None,
            "FB1_2-CdPI": None, "40808515-D": None, "408085150-D1": None, "40808515-X1": None,
            "40808515": None, "": None, None: None,
        }
        for codigo, esperado in casos.items():
            with self.subTest(codigo=codigo):
                self.assertEqual(caso_de_tarea(codigo), esperado)


class TestMotivoYPeligro(unittest.TestCase):
    def test_motivo(self):
        self.assertEqual(motivo_de_descripcion("Motivo: Sin Suministro. Cliente…"), "Sin Suministro")
        self.assertEqual(motivo_de_descripcion("Obs\nMotivo: Líneas Cortadas\nCliente JUAN"),
                         "Líneas Cortadas")
        self.assertEqual(motivo_de_descripcion("motivo:Sin Suministro WEB/IVR/SMS/Exprés"),
                         "Sin Suministro WEB/IVR/SMS/Exprés")
        self.assertEqual(motivo_de_descripcion("Motivo: Medidor, Cercos/Techos Energ"),
                         "Medidor, Cercos/Techos Energ")
        self.assertEqual(motivo_de_descripcion("sin motivo"), "")
        self.assertEqual(motivo_de_descripcion(None), "")

    def test_motivo_corta_antes_de_datos_personales(self):
        for desc in ("Motivo: Sin Suministro cliente JUAN PRUEBA",
                     "Motivo: Sin Suministro fono +56 9 1111 1111",
                     "Motivo: Sin Suministro 91111111",
                     "Motivo: Sin Suministro; JUAN PRUEBA",
                     "Motivo: Sin Suministro - JUAN PRUEBA",
                     "Motivo: Sin Suministro juan@prueba.cl"):
            with self.subTest(desc=desc):
                self.assertEqual(motivo_de_descripcion(desc), "Sin Suministro")

    def test_peligro(self):
        self.assertIs(peligro_de("Con peligro"), True)
        self.assertIs(peligro_de("CON PELIGRO"), True)
        self.assertIs(peligro_de("Sin peligro"), False)
        self.assertIsNone(peligro_de("Requiere inspección"))
        self.assertIsNone(peligro_de("Requiere inspeccion"))
        self.assertIsNone(peligro_de(""))
        self.assertIs(peligro_de(True), True)
        self.assertIs(peligro_de("Verdadero"), True)
        self.assertIs(peligro_de("no"), False)


class TestFormatoCSV(unittest.TestCase):
    def test_solo_encabezado_es_valido_y_vacio(self):
        for texto in ("Caso,Clientes Interrumpidos\n", "Caso;Clientes Interrumpidos",
                      "﻿Caso;Clientes Interrumpidos\r\n\r\n"):
            with self.subTest(texto=texto):
                snap = parse_csv(texto, "casos", T)
                self.assertEqual(snap.registros, [])
                self.assertEqual(snap.filas_leidas, 0)
                self.assertIn("alimentador", snap.faltantes)

    def test_error_con_encabezados_vistos(self):
        enc = [f"Columna {i}" for i in range(20)]
        with self.assertRaises(ErrorContrato) as cm:
            parse_csv(";".join(enc) + "\n" + ";".join("x" * 20) + "\n", "casos", T)
        msg = str(cm.exception)
        self.assertIn("Columna 0", msg)
        self.assertIn("Columna 11", msg)
        self.assertNotIn("Columna 12", msg)      # sólo los primeros 12

    def test_error_tapa_numeros_largos(self):
        # Archivo sin encabezado: la "primera línea" son datos de un cliente.
        with self.assertRaises(ErrorContrato) as cm:
            parse_csv("R-1;JUAN;+56 9 1111 1111;1000001\nR-2;X;1;2\n", "casos", T)
        self.assertNotIn("1111 1111", str(cm.exception))
        self.assertNotIn("1000001", str(cm.exception))

    def test_vacio_o_no_csv(self):
        for datos in (b"", b"   \n  ", "", b"<html>Centrality - Login</html>", "hola"):
            with self.subTest(datos=datos):
                with self.assertRaises(ErrorContrato) as cm:
                    parse_csv(datos, "casos", T)
                self.assertIsInstance(cm.exception, ValueError)

    def test_fuente_desconocida(self):
        with self.assertRaises(ValueError):
            parse_csv("Caso\n1\n", "otra", T)

    def test_separadores(self):
        for sep in (";", ",", "\t"):
            with self.subTest(sep=repr(sep)):
                texto = sep.join(["Caso", "Codigo de alimentador", "Clientes Interrumpidos"]) + "\n"
                texto += sep.join(["40808515", "A260", "1200"]) + "\n"
                snap = parse_csv(texto, "casos", T)
                (r,) = snap.registros
                self.assertEqual((r.caso, r.alimentador, r.clientes), ("40808515", "A260", 1200))

    def test_separador_mayoritario_de_la_primera_linea(self):
        # La dirección trae comas, pero el encabezado usa ';'.
        texto = "Caso;Dirección;NIS\n40808515;AV. MATTA 1, RAHUE, OSORNO;1\n"
        (r,) = parse_csv(texto, "reclamos", T).registros
        self.assertEqual(r.direccion, "AV. MATTA 1, RAHUE, OSORNO")

    def test_comillas_con_saltos_embebidos(self):
        texto = ('Caso,Descripcion despacho,Clientes Interrumpidos\r\n'
                 '40808515,"linea 1, con coma\r\nlinea 2\nlinea 3",12\r\n'
                 '40811111,"otra ""cita""",3\r\n')
        snap = parse_csv(texto, "casos", T)
        self.assertEqual([(r.caso, r.clientes) for r in snap.registros],
                         [("40808515", 12), ("40811111", 3)])

    def test_encoding_mixto_cp1252(self):
        # UTF-8 con un byte cp1252 suelto ('ñ' = 0xF1), como el plaincsv real.
        datos = ("Caso;Dirección;Área\n40808515;PEÑA 12, OSORNO;OSORNO\n".encode("utf-8")
                 + b"40808515;LA CA\xd1ADA 3, OSORNO;OSORNO\n")
        snap = parse_csv(datos, "reclamos", T)
        self.assertIn("1 bytes cp1252 recuperados", snap.advertencias)
        self.assertEqual(snap.registros[0].direccion, "PEÑA 12, OSORNO")
        self.assertEqual(snap.registros[1].direccion, "LA CAÑADA 3, OSORNO")

    def test_bom_y_crlf(self):
        datos = "﻿Caso,Clientes Interrumpidos\r\n40808515,1.240\r\n".encode("utf-8")
        (r,) = parse_csv(datos, "casos", T).registros
        self.assertEqual(r.clientes, 1240)

    def test_preambulo_antes_del_encabezado(self):
        # El export de la bandeja trae líneas de título (el monitor usa skiprows=4).
        texto = ("Reclamos SAESA\nExportado el 21-10-2026, 15:00\n\n"
                 "Caso;Dirección;NIS\n40808515;BULNES 1, OSORNO;1\n")
        snap = parse_csv(texto, "reclamos", T)
        self.assertEqual(len(snap.registros), 1)
        self.assertIn("se omitieron 2 líneas antes del encabezado", snap.advertencias)

    def test_filas_desparejas_y_caso_flotante(self):
        texto = "Caso,Clientes Interrumpidos,Componente\n40808515.0,5\n40811111,3,Poste,extra\n"
        snap = parse_csv(texto, "casos", T)
        self.assertEqual([r.caso for r in snap.registros], ["40808515", "40811111"])
        self.assertTrue(any("columnas distinto" in a for a in snap.advertencias))

    def test_ventana_y_recibido(self):
        d, h = datetime(2026, 10, 18), datetime(2026, 10, 21, 15)
        snap = parse_csv("Caso\n40808515\n", "casos", T, ventana_desde=d, ventana_hasta=h)
        self.assertEqual((snap.ventana_desde, snap.ventana_hasta, snap.recibido), (d, h, T))
        self.assertEqual(snap.fuente, "casos")

    def test_clientes_negativo_o_basura(self):
        texto = "Caso,Clientes Interrumpidos\n1,-5\n2,abc\n3,\n"
        self.assertEqual([r.clientes for r in parse_csv(texto, "casos", T).registros],
                         [None, None, None])


class TestMapeoEncabezados(unittest.TestCase):
    def canon(self, encabezados, fuente="reclamos"):
        m = mapear_encabezados(encabezados, fuente)
        return {encabezados[i]: c for i, c in m.items()}

    def test_prohibidos(self):
        enc = ["Cliente", "Nombre", "Nombre Cliente", "Teléfono", "Telefono Cliente", "Correo",
               "Email", "Telefonista", "Analista Inicial", "Analista Final", "Comentarios",
               "Descripcion despacho", "Observación", "Observaciones del usuario", "Brigadas",
               "Resolución", "RUT", "Celular", "Contacto"]
        self.assertEqual(self.canon(enc), {})

    def test_contencion_acotada(self):
        m = self.canon(["N° Caso", "Clientes afectados (BT)", "Cod. Alimentador SCADA",
                        "Nivel peligro", "¿En interrupción?", "Jerarquía", "Dirección de suministro",
                        "Nis Cliente"])
        self.assertEqual(m, {"N° Caso": "caso", "Clientes afectados (BT)": "clientes",
                             "Cod. Alimentador SCADA": "alimentador", "Nivel peligro": "peligro",
                             "¿En interrupción?": "en_interrupcion", "Jerarquía": "jerarquia",
                             "Dirección de suministro": "direccion", "Nis Cliente": "nis"})

    def test_contencion_no_se_pasa(self):
        m = self.canon(["Administrador", "Tipo interrupción", "Nombre alimentador",
                        "Clientes alimentador", "Origen interrupción "])
        self.assertEqual(m, {"Origen interrupción ": "origen"})

    def test_exacto_gana_y_un_canonico_por_columna(self):
        m = self.canon(["Dirección cliente", "Dirección", "Fecha", "Fecha de creación"])
        # Los dos exactos de inicio: gana el primero en orden de columna.
        self.assertEqual(m, {"Dirección cliente": "direccion", "Fecha": "inicio"})

    def test_tarea_y_descripcion_solo_en_432(self):
        self.assertEqual(self.canon(["Caso", "Tarea", "Descripción"], "reclamos"), {"Caso": "caso"})
        self.assertEqual(self.canon(["Tarea", "Descripción"], "abiertas"),
                         {"Tarea": "tarea", "Descripción": "descripcion"})

    def test_campos_utiles(self):
        self.assertEqual(set(CAMPOS_UTILES), {"casos", "reclamos", "abiertas", "grilla"})


class TestGrilla(unittest.TestCase, _SinPersonales):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads((FIXTURES / "grilla.json").read_text(encoding="utf-8"))
        cls.snap = parse_grilla(copy.deepcopy(cls.payload), T)

    def test_toma_la_grilla_con_caso_e_ignora_la_otra(self):
        s = self.snap
        self.assertEqual(s.fuente, "grilla")
        self.assertFalse(s.completa)
        self.assertEqual(s.filas_leidas, 4)
        self.assertEqual(s.filas_descartadas, 1)
        self.assertEqual(len(s.registros), 3)
        self.assertTrue(any("Brigadas en turno" in a and "ignoradas" in a for a in s.advertencias))
        self.assertTrue(any("4 de 10" in a for a in s.advertencias))
        self.assertEqual(s.faltantes, ["motivo"])

    def test_valores_tipados(self):
        regs = {r.id: r for r in self.snap.registros}
        r = regs["ext-record-1"]
        self.assertEqual((r.caso, r.nis, r.clientes, r.area), ("40877777", "7000001", 120, "OSORNO"))
        self.assertIs(r.en_interrupcion, True)
        self.assertIs(r.peligro, False)
        self.assertEqual(r.inicio, datetime(2026, 10, 21, 13, 10))
        self.assertIs(regs["ext-record-2"].en_interrupcion, False)
        self.assertIs(regs["ext-record-2"].peligro, True)
        self.assertEqual(regs["ext-record-3"].caso, "40808515")       # venía como número

    def test_cliente_nunca_se_mapea(self):
        m = self.snap.columnas_mapeadas
        for k in ("Cliente", "Teléfono", "nombreCliente", "cliente", "telefono"):
            self.assertNotIn(k, m)
        self.assertSinPersonales(self.snap)

    def test_ninguna_sirve(self):
        p = {"grillas": [self.payload["grillas"][1]]}
        with self.assertRaises(ErrorContrato) as cm:
            parse_grilla(p, T)
        self.assertIn("Brigadas en turno", str(cm.exception))
        for p in ({}, {"grillas": []}, {"error": "La página no expone ExtJS"}, []):
            with self.subTest(p=p), self.assertRaises(ErrorContrato):
                parse_grilla(p, T)

    def test_grilla_por_tarea_de_emergencia(self):
        p = {"grillas": [{"id": "g1", "titulo": "Tareas", "columnas": [],
                          "filas": [{"Tarea": "40808515-SD19", "Estado": "En ejecución [X]",
                                     "Descripción": "Motivo: Sin Suministro. Cliente JUAN PRUEBA"},
                                    {"Tarea": "1234567-IERD", "Estado": "En ejecución"}]},
                         {"id": "g2", "titulo": "Comerciales", "columnas": [],
                          "filas": [{"Tarea": "1234567-IERD"}]}]}
        s = parse_grilla(p, T)
        (r,) = s.registros
        self.assertEqual((r.caso, r.id, r.motivo, r.estado),
                         ("40808515", "40808515-SD19", "Sin Suministro", "En ejecución"))
        self.assertEqual(s.filas_descartadas, 1)
        self.assertTrue(any("Comerciales" in a for a in s.advertencias))

    def test_filas_repetidas_entre_frames(self):
        g = self.payload["grillas"][0]
        p = {"grillas": [copy.deepcopy(g), copy.deepcopy(g)]}
        s = parse_grilla(p, T)
        self.assertEqual(len(s.registros), 3)
        self.assertTrue(any("repetidas" in a for a in s.advertencias))

    def test_grilla_vacia_con_columna_caso_es_valida(self):
        p = {"grillas": [{"id": "g", "titulo": "Bandeja", "filas": [],
                          "columnas": [{"dataIndex": "caso", "texto": "Caso"}]}]}
        s = parse_grilla(p, T)
        self.assertEqual(s.registros, [])

    def test_objetos_se_ignoran(self):
        p = {"grillas": [{"id": "g", "titulo": "B", "columnas": [],
                          "filas": [{"caso": "40808515", "direccion": {"x": 1}, "nis": [1, 2]}]}]}
        (r,) = parse_grilla(p, T).registros
        self.assertEqual((r.direccion, r.nis), ("", ""))


if __name__ == "__main__":
    unittest.main()
