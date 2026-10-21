"""Tests de telegram_comandos.py: procesar_update (pura) y el ciclo con red falsa."""

from __future__ import annotations

import json
import logging
import unittest
from datetime import datetime, timedelta

from bot_fallas.notificadores import ErrorEnvio
from bot_fallas.telegram_comandos import AYUDA, ComandosTelegram

TOKEN = "000000000:token-de-prueba-no-es-real"
AHORA = datetime(2026, 10, 21, 15, 0, 0)
CHAT_OK = -100111
CHAT_AJENO = -100999
# Lo que devolvería el servicio: si aparece en una respuesta a un chat ajeno, se filtraron datos.
DATO_FALLA = "AV. MATTA"


def setUpModule():
    logging.disable(logging.WARNING)


def tearDownModule():
    logging.disable(logging.NOTSET)


class FakeServicio:
    """Dobles de los métodos de ServicioFallas (SPEC §4.11) que usan los comandos."""

    def __init__(self):
        self.llamadas: list[tuple] = []
        self.casos = {"40808515": f"🔴 <b>DETALLE</b> · Caso <code>40808515</code>\n• {DATO_FALLA}"}
        self.revienta = False

    def texto_resumen(self):
        self.llamadas.append(("resumen",))
        if self.revienta:
            raise RuntimeError("fallo interno con /ruta/secreta")
        return f"Fallas activas (1)\n🔴 <code>40808515</code> · {DATO_FALLA}"

    def texto_caso(self, caso):
        self.llamadas.append(("caso", caso))
        return self.casos.get(caso, "")

    def texto_estado(self):
        self.llamadas.append(("estado",))
        return "Fuentes: casos hace 2 min"

    # El resto del contrato, por completitud.
    def salud(self):
        return {}

    def fallas_activas(self):
        return []

    def falla(self, caso):
        return None


def _upd(texto, chat=CHAT_OK, uid=1, tipo="group", hace=timedelta(seconds=5), es_bot=False):
    msg = {"message_id": uid, "chat": {"id": chat, "type": tipo},
           "from": {"id": 42, "is_bot": es_bot, "first_name": "Persona Inventada"},
           "text": texto}
    if hace is not None:
        msg["date"] = int((AHORA - hace).timestamp())
    return {"update_id": uid, "message": msg}


class AbrirFalso:
    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.llamadas: list[dict] = []

    def __call__(self, url, data, headers, timeout):
        metodo = url.rsplit("/", 1)[-1]
        self.llamadas.append({"metodo": metodo, "url": url, "data": json.loads(data), "timeout": timeout})
        if metodo == "sendMessage":
            return 200, b'{"ok": true, "result": {}}'
        r = self.respuestas.pop(0) if self.respuestas else (200, b'{"ok": true, "result": []}')
        if isinstance(r, BaseException):
            raise r
        return r

    def metodos(self):
        return [c["metodo"] for c in self.llamadas]


def _resp(obj, status=200):
    return status, json.dumps(obj).encode()


class Base(unittest.TestCase):
    def setUp(self):
        self.servicio = FakeServicio()
        self.offset = {"v": 0}
        self.esperas: list[float] = []
        self.abrir = AbrirFalso()
        self.cmd = self.nuevo()

    def nuevo(self, chats=(str(CHAT_OK),), abrir=None):
        return ComandosTelegram(TOKEN, list(chats), self.servicio,
                                lambda: self.offset["v"], lambda v: self.offset.__setitem__("v", v),
                                abrir=abrir or self.abrir, dormir=self.esperas.append,
                                reloj=lambda: AHORA)

    def responder(self, texto, **kw) -> list[tuple]:
        return self.cmd.procesar_update(_upd(texto, **kw))

    def unico(self, texto, **kw) -> str:
        r = self.responder(texto, **kw)
        self.assertEqual(len(r), 1, r)
        return r[0][1]


class TestProcesarUpdate(Base):
    def test_fallas(self):
        r = self.responder("/fallas")
        self.assertEqual(r[0][0], CHAT_OK)
        self.assertIn(DATO_FALLA, r[0][1])
        self.assertEqual(self.servicio.llamadas, [("resumen",)])

    def test_caso(self):
        self.assertIn("DETALLE", self.unico("/caso 40808515"))
        self.assertEqual(self.servicio.llamadas, [("caso", "40808515")])

    def test_caso_acepta_codigo_de_tarea(self):
        self.unico("/caso 40808515-SD19")
        self.assertEqual(self.servicio.llamadas, [("caso", "40808515")])

    def test_caso_sin_numero_explica_el_uso(self):
        txt = self.unico("/caso")
        self.assertIn("Uso", txt)
        self.assertEqual(self.servicio.llamadas, [])

    def test_caso_con_basura_no_consulta_y_escapa(self):
        txt = self.unico("/caso <b>hola</b>")
        self.assertNotIn("<b>hola", txt)
        self.assertIn("&lt;b&gt;", txt)
        self.assertEqual(self.servicio.llamadas, [])

    def test_caso_inexistente(self):
        self.assertIn("No encuentro el caso <code>12345678</code>", self.unico("/caso 12345678"))

    def test_estado(self):
        self.assertIn("Fuentes", self.unico("/estado"))

    def test_ayuda_y_start(self):
        self.assertEqual(self.unico("/ayuda"), AYUDA)
        self.assertEqual(self.unico("/start"), AYUDA)

    def test_mayusculas_y_sufijo_bot(self):
        self.assertIn(DATO_FALLA, self.unico("/FALLAS@MiBotDeFallas"))

    def test_sufijo_de_otro_bot_se_ignora_si_se_conoce_el_propio(self):
        self.cmd.usuario_bot = "MiBotDeFallas"
        self.assertEqual(self.responder("/fallas@OtroBot"), [])
        self.assertIn(DATO_FALLA, self.unico("/fallas@mibotdefallas"))

    def test_id_responde_a_cualquiera(self):
        self.assertEqual(self.unico("/id", chat=CHAT_AJENO), f"Tu chat_id es <code>{CHAT_AJENO}</code>")

    def test_chat_no_autorizado_no_recibe_datos(self):
        for comando in ("/fallas", "/caso 40808515", "/estado", "/ayuda", "/start"):
            txt = self.unico(comando, chat=CHAT_AJENO)
            self.assertIn("Chat no autorizado", txt)
            self.assertIn(str(CHAT_AJENO), txt)
            self.assertIn("telegram.chats_comandos", txt)
            self.assertNotIn(DATO_FALLA, txt)
        self.assertEqual(self.servicio.llamadas, [])

    def test_chat_autorizado_como_int_o_str(self):
        cmd = self.nuevo(chats=[CHAT_OK])
        self.assertIn(DATO_FALLA, cmd.procesar_update(_upd("/fallas"))[0][1])

    def test_texto_normal_y_bots_se_ignoran(self):
        self.assertEqual(self.responder("hola a todos"), [])
        self.assertEqual(self.responder("/fallas", es_bot=True), [])
        self.assertEqual(self.cmd.procesar_update({"update_id": 3}), [])
        self.assertEqual(self.cmd.procesar_update({"update_id": 4, "message": {"text": "/fallas"}}), [])

    def test_comando_desconocido(self):
        self.assertEqual(self.responder("/start_otro_bot"), [])            # grupo: silencio
        self.assertIn("No conozco", self.unico("/xyz", tipo="private"))

    def test_comando_viejo_se_descarta(self):
        self.assertEqual(self.responder("/fallas", hace=timedelta(hours=3)), [])
        self.assertIn(DATO_FALLA, self.unico("/fallas", hace=None))       # sin fecha: se procesa

    def test_error_del_servicio_no_filtra_traza(self):
        self.servicio.revienta = True
        with self.assertLogs("bot_fallas.telegram_comandos", "ERROR"):   # la traza va al log
            txt = self.unico("/fallas")
        self.assertIn("Error interno", txt)
        self.assertNotIn("/ruta/secreta", txt)


class TestCiclo(Base):
    def test_ciclo_responde_y_avanza_offset(self):
        self.abrir.respuestas = [
            _resp({"ok": True, "result": {"username": "MiBotDeFallas"}}),           # getMe
            _resp({"ok": True, "result": [_upd("/fallas", uid=10), _upd("/id", chat=CHAT_AJENO, uid=11)]}),
            _resp({"ok": True, "result": []}),
        ]
        self.assertEqual(self.cmd.ciclo_una_vez(), 2)
        self.assertEqual(self.cmd.usuario_bot, "MiBotDeFallas")
        self.assertEqual(self.abrir.metodos(), ["getMe", "getUpdates", "sendMessage", "sendMessage"])
        gu = self.abrir.llamadas[1]
        self.assertEqual(gu["data"], {"timeout": 25, "allowed_updates": ["message"]})
        self.assertGreater(gu["timeout"], 25)                   # el HTTP espera más que el long-poll
        envios = [c["data"] for c in self.abrir.llamadas if c["metodo"] == "sendMessage"]
        self.assertEqual([e["chat_id"] for e in envios], [CHAT_OK, CHAT_AJENO])
        self.assertEqual(envios[0]["parse_mode"], "HTML")
        self.assertEqual(self.offset["v"], 11)

        self.cmd.ciclo_una_vez()
        self.assertEqual(self.abrir.llamadas[-1]["data"]["offset"], 12)
        self.assertEqual(self.abrir.metodos().count("getMe"), 1)   # se identifica una sola vez

    def test_update_repetido_no_se_responde_dos_veces(self):
        self.offset["v"] = 10
        self.abrir.respuestas = [_resp({"ok": False}), _resp({"ok": True, "result": [_upd("/fallas", uid=10)]})]
        self.cmd.ciclo_una_vez()
        self.assertNotIn("sendMessage", self.abrir.metodos())

    def test_conflicto_409_explica_y_no_muestra_token(self):
        self.abrir.respuestas = [_resp({"ok": False}), _resp({"ok": False, "description": "Conflict"}, 409)]
        with self.assertRaises(ErrorEnvio) as cm:
            self.cmd.ciclo_una_vez()
        self.assertIn("409", str(cm.exception))
        self.assertIn("dos bots", str(cm.exception))
        self.assertNotIn(TOKEN, str(cm.exception))

    def test_bucle_backoff_sin_reventar(self):
        abrir = AbrirFalso(*[OSError("red caída")] * 50)
        cmd = self.nuevo(abrir=abrir)

        def dormir(s):
            self.esperas.append(s)
            if len(self.esperas) >= 7:
                cmd._parar.set()
        cmd._dormir = dormir
        cmd._bucle()                       # vuelve solo cuando se pide parar
        self.assertEqual(self.esperas, [5, 10, 20, 40, 60, 60, 60])

    def test_repr_sin_token(self):
        self.assertNotIn(TOKEN, repr(self.cmd))


if __name__ == "__main__":
    unittest.main()
