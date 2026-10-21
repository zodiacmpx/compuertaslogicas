"""Utilidades de texto, fechas y números compartidas por todo el bot.

Varias de estas funciones vienen del motor de despacho (repo
despacho_automatico, `motor/centrality.py`), donde se verificaron contra los
reportes reales de Centrality. Se copian —no se importan— para que el bot no
dependa de ese repo.
"""

from __future__ import annotations

import codecs
import html
import re
import threading
import unicodedata
from datetime import datetime, timedelta

# --------------------------------------------------------------------------
# Normalización
# --------------------------------------------------------------------------


def sin_tildes(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c))


def slug(s: str) -> str:
    """'Fecha de  Creación' -> 'fecha_de_creacion'. Para encabezados."""
    s = sin_tildes(s).lower().strip()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def norm(s: str) -> str:
    """Mayúsculas, sin tildes, espacios colapsados. Para comparar valores."""
    return " ".join(sin_tildes(s).upper().split())


def limpio(v) -> str:
    """Texto recortado con espacios internos colapsados ('' si None)."""
    return " ".join(str(v).split()) if v is not None else ""


_PALABRAS_MENORES = {"de", "del", "la", "las", "los", "el", "y", "e", "en", "a"}


def titulo(s: str) -> str:
    """'PUERTO OCTAY' -> 'Puerto Octay'; 'SAN JUAN DE LA COSTA' -> 'San Juan de la Costa'."""
    palabras = limpio(s).lower().split(" ")
    out = []
    for i, p in enumerate(palabras):
        if i > 0 and p in _PALABRAS_MENORES:
            out.append(p)
        else:
            out.append(p[:1].upper() + p[1:])
    return " ".join(out).strip()


# --------------------------------------------------------------------------
# Decodificación del plaincsv de Centrality
# --------------------------------------------------------------------------

_RECUPERADOS = threading.local()


def _reparar_cp1252(err: UnicodeDecodeError):
    trozo = err.object[err.start:err.end]
    _RECUPERADOS.n = getattr(_RECUPERADOS, "n", 0) + len(trozo)
    return (trozo.decode("cp1252", errors="replace"), err.end)


codecs.register_error("bot_fallas_mixto", _reparar_cp1252)


def decodificar(datos: bytes | str) -> tuple[str, int]:
    """Decodifica el `plaincsv` de Centrality.

    VERIFICADO en el motor de despacho (2026-08-15, reporte 432): el archivo es
    UTF-8 salvo ~164 bytes sueltos en cp1252. Todo-cp1252 mojibakea el resto y
    UTF-8 estricto revienta, así que se decodifica UTF-8 y sólo los bytes malos
    se leen como cp1252. Devuelve (texto, bytes_recuperados).
    """
    if isinstance(datos, str):
        return datos.lstrip("﻿"), 0
    datos = bytes(datos).lstrip(codecs.BOM_UTF8)
    try:
        return datos.decode("utf-8"), 0
    except UnicodeDecodeError:
        pass
    _RECUPERADOS.n = 0
    texto = datos.decode("utf-8", errors="bot_fallas_mixto")
    return texto, getattr(_RECUPERADOS, "n", 0)


# --------------------------------------------------------------------------
# Valores
# --------------------------------------------------------------------------

# Centinelas que Centrality usa para "sin valor". `-infinity` aparece en las
# fechas del informe de casos (verificado en monitor-meteo, 20 filas en julio).
NULOS = {"", "-infinity", "infinity", "null", "none", "nan", "n/a", "-"}

_FMT_FECHA = (
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
)


def es_nulo(v) -> bool:
    return v is None or limpio(v).lower() in NULOS


def parse_fecha(v) -> datetime | None:
    """Fecha de Centrality -> datetime local ingenuo (sin zona).

    Acepta 'T' o espacio como separador, fracciones de segundo y offset
    ('2026-04-13 10:07:49-04', '…-03:00', '…Z'). El offset se DESCARTA: los
    horarios de Centrality ya vienen en hora local de Chile y el bot compara
    siempre contra la hora local del equipo.
    """
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if es_nulo(v):
        return None
    s = limpio(v).replace("T", " ")
    s = re.sub(r"(Z|[+-]\d{2}(:?\d{2})?)$", "", s).strip() if re.search(
        r"\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}(:?\d{2})?)$", s) else s
    for fmt in _FMT_FECHA:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_entero(v) -> int | None:
    """'1.240' / '1,240' / '1240.0' / 1240 -> 1240. None si no es número."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = limpio(v)
    if s.lower() in NULOS:
        return None
    if re.fullmatch(r"-?\d+[.,]0+", s):          # '1240.0' -> 1240
        s = re.split(r"[.,]", s)[0]
    s = s.replace(".", "").replace(",", "").replace(" ", "")
    try:
        return int(s)
    except ValueError:
        return None


_VERDADERO = {"true", "verdadero", "si", "sí", "s", "1", "yes", "y", "x"}
_FALSO = {"false", "falso", "no", "n", "0"}


def parse_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if es_nulo(v):
        return None
    s = sin_tildes(limpio(v)).lower()
    if s in _VERDADERO:
        return True
    if s in _FALSO:
        return False
    return None


def normalizar_caso(v) -> str:
    """'40808515' / '40808515.0' / ' 40808515 ' -> '40808515'."""
    s = limpio(v)
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return s.upper()


# --------------------------------------------------------------------------
# Formato para mensajes (convención chilena)
# --------------------------------------------------------------------------


def fmt_miles(n: int | None) -> str:
    """1240 -> '1.240'. None -> 's/d'."""
    if n is None:
        return "s/d"
    return f"{int(n):,}".replace(",", ".")


def fmt_hora(d: datetime | None) -> str:
    """'21-10 14:32'."""
    return d.strftime("%d-%m %H:%M") if d else "s/d"


def fmt_duracion(td: timedelta | None) -> str:
    """'35 min', '2 h 14 min', '1 d 3 h'. Negativo o None -> 's/d'."""
    if td is None:
        return "s/d"
    seg = int(td.total_seconds())
    if seg < 0:
        return "s/d"
    minutos = seg // 60
    if minutos < 60:
        return f"{minutos} min"
    horas, minutos = divmod(minutos, 60)
    if horas < 24:
        return f"{horas} h {minutos} min" if minutos else f"{horas} h"
    dias, horas = divmod(horas, 24)
    return f"{dias} d {horas} h" if horas else f"{dias} d"


def escape_html(s) -> str:
    """Escape para Telegram parse_mode=HTML (sólo &, <, >)."""
    return html.escape(str(s if s is not None else ""), quote=False)
