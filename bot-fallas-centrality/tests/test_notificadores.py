"""Tests de notificadores.py con un `abrir` falso: nada sale a la red."""

from __future__ import annotations

import copy
import json
import logging
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path

from bot_fallas.config import DEFAULTS
from bot_fallas.modelo import Mensaje
from bot_fallas.notificadores import (
    ArchivoNotificador,
    Despachador,
    ErrorEnvio,
    Notificador,
    TeamsNotificador,
    TelegramNotificador,
    construir_notificadores,
    opener_http,
    redactar,
)

# Inventados, con la forma real (para que la redacción por patrón se ejercite).
TOKEN = "000000000:token-de-prueba-no-es-real"
WEBHOOK = "https://flujo.invalid/workflows/abc/triggers/manual/paths/invoke?sig=FirmaSecretaFalsa"


def setUpModule():
    # Los reintentos fallidos se loguean como WARNING a propósito; en los tests sólo meten ruido.
    logging.disable(logging.WARNING)


def tearDownModule():
    logging.disable(logging.NOTSET)


def _msg(caso="40808515") -> Mensaje:
    return Mensaje(titulo=f"Falla nueva · Caso {caso} · 1.240 clientes",
                   html=f"🔴 <b>FALLA NUEVA</b> · Caso <code>{caso}</code>\n👥 <b>1.240 clientes</b>",
                   texto=f"🔴 FALLA NUEVA · Caso {caso}\n👥 1.240 clientes\n\n• AV. MATTA — Osorno (12)",
                   tipo="nueva", caso=caso)


def _ok(resultado=None) -> tuple[int, bytes]:
    return 200, json.dumps({"ok": True, "result": resultado or {}}).encode()


def _err(status: int, desc: str, **params) -> tuple[int, bytes]:
    cuerpo = {"ok": False, "error_code": status, "description": desc}
    if params:
        cuerpo["parameters"] = params
    return status, json.dumps(cuerpo).encode()


class AbrirFalso:
    """Respuestas en orden; una excepción en la lista se levanta. Registra llamadas."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.llamadas: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, url, data, headers, timeout):
        with self.lock:
            self.llamadas.append({"url": url, "data": json.loads(data) if data else None,
                                  "headers": headers, "timeout": timeout})
            r = self.respuestas.pop(0) if self.respuestas else _ok()
        if isinstance(r, BaseException):
            raise r
        return r


class Reloj:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Dormir:
    def __init__(self, reloj: Reloj | None = None):
        self.llamadas: list[float] = []
        self.reloj = reloj

    def __call__(self, s):
        self.llamadas.append(s)
        if self.reloj:
            self.reloj.t += s


def _telegram(abrir, chats=(-1001,), reloj=None, dormir=None):
    reloj = reloj or Reloj()
    dormir = dormir or Dormir(reloj)
    return TelegramNotificador(TOKEN, list(chats), abrir=abrir, dormir=dormir, reloj=reloj), dormir, reloj


class TestTelegram(unittest.TestCase):
    def test_payload_correcto_a_cada_chat(self):
        abrir = AbrirFalso()
        tg, _, _ = _telegram(abrir, chats=[-1001, "-1002"])
        m = _msg()
        tg.enviar(m)
        self.assertEqual(len(abrir.llamadas), 2)
        for llamada, chat in zip(abrir.llamadas, [-1001, "-1002"]):
            self.assertEqual(llamada["url"], f"https://api.telegram.org/bot{TOKEN}/sendMessage")
            self.assertEqual(llamada["data"], {"chat_id": chat, "text": m.html, "parse_mode": "HTML",
                                               "disable_web_page_preview": True})
            self.assertEqual(llamada["headers"]["Content-Type"], "application/json")
            self.assertEqual(llamada["timeout"], 20)

    def test_429_espera_retry_after_y_reintenta_una_vez(self):
        abrir = AbrirFalso(_err(429, "Too Many Requests: retry after 7", retry_after=7), _ok())
        tg, dormir, _ = _telegram(abrir)
        tg.enviar(_msg())
        self.assertEqual(len(abrir.llamadas), 2)
        self.assertIn(7, dormir.llamadas)

    def test_429_retry_after_se_topa_en_60(self):
        abrir = AbrirFalso(_err(429, "Too Many Requests", retry_after=500), _ok())
        tg, dormir, _ = _telegram(abrir)
        tg.enviar(_msg())
        self.assertIn(60, dormir.llamadas)
        self.assertNotIn(500, dormir.llamadas)

    def test_429_dos_veces_es_error(self):
        abrir = AbrirFalso(_err(429, "Too Many Requests", retry_after=1),
                           _err(429, "Too Many Requests", retry_after=1))
        tg, _, _ = _telegram(abrir)
        with self.assertRaises(ErrorEnvio) as cm:
            tg.enviar(_msg())
        self.assertFalse(cm.exception.permanente)
        self.assertEqual(len(abrir.llamadas), 2)

    def test_html_invalido_reintenta_en_texto_plano(self):
        abrir = AbrirFalso(_err(400, "Bad Request: can't parse entities: unsupported start tag"), _ok())
        tg, _, _ = _telegram(abrir)
        m = _msg()
        tg.enviar(m)
        self.assertEqual(len(abrir.llamadas), 2)
        segundo = abrir.llamadas[1]["data"]
        self.assertNotIn("parse_mode", segundo)
        self.assertEqual(segundo["text"], m.texto)
        self.assertEqual(segundo["chat_id"], -1001)

    def test_otro_400_es_permanente_y_sin_token(self):
        abrir = AbrirFalso(_err(400, "Bad Request: chat not found"))
        tg, _, _ = _telegram(abrir)
        with self.assertRaises(ErrorEnvio) as cm:
            tg.enviar(_msg())
        self.assertTrue(cm.exception.permanente)
        self.assertIn("chat not found", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertEqual(len(abrir.llamadas), 1)

    def test_error_de_red_redacta_el_token(self):
        # Una excepción de red que (en el peor caso) trae la URL completa.
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        abrir = AbrirFalso(OSError(f"fallo conectando a {url}"))
        tg, _, _ = _telegram(abrir)
        with self.assertRaises(ErrorEnvio) as cm:
            tg.enviar(_msg())
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertNotIn(TOKEN.split(":")[1], str(cm.exception))
        # Ni la causa ni el contexto encadenado (que saldría en log.exception).
        self.assertIsNone(cm.exception.__cause__)
        self.assertTrue(cm.exception.__context__ is None or cm.exception.__suppress_context__)

    def test_repr_no_muestra_token(self):
        tg, _, _ = _telegram(AbrirFalso(), chats=[-1001, -1002])
        self.assertNotIn(TOKEN, repr(tg))
        self.assertIn("chats=2", repr(tg))

    def test_pausa_entre_mensajes_al_mismo_chat(self):
        abrir = AbrirFalso()
        tg, dormir, _ = _telegram(abrir)
        tg.enviar(_msg("1"))
        tg.enviar(_msg("2"))
        self.assertEqual(len(dormir.llamadas), 1)
        self.assertAlmostEqual(dormir.llamadas[0], 1.1, places=3)

    def test_sin_pausa_entre_chats_distintos(self):
        tg, dormir, _ = _telegram(AbrirFalso(), chats=[-1001, -1002])
        tg.enviar(_msg())
        self.assertEqual(dormir.llamadas, [])

    def test_fallo_parcial_informa_pendientes(self):
        abrir = AbrirFalso(_ok(), _err(500, "Internal Server Error"))
        tg, _, _ = _telegram(abrir, chats=[-1001, -1002])
        with self.assertRaises(ErrorEnvio) as cm:
            tg.enviar(_msg())
        self.assertEqual(cm.exception.pendientes, [-1002])
        self.assertFalse(cm.exception.permanente)

    def test_destinos_limita_los_chats(self):
        abrir = AbrirFalso()
        tg, _, _ = _telegram(abrir, chats=[-1001, -1002])
        tg.enviar(_msg(), destinos=[-1002])
        self.assertEqual([c["data"]["chat_id"] for c in abrir.llamadas], [-1002])

    def test_grupo_migrado_se_reenvia_al_nuevo_id(self):
        abrir = AbrirFalso(_err(400, "Bad Request: group chat was upgraded to a supergroup chat",
                                migrate_to_chat_id=-100777), _ok())
        tg, _, _ = _telegram(abrir)
        tg.enviar(_msg())
        self.assertEqual(abrir.llamadas[1]["data"]["chat_id"], -100777)


class TestRedactar(unittest.TestCase):
    def test_patron_de_token(self):
        self.assertEqual(redactar(f"x bot{TOKEN} y"), "x bot*** y")
        self.assertEqual(redactar("hola secreto", "secreto"), "hola ***")


class TestTeams(unittest.TestCase):
    def test_adaptive_card(self):
        abrir = AbrirFalso((202, b""))
        tm = TeamsNotificador(WEBHOOK, abrir=abrir)
        m = _msg()
        tm.enviar(m)
        llamada = abrir.llamadas[0]
        self.assertEqual(llamada["url"], WEBHOOK)
        d = llamada["data"]
        self.assertEqual(d["type"], "message")
        adj = d["attachments"][0]
        self.assertEqual(adj["contentType"], "application/vnd.microsoft.card.adaptive")
        card = adj["content"]
        self.assertEqual(card["type"], "AdaptiveCard")
        self.assertEqual(card["version"], "1.4")
        self.assertEqual(card["$schema"], "http://adaptivecards.io/schemas/adaptive-card.json")
        self.assertEqual(card["body"][0], {"type": "TextBlock", "text": m.titulo, "weight": "Bolder",
                                           "wrap": True})
        textos = [b["text"] for b in card["body"][1:]]
        self.assertEqual(textos, [ln for ln in m.texto.splitlines() if ln.strip()])
        self.assertTrue(all(b["type"] == "TextBlock" and b["wrap"] for b in card["body"]))

    def test_error_no_muestra_webhook(self):
        abrir = AbrirFalso((403, f"denegado {WEBHOOK}".encode()))
        tm = TeamsNotificador(WEBHOOK, abrir=abrir)
        with self.assertRaises(ErrorEnvio) as cm:
            tm.enviar(_msg())
        self.assertNotIn("FirmaSecretaFalsa", str(cm.exception))
        self.assertTrue(cm.exception.permanente)
        self.assertNotIn("FirmaSecretaFalsa", repr(tm))

    def test_error_de_red_no_muestra_webhook(self):
        tm = TeamsNotificador(WEBHOOK, abrir=AbrirFalso(OSError(f"timeout {WEBHOOK}")))
        with self.assertRaises(ErrorEnvio) as cm:
            tm.enviar(_msg())
        self.assertNotIn("FirmaSecretaFalsa", str(cm.exception))


class TestArchivo(unittest.TestCase):
    def test_jsonl_crea_dir_y_agrega(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "sub" / "avisos.jsonl"
            ar = ArchivoNotificador(ruta, reloj=lambda: datetime(2026, 10, 21, 14, 44, 3))
            ar.enviar(_msg("1"))
            ar.enviar(_msg("2"))
            lineas = ruta.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lineas), 2)
            fila = json.loads(lineas[0])
            self.assertEqual(set(fila), {"ts", "tipo", "caso", "titulo", "texto"})
            self.assertEqual(fila["ts"], "2026-10-21T14:44:03")
            self.assertEqual(fila["caso"], "1")
            self.assertEqual(fila["tipo"], "nueva")
            self.assertIn("AV. MATTA", fila["texto"])
            self.assertIn("—", lineas[0])          # ensure_ascii=False: legible

    def test_error_de_disco_es_error_envio(self):
        with tempfile.TemporaryDirectory() as tmp:
            bloqueo = Path(tmp) / "archivo_no_dir"
            bloqueo.write_text("x")
            ar = ArchivoNotificador(bloqueo / "avisos.jsonl")
            with self.assertRaises(ErrorEnvio):
                ar.enviar(_msg())


# --------------------------------------------------------------------------
# Despachador
# --------------------------------------------------------------------------


class Falso(Notificador):
    """Falla las primeras `fallas` veces; registra lo enviado."""

    def __init__(self, nombre="falso", fallas=0, permanente=False, excepcion=None):
        self.nombre = nombre
        self.fallas = fallas
        self.permanente = permanente
        self.excepcion = excepcion
        self.intentos = 0
        self.enviados: list[str] = []

    def enviar(self, m):
        self.intentos += 1
        if self.intentos <= self.fallas:
            if self.excepcion:
                raise self.excepcion
            raise ErrorEnvio(f"caído (intento {self.intentos})", permanente=self.permanente)
        self.enviados.append(m.caso)


class Bloqueado(Notificador):
    nombre = "lento"

    def __init__(self):
        self.soltar = threading.Event()
        self.enviados: list[str] = []

    def enviar(self, m):
        self.soltar.wait(5)
        self.enviados.append(m.caso)


class TestDespachador(unittest.TestCase):
    def despachar(self, *notificadores, mensajes=1):
        dormir = Dormir()
        d = Despachador(list(notificadores), dormir=dormir)
        d.iniciar()
        self.addCleanup(d.detener, 1)
        for i in range(mensajes):
            d.encolar(_msg(str(i)))
        self.assertTrue(d.vaciar(5))
        return d, dormir

    def test_entrega_en_orden(self):
        n = Falso()
        d, _ = self.despachar(n, mensajes=3)
        self.assertEqual(n.enviados, ["0", "1", "2"])
        self.assertEqual(d.estado()["falso"]["enviados"], 3)
        self.assertIsNotNone(d.estado()["falso"]["ultimo_ok"])

    def test_reintenta_con_backoff(self):
        n = Falso(fallas=2)
        d, dormir = self.despachar(n)
        self.assertEqual(n.intentos, 3)
        self.assertEqual(n.enviados, ["0"])
        self.assertEqual(dormir.llamadas, [2, 5])
        self.assertEqual(d.estado()["falso"]["fallidos"], 0)

    def test_agota_tres_intentos_y_registra_error(self):
        n = Falso(fallas=99)
        d, dormir = self.despachar(n)
        self.assertEqual(n.intentos, 3)
        e = d.estado()["falso"]
        self.assertEqual((e["enviados"], e["fallidos"]), (0, 1))
        self.assertIn("caído", e["ultimo_error"])
        self.assertEqual(dormir.llamadas, [2, 5])     # sin más mensajes, no hay enfriamiento

    def test_enfriamiento_si_quedan_mensajes(self):
        n = Falso(fallas=3)
        _, dormir = self.despachar(n, mensajes=2)
        self.assertEqual(dormir.llamadas, [2, 5, 15])
        self.assertEqual(n.enviados, ["1"])

    def test_error_permanente_no_reintenta(self):
        n = Falso(fallas=99, permanente=True)
        d, dormir = self.despachar(n)
        self.assertEqual(n.intentos, 1)
        self.assertEqual(dormir.llamadas, [])
        self.assertEqual(d.estado()["falso"]["fallidos"], 1)

    def test_excepcion_inesperada_no_mata_el_canal(self):
        n = Falso(fallas=1, excepcion=RuntimeError("bug"))
        d, _ = self.despachar(n, mensajes=2)
        self.assertEqual(n.enviados, ["0", "1"])

    def test_canal_caido_no_bloquea_al_otro(self):
        lento, rapido = Bloqueado(), Falso("rapido")
        d = Despachador([lento, rapido], dormir=Dormir())
        d.iniciar()
        self.addCleanup(d.detener, 1)
        d.encolar(_msg("A"))
        limite = time.monotonic() + 3
        while not rapido.enviados and time.monotonic() < limite:
            time.sleep(0.01)
        self.assertEqual(rapido.enviados, ["A"])
        self.assertEqual(lento.enviados, [])
        self.assertFalse(d.vaciar(0.1))          # el lento sigue pendiente
        self.assertEqual(d.estado()["lento"]["en_cola"], 1)
        lento.soltar.set()
        self.assertTrue(d.vaciar(3))
        self.assertEqual(lento.enviados, ["A"])

    def test_reintento_parcial_va_solo_al_chat_que_fallo(self):
        abrir = AbrirFalso(_ok(), _err(500, "Internal Server Error"), _ok())
        tg, _, _ = _telegram(abrir, chats=[-1001, -1002])
        d, dormir = self.despachar(tg)
        self.assertEqual([c["data"]["chat_id"] for c in abrir.llamadas], [-1001, -1002, -1002])
        self.assertEqual(d.estado()["telegram"]["enviados"], 1)

    def test_estado_no_muestra_token(self):
        abrir = AbrirFalso(*[OSError(f"no conecta https://api.telegram.org/bot{TOKEN}/x")] * 3)
        tg, _, _ = _telegram(abrir)
        d, _ = self.despachar(tg)
        crudo = json.dumps(d.estado())
        self.assertNotIn(TOKEN, crudo)
        self.assertIn("error de red", d.estado()["telegram"]["ultimo_error"])

    def test_nombres_repetidos_se_distinguen(self):
        d = Despachador([Falso("x"), Falso("x")])
        self.assertEqual(d.canales, ["x", "x-2"])

    def test_vaciar_sin_iniciar_vence(self):
        d = Despachador([Falso()])
        d.encolar(_msg())
        self.assertFalse(d.vaciar(0.05))

    def test_detener_es_rapido_con_sleep_real(self):
        d = Despachador([Falso(fallas=99)])      # dormir = time.sleep -> usa el Event
        d.iniciar()
        d.encolar(_msg())
        time.sleep(0.05)
        t0 = time.monotonic()
        d.detener(2)
        self.assertLess(time.monotonic() - t0, 1.5)


class TestConstruccion(unittest.TestCase):
    def test_canales_segun_config(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["telegram"].update(activo=True, chats=[-1001])
        cfg["teams"].update(activo=True)
        with tempfile.TemporaryDirectory() as tmp:
            ns = construir_notificadores(cfg, Path(tmp), env={"BOT_FALLAS_TELEGRAM_TOKEN": TOKEN,
                                                               "BOT_FALLAS_TEAMS_WEBHOOK": WEBHOOK})
            self.assertEqual([n.nombre for n in ns], ["telegram", "teams", "archivo"])
            self.assertEqual(ns[2].ruta, Path(tmp) / "datos" / "avisos.jsonl")

    def test_sin_token_se_omite(self):
        cfg = copy.deepcopy(DEFAULTS)
        cfg["telegram"].update(activo=True, chats=[-1001])
        cfg["archivo"]["activo"] = False
        with self.assertLogs("bot_fallas.notificadores", "ERROR"):
            self.assertEqual(construir_notificadores(cfg, Path("."), env={}), [])

    def test_opener_http(self):
        op = opener_http("http://proxy.invalid:8080", "", 12)
        self.assertIsInstance(op, urllib.request.OpenerDirector)
        self.assertEqual(op.timeout_s, 12)
        proxies = [h for h in op.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(proxies[0].proxies.get("https"), "http://proxy.invalid:8080")


if __name__ == "__main__":
    unittest.main()
