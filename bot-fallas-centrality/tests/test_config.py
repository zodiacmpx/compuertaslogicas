"""Tests de config.py: deep-merge, rutas relativas, entorno y validación."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from bot_fallas import config
from bot_fallas.config import DEFAULTS, advertencias, cargar_config, chats_comandos, validar

# Token con FORMA de token de Telegram pero inventado.
TOKEN_FALSO = "000000000:token-de-prueba-no-real"


def _cfg_ok(**cambios) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    for ruta, valor in cambios.items():
        nodo = cfg
        claves = ruta.split("__")
        for k in claves[:-1]:
            nodo = nodo[k]
        nodo[claves[-1]] = valor
    return cfg


class _ConArchivo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def escribir(self, datos, nombre="config.json", crudo: bytes | None = None) -> Path:
        ruta = self.dir / nombre
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(crudo if crudo is not None else json.dumps(datos).encode("utf-8"))
        return ruta


class TestCarga(_ConArchivo):
    def test_sin_archivo_son_los_defaults(self):
        cfg = cargar_config(None, env={})
        self.assertEqual(cfg["servidor"]["puerto"], 8771)
        self.assertEqual(cfg["niveles"], {"masivo": 1000, "alta": 300, "media": 50})
        self.assertTrue(any("sin archivo" in a for a in cfg["_advertencias"]))
        self.assertEqual(validar(cfg, env={}), [])

    def test_deep_merge_no_pisa_hermanos_ni_toca_defaults(self):
        ruta = self.escribir({"servidor": {"puerto": 9000},
                              "avisos": {"delta_clientes_abs": 80},
                              "fuentes": {"casos": {"frescura_min": 45}}})
        cfg = cargar_config(ruta, env={})
        self.assertEqual(cfg["servidor"]["puerto"], 9000)
        self.assertEqual(cfg["servidor"]["host"], "127.0.0.1")         # hermano intacto
        self.assertEqual(cfg["avisos"]["delta_clientes_abs"], 80)
        self.assertEqual(cfg["avisos"]["delta_clientes_rel"], 0.25)
        self.assertEqual(cfg["fuentes"]["casos"], {"frescura_min": 45, "cuenta_ausencia": True})
        self.assertFalse(cfg["fuentes"]["grilla"]["cuenta_ausencia"])
        self.assertEqual(DEFAULTS["servidor"]["puerto"], 8771)          # DEFAULTS sin mutar

    def test_listas_se_reemplazan(self):
        ruta = self.escribir({"filtros": {"ccd": ["SUR"]}})
        cfg = cargar_config(ruta, env={})
        self.assertEqual(cfg["filtros"]["ccd"], ["SUR"])
        cfg["filtros"]["ccd"].append("X")
        self.assertEqual(DEFAULTS["filtros"]["ccd"], [])

    def test_claves_con_guion_bajo_son_comentarios_y_desconocidas_se_avisan(self):
        ruta = self.escribir({"_ayuda": "comentario", "telegram": {"_ayuda": "x", "tokn": "z"}})
        cfg = cargar_config(ruta, env={})
        self.assertNotIn("_ayuda", cfg["telegram"])
        self.assertTrue(any("telegram.tokn" in a for a in cfg["_advertencias"]))

    def test_rutas_relativas_al_dir_del_archivo(self):
        sub = self.dir / "conf"
        ruta = self.escribir({"maestros": {"zonal": "maestros/zonal.json"},
                              "telegram": {"ca_bundle": "ca/empresa.pem"}}, "conf/config.json")
        cfg = cargar_config(ruta, env={})
        base = sub.resolve()
        self.assertEqual(Path(cfg["_base_dir"]), base)
        self.assertEqual(Path(cfg["datos_dir"]), base / "datos")
        self.assertEqual(Path(cfg["archivo"]["ruta"]), base / "datos" / "avisos.jsonl")
        self.assertEqual(Path(cfg["log"]["ruta"]), base / "datos" / "bot.log")
        self.assertEqual(Path(cfg["maestros"]["zonal"]), base / "maestros" / "zonal.json")
        self.assertEqual(Path(cfg["telegram"]["ca_bundle"]), base / "ca" / "empresa.pem")
        self.assertEqual(cfg["maestros"]["alimentadores"], "")          # vacío sigue vacío
        self.assertEqual(cfg["teams"]["ca_bundle"], "")

    def test_ruta_absoluta_se_respeta(self):
        absoluta = str((self.dir / "otro" / "avisos.jsonl").resolve())
        ruta = self.escribir({"archivo": {"ruta": absoluta}})
        self.assertEqual(cargar_config(ruta, env={})["archivo"]["ruta"], absoluta)

    def test_bom_y_cp1252_se_aceptan(self):
        con_bom = b"\xef\xbb\xbf" + json.dumps({"servidor": {"puerto": 9100}}).encode()
        self.assertEqual(cargar_config(self.escribir(None, crudo=con_bom), env={})["servidor"]["puerto"], 9100)
        ansi = '{"filtros": {"comunas": ["Peñaflor"]}}'.encode("cp1252")
        cfg = cargar_config(self.escribir(None, "ansi.json", crudo=ansi), env={})
        self.assertEqual(cfg["filtros"]["comunas"], ["Peñaflor"])

    def test_json_invalido_da_mensaje_util(self):
        ruta = self.escribir(None, crudo=b'{"maestros": {"zonal": "C:\\datos\\z.json"}}')
        with self.assertRaises(ValueError) as cm:
            cargar_config(ruta, env={})
        self.assertIn("línea 1", str(cm.exception))
        self.assertIn("doble barra", str(cm.exception))

    def test_seccion_con_tipo_equivocado(self):
        with self.assertRaises(ValueError):
            cargar_config(self.escribir({"telegram": "si"}), env={})

    def test_archivo_inexistente(self):
        with self.assertRaises(FileNotFoundError):
            cargar_config(self.dir / "no_existe.json", env={})


class TestEntorno(_ConArchivo):
    def test_env_puerto_y_token_compartido(self):
        ruta = self.escribir({"servidor": {"puerto": 9000}})
        cfg = cargar_config(ruta, env={"BOT_FALLAS_PUERTO": "9555",
                                       "BOT_FALLAS_TOKEN_COMPARTIDO": " secreto-local "})
        self.assertEqual(cfg["servidor"]["puerto"], 9555)
        self.assertEqual(cfg["servidor"]["token_compartido"], "secreto-local")

    def test_env_puerto_invalido_lo_marca_validar(self):
        cfg = cargar_config(None, env={"BOT_FALLAS_PUERTO": "ochenta"})
        self.assertTrue(any("servidor.puerto" in e for e in validar(cfg, env={})))

    def test_token_desde_archivo_o_env(self):
        cfg = _cfg_ok()
        self.assertEqual(config.token_telegram(cfg, env={}), "")
        self.assertEqual(config.token_telegram(cfg, env={"BOT_FALLAS_TELEGRAM_TOKEN": f'"{TOKEN_FALSO}"'}),
                         TOKEN_FALSO)                                   # comillas de setx fuera
        cfg["telegram"]["token_env"] = "MI_TOKEN"
        self.assertEqual(config.token_telegram(cfg, env={"MI_TOKEN": TOKEN_FALSO}), TOKEN_FALSO)
        cfg["telegram"]["token"] = "del-archivo"
        self.assertEqual(config.token_telegram(cfg, env={"MI_TOKEN": TOKEN_FALSO}), "del-archivo")

    def test_token_no_se_copia_al_dict(self):
        cfg = cargar_config(None, env={"BOT_FALLAS_TELEGRAM_TOKEN": TOKEN_FALSO})
        self.assertNotIn(TOKEN_FALSO, json.dumps(cfg))

    def test_webhook_desde_env(self):
        cfg = _cfg_ok()
        self.assertEqual(config.webhook_teams(cfg, env={"BOT_FALLAS_TEAMS_WEBHOOK": "https://x.invalid/w"}),
                         "https://x.invalid/w")


class TestChatsComandos(_ConArchivo):
    def test_vacio_usa_chats(self):
        ruta = self.escribir({"telegram": {"chats": [-1001, "-1002"]}})
        cfg = cargar_config(ruta, env={})
        self.assertEqual(cfg["telegram"]["chats_comandos"], [-1001, "-1002"])
        self.assertEqual(chats_comandos(cfg), [-1001, "-1002"])

    def test_explicito_se_respeta(self):
        ruta = self.escribir({"telegram": {"chats": [-1001], "chats_comandos": [555]}})
        self.assertEqual(chats_comandos(cargar_config(ruta, env={})), [555])

    def test_helper_con_dict_crudo(self):
        self.assertEqual(chats_comandos({"telegram": {"chats": [1], "chats_comandos": []}}), [1])


class TestValidar(unittest.TestCase):
    def errores(self, env=None, **cambios) -> list[str]:
        return validar(_cfg_ok(**cambios), env=env or {})

    def assertError(self, texto: str, env=None, **cambios):
        errs = self.errores(env=env, **cambios)
        self.assertTrue(any(texto in e for e in errs), f"esperaba '{texto}' en {errs}")

    def test_defaults_validos(self):
        self.assertEqual(self.errores(), [])

    def test_puerto(self):
        for malo in (0, 70000, "8771", True, None, 80.5):
            self.assertError("servidor.puerto", servidor__puerto=malo)
        self.assertEqual(self.errores(servidor__puerto=1), [])
        self.assertEqual(self.errores(servidor__puerto=65535), [])

    def test_host_siempre_loopback(self):
        for malo in ("0.0.0.0", "192.168.1.10", "", "::"):
            self.assertError("servidor.host", servidor__host=malo)
        for bueno in ("127.0.0.1", "localhost", "::1", "LOCALHOST"):
            self.assertEqual(self.errores(servidor__host=bueno), [], bueno)

    def test_telegram_activo_sin_token(self):
        self.assertError("no hay token", telegram__activo=True, telegram__chats=[-1001])

    def test_telegram_activo_con_token_env_ok(self):
        self.assertEqual(self.errores(env={"BOT_FALLAS_TELEGRAM_TOKEN": TOKEN_FALSO},
                                      telegram__activo=True, telegram__chats=[-1001]), [])

    def test_telegram_activo_sin_chats(self):
        self.assertError("telegram.chats", env={"BOT_FALLAS_TELEGRAM_TOKEN": TOKEN_FALSO},
                         telegram__activo=True)

    def test_mensaje_de_token_no_incluye_el_token(self):
        errs = self.errores(env={"BOT_FALLAS_TELEGRAM_TOKEN": TOKEN_FALSO}, telegram__activo=True)
        self.assertFalse(any(TOKEN_FALSO in e for e in errs))

    def test_teams_activo_sin_webhook(self):
        self.assertError("webhook", teams__activo=True)
        self.assertEqual(self.errores(env={"BOT_FALLAS_TEAMS_WEBHOOK": "https://x.invalid/w"},
                                      teams__activo=True), [])

    def test_niveles_ordenados(self):
        self.assertError("masivo > alta > media > 0", niveles={"masivo": 300, "alta": 1000, "media": 50})
        self.assertError("masivo > alta > media > 0", niveles={"masivo": 1000, "alta": 300, "media": 0})
        self.assertError("masivo > alta > media > 0", niveles={"masivo": 1000, "alta": 300, "media": 300})
        self.assertError("niveles", niveles={"masivo": "mil", "alta": 300, "media": 50})

    def test_fracciones(self):
        self.assertError("avisos.delta_clientes_rel", avisos__delta_clientes_rel=25)
        self.assertError("avisos.delta_clientes_rel", avisos__delta_clientes_rel=-0.1)
        self.assertError("maestros.ratio_alimentador_completo", maestros__ratio_alimentador_completo=1.5)
        self.assertEqual(self.errores(avisos__delta_clientes_rel=0, maestros__ratio_alimentador_completo=1), [])

    def test_origenes(self):
        self.assertError("origenes_permitidos", servidor__origenes_permitidos=["*"])
        self.assertError("origenes_permitidos", servidor__origenes_permitidos=["https://h.invalid/ruta"])
        self.assertError("origenes_permitidos", servidor__origenes_permitidos="https://h.invalid")
        self.assertEqual(self.errores(servidor__origenes_permitidos=["https://h.invalid:8443/"]), [])

    def test_otros_numeros(self):
        self.assertError("fuentes.casos.frescura_min", fuentes__casos__frescura_min=0)
        self.assertError("avisos.evaluaciones_ausencia_normalizar", avisos__evaluaciones_ausencia_normalizar=0)
        self.assertError("avisos.recalculo_periodico_s", avisos__recalculo_periodico_s=0)
        self.assertError("servidor.max_bytes", servidor__max_bytes=0)
        self.assertError("log.nivel", log__nivel="CHARLATAN")

    def test_varios_errores_a_la_vez(self):
        errs = self.errores(servidor__puerto=0, servidor__host="0.0.0.0", teams__activo=True)
        self.assertGreaterEqual(len(errs), 3)


class TestAdvertencias(unittest.TestCase):
    def test_ningun_canal_activo_es_advertencia_no_error(self):
        cfg = _cfg_ok(archivo__activo=False)
        self.assertEqual(validar(cfg, env={}), [])
        self.assertTrue(any("ningún canal" in a for a in advertencias(cfg, env={})))

    def test_solo_archivo(self):
        self.assertTrue(any("sólo el canal 'archivo'" in a for a in advertencias(_cfg_ok(), env={})))

    def test_token_en_archivo_se_advierte_sin_mostrarlo(self):
        avs = advertencias(_cfg_ok(telegram__token=TOKEN_FALSO, telegram__activo=True), env={})
        self.assertTrue(any("token de Telegram" in a for a in avs))
        self.assertFalse(any(TOKEN_FALSO in a for a in avs))


class TestEjemplo(unittest.TestCase):
    """config.example.json debe ser JSON válido, cubrir DEFAULTS y ser seguro."""

    RUTA = Path(__file__).resolve().parent.parent / "config.example.json"

    def test_ejemplo_cubre_defaults_y_valida(self):
        crudo = json.loads(self.RUTA.read_text(encoding="utf-8"))

        def claves(d, prefijo=""):
            for k, v in d.items():
                if k.startswith("_"):
                    continue
                yield prefijo + k
                if isinstance(v, dict):
                    yield from claves(v, prefijo + k + ".")

        self.assertEqual(set(claves(crudo)), set(claves(DEFAULTS)))
        cfg = cargar_config(self.RUTA, env={})
        self.assertEqual(validar(cfg, env={}), [])
        self.assertFalse(cfg["telegram"]["activo"])
        self.assertEqual(cfg["telegram"]["token"], "")
        self.assertEqual(cfg["telegram"]["chats"], [])
        self.assertNotIn("saesa" + ".cl", self.RUTA.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
