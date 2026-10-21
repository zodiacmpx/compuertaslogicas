"""Maestros locales OPCIONALES: alimentadores, zonal de despacho y calles.

Ninguno viene en el repo (es público y son datos internos de la empresa): el
usuario apunta a sus archivos en `config.json -> maestros`. Sin maestros el bot
funciona igual, sólo muestra menos contexto ("A260" en vez de "A260 Pilauco
(SE Osorno)", sin CCD ni zonal).

Lección heredada del monitor-meteo (`ingesta_casos._mapa_alimentadores`): el
maestro de alimentadores faltó en producción y el join falló para el 100% de
las filas SIN ningún error, porque un try/except se comía la ausencia del
archivo. Por eso aquí un maestro que falta o no se puede leer NO revienta el
bot, pero queda escrito en `Maestros.diagnostico` (lo expone GET /estado) y en
el log: "maestro vacío" tiene que verse, no adivinarse por el efecto lateral.

Formatos:

- Alimentadores: el de `dim_alimentador_cluster.csv` (UTF-8 con BOM):
    COD,cod_centrality,EMPRESA,ZONA,SUB-ZONA,SUBESTACIÓN,NOMBRE ALIMENTADOR,
    clientes_total,kva_total,n_comunas,comuna_dominante,pureza,flag_ambiguo,
    comuna_id,cluster,ccd
  El informe de casos usa el código Centrality ("A260") y otros archivos el
  numérico ("260"): se indexa por ambas columnas, normalizadas.

- Zonal: el de `zonal_despacho_maestro.json`:
    {"maestro": [{"COMUNA": "CUNCO", "EMPRESA": "FRONTEL",
                  "ZONAL": "CAUTIN CORDILLERA", "CCD": "NORTE"}, ...]}
  GORBEA, TEMUCO, VILLARRICA y TOLTEN aparecen en DOS zonales (y algunas
  cruzan el límite CCD Norte/Sur). No se elige por ellas: se devuelven todas,
  igual que `core/zonal_despacho.py` del monitor.

- Calles (formato PROPIO de este bot, CSV con encabezado):
    tipo,clave,calle,comuna,clientes
    sed,12345,AV. MATTA,OSORNO,40
    alimentador,A260,LOS CARRERA,OSORNO,300
  `tipo` ∈ {sed, alimentador}; `clave` = código SED o del alimentador (acepta
  "260" / "A260"); `clientes` = clientes de esa calle en esa SED/alimentador
  (opcional). Si (tipo, clave, calle, comuna) se repite, los clientes se SUMAN:
  así se puede generar desde un listado de clientes agregando sin deduplicar.
  Separador `,` `;` o tabulador.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .direcciones import clave_calle, nombre_calle
from .modelo import Calle
from .texto import decodificar, es_nulo, limpio, norm, parse_entero, slug, titulo

log = logging.getLogger(__name__)

__all__ = ["InfoAlimentador", "Maestros", "normalizar_ccd", "normalizar_alimentador",
           "normalizar_sed", "clave_comuna"]


# --------------------------------------------------------------------------
# Normalizadores de claves
# --------------------------------------------------------------------------


def normalizar_ccd(v: str) -> str:
    """'ccd_sur' / 'CCD Sur' / 'SUR' / 'sur' -> 'SUR'. Desconocido -> ''."""
    partes = set(re.split(r"[^A-Z]+", norm(v or "")))
    if "SUR" in partes:
        return "SUR"
    if "NORTE" in partes:
        return "NORTE"
    return ""


def normalizar_alimentador(v: Any) -> str:
    """'260' / 'A0260' / ' a260 ' / 260 / '260.0' -> 'A260'.

    Los códigos no numéricos se devuelven en mayúsculas y sin espacios, tal
    cual: inventarles una 'A' haría calzar alimentadores que no son.
    """
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = re.sub(r"\s+", "", str(v)).upper()
    if es_nulo(s):
        return ""
    s = re.sub(r"^(A?\d+)\.0+$", r"\1", s)
    m = re.fullmatch(r"A?0*(\d+)", s)
    if m:
        return "A" + str(int(m.group(1)))
    return s


def normalizar_sed(v: Any) -> str:
    """'SED 12345' / 'sed-012345' / 12345 -> '12345'."""
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = re.sub(r"[\s:#Nº°.-]+", "", norm(str(v)))
    if es_nulo(s):
        return ""
    s = re.sub(r"^SED", "", s)
    if s.isdigit():
        return str(int(s))
    return s


# Variantes de escritura de comunas medidas en los reportes y maestros del
# usuario (alias de `motor/routing.py` y `retrospectiva_reclamos_pec.py`). Sólo
# afectan la CLAVE de búsqueda, nunca lo que se muestra.
_ALIAS_COMUNA = {
    "PTO MONTT": "PUERTO MONTT", "PTO VARAS": "PUERTO VARAS",
    "PTO OCTAY": "PUERTO OCTAY", "STA BARBARA": "SANTA BARBARA",
    "STA JUANA": "SANTA JUANA", "CURACO VELEZ": "CURACO DE VELEZ",
    "SAN JOSE DE LA MARIQUINA": "MARIQUINA", "AISEN": "AYSEN",
    "PUERTO AYSEN": "AYSEN", "COIHAIQUE": "COYHAIQUE",
    "N IMPERIAL": "NUEVA IMPERIAL", "S J COSTA": "SAN JUAN DE LA COSTA",
    "SJ COSTA": "SAN JUAN DE LA COSTA", "T SCHMIDT": "TEODORO SCHMIDT",
    "PUERTO SAAVEDRA": "SAAVEDRA",
}


def clave_comuna(comuna: str) -> str:
    """Clave de comparación: 'Pto. Octay' = 'PUERTO OCTAY' = 'puerto octay'."""
    k = " ".join(re.sub(r"[^A-Z0-9]+", " ", norm(comuna)).split())
    return _ALIAS_COMUNA.get(k, k)


# --------------------------------------------------------------------------
# Modelo
# --------------------------------------------------------------------------


@dataclass
class InfoAlimentador:
    codigo: str            # "A260"
    nombre: str = ""       # NOMBRE ALIMENTADOR
    subestacion: str = ""
    empresa: str = ""
    comuna: str = ""       # comuna_dominante (Title Case)
    clientes_total: int | None = None
    ccd: str = ""          # "SUR" | "NORTE" | ""

    def para_mostrar(self) -> str:
        """'A260 Pilauco (SE Osorno)'; sin datos -> sólo el código."""
        s = self.codigo
        if self.nombre:
            s += f" {self.nombre}"
        if self.subestacion:
            s += f" (SE {self.subestacion})"
        return s


def _diag(ruta: str = "", configurado: bool = False) -> dict:
    return {"ruta": ruta, "configurado": configurado, "filas": 0, "ok": not configurado,
            "error": ""}


class Maestros:
    """Contenedor de los tres maestros. Todo es opcional y de sólo lectura."""

    def __init__(self, alimentadores=None, zonal=None, calles_sed=None, calles_alim=None):
        # alimentadores: dict {código: InfoAlimentador} o iterable de InfoAlimentador.
        self._alim: dict[str, InfoAlimentador] = {}
        if isinstance(alimentadores, dict):
            pares = list(alimentadores.items())
        else:
            pares = [(i.codigo, i) for i in (alimentadores or [])]
        for k, info in pares:
            for c in {normalizar_alimentador(k), normalizar_alimentador(info.codigo)}:
                if c:
                    self._alim[c] = info

        # zonal: el JSON crudo {"maestro": [...]}, la lista de filas, o un dict
        # ya indexado {comuna: [(ZONAL, CCD), ...]}.
        self._zonal: dict[str, list[tuple[str, str]]] = {}
        if isinstance(zonal, dict) and "maestro" in zonal:
            zonal = zonal["maestro"]
        if isinstance(zonal, dict):
            for comuna, lista in zonal.items():
                for z, ccd in lista:
                    self._agregar_zonal(comuna, z, ccd)
        else:
            for fila in zonal or []:
                if isinstance(fila, dict):
                    self._agregar_zonal(fila.get("COMUNA"), fila.get("ZONAL"), fila.get("CCD"))

        self._calles_sed = self._indexar_calles(calles_sed, normalizar_sed, "maestro_sed")
        self._calles_alim = self._indexar_calles(calles_alim, normalizar_alimentador,
                                                 "maestro_alimentador")
        self.diagnostico: dict = {"alimentadores": _diag(), "zonal": _diag(), "calles": _diag()}

    # ----------------------------------------------------------------- índices

    def _agregar_zonal(self, comuna, zonal, ccd) -> None:
        k = clave_comuna(limpio(comuna))
        z = limpio(zonal)
        if not k or not z:
            return
        par = (z, normalizar_ccd(limpio(ccd)))
        lista = self._zonal.setdefault(k, [])
        if par not in lista:          # sin duplicados exactos, conservando candidatos distintos
            lista.append(par)

    @staticmethod
    def _indexar_calles(datos, normalizador, fuente: str) -> dict[str, list[Calle]]:
        out: dict[str, list[Calle]] = {}
        for clave, calles in (datos or {}).items():
            k = normalizador(clave)
            if k:
                out.setdefault(k, []).extend(replace(c, fuente=fuente, reclamos=0) for c in calles)
        return out

    # ----------------------------------------------------------------- consultas

    def alimentador(self, codigo: str) -> InfoAlimentador | None:
        """Acepta 'A260', '260', 'a0260'."""
        return self._alim.get(normalizar_alimentador(codigo))

    def zonales_de_comuna(self, comuna: str) -> list[tuple[str, str]]:
        """[(ZONAL, CCD)] con CCD 'SUR'/'NORTE'. Las comunas ambiguas devuelven todos."""
        return list(self._zonal.get(clave_comuna(comuna), []))

    def calles_de_sed(self, sed: str) -> list[Calle]:
        # Copias: el agregador les suma reclamos y no debe ensuciar el maestro.
        return [replace(c) for c in self._calles_sed.get(normalizar_sed(sed), [])]

    def calles_de_alimentador(self, codigo: str) -> list[Calle]:
        return [replace(c) for c in self._calles_alim.get(normalizar_alimentador(codigo), [])]

    @property
    def vacio(self) -> bool:
        return not (self._alim or self._zonal or self._calles_sed or self._calles_alim)

    # ----------------------------------------------------------------- carga

    @classmethod
    def cargar(cls, rutas: dict | None, base_dir: Path) -> "Maestros":
        """rutas = cfg["maestros"]. '' = no cargar. Nunca levanta excepción.

        Un archivo inexistente o ilegible queda en `diagnostico` con ok=False y
        un warning en el log; el resto de los maestros se carga igual.
        """
        rutas = rutas or {}
        base_dir = Path(base_dir) if base_dir else Path.cwd()
        diag = {}
        alim: list[InfoAlimentador] = []
        zonal: list[dict] = []
        sed: dict[str, list[Calle]] = {}
        por_alim: dict[str, list[Calle]] = {}

        for nombre, lector in (("alimentadores", _leer_alimentadores), ("zonal", _leer_zonal),
                               ("calles", _leer_calles)):
            valor = limpio(rutas.get(nombre) or "")
            if not valor:
                diag[nombre] = _diag()
                continue
            ruta = Path(valor).expanduser()
            if not ruta.is_absolute():
                ruta = base_dir / ruta
            d = _diag(str(ruta), configurado=True)
            diag[nombre] = d
            if not ruta.is_file():
                d["error"] = f"no existe el archivo: {ruta.name}"
                log.warning("Maestro de %s configurado pero no existe: %s", nombre, ruta)
                continue
            try:
                datos = ruta.read_bytes()
                if nombre == "alimentadores":
                    alim, d["filas"] = lector(datos)
                elif nombre == "zonal":
                    zonal, d["filas"] = lector(datos)
                else:
                    sed, por_alim, d["filas"] = lector(datos)
                    d["seds"], d["alimentadores"] = len(sed), len(por_alim)
            except Exception as e:      # noqa: BLE001 — un maestro malo no tumba el bot
                d["error"] = f"{type(e).__name__}: {e}"
                log.warning("No se pudo leer el maestro de %s (%s): %s", nombre, ruta.name, d["error"])
                continue
            d["ok"] = d["filas"] > 0
            if not d["ok"]:
                d["error"] = "el archivo no trae filas utilizables"
                log.warning("Maestro de %s sin filas utilizables: %s", nombre, ruta.name)
            else:
                log.info("Maestro de %s cargado: %d filas (%s)", nombre, d["filas"], ruta.name)

        m = cls(alimentadores=alim, zonal=zonal, calles_sed=sed, calles_alim=por_alim)
        m.diagnostico = diag
        return m


# --------------------------------------------------------------------------
# Lectores
# --------------------------------------------------------------------------


def _filas_csv(datos: bytes | str) -> list[dict[str, str]]:
    """CSV -> filas {slug(encabezado): valor}. Separador por la primera línea.

    Excel en configuración regional chilena guarda con ';', pandas con ','.
    """
    texto, _ = decodificar(datos)
    primera = next((l for l in texto.splitlines() if l.strip()), "")
    sep = max((";", ",", "\t"), key=primera.count) if primera else ","
    lector = csv.reader(io.StringIO(texto, newline=""), delimiter=sep)
    enc: list[str] | None = None
    out = []
    for fila in lector:
        if not any(c.strip() for c in fila):
            continue
        if enc is None:
            enc = [slug(c) for c in fila]
            continue
        out.append({k: (fila[i].strip() if i < len(fila) else "") for i, k in enumerate(enc) if k})
    return out


def _entero_maestro(v: str) -> int | None:
    """'1300.0' -> 1300; '8,0' -> 8; '1.240' (miles) -> 1240.

    pandas escribe enteros como float ('1300.0'). parse_entero cubre eso y los
    miles chilenos; una fracción no nula ('12.5') se redondea.
    """
    s = limpio(v)
    if re.fullmatch(r"-?\d+[.,]\d{1,2}|-?\d+[.,]\d{4,}", s):
        try:
            return int(round(float(s.replace(",", "."))))
        except ValueError:
            return None
    return parse_entero(s)


def _leer_alimentadores(datos: bytes) -> tuple[list[InfoAlimentador], int]:
    out = []
    for f in _filas_csv(datos):
        codigo = normalizar_alimentador(f.get("cod_centrality")) or normalizar_alimentador(f.get("cod"))
        if not codigo:
            continue
        info = InfoAlimentador(
            codigo=codigo,
            nombre=limpio(f.get("nombre_alimentador")),
            subestacion=titulo(f.get("subestacion", "")),
            empresa=limpio(f.get("empresa")),
            comuna=titulo(f.get("comuna_dominante", "")),
            clientes_total=_entero_maestro(f.get("clientes_total", "")),
            ccd=normalizar_ccd(f.get("ccd", "")),
        )
        out.append(info)
        # Si COD y cod_centrality no calzan entre sí, que el código numérico
        # también encuentre la fila.
        otro = normalizar_alimentador(f.get("cod"))
        if otro and otro != codigo:
            out.append(replace(info, codigo=otro))
    return out, len(out)


def _leer_zonal(datos: bytes) -> tuple[list[dict], int]:
    texto, _ = decodificar(datos)
    crudo = json.loads(texto)
    filas = crudo.get("maestro", []) if isinstance(crudo, dict) else crudo
    if not isinstance(filas, list):
        raise ValueError("se esperaba {'maestro': [...]} o una lista de filas")
    filas = [f for f in filas if isinstance(f, dict) and limpio(f.get("COMUNA")) and limpio(f.get("ZONAL"))]
    return filas, len(filas)


_TIPO_CALLE = {"SED": "sed", "ALIMENTADOR": "alimentador", "ALIM": "alimentador",
               "ALIMENTADORES": "alimentador"}


def _leer_calles(datos: bytes) -> tuple[dict, dict, int]:
    acum: dict[tuple[str, str, str, str], Calle] = {}
    filas = 0
    for f in _filas_csv(datos):
        tipo = _TIPO_CALLE.get(norm(f.get("tipo", "")))
        if not tipo:
            continue
        clave = (normalizar_sed if tipo == "sed" else normalizar_alimentador)(f.get("clave"))
        calle = nombre_calle(f.get("calle", ""))
        if not clave or not calle:
            continue
        comuna = titulo(f.get("comuna", ""))
        clientes = _entero_maestro(f.get("clientes", ""))
        k = (tipo, clave, clave_calle(calle), clave_comuna(comuna))
        filas += 1
        if k in acum:
            c = acum[k]
            if clientes is not None:
                c.clientes = (c.clientes or 0) + clientes
            continue
        fuente = "maestro_sed" if tipo == "sed" else "maestro_alimentador"
        acum[k] = Calle(nombre=calle, comuna=comuna, reclamos=0, clientes=clientes, fuente=fuente)

    sed: dict[str, list[Calle]] = {}
    alim: dict[str, list[Calle]] = {}
    for (tipo, clave, _, _), c in acum.items():
        (sed if tipo == "sed" else alim).setdefault(clave, []).append(c)
    for d in (sed, alim):
        for lista in d.values():
            # Las calles con más clientes primero: son las que se muestran si hay que recortar.
            lista.sort(key=lambda c: (-(c.clientes or 0), c.nombre))
    return sed, alim, filas

