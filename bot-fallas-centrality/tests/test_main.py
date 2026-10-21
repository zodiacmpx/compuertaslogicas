"""Tests de __main__.py (SPEC §4.13): --version, --simular, --probar y el arranque real.

Cada test usa su propio config.json en una carpeta temporal: así nunca se lee
el config.json del despachador (si existe en la raíz del proyecto) ni se
escribe en su carpeta datos/.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from bot_fallas import __version__
from bot_fallas.__main__ import correr, main
from bot_fallas.config import cargar_config

FIX = Path(__file__).parent / "fixtures"


def ejecutar(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        codigo = main(list(argv))
    return codigo, out.getvalue(), err.getvalue()


class _ConCarpeta(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.avisos = self.dir / "datos" / "avisos.jsonl"

    def tearDown(self):
        # main() quita sus propios handlers de logging al terminar (ver _quitar_logging).
        self._tmp.cleanup()

    def config(self, **secciones) -> str:
        cfg = {"archivo": {"activo": True, "ruta": "datos/avisos.jsonl"},
               "log": {"ruta": "datos/bot.log", "nivel": "INFO"}}
        cfg.update(secciones)
        ruta = self.dir / "config.json"
        ruta.write_text(json.dumps(cfg), encoding="utf-8")
        return str(ruta)


class TestVersion(unittest.TestCase):
    def test_version(self):
        codigo, out, _ = ejecutar("--version")
        self.assertEqual(codigo, 0)
        self.assertEqual(out.strip(), f"bot_fallas {__version__}")

    def test_enviar_sin_simular_es_error_de_uso(self):
        codigo, _, err = ejecutar("--enviar")
        self.assertEqual(codigo, 2)
        self.assertIn("--simular", err)


class TestSimular(_ConCarpeta):
    def test_simular_casos_imprime_eventos_y_fallas_sin_enviar(self):
        # Telegram activo SIN token: si --simular intentara enviar, fallaría la
        # validación o se llamaría a construir_notificadores.
        cfg = self.config(telegram={"activo": True, "chats": [-100123]})
        with mock.patch("bot_fallas.__main__.construir_notificadores",
                        side_effect=AssertionError("--simular no debe crear canales")), \
                mock.patch.dict("os.environ", {"BOT_FALLAS_TELEGRAM_TOKEN": ""}):
            codigo, out, err = ejecutar("--config", cfg, "--simular", "casos", str(FIX / "casos.csv"))
        self.assertEqual(codigo, 0, err)
        self.assertIn("==== Ingesta casos: 6 registros de 7 filas (1 descartadas)", out)
        self.assertIn("==== Eventos (1)", out)
        self.assertIn("resumen_inicio", out)
        self.assertIn("AVISO [resumen_inicio]", out)
        self.assertIn("Bot de fallas iniciado — 2 fallas activas", out)
        self.assertIn("40808515 · ABIERTA · 1.240 clientes (informe_casos) · MASIVO", out)
        self.assertIn("40833333 · ABIERTA", out)
        self.assertIn("[programada; no pasa los filtros]", out)
        self.assertIn("40822222 · CERRADA", out)
        self.assertIn("---- Detalle caso 40808515", out)
        self.assertIn("no se envió nada", out)
        # Nada personal del fixture, y nada escrito en la carpeta del config.
        for p in ("PRUEBA", "+56", "1111 1111", "MATTA 1234"):
            self.assertNotIn(p, out)
        self.assertFalse((self.dir / "datos").exists(), "no toca datos/ (estado, avisos ni log)")

    def test_simular_reclamos_muestra_calles_sin_numero(self):
        codigo, out, err = ejecutar("--config", self.config(), "--simular", "reclamos",
                                    str(FIX / "reclamos.csv"))
        self.assertEqual(codigo, 0, err)
        self.assertIn("• AV. MATTA — Osorno (2)", out)
        self.assertIn("• 21 DE MAYO — Coyhaique (2)", out)
        for p in ("PRUEBA", "+56", "1000001", "MATTA 1234", "S/N", "DEPTO", "Lat:"):
            self.assertNotIn(p, out)

    def test_simular_reclamos_posicional(self):
        codigo, out, err = ejecutar("--config", self.config(), "--simular", "reclamos",
                                    str(FIX / "reclamos_posicional.csv"))
        self.assertEqual(codigo, 0, err)
        self.assertIn("orden posicional de 21 columnas", out)

    def test_simular_grilla(self):
        codigo, out, err = ejecutar("--config", self.config(), "--simular", "grilla",
                                    str(FIX / "grilla.json"))
        self.assertEqual(codigo, 0, err)
        self.assertIn("40877777", out)
        self.assertIn("bandeja capturada", out)
        self.assertNotIn("PRUEBA", out)

    def test_simular_archivo_que_no_sirve(self):
        malo = self.dir / "malo.csv"
        malo.write_text("Hola;Mundo\n1;2\n", encoding="utf-8")
        codigo, out, err = ejecutar("--config", self.config(), "--simular", "casos", str(malo))
        self.assertEqual(codigo, 1)
        self.assertIn("no sirve como fuente 'casos'", err)

    def test_simular_errores_de_uso(self):
        cfg = self.config()
        codigo, _, err = ejecutar("--config", cfg, "--simular", "casos", str(self.dir / "no_existe.csv"))
        self.assertEqual(codigo, 2)
        self.assertIn("No se pudo leer", err)
        codigo, _, err = ejecutar("--config", cfg, "--simular", "otra", str(FIX / "casos.csv"))
        self.assertEqual(codigo, 2)
        self.assertIn("Fuente desconocida", err)
        grilla_mala = self.dir / "g.json"
        grilla_mala.write_text("{no es json", encoding="utf-8")
        codigo, _, err = ejecutar("--config", cfg, "--simular", "grilla", str(grilla_mala))
        self.assertEqual(codigo, 2)

    def test_simular_con_enviar_usa_los_canales(self):
        codigo, out, err = ejecutar("--config", self.config(), "--simular", "casos",
                                    str(FIX / "casos.csv"), "--enviar")
        self.assertEqual(codigo, 0, err)
        self.assertIn("✓ archivo: enviado", out)
        lineas = self.avisos.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x)["tipo"] for x in lineas], ["resumen_inicio"])
        self.assertNotIn("PRUEBA", self.avisos.read_text(encoding="utf-8"))
        self.assertFalse((self.dir / "datos" / "estado.json").exists(), "el estado real no se toca")

    def test_config_inexistente(self):
        codigo, _, err = ejecutar("--config", str(self.dir / "no_hay.json"), "--simular", "casos",
                                  str(FIX / "casos.csv"))
        self.assertEqual(codigo, 2)
        self.assertIn("no existe el archivo de configuración", err)

    def test_config_invalida(self):
        cfg = self.config(niveles={"masivo": 10, "alta": 300, "media": 50})
        codigo, _, err = ejecutar("--config", cfg, "--simular", "casos", str(FIX / "casos.csv"))
        self.assertEqual(codigo, 2)
        self.assertIn("niveles", err)


class TestProbar(_ConCarpeta):
    def test_probar_por_archivo(self):
        codigo, out, err = ejecutar("--config", self.config(), "--probar")
        self.assertEqual(codigo, 0, err)
        self.assertIn("✓ archivo: enviado", out)
        fila = json.loads(self.avisos.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(fila["tipo"], "prueba")
        self.assertIn("Mensaje de prueba", fila["texto"])

    def test_probar_sin_canales(self):
        codigo, _, err = ejecutar("--config", self.config(archivo={"activo": False}), "--probar")
        self.assertEqual(codigo, 1)
        self.assertIn("ningún canal activo", err)

    def test_probar_con_config_invalida(self):
        cfg = self.config(telegram={"activo": True, "chats": []})
        with mock.patch.dict("os.environ", {"BOT_FALLAS_TELEGRAM_TOKEN": ""}):
            codigo, _, err = ejecutar("--config", cfg, "--probar")
        self.assertEqual(codigo, 2)
        self.assertIn("telegram.activo", err)


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestCorrer(_ConCarpeta):
    def test_arranca_recibe_y_se_detiene_limpio(self):
        puerto = _puerto_libre()
        cfg = cargar_config(Path(self.config(servidor={"puerto": puerto})), env={})
        parar = threading.Event()
        resultado = {}
        hilo = threading.Thread(target=lambda: resultado.setdefault("codigo", correr(cfg, parar)))
        with self.assertLogs("bot_fallas", level="INFO") as logs:
            hilo.start()
            base = f"http://127.0.0.1:{puerto}"
            for _ in range(100):
                try:
                    with urllib.request.urlopen(f"{base}/ping", timeout=1) as r:
                        if r.status == 200:
                            break
                except OSError:
                    time.sleep(0.05)
            req = urllib.request.Request(f"{base}/ingest/casos", data=(FIX / "casos.csv").read_bytes(),
                                         method="POST", headers={"Content-Type": "text/csv"})
            with urllib.request.urlopen(req, timeout=5) as r:
                ingesta = json.loads(r.read())
            with urllib.request.urlopen(f"{base}/fallas", timeout=5) as r:
                fallas = json.loads(r.read())
            with urllib.request.urlopen(f"{base}/estado", timeout=5) as r:
                salud = json.loads(r.read())
            parar.set()
            hilo.join(15)
        self.assertFalse(hilo.is_alive())
        self.assertEqual(resultado.get("codigo"), 0)
        self.assertEqual(ingesta["registros"], 6)
        self.assertEqual([f["caso"] for f in fallas], ["40808515", "40811111"])
        self.assertTrue(salud["fuentes"]["casos"]["fresca"])
        self.assertTrue((self.dir / "datos" / "estado.json").is_file(), "el estado se guarda al detener")
        texto = "\n".join(logs.output)
        self.assertIn(f"escuchando en http://127.0.0.1:{puerto}/", texto)
        self.assertIn("Canales de aviso: archivo", texto)
        self.assertIn("Bot detenido", texto)
        self.assertNotIn("PRUEBA", texto)

    def test_puerto_ocupado(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            puerto = s.getsockname()[1]
            cfg = cargar_config(Path(self.config(servidor={"puerto": puerto})), env={})
            err = io.StringIO()
            with contextlib.redirect_stderr(err), self.assertLogs("bot_fallas", level="INFO"):
                logging.getLogger("bot_fallas").info("inicio de la prueba")
                codigo = correr(cfg, threading.Event())
        self.assertEqual(codigo, 1)
        self.assertIn("No se pudo abrir el servidor local", err.getvalue())


if __name__ == "__main__":
    unittest.main()
