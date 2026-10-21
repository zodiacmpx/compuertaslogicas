"""Dirección cruda de Centrality -> nombre de calle (sin número) + comuna.

Portado de `parse_direccion` del motor de despacho (repo despacho_automatico,
`motor/routing.py`), donde se midieron los cuatro formatos sobre 6.367 tareas
abiertas reales:

    13,3%  "Lat: -37.900…, Lon: -72.564…"        coordenada, sin calle
    67,0%  "CALLE 46, LOCALIDAD, SECTOR, COMUNA"  el ÚLTIMO campo es la comuna
     8,6%  "CALLE 49 / LOCALIDAD-COMUNA / CL"     formato con slash
    23,3%  "Caupolican y O'Higgins"               sin comuna: cae al Área

Diferencias con el motor, a propósito:
  - El motor geocodifica y necesita el número de casa. El bot NO: sólo muestra
    el nombre de la calle y el número de casa es un dato personal (junto con la
    calle identifica al cliente). Por eso `Direccion.calle` ya viene SIN número.
  - El motor valida la comuna contra su diccionario de coordenadas. Aquí no
    existe ese diccionario, así que "¿es comuna?" es una heurística: el último
    campo, sin dígitos, con 3+ letras y que no sea el país.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .texto import limpio, norm

__all__ = ["Direccion", "parse_direccion", "nombre_calle", "clave_calle"]


@dataclass
class Direccion:
    cruda: str
    calle: str = ""        # nombre de calle SIN número de casa / depto / km de casa
    localidad: str = ""
    comuna: str = ""
    lat: float | None = None
    lon: float | None = None

    def tiene_calle(self) -> bool:
        return bool(self.calle)


# --------------------------------------------------------------------------
# Partir la dirección en calle / localidad / comuna
# --------------------------------------------------------------------------

# "S/N" (sin número) es habitual en direcciones rurales y lleva un slash, que
# es justo uno de los separadores de Centrality. Hay que protegerlo antes de
# partir, o la comuna termina siendo "N".
_SN = re.compile(r"\bS\s*/\s*N\b", re.IGNORECASE)
_SN_TOKEN = "\x00SN\x00"

# Coordenada exacta embebida en el campo dirección (13,3% de las tareas).
_LATLON = re.compile(
    r"(?i)lat\s*:\s*(-?\d+[.,]\d+)\s*[,;]?\s*lon\s*:\s*(-?\d+[.,]\d+)")

# Ruido de cola del último campo: "OSORNO 0" (número de calle 0 mal puesto).
_COLA = re.compile(r"\s+0$")

_PAISES = {"CHILE", "CL"}


def _parece_comuna(s: str) -> bool:
    """Heurística sin diccionario: sin dígitos, 3+ letras y que no sea el país.

    Sin el mínimo de letras, restos de parsing como "N" (de S/N) o "CL" pasaban
    por comuna. Con dígitos casi siempre es una calle con número.
    """
    s = limpio(s)
    if not s or re.search(r"\d", s):
        return False
    if norm(s) in _PAISES:
        return False
    return sum(c.isalpha() for c in s) >= 3


def _restaurar(s: str) -> str:
    return limpio(s.replace(_SN_TOKEN, "S/N"))


def parse_direccion(cruda: str, area: str = "") -> Direccion:
    """Parsea el campo Dirección (432 / reclamos / grilla).

    `area` es la pista de respaldo cuando la dirección no trae una comuna
    reconocible (formato "Caupolican y O'Higgins", coordenadas, basura).
    """
    d = Direccion(cruda=limpio(cruda))
    area = limpio(area)
    if not d.cruda:
        d.comuna = area
        return d

    m = _LATLON.search(d.cruda)
    if m:
        # Coordenada sin calle: no hay nombre que mostrar, sólo la comuna del Área.
        try:
            d.lat = float(m.group(1).replace(",", "."))
            d.lon = float(m.group(2).replace(",", "."))
        except ValueError:
            pass
        d.comuna = area
        return d

    protegida = _SN.sub(_SN_TOKEN, d.cruda)
    calle_cruda = ""
    candidata = ""

    if "/" in protegida:
        trozos = [_restaurar(t) for t in protegida.split("/")]
        calle_cruda = trozos[0]
        if len(trozos) >= 2 and trozos[1]:
            medio = trozos[1]
            if "-" in medio:
                izq, der = medio.rsplit("-", 1)
                d.localidad, candidata = limpio(izq), limpio(der)
            else:
                d.localidad = candidata = medio
    elif "," in protegida:
        trozos = [_restaurar(t) for t in protegida.split(",")]
        # El primer campo es la calle aunque venga vacío (", OSORNO"): si se
        # filtraran los vacíos antes, la comuna pasaría a ser la calle.
        calle_cruda = trozos[0]
        resto = [t for t in trozos[1:] if t]
        while resto and norm(resto[-1]) in _PAISES:
            resto.pop()
        if resto:
            candidata = _COLA.sub("", resto[-1]).strip()
            if len(resto) >= 2:
                d.localidad = resto[0]
    else:
        calle_cruda = _restaurar(protegida)
        # "AV. MATTA 1234 OSORNO": sin separadores, la comuna pegada al final
        # haría que el número de casa quedara en medio y no se pudiera quitar.
        if area and _parece_comuna(area):
            n_calle, n_area = norm(calle_cruda), norm(area)
            if n_calle.endswith(" " + n_area) and len(n_calle) > len(n_area) + 1:
                calle_cruda = calle_cruda[: len(calle_cruda) - len(area)].strip(" ,-")
                candidata = area

    candidata = candidata.strip(" .")
    if _parece_comuna(candidata):
        d.comuna = candidata
    else:
        # El último campo no era una comuna -> usar el Área. Si ese campo tenía
        # pinta de lugar, se conserva como localidad para no perderlo.
        if candidata and not d.localidad and not re.search(r"\d", candidata) \
                and norm(candidata) not in _PAISES:
            d.localidad = candidata
        d.comuna = area

    d.calle = nombre_calle(calle_cruda)
    return d


# --------------------------------------------------------------------------
# Nombre de calle para mostrar (sin número de casa)
# --------------------------------------------------------------------------

# Tipos de vía: se unifican para mostrar y para agrupar variantes.
_TIPOS = (
    (re.compile(r"\b(?:AVENIDA|AVDA|AV)\b\.?"), "AV. "),
    (re.compile(r"\b(?:PASAJE|PSJE|PJE|PJ)\b\.?"), "PJE. "),
    (re.compile(r"\b(?:POBLACI[OÓ]N|POBL|POB)\b\.?"), "POB. "),
)

# Si al quitar el número sólo queda uno de estos, el número ES el nombre:
# "PASAJE 5", "CALLE 46", "RUTA 215" son calles reales en poblaciones y rutas.
_SOLO_TIPO = {"AV.", "PJE.", "POB.", "VILLA", "CALLE", "CAMINO", "RUTA", "SECTOR",
              "CALLEJON", "CALLEJÓN", "CONDOMINIO", "LOTEO", "BARRIO"}

# Lo que identifica la vivienda dentro de la calle. Se corta desde el marcador
# (con su valor) hasta el final.
_MARCADORES = (r"DEPTO|DPTO|DEPARTAMENTO|DTO|DEP|CASA|BLOCK|BLOCKS|BLOQUE|BLK|LOCAL|"
               r"OF|OFIC|OFICINA|PISO|TORRE|LT|LOTE|SITIO|PARCELA|PARC|MZ|MANZANA")
_VALOR = r"(?:N\s*[°º]|NRO\.?|#)?\s*(?:\d+[A-Z]?|[A-Z])(?![A-Z0-9])"
_RX_MARCADOR = re.compile(rf"\b(?:{_MARCADORES})\b\.?\s*{_VALOR}")

# Después del número de casa a veces viene la localidad, no la calle:
# "LOS CARRERA 980 VILLA X". Cortar ahí no pierde nada del nombre.
_LUGARES = r"SECTOR|VILLA|BARRIO|LOCALIDAD|CONDOMINIO"

# El km de un camino rural identifica una casa, no la calle.
_RX_KM = re.compile(r"\b(?:KM|KMS|KILOMETRO|KILÓMETRO)\b\.?\s*\d+(?:[.,]\d+)?.*$")

# "N° 1234", "NRO 1234", "#1234": marcador explícito de número de casa.
_RX_NUMERO = re.compile(
    r"(?:\bN\s*[°º]|\bNRO\b\.?|\bNUM\b\.?|\bN[UÚ]MERO\b|#|\bN\b\.?)\s*\d+.*$")

_RX_SN = re.compile(r"\bS\s*/\s*N(?:RO\b\.?|[°º]|\b)|\bSIN\s+N[UÚ]MERO\b")

# Número de casa al final: "1234", "1234-B", "1234 B".
_RX_NUM_FINAL = re.compile(r"\s+\d+[A-Z]?(?:\s*-\s*[A-Z0-9]{1,4})?(?:\s+[A-Z])?\s*$")
# Número de casa seguido de un marcador de vivienda o de localidad.
_RX_NUM_MARCADOR = re.compile(
    rf"\s+\d+[A-Z]?(?:\s*-\s*[A-Z0-9]{{1,4}})?\s+(?=(?:{_MARCADORES}|{_LUGARES})\b|POB\.)")

_BORDES = " ,;:-/#"


def _tiene_letras(s: str) -> bool:
    return any(c.isalpha() for c in s)


def _recortar(s: str) -> str:
    s = " ".join(s.split()).strip(_BORDES)
    # Un punto final sobra ("LOS CARRERA."), salvo que sea de una abreviatura.
    if s.endswith(".") and s.rsplit(" ", 1)[-1] not in {"AV.", "PJE.", "POB."}:
        s = s.rstrip(".").strip(_BORDES)
    return s


def nombre_calle(calle_con_numero: str) -> str:
    """'AV. MATTA 1234' -> 'AV. MATTA'. Quita número, S/N, depto, block, casa, km.

    Regla para no comerse números que SON parte del nombre ("21 DE MAYO 450",
    "CALLE 5 NORTE 123"): sólo se quita el número que va al final o el que va
    seguido de un marcador de vivienda/localidad. Es idempotente: aplicarla dos
    veces da lo mismo (el agregador la vuelve a pasar sobre `Direccion.calle`).
    """
    s = limpio(calle_con_numero).upper()
    if not s:
        return ""
    s = _RX_SN.sub(" ", s)
    for rx, rep in _TIPOS:
        s = rx.sub(rep, s)
    s = " ".join(s.split())

    # Depto / casa / block…: cortar desde el marcador. Si el marcador va al
    # principio ("BLOCK 5 LOS AROMOS 12"), cortar dejaría vacío: quitarlo y seguir.
    m = _RX_MARCADOR.search(s)
    while m:
        antes = s[: m.start()]
        s = antes if _tiene_letras(antes) else (antes + " " + s[m.end():])
        s = " ".join(s.split())
        m = _RX_MARCADOR.search(s)

    s = _RX_KM.sub("", s)
    s = _RX_NUMERO.sub("", s)
    s = _recortar(s)
    if not _tiene_letras(s):
        return ""             # "N° 123", "1234": sólo número, no hay calle

    m = _RX_NUM_MARCADOR.search(s)
    if m:
        s = s[: m.start()]
    sin_num = _RX_NUM_FINAL.sub("", s)
    if sin_num != s:
        resto = _recortar(sin_num)
        if resto and resto not in _SOLO_TIPO and _tiene_letras(resto):
            s = sin_num
        elif not resto:
            return ""
    # "CALLE X" -> "X" (prefijo redundante), salvo "CALLE 46" donde no queda nombre.
    m = re.match(r"CALLE\s+(.+)$", s)
    if m and _tiene_letras(m.group(1)):
        s = m.group(1)
    return _recortar(s)


def clave_calle(nombre: str) -> str:
    """Clave para agrupar variantes: 'Avenida Matta 120' = 'AV. MATTA' = 'av matta'.

    Además de norm() quita la puntuación ("O'HIGGINS" = "OHIGGINS"), que en
    los reclamos se escribe de las dos formas.
    """
    n = norm(nombre_calle(nombre))
    return " ".join(re.sub(r"[^A-Z0-9 ]", "", n).split())
