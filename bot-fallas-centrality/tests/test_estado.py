"""Tests de estado.py: roundtrip JSON, guardado atómico y JSON corrupto."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from bot_fallas import estado as estado_mod
from bot_fallas.estado import EstadoBot, Seguimiento, cargar, guardar
from bot_fallas.modelo import Calle, Falla

T0 = datetime(2026, 10, 21, 14, 32, 5)


def _falla() -> Falla:
    return Falla(
        caso="40808515", clientes=1240, clientes_fuente="informe_casos",
        calles=[Calle("AV. MATTA", "Osorno", 12), Calle("PJE. LOS ÑANDÚES", "Río Bueno", 3)],
        calles_fuente="reclamos", comunas=["Osorno", "Río Bueno"],
        alimentadores=["A260"], alimentadores_cod=["A260"],
        inicio=datetime(2026, 10, 21, 13, 0), fuentes=["casos", "reclamos"], nivel="masivo",
    )


def _estado() -> EstadoBot:
    s = Seguimiento(
        caso="40808515", primera_vista=T0,
        ultima_vista=datetime(2026, 10, 21, 15, 0, 0, 123456),   # con microsegundos
        notificada=True, ultimo_aviso=T0, clientes_notificados=1240, max_clientes=1300,
        nivel_notificado="masivo", peligro_notificado=True,
        calles_notificadas=["AV. MATTA|OSORNO"], ausente_desde=datetime(2026, 10, 21, 15, 5),
        evaluaciones_ausente=1, fuentes_completas=["casos"], resumen=_falla().to_dict(),
    )
    s2 = Seguimiento(caso="40808516", primera_vista=T0, ultima_vista=T0,
                     normalizada=True, normalizada_en=T0)
    return EstadoBot(iniciado=True, casos={s.caso: s, s2.caso: s2},
                     fuentes={"casos": {"ok": True, "registros": 3}},
                     alertas={"sin_datos": False}, telegram_offset=991)


class TestRoundtrip(unittest.TestCase):
    def test_to_dict_es_json_y_vuelve_igual(self):
        est = _estado()
        crudo = json.dumps(est.to_dict(), ensure_ascii=False)
        otro = EstadoBot.from_dict(json.loads(crudo))
        self.assertEqual(otro, est)
        s = otro.casos["40808515"]
        self.assertIsInstance(s.primera_vista, datetime)
        self.assertEqual(s.ultima_vista.microsecond, 123456)
        self.assertIsNone(otro.casos["40808516"].ultimo_aviso)

    def test_falla_desde_resumen(self):
        s = _estado().casos["40808515"]
        f = s.falla()
        self.assertEqual(f.caso, "40808515")
        self.assertEqual(f.clientes, 1240)
        self.assertIsInstance(f.inicio, datetime)
        self.assertIsInstance(f.calles[0], Calle)
        self.assertEqual(f.calles[1].nombre, "PJE. LOS ÑANDÚES")

    def test_falla_sin_resumen_usa_caso(self):
        s = Seguimiento(caso="123", primera_vista=T0, ultima_vista=T0)
        self.assertEqual(s.falla().caso, "123")

    def test_from_dict_tolera_campos_faltantes_y_sobrantes(self):
        est = EstadoBot.from_dict({
            "iniciado": True, "campo_futuro": 1,
            "casos": {"777": {"ultima_vista": "2026-10-21T14:00:00", "otro": "x"}},
        })
        s = est.casos["777"]
        self.assertEqual(s.caso, "777")                 # la clave del dict suple el caso
        self.assertEqual(s.primera_vista, datetime(2026, 10, 21, 14, 0))
        self.assertFalse(s.notificada)
        self.assertEqual(s.calles_notificadas, [])
        self.assertEqual(est.telegram_offset, 0)

    def test_seguimiento_danado_se_descarta_solo(self):
        with self.assertLogs("bot_fallas.estado", level="WARNING"):
            est = EstadoBot.from_dict({"casos": {
                "1": {"caso": "1", "primera_vista": "2026-10-21T14:00:00"},
                "2": {"caso": "2"},                       # sin fechas: inservible
                "3": "basura",
            }})
        self.assertEqual(list(est.casos), ["1"])


class TestArchivo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_guardar_y_cargar(self):
        ruta = self.dir / "sub" / "dir" / "estado.json"   # crea directorios
        est = _estado()
        guardar(est, ruta)
        self.assertEqual(cargar(ruta), est)
        # utf-8 legible, sin \u escapes
        crudo = ruta.read_text(encoding="utf-8")
        self.assertIn("ÑANDÚES", crudo)
        self.assertIn("Río Bueno", crudo)

    def test_guardar_no_deja_temporales(self):
        ruta = self.dir / "estado.json"
        guardar(_estado(), ruta)
        guardar(_estado(), ruta)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["estado.json"])

    def test_guardar_atomico_si_falla_el_reemplazo(self):
        ruta = self.dir / "estado.json"
        guardar(EstadoBot(telegram_offset=1), ruta)
        antes = ruta.read_bytes()
        with mock.patch.object(estado_mod.os, "replace", side_effect=OSError("disco lleno")):
            with self.assertRaises(OSError):
                guardar(EstadoBot(telegram_offset=2), ruta)
        self.assertEqual(ruta.read_bytes(), antes)       # el viejo queda entero
        self.assertEqual([p.name for p in self.dir.iterdir()], ["estado.json"])

    def test_guardar_reintenta_permission_error(self):
        # Windows: antivirus con el archivo abierto un instante.
        ruta = self.dir / "estado.json"
        real = os.replace
        llamadas = []

        def replace_flojo(a, b):
            llamadas.append(1)
            if len(llamadas) < 3:
                raise PermissionError("en uso")
            return real(a, b)

        with mock.patch.object(estado_mod.os, "replace", side_effect=replace_flojo), \
                mock.patch.object(estado_mod.time, "sleep"):
            guardar(EstadoBot(telegram_offset=5), ruta)
        self.assertEqual(cargar(ruta).telegram_offset, 5)
        self.assertEqual(len(llamadas), 3)

    def test_guardar_serializa_fechas_en_alertas(self):
        ruta = self.dir / "estado.json"
        guardar(EstadoBot(alertas={"sin_datos_desde": T0, "tipos": {"a"}}), ruta)
        est = cargar(ruta)
        self.assertEqual(est.alertas["sin_datos_desde"], T0.isoformat())
        self.assertEqual(est.alertas["tipos"], ["a"])

    def test_cargar_inexistente(self):
        self.assertEqual(cargar(self.dir / "no_existe.json"), EstadoBot())

    def _corrupto(self, contenido: bytes):
        ruta = self.dir / "estado.json"
        ruta.write_bytes(contenido)
        with self.assertLogs("bot_fallas.estado", level="WARNING"):
            est = cargar(ruta)
        self.assertEqual(est, EstadoBot())
        self.assertFalse(ruta.exists())
        respaldos = list(self.dir.glob("estado.corrupto-*.json"))
        self.assertEqual(len(respaldos), 1)
        self.assertEqual(respaldos[0].read_bytes(), contenido)   # se conserva para diagnóstico
        return ruta

    def test_json_corrupto_respalda_y_parte_vacio(self):
        ruta = self._corrupto(b'{"iniciado": true, "casos": {')
        # y se puede volver a guardar encima sin problema
        guardar(EstadoBot(iniciado=True), ruta)
        self.assertTrue(cargar(ruta).iniciado)

    def test_json_vacio_es_corrupto(self):
        self._corrupto(b"")

    def test_json_no_objeto_es_corrupto(self):
        self._corrupto(b"[1, 2, 3]")

    def test_bytes_invalidos_es_corrupto(self):
        self._corrupto(b"\xff\xfe\x00basura")

    def test_dos_corruptos_no_se_pisan(self):
        ruta = self.dir / "estado.json"
        for _ in range(2):
            ruta.write_bytes(b"{mal")
            with self.assertLogs("bot_fallas.estado", level="WARNING"):
                cargar(ruta)
        self.assertEqual(len(list(self.dir.glob("estado.corrupto-*.json"))), 2)


if __name__ == "__main__":
    unittest.main()
