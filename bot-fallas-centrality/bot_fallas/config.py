"""Configuración del bot: valores por defecto + config.json + variables de entorno.

Orden de precedencia (el último gana):

    DEFAULTS  <-  config.json (deep-merge)  <-  BOT_FALLAS_PUERTO / BOT_FALLAS_TOKEN_COMPARTIDO

Decisiones que vale la pena saber antes de tocar esto:

- Los SECRETOS (token de Telegram, webhook de Teams) no se copian dentro del
  dict de configuración: se leen a pedido con `token_telegram()` /
  `webhook_teams()`. Así un `json.dumps(cfg)` en un log o en /estado nunca
  los arrastra. Lo recomendado es dejarlos en variables de entorno (`setx`),
  no en el archivo.
- Las rutas relativas se resuelven contra la carpeta DEL ARCHIVO de config,
  no contra el directorio actual: el .vbs de la carpeta Inicio de Windows
  arranca con otro directorio actual y, si no, `datos/` terminaría en
  C:\\Windows\\System32 (o donde caiga).
- `validar()` devuelve errores legibles en vez de levantar excepción: el
  despachador ve TODOS los problemas de una vez, no uno por arranque.
- Las claves que empiezan con "_" en el archivo (p. ej. "_ayuda") son
  comentarios y se ignoran; JSON no tiene comentarios.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULTS: dict = {
    "servidor": {
        "host": "127.0.0.1",
        "puerto": 8771,
        "token_compartido": "",
        "origenes_permitidos": [],
        "permitir_extensiones": True,
        "max_bytes": 52428800,
    },
    "datos_dir": "datos",
    "fuentes": {
        "casos": {"frescura_min": 30, "cuenta_ausencia": True},
        "reclamos": {"frescura_min": 30, "cuenta_ausencia": True},
        "abiertas": {"frescura_min": 30, "cuenta_ausencia": True},
        "grilla": {"frescura_min": 30, "cuenta_ausencia": False},
    },
    "filtros": {
        "min_clientes": 1,
        "min_reclamos_sin_clientes": 3,
        "incluir_programadas": False,
        "ccd": [],
        "zonales": [],
        "comunas": [],
        "empresas": [],
        "incluir_si_desconocido": True,
    },
    "niveles": {"masivo": 1000, "alta": 300, "media": 50},
    "avisos": {
        "min_intervalo_actualizacion_min": 10,
        "delta_clientes_abs": 50,
        "delta_clientes_rel": 0.25,
        "min_calles_nuevas": 3,
        "minutos_ausencia_normalizar": 15,
        "evaluaciones_ausencia_normalizar": 2,
        "expiracion_sin_datos_min": 720,
        "retener_normalizadas_h": 24,
        "resumen_al_iniciar": True,
        "max_calles_mensaje": 15,
        "avisar_normalizadas": True,
        "avisar_reposicion_parcial": True,
        "alerta_sin_datos_min": 30,
        "recalculo_periodico_s": 120,
    },
    "maestros": {"alimentadores": "", "zonal": "", "calles": "", "ratio_alimentador_completo": 0.8},
    "telegram": {
        "activo": False,
        "token": "",
        "token_env": "BOT_FALLAS_TELEGRAM_TOKEN",
        "chats": [],
        "comandos": True,
        "chats_comandos": [],
        "timeout_s": 20,
        "proxy": "",
        "ca_bundle": "",
    },
    "teams": {
        "activo": False,
        "webhook_url": "",
        "webhook_env": "BOT_FALLAS_TEAMS_WEBHOOK",
        "timeout_s": 20,
        "proxy": "",
        "ca_bundle": "",
    },
    "archivo": {"activo": True, "ruta": "datos/avisos.jsonl"},
    "log": {"ruta": "datos/bot.log", "nivel": "INFO"},
}

ENV_PUERTO = "BOT_FALLAS_PUERTO"
ENV_TOKEN_COMPARTIDO = "BOT_FALLAS_TOKEN_COMPARTIDO"

# Claves que son rutas de archivo/carpeta y se resuelven contra el dir del config.
_RUTAS = (
    ("datos_dir",),
    ("maestros", "alimentadores"),
    ("maestros", "zonal"),
    ("maestros", "calles"),
    ("archivo", "ruta"),
    ("log", "ruta"),
    ("telegram", "ca_bundle"),
    ("teams", "ca_bundle"),
)

HOSTS_LOOPBACK = ("127.0.0.1", "localhost", "::1")

_NIVELES_LOG = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Un origen es esquema + host (+ puerto), sin ruta. "*" se rechaza a propósito:
# abriría el POST a cualquier página web (ver servidor.py, CSRF).
_RE_ORIGEN = re.compile(r"^https?://[^/\s*]+$", re.IGNORECASE)


# --------------------------------------------------------------------------
# Carga
# --------------------------------------------------------------------------


def _leer_json(ruta: Path) -> dict:
    datos = ruta.read_bytes()
    # El Bloc de notas de Windows guarda UTF-8 con BOM, o ANSI (cp1252) si el
    # usuario escribió una "ñ" en una versión vieja: se aceptan los dos.
    try:
        texto = datos.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = datos.decode("cp1252", errors="replace")
    try:
        cfg = json.loads(texto)
    except json.JSONDecodeError as e:
        pista = ""
        if "escape" in e.msg.lower():
            pista = (" Las rutas de Windows en JSON llevan doble barra invertida "
                     "(C:\\\\carpeta) o barra normal (C:/carpeta).")
        raise ValueError(f"{ruta.name}: JSON inválido en línea {e.lineno}, columna {e.colno}: "
                         f"{e.msg}.{pista} Ojo también con comas al final de una lista.") from None
    if not isinstance(cfg, dict):
        raise ValueError(f"{ruta.name}: el archivo debe ser un objeto JSON {{...}}")
    return cfg


def _fusionar(dest: dict, src: Mapping, prefijo: str, desconocidas: list[str]) -> None:
    """Deep-merge de `src` sobre `dest` (in place). Las listas se REEMPLAZAN.

    Una clave que no existe en DEFAULTS se conserva (compatibilidad hacia
    adelante) pero se informa: casi siempre es un error de tipeo ("tokem") que
    de otro modo pasaría en silencio.
    """
    for k, v in src.items():
        if str(k).startswith("_"):
            continue
        ruta = f"{prefijo}{k}"
        if k not in dest:
            desconocidas.append(ruta)
            dest[k] = copy.deepcopy(v)
        elif isinstance(dest[k], dict):
            if not isinstance(v, Mapping):
                raise ValueError(f"config: '{ruta}' debe ser un objeto {{...}}, no {type(v).__name__}")
            _fusionar(dest[k], v, ruta + ".", desconocidas)
        else:
            dest[k] = copy.deepcopy(v)


def _resolver_rutas(cfg: dict, base_dir: Path) -> None:
    for claves in _RUTAS:
        nodo = cfg
        for k in claves[:-1]:
            nodo = nodo.get(k) if isinstance(nodo, dict) else None
        if not isinstance(nodo, dict):
            continue
        valor = nodo.get(claves[-1])
        if not isinstance(valor, str) or not valor.strip():
            continue
        p = Path(valor.strip()).expanduser()
        if not p.is_absolute():
            p = base_dir / p
        nodo[claves[-1]] = str(p)


def _aplicar_env(cfg: dict, env: Mapping[str, str], avisos: list[str]) -> None:
    puerto = (env.get(ENV_PUERTO) or "").strip()
    if puerto:
        try:
            cfg["servidor"]["puerto"] = int(puerto)
        except ValueError:
            # Se deja el texto tal cual para que validar() lo marque como error
            # en vez de arrancar en silencio en el puerto del archivo.
            cfg["servidor"]["puerto"] = puerto
        avisos.append(f"servidor.puerto tomado de la variable de entorno {ENV_PUERTO}")
    token = env.get(ENV_TOKEN_COMPARTIDO)
    if token is not None and token.strip():
        cfg["servidor"]["token_compartido"] = token.strip()


def cargar_config(ruta: Path | None, env: Mapping[str, str] = os.environ) -> dict:
    """DEFAULTS <- archivo <- entorno. Devuelve un dict nuevo (DEFAULTS no se toca).

    - `ruta=None` -> sólo DEFAULTS, con rutas relativas al directorio actual.
    - Archivo inexistente -> FileNotFoundError; JSON inválido o una sección
      con tipo equivocado -> ValueError con un mensaje para el despachador.
    - Agrega `_base_dir` (str, carpeta del archivo), `_ruta_config`,
      `_advertencias_carga` y `_advertencias` (ver `advertencias()`).
    - `telegram.chats_comandos` vacío queda igual a `telegram.chats`.
    """
    cfg = copy.deepcopy(DEFAULTS)
    carga: list[str] = []
    if ruta is None:
        base_dir = Path.cwd()
        carga.append("sin archivo de configuración: se usan los valores por defecto "
                     "(Telegram y Teams desactivados)")
        cfg["_ruta_config"] = ""
    else:
        ruta = Path(ruta)
        if not ruta.is_file():
            raise FileNotFoundError(f"no existe el archivo de configuración: {ruta}")
        base_dir = ruta.resolve().parent
        desconocidas: list[str] = []
        _fusionar(cfg, _leer_json(ruta), "", desconocidas)
        for clave in desconocidas:
            carga.append(f"clave desconocida en {ruta.name}: '{clave}' (¿error de tipeo?)")
        cfg["_ruta_config"] = str(ruta.resolve())

    _aplicar_env(cfg, env, carga)
    _resolver_rutas(cfg, base_dir)
    tg = cfg.get("telegram")
    if isinstance(tg, dict) and not tg.get("chats_comandos") and isinstance(tg.get("chats"), list):
        tg["chats_comandos"] = list(tg["chats"])

    cfg["_base_dir"] = str(base_dir)
    cfg["_advertencias_carga"] = carga
    cfg["_advertencias"] = advertencias(cfg, env)
    return cfg


# --------------------------------------------------------------------------
# Secretos y derivados
# --------------------------------------------------------------------------


def _secreto(valor: Any) -> str:
    # `setx VAR "valor"` a veces deja comillas pegadas; un token con comillas
    # da 404 en Telegram y el error no lo delata.
    return str(valor or "").strip().strip("\"'").strip()


def token_telegram(cfg: dict, env: Mapping[str, str] = os.environ) -> str:
    """telegram.token si está en el archivo; si no, la variable telegram.token_env."""
    tg = cfg.get("telegram") or {}
    token = _secreto(tg.get("token"))
    if token:
        return token
    nombre = str(tg.get("token_env") or "").strip()
    return _secreto(env.get(nombre)) if nombre else ""


def webhook_teams(cfg: dict, env: Mapping[str, str] = os.environ) -> str:
    """teams.webhook_url si está en el archivo; si no, la variable teams.webhook_env."""
    tm = cfg.get("teams") or {}
    url = _secreto(tm.get("webhook_url"))
    if url:
        return url
    nombre = str(tm.get("webhook_env") or "").strip()
    return _secreto(env.get(nombre)) if nombre else ""


def chats_comandos(cfg: dict) -> list:
    """Chats autorizados para comandos: chats_comandos, o los de avisos si está vacío."""
    tg = cfg.get("telegram") or {}
    return list(tg.get("chats_comandos") or tg.get("chats") or [])


# --------------------------------------------------------------------------
# Validación
# --------------------------------------------------------------------------


def _get(cfg: dict, ruta: str, defecto: Any = None) -> Any:
    nodo: Any = cfg
    for k in ruta.split("."):
        if not isinstance(nodo, dict) or k not in nodo:
            return defecto
        nodo = nodo[k]
    return nodo


def _es_numero(v: Any) -> bool:
    # bool es subclase de int: "puerto": true no es un puerto.
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num(cfg: dict, ruta: str, errores: list[str], minimo: float = 0, entero: bool = False,
         estricto: bool = False) -> None:
    v = _get(cfg, ruta)
    ok = _es_numero(v) and (not entero or float(v).is_integer())
    if ok:
        ok = v > minimo if estricto else v >= minimo
    if not ok:
        tipo = "un entero" if entero else "un número"
        cota = f"mayor que {minimo:g}" if estricto else f"≥ {minimo:g}"
        errores.append(f"{ruta} debe ser {tipo} {cota} (vino: {v!r})")


def _lista(cfg: dict, ruta: str, errores: list[str]) -> list:
    v = _get(cfg, ruta, [])
    if not isinstance(v, list):
        errores.append(f"{ruta} debe ser una lista [...] (vino: {type(v).__name__})")
        return []
    return v


def validar(cfg: dict, env: Mapping[str, str] = os.environ) -> list[str]:
    """Errores que impiden arrancar, en castellano. [] = ok.

    Las advertencias (p. ej. "ningún canal activo") NO están aquí: ver
    `advertencias()`. `env` sólo se usa para ver si hay token/webhook.
    """
    errores: list[str] = []

    # --- servidor
    puerto = _get(cfg, "servidor.puerto")
    if not (isinstance(puerto, int) and not isinstance(puerto, bool) and 1 <= puerto <= 65535):
        errores.append(f"servidor.puerto debe ser un entero entre 1 y 65535 (vino: {puerto!r})")
    host = str(_get(cfg, "servidor.host", "") or "").strip().lower()
    if host not in HOSTS_LOOPBACK:
        # Siempre loopback: el servicio recibe datos de clientes y no tiene
        # autenticación de usuarios; expuesto en la red cualquiera del
        # segmento podría inyectar fallas falsas o leer el estado.
        errores.append(f"servidor.host debe ser 127.0.0.1, localhost o ::1 "
                       f"(vino: {_get(cfg, 'servidor.host')!r}); el bot no debe escuchar en la red")
    _num(cfg, "servidor.max_bytes", errores, minimo=0, entero=True, estricto=True)
    for o in _lista(cfg, "servidor.origenes_permitidos", errores):
        if not isinstance(o, str) or not _RE_ORIGEN.match(o.strip().rstrip("/")):
            errores.append(f"servidor.origenes_permitidos: {o!r} no es un origen válido "
                           "(esquema://host[:puerto], sin ruta; '*' no se permite)")
    tc = _get(cfg, "servidor.token_compartido", "")
    if not isinstance(tc, str):
        errores.append("servidor.token_compartido debe ser texto")

    # --- fuentes
    fuentes = _get(cfg, "fuentes", {})
    if isinstance(fuentes, dict):
        for nombre in fuentes:
            _num(cfg, f"fuentes.{nombre}.frescura_min", errores, minimo=0, estricto=True)

    # --- filtros
    _num(cfg, "filtros.min_clientes", errores, minimo=0)
    _num(cfg, "filtros.min_reclamos_sin_clientes", errores, minimo=0)
    for k in ("ccd", "zonales", "comunas", "empresas"):
        for v in _lista(cfg, f"filtros.{k}", errores):
            if not isinstance(v, str):
                errores.append(f"filtros.{k}: cada valor debe ser texto (vino: {v!r})")

    # --- niveles: masivo > alta > media > 0
    niv = {k: _get(cfg, f"niveles.{k}") for k in ("masivo", "alta", "media")}
    if not all(_es_numero(v) for v in niv.values()):
        errores.append(f"niveles.masivo/alta/media deben ser números (vino: {niv})")
    elif not (niv["masivo"] > niv["alta"] > niv["media"] > 0):
        errores.append("niveles: debe cumplirse masivo > alta > media > 0 "
                       f"(vino: masivo={niv['masivo']}, alta={niv['alta']}, media={niv['media']})")

    # --- fracciones en [0, 1]
    for ruta in ("avisos.delta_clientes_rel", "maestros.ratio_alimentador_completo"):
        v = _get(cfg, ruta)
        if not (_es_numero(v) and 0 <= v <= 1):
            errores.append(f"{ruta} debe ser una fracción entre 0 y 1 (vino: {v!r}); 25 % se escribe 0.25")

    # --- avisos
    for k in ("min_intervalo_actualizacion_min", "delta_clientes_abs", "minutos_ausencia_normalizar",
              "expiracion_sin_datos_min", "retener_normalizadas_h", "alerta_sin_datos_min"):
        _num(cfg, f"avisos.{k}", errores, minimo=0)
    for k in ("min_calles_nuevas", "evaluaciones_ausencia_normalizar", "max_calles_mensaje"):
        _num(cfg, f"avisos.{k}", errores, minimo=1, entero=True)
    _num(cfg, "avisos.recalculo_periodico_s", errores, minimo=0, estricto=True)

    # --- canales
    if _get(cfg, "telegram.activo"):
        if not token_telegram(cfg, env):
            nombre = _get(cfg, "telegram.token_env") or DEFAULTS["telegram"]["token_env"]
            errores.append("telegram.activo es true pero no hay token: ponlo en la variable de "
                           f"entorno {nombre} (recomendado) o en telegram.token")
        chats = _lista(cfg, "telegram.chats", errores)
        if not chats:
            errores.append("telegram.activo es true pero telegram.chats está vacío: agrega el bot "
                           "al grupo, escribe /id y copia el chat_id")
        for c in chats:
            if not isinstance(c, (int, str)) or isinstance(c, bool) or not str(c).strip():
                errores.append(f"telegram.chats: {c!r} no es un chat_id válido")
        _lista(cfg, "telegram.chats_comandos", errores)
        _num(cfg, "telegram.timeout_s", errores, minimo=0, estricto=True)
    if _get(cfg, "teams.activo"):
        if not webhook_teams(cfg, env):
            nombre = _get(cfg, "teams.webhook_env") or DEFAULTS["teams"]["webhook_env"]
            errores.append("teams.activo es true pero no hay webhook: ponlo en la variable de "
                           f"entorno {nombre} o en teams.webhook_url")
        _num(cfg, "teams.timeout_s", errores, minimo=0, estricto=True)
    if _get(cfg, "archivo.activo") and not str(_get(cfg, "archivo.ruta", "") or "").strip():
        errores.append("archivo.activo es true pero archivo.ruta está vacío")

    # --- log
    nivel = str(_get(cfg, "log.nivel", "") or "").upper()
    if nivel not in _NIVELES_LOG:
        errores.append(f"log.nivel debe ser uno de {', '.join(_NIVELES_LOG)} (vino: {_get(cfg, 'log.nivel')!r})")

    return errores


def advertencias(cfg: dict, env: Mapping[str, str] = os.environ) -> list[str]:
    """Problemas que NO impiden arrancar pero el despachador debería ver (/estado, log)."""
    out = list(cfg.get("_advertencias_carga") or [])
    tg, tm, ar = (bool(_get(cfg, f"{c}.activo")) for c in ("telegram", "teams", "archivo"))
    if not (tg or tm or ar):
        out.append("ningún canal de aviso activo (telegram, teams, archivo): el bot detecta "
                   "fallas pero no avisa a nadie")
    elif ar and not (tg or tm):
        out.append("sólo el canal 'archivo' está activo: los avisos quedan en "
                   f"{Path(str(_get(cfg, 'archivo.ruta', ''))).name or 'el archivo'} y no llegan a nadie")
    if str(_get(cfg, "telegram.token", "") or "").strip():
        out.append("el token de Telegram está escrito en el archivo de configuración: es más seguro "
                   f"dejarlo en la variable de entorno {_get(cfg, 'telegram.token_env') or ''}".rstrip())
    if str(_get(cfg, "teams.webhook_url", "") or "").strip():
        out.append("el webhook de Teams está escrito en el archivo de configuración: quien lo lea "
                   "puede publicar en el canal; mejor en la variable de entorno "
                   f"{_get(cfg, 'teams.webhook_env') or ''}".rstrip())
    return out
