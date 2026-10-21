"""Tests de servidor.py contra el servidor REAL en 127.0.0.1 (puerto efímero)."""

from __future__ import annotations

import copy
import http.client
import json
import logging
import unittest

from bot_fallas.config import DEFAULTS
from bot_fallas.contrato import ErrorContrato
from bot_fallas.servidor import crear_servidor, iniciar_en_hilo, origen_permitido

ORIGEN_CENTRALITY = "https://centrality.invalid:8443"     # inventado; el real lo pone el usuario
ORIGEN_MALO = "https://evil.invalid"
EXTENSION = "chrome-extension://" + "abcdefghijklmnopabcdefghijklmnop"   # 32 letras a-p
CSV = "Caso;Codigo de alimentador;Clientes Interrumpidos\n40808515;A260;120\n".encode("utf-8")


def setUpModule():
    logging.disable(logging.WARNING)


def tearDownModule():
    logging.disable(logging.NOTSET)


class ErrorContratoPorNombre(ValueError):
    pass


ErrorContratoPorNombre.__name__ = "ErrorContrato"   # otra clase con el mismo nombre


class FakeServicio:
    """Dobles de ServicioFallas (SPEC §4.11)."""

    def __init__(self):
        self.llamadas: list[tuple] = []
        self.fallas = [{
            "caso": "40808515", "nivel": "alta", "clientes": 1240, "clientes_fuente": "informe_casos",
            "comunas": ["Osorno", "<script>alert(1)</script>"],
            "calles": [{"nombre": "LOS AROMOS & CIA", "comuna": "Osorno", "reclamos": 3},
                       {"nombre": "AV. MATTA", "comuna": "Osorno", "reclamos": 2}],
            "alimentadores": ["A260"], "inicio": "2026-10-21T14:32:00", "fuentes": ["casos"],
        }]

    def ingest_csv(self, fuente, datos, params=None):
        self.llamadas.append(("csv", fuente, datos, params))
        if datos == b"MALO":
            raise ErrorContrato("no encuentro la columna caso; encabezados vistos: A, B")
        if datos == b"MALO2":
            raise ErrorContratoPorNombre("contrato por nombre")
        if datos == b"VALOR":
            raise ValueError("fuente desconocida")
        if datos == b"BUG":
            raise RuntimeError("detalle interno que no debe salir")
        return {"ok": True, "fuente": fuente, "registros": 1, "eventos": []}

    def ingest_grilla(self, payload):
        self.llamadas.append(("grilla", payload))
        return {"ok": True, "fuente": "grilla"}

    def latido(self, payload):
        self.llamadas.append(("latido", payload))
        return {"ok": True}

    def salud(self):
        return {"version": "1.0.0", "ahora": "2026-10-21T15:00:00",
                "fuentes": {"casos": {"recibido": "2026-10-21T14:58:00", "edad_min": 2, "fresca": True,
                                      "registros": 3, "faltantes": ["direccion"], "ok": True,
                                      "advertencias": ["<b>ojo</b>"]}},
                "fallas_activas": 1, "casos_seguidos": 1,
                "extension": {"edad_min": 1, "ultimoError": ""},
                "notificadores": {"telegram": {"enviados": 2, "fallidos": 0, "ultimo_error": None}},
                "maestros": {}, "config_advertencias": ["Peñaflor sin maestro"]}

    def fallas_activas(self):
        return self.fallas

    def falla(self, caso):
        return next((f for f in self.fallas if f["caso"] == caso), None)


class Resp:
    def __init__(self, r: http.client.HTTPResponse):
        self.status = r.status
        self.headers = r.headers
        self.crudo = r.read()

    @property
    def json(self):
        return json.loads(self.crudo.decode("utf-8"))

    @property
    def texto(self):
        return self.crudo.decode("utf-8")


class TestServidor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servicio = FakeServicio()
        cfg = copy.deepcopy(DEFAULTS)
        cfg["servidor"].update(puerto=0, max_bytes=1000, origenes_permitidos=[ORIGEN_CENTRALITY + "/"])
        cls.cfg = cfg
        cls.srv = crear_servidor(cls.servicio, cfg)
        cls.puerto = cls.srv.server_address[1]
        cls.hilo = iniciar_en_hilo(cls.srv)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.hilo.join(2)

    def setUp(self):
        self.servicio.llamadas.clear()
        self._srv_cfg = copy.deepcopy(self.cfg["servidor"])

    def tearDown(self):
        self.cfg["servidor"].clear()
        self.cfg["servidor"].update(self._srv_cfg)

    def pedir(self, metodo, ruta, cuerpo: bytes | None = None, headers: dict | None = None,
              host: str | None = None, con_largo: bool = True) -> Resp:
        c = http.client.HTTPConnection("127.0.0.1", self.puerto, timeout=5)
        try:
            c.putrequest(metodo, ruta, skip_host=host is not None, skip_accept_encoding=True)
            if host:
                c.putheader("Host", host)
            for k, v in (headers or {}).items():
                c.putheader(k, v)
            if cuerpo is not None and con_largo:
                c.putheader("Content-Length", str(len(cuerpo)))
            c.endheaders(cuerpo if cuerpo is not None and con_largo else None)
            return Resp(c.getresponse())
        finally:
            c.close()

    # ------------------------------------------------------------ preflight

    def test_preflight_con_pna(self):
        r = self.pedir("OPTIONS", "/ingest/casos", headers={
            "Origin": ORIGEN_CENTRALITY, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-bot-token",
            "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(r.status, 204)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], ORIGEN_CENTRALITY)
        self.assertIn("Origin", r.headers["Vary"])
        self.assertEqual(r.headers["Access-Control-Allow-Methods"], "GET, POST, OPTIONS")
        self.assertEqual(r.headers["Access-Control-Allow-Headers"], "Content-Type, X-Bot-Token")
        self.assertEqual(r.headers["Access-Control-Max-Age"], "600")
        self.assertEqual(r.headers["Access-Control-Allow-Private-Network"], "true")

    def test_preflight_sin_pna_no_agrega_la_cabecera(self):
        r = self.pedir("OPTIONS", "/ingest/casos", headers={"Origin": ORIGEN_CENTRALITY})
        self.assertEqual(r.status, 204)
        self.assertIsNone(r.headers["Access-Control-Allow-Private-Network"])

    def test_preflight_origen_no_permitido(self):
        for origen in (ORIGEN_MALO, "null", ORIGEN_CENTRALITY + ".evil.invalid"):
            r = self.pedir("OPTIONS", "/ingest/casos", headers={
                "Origin": origen, "Access-Control-Request-Private-Network": "true"})
            self.assertEqual(r.status, 403, origen)
            self.assertIsNone(r.headers["Access-Control-Allow-Origin"])
            self.assertIsNone(r.headers["Access-Control-Allow-Private-Network"])
        self.assertEqual(self.pedir("OPTIONS", "/ingest/casos").status, 403)     # sin Origin

    def test_preflight_extension_chrome(self):
        r = self.pedir("OPTIONS", "/ingest/casos", headers={"Origin": EXTENSION})
        self.assertEqual(r.status, 204)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], EXTENSION)
        for mala in ("chrome-extension://" + "z" * 32, "chrome-extension://" + "a" * 31,
                     "moz-extension://" + "a" * 32):
            self.assertEqual(self.pedir("OPTIONS", "/", headers={"Origin": mala}).status, 403, mala)

    def test_extensiones_desactivadas(self):
        self.cfg["servidor"]["permitir_extensiones"] = False
        self.assertEqual(self.pedir("OPTIONS", "/", headers={"Origin": EXTENSION}).status, 403)
        self.assertEqual(self.pedir("POST", "/ping", b"", headers={"Origin": EXTENSION}).status, 403)

    def test_propia_pagina_es_origen_permitido(self):
        for o in (f"http://127.0.0.1:{self.puerto}", f"http://localhost:{self.puerto}"):
            self.assertEqual(self.pedir("OPTIONS", "/", headers={"Origin": o}).status, 204, o)
        self.assertFalse(origen_permitido(f"http://127.0.0.1:{self.puerto + 1}", self.cfg, self.puerto))

    # ------------------------------------------------------------ POST / Origin

    def test_post_ingest_origen_permitido(self):
        r = self.pedir("POST", "/ingest/casos?desde=2026-10-18%2000:00:00&hasta=2026-10-21", CSV,
                       headers={"Origin": ORIGEN_CENTRALITY, "Content-Type": "text/csv"})
        self.assertEqual(r.status, 200, r.texto)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], ORIGEN_CENTRALITY)
        self.assertEqual(r.json["registros"], 1)
        self.assertEqual(self.servicio.llamadas,
                         [("csv", "casos", CSV, {"desde": "2026-10-18 00:00:00", "hasta": "2026-10-21"})])

    def test_post_origen_no_permitido_es_403_y_no_llega_al_servicio(self):
        # text/plain = "simple request": el navegador NO hace preflight, por eso se corta aquí.
        for origen in (ORIGEN_MALO, "null"):
            r = self.pedir("POST", "/ingest/casos", CSV, headers={"Origin": origen,
                                                                   "Content-Type": "text/plain"})
            self.assertEqual(r.status, 403, origen)
            self.assertIsNone(r.headers["Access-Control-Allow-Origin"])
        self.assertEqual(self.servicio.llamadas, [])

    def test_post_sin_origin_se_acepta(self):
        r = self.pedir("POST", "/ingest/abiertas", CSV)
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.headers["Access-Control-Allow-Origin"])
        self.assertEqual(self.servicio.llamadas[0][:2], ("csv", "abiertas"))

    def test_post_desde_extension(self):
        r = self.pedir("POST", "/ingest/reclamos", CSV, headers={"Origin": EXTENSION})
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], EXTENSION)

    def test_grilla_latido_y_ping(self):
        payload = {"url": "x", "grillas": [{"id": "g1", "filas": [{"Caso": "40808515"}]}]}
        r = self.pedir("POST", "/ingest/grilla", json.dumps(payload).encode(), headers={"Origin": EXTENSION})
        self.assertEqual(r.status, 200)
        r = self.pedir("POST", "/extension/latido", json.dumps({"nivel": "ok", "ciclo": 3}).encode())
        self.assertEqual(r.status, 200)
        self.assertEqual(self.servicio.llamadas, [("grilla", payload), ("latido", {"nivel": "ok", "ciclo": 3})])
        self.assertEqual(self.pedir("POST", "/ping", b"").json, {"ok": True})

    def test_json_invalido_es_400(self):
        self.assertEqual(self.pedir("POST", "/ingest/grilla", b"{no es json").status, 400)
        self.assertEqual(self.pedir("POST", "/extension/latido", b"[1, 2]").status, 400)

    def test_fuente_desconocida_es_404(self):
        r = self.pedir("POST", "/ingest/otra", CSV)
        self.assertEqual(r.status, 404)
        self.assertEqual(self.servicio.llamadas, [])

    # ------------------------------------------------------------ Host

    def test_host_malicioso_es_421(self):
        for host in ("evil.invalid", "evil.invalid:8771", "127.0.0.1.evil.invalid", "localhost.evil.invalid"):
            self.assertEqual(self.pedir("GET", "/estado", host=host).status, 421, host)
            self.assertEqual(self.pedir("POST", "/ingest/casos", CSV, host=host).status, 421, host)
            self.assertEqual(self.pedir("OPTIONS", "/", host=host,
                                        headers={"Origin": ORIGEN_CENTRALITY}).status, 421, host)
        self.assertEqual(self.servicio.llamadas, [])

    def test_sin_host_es_421(self):
        self.assertEqual(self.pedir("GET", "/estado", host="").status, 421)

    def test_hosts_loopback_validos(self):
        for host in (f"127.0.0.1:{self.puerto}", f"localhost:{self.puerto}", "LOCALHOST",
                     f"[::1]:{self.puerto}", "127.0.0.1"):
            self.assertEqual(self.pedir("GET", "/estado", host=host).status, 200, host)

    # ------------------------------------------------------------ token

    def test_token_compartido(self):
        self.cfg["servidor"]["token_compartido"] = "token-local-de-prueba"
        self.assertEqual(self.pedir("POST", "/ingest/casos", CSV).status, 401)
        r = self.pedir("POST", "/ingest/casos", CSV, headers={"X-Bot-Token": "otro",
                                                              "Origin": ORIGEN_CENTRALITY})
        self.assertEqual(r.status, 401)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], ORIGEN_CENTRALITY)   # la extensión lo lee
        self.assertNotIn("token-local-de-prueba", r.texto)
        self.assertEqual(self.servicio.llamadas, [])
        r = self.pedir("POST", "/ingest/casos", CSV, headers={"X-Bot-Token": "token-local-de-prueba"})
        self.assertEqual(r.status, 200)
        self.assertEqual(self.pedir("GET", "/estado").status, 200)       # GET no lo exige

    # ------------------------------------------------------------ tamaño

    def test_cuerpo_grande_es_413(self):
        r = self.pedir("POST", "/ingest/casos", headers={"Content-Length": "5000"})
        self.assertEqual(r.status, 413)
        self.assertEqual(self.servicio.llamadas, [])

    def test_sin_content_length_es_411(self):
        self.assertEqual(self.pedir("POST", "/ingest/casos").status, 411)
        r = self.pedir("POST", "/ingest/casos", headers={"Transfer-Encoding": "chunked"})
        self.assertEqual(r.status, 411)
        self.assertEqual(self.servicio.llamadas, [])

    def test_content_length_invalido_es_400(self):
        self.assertEqual(self.pedir("POST", "/ingest/casos", headers={"Content-Length": "mucho"}).status, 400)

    # ------------------------------------------------------------ errores

    def test_error_contrato_es_422(self):
        r = self.pedir("POST", "/ingest/casos", b"MALO")
        self.assertEqual(r.status, 422)
        self.assertEqual(r.json["ok"], False)
        self.assertIn("columna caso", r.json["error"])

    def test_error_contrato_reconocido_por_nombre(self):
        self.assertEqual(self.pedir("POST", "/ingest/casos", b"MALO2").status, 422)

    def test_value_error_es_400(self):
        self.assertEqual(self.pedir("POST", "/ingest/casos", b"VALOR").status, 400)

    def test_excepcion_es_500_sin_traza(self):
        with self.assertLogs("bot_fallas.servidor", "ERROR"):
            r = self.pedir("POST", "/ingest/casos", b"BUG")
        self.assertEqual(r.status, 500)
        self.assertNotIn("detalle interno", r.texto)
        self.assertNotIn("Traceback", r.texto)

    # ------------------------------------------------------------ GET

    def test_get_raiz_escapa_html(self):
        r = self.pedir("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers["Content-Type"].startswith("text/html"))
        self.assertIn("default-src 'none'", r.headers["Content-Security-Policy"])
        html = r.texto
        self.assertNotIn("<script", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("LOS AROMOS &amp; CIA", html)
        self.assertNotIn("<b>ojo</b>", html)
        self.assertIn("40808515", html)
        self.assertIn("1.240", html)

    def test_get_estado_json_utf8(self):
        r = self.pedir("GET", "/estado")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("Peñaflor".encode("utf-8"), r.crudo)            # ensure_ascii=False
        self.assertEqual(r.json["fallas_activas"], 1)

    def test_get_fallas_y_caso(self):
        self.assertEqual(self.pedir("GET", "/fallas").json[0]["caso"], "40808515")
        self.assertEqual(self.pedir("GET", "/fallas/40808515").json["clientes"], 1240)
        r = self.pedir("GET", "/fallas/99999999")
        self.assertEqual(r.status, 404)
        self.assertFalse(r.json["ok"])

    def test_get_con_origen_no_permitido_sin_cors(self):
        r = self.pedir("GET", "/estado", headers={"Origin": ORIGEN_MALO})
        self.assertEqual(r.status, 200)
        self.assertIsNone(r.headers["Access-Control-Allow-Origin"])
        r = self.pedir("GET", "/estado", headers={"Origin": EXTENSION})
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], EXTENSION)

    def test_ruta_desconocida_es_404(self):
        self.assertEqual(self.pedir("GET", "/nada").status, 404)
        self.assertEqual(self.pedir("POST", "/nada", b"").status, 404)


class TestCrear(unittest.TestCase):
    def test_rechaza_host_no_loopback(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["servidor"].update(host="0.0.0.0", puerto=0)
        with self.assertRaises(ValueError):
            crear_servidor(FakeServicio(), cfg)


if __name__ == "__main__":
    unittest.main()
