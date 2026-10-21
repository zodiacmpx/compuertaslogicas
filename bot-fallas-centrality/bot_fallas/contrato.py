"""Contrato de entrada: CSV de Centrality / grilla capturada -> Snapshot.

Es la ÚNICA definición de cómo se leen las cuatro fuentes. Misma filosofía que
`ingesta/casos_contrato.py` y `tareas_contrato.py` del monitor-meteo: los
defectos reales del archivo viven aquí y no se repiten en cada consumidor.

Fuentes:
  casos     "Informe de casos en periodos" (el que hoy baja un RPA). Una fila por
            (caso, alimentador). Trae Clientes Interrumpidos.
  reclamos  Bandeja de reclamos del DMS (export reclamos.saesa@<fecha>). Una
            fila por ticket. Trae NIS y Dirección.
  abiertas  Reporte 432 "Tareas abiertas". Una fila por tarea; sólo las de
            emergencia (########-D#/-SD#/-R#) son fallas.
  grilla    Lo que el despachador tiene a la vista en una grilla ExtJS. Nunca es
            el universo completo (se puede filtrar o paginar).

Defectos verificados que este módulo absorbe (ver SPEC §1):
  - Encabezados con doble espacio ('Fecha de  Creacion'), con y sin tildes,
    y con íconos en vez de texto (bandeja): se mapea por SINÓNIMOS sobre
    slug(encabezado), y la bandeja tiene un respaldo POSICIONAL de 21 columnas.
  - Encoding mixto: UTF-8 con bytes sueltos cp1252 (texto.decodificar).
  - Separador `;` o `,` (y tabulador cuando alguien pega desde Excel).
  - Comillas con comas y saltos de línea embebidos en el texto libre.
  - '-infinity' como fecha: None, la fila NO se descarta.
  - 'Origen interrupcion' con espacios finales inconsistentes.
  - 'Estado' del 432 con sufijo ' [X]'.
  - El export de la bandeja trae líneas de título antes del encabezado (el
    lector del monitor-meteo usa skiprows=4): se busca el encabezado en las
    primeras líneas en vez de asumir que es la primera.

PRIVACIDAD: las columnas con nombres, teléfonos, correos, comentarios u
observaciones NUNCA se mapean (ni por sinónimo ni por contención), así que su
valor ni siquiera se copia al Registro. La `Descripción` del 432 sólo se lee
para extraer el motivo y se descarta.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from typing import Any

from .maestros import normalizar_alimentador, normalizar_sed
from .modelo import FUENTES_CSV, Registro, Snapshot
from .texto import (decodificar, es_nulo, limpio, norm, normalizar_caso, parse_bool,
                    parse_entero, parse_fecha, slug)

__all__ = ["CAMPOS_UTILES", "ErrorContrato", "parse_csv", "parse_grilla", "caso_de_tarea",
           "motivo_de_descripcion", "peligro_de", "mapear_encabezados", "COLUMNAS_POSICIONALES"]


class ErrorContrato(ValueError):
    """El CSV / la grilla no sirve (sin columna caso / tarea, vacío, no es CSV)."""


# Canónicos que conviene tener por fuente. Lo que falte se informa en
# `Snapshot.faltantes` para que el despachador sepa qué columna agregar al reporte.
CAMPOS_UTILES: dict[str, list[str]] = {
    "casos": ["caso", "alimentador", "clientes", "inicio", "cierre", "origen", "causa",
              "componente"],
    "reclamos": ["caso", "id", "nis", "direccion", "motivo", "inicio", "peligro",
                 "en_interrupcion", "area", "jerarquia", "empresa"],
    "abiertas": ["tarea", "direccion", "nis", "estado", "descripcion", "area", "inicio"],
    "grilla": ["caso", "direccion", "nis", "clientes", "motivo"],
}

# Orden exacto de las 21 columnas de la bandeja, tal como la lee posicionalmente
# el código del usuario (CLAIM_COLUMNS de retrospectiva_reclamos_pec.py).
COLUMNAS_POSICIONALES = (
    "id", "caso", "empresa", "direccion", "nis", "cliente", "telefono", "descripcion",
    "telefonista", "comentarios", "motivo", "fecha", "brigadas", "resolucion",
    "motivo_cierre", "fecha_cierre", "peligro", "en_interrupcion", "sensibilidad", "area",
    "jerarquia",
)
# nombre interno posicional -> canónico. Lo que no está aquí (cliente, teléfono,
# telefonista, comentarios, descripción, brigadas…) se ignora a propósito.
_POSICIONAL_A_CANONICO = {
    "id": "id", "caso": "caso", "empresa": "empresa", "direccion": "direccion", "nis": "nis",
    "motivo": "motivo", "fecha": "inicio", "fecha_cierre": "cierre", "peligro": "peligro",
    "en_interrupcion": "en_interrupcion", "sensibilidad": "sensibilidad", "area": "area",
    "jerarquia": "jerarquia",
}

_SINONIMOS: dict[str, tuple[str, ...]] = {
    "caso": ("caso", "n_caso", "numero_caso", "nro_caso", "id_caso", "caso_dms",
             "n_de_caso", "numero_de_caso", "nro_de_caso", "no_caso"),
    "alimentador": ("codigo_de_alimentador", "alimentador", "cod_alimentador",
                    "codigo_alimentador"),
    "clientes": ("clientes_interrumpidos", "clientes_afectados", "clientes", "n_clientes",
                 "cant_clientes", "cantidad_clientes", "clientes_sin_suministro"),
    "inicio": ("fecha_de_creacion", "fecha_creacion", "fecha", "fecha_inicio", "inicio",
               "fecha_y_hora_de_creacion"),
    "cierre": ("fecha_de_cierre", "fecha_cierre", "cierre", "fecha_y_hora_de_cierre",
               "fecha_normalizacion", "fecha_reposicion"),
    "direccion": ("direccion", "direccion_cliente", "direccion_suministro", "domicilio"),
    "nis": ("nis", "numero_servicio", "numero_de_servicio", "n_servicio", "nro_servicio",
            "suministro"),
    "area": ("area", "comuna_area"),
    "comuna": ("comuna",),
    "empresa": ("empresa", "empresa_afectada"),
    "motivo": ("motivo", "motivo_reclamo", "casuistica"),
    "causa": ("causa_sec", "causa"),
    "componente": ("componente",),
    "origen": ("origen_interrupcion", "origen"),
    "tipo_instalacion": ("tipo_instalacion",),
    "jerarquia": ("jerarquia_de_red", "jerarquia"),
    "sed": ("sed", "subestacion_distribucion", "codigo_sed"),
    "sensibilidad": ("sensibilidad",),
    "peligro": ("nivel_de_peligro", "peligro"),
    "en_interrupcion": ("en_interrupcion",),
    "estado": ("estado", "estado_caso", "estado_reclamo"),
    "id": ("id", "ticket", "n_ticket", "reclamo", "id_reclamo"),
    "tarea": ("tarea", "n_tarea"),
    "descripcion": ("descripcion",),
}
_EXACTO = {s: c for c, ss in _SINONIMOS.items() for s in ss}
# Mismo sinónimo sin guiones bajos: las claves de una grilla ExtJS suelen venir
# en camelCase ('fechaCreacion' -> slug 'fechacreacion').
_COMPACTO = {s.replace("_", ""): c for c, ss in _SINONIMOS.items() for s in ss}

# Sólo el 432 identifica el caso por la tarea; sólo 432/grilla traen la
# descripción de donde sale el motivo.
_SOLO_EN = {"tarea": {"abiertas", "grilla"}, "descripcion": {"abiertas", "grilla"}}
_INICIO_RESPALDO = "ingreso_a_centrality"

# Columnas que NUNCA se mapean: datos personales o texto libre que los trae.
_PROHIBIDO_EXACTO = {"cliente", "nombre", "telefono", "correo", "email", "telefonista",
                     "comentarios", "comentario", "descripcion_despacho", "brigadas", "brigada",
                     "resolucion", "rut", "fono", "celular"}
_PROHIBIDO_CONTIENE = ("nombre", "telefon", "fono", "correo", "email", "e_mail", "comentario",
                       "observacion", "analista", "celular", "contacto")
_PROHIBIDO_TOKEN = {"rut", "movil", "mail"}


def _prohibido(s: str) -> bool:
    if s in _PROHIBIDO_EXACTO:
        return True
    if any(p in s for p in _PROHIBIDO_CONTIENE):
        return True
    return bool(_PROHIBIDO_TOKEN & set(s.split("_")))


def _por_contencion(s: str) -> str | None:
    """Reglas de contención acotadas, sólo para encabezados sin sinónimo exacto."""
    compacto = s.replace("_", "")
    if "client" in s and ("interrump" in s or "afectad" in s):
        return "clientes"
    if "alimentador" in s and not any(x in s for x in ("client", "kva", "total", "cant")):
        return "alimentador"
    if "peligro" in s:
        return "peligro"
    if "interrupc" in s and not any(x in s for x in ("origen", "tipo", "causa", "duracion",
                                                      "fecha", "hora", "client")):
        return "en_interrupcion"
    if "jerarq" in s:
        return "jerarquia"
    if "direcc" in s:
        return "direccion"
    # 'nis' como palabra, no como substring: 'administrador' contiene "nis".
    if "nis" in s.split("_") or any(x in compacto for x in ("numeroservicio",
                                                              "numerodeservicio",
                                                              "nroservicio")) \
            or re.search(r"(^|_)n_servicio($|_)", s):
        return "nis"
    return None


def _canonico(encabezado: Any, fuente: str) -> tuple[str | None, int]:
    """(canónico, prioridad) de un encabezado. Prioridad: 0 exacto, 1 contención."""
    s = slug(str(encabezado or ""))
    if not s or _prohibido(s):
        return None, 9
    c = _EXACTO.get(s) or _COMPACTO.get(s.replace("_", ""))
    prioridad = 0
    if c is None:
        c, prioridad = _por_contencion(s), 1
    if c is None or fuente not in _SOLO_EN.get(c, {fuente}):
        return None, 9
    return c, prioridad


def mapear_encabezados(encabezados: list[str], fuente: str) -> dict[int, str]:
    """{índice de columna: canónico}. Un canónico se asigna a UNA sola columna:
    primero los sinónimos exactos (en orden de columna), luego la contención.
    'Ingreso a Centrality' sólo hace de inicio si no hay otra fecha de inicio.
    """
    candidatos = [(_canonico(h, fuente), i) for i, h in enumerate(encabezados)]
    mapa: dict[int, str] = {}
    usados: set[str] = set()
    for nivel in (0, 1):
        for (c, prioridad), i in candidatos:
            if c and prioridad == nivel and c not in usados and i not in mapa:
                mapa[i] = c
                usados.add(c)
    if "inicio" not in usados:
        for i, h in enumerate(encabezados):
            if i not in mapa and slug(str(h or "")) == _INICIO_RESPALDO:
                mapa[i] = "inicio"
                break
    return mapa


# --------------------------------------------------------------------------
# Valores
# --------------------------------------------------------------------------

# Familia de emergencia del 432: 8 dígitos + etapa (D/SD/R) + secuencial de
# reintento. El prefijo ES el número de caso (verificado en el monitor-meteo:
# los 2.568 casos del informe tienen 8 dígitos). Las otras familias
# (#######-IERD, ##########-##-EnlaC, mb-…, T#######, L-#-#) no son fallas.
_RX_TAREA_EMERGENCIA = re.compile(r"(\d{8})-(?:SD|D|R)\d+", re.IGNORECASE)


def caso_de_tarea(codigo: str) -> str | None:
    """'40808515-SD19' -> '40808515'; cualquier otra familia -> None."""
    m = _RX_TAREA_EMERGENCIA.fullmatch(limpio(codigo))
    return m.group(1) if m else None


_RX_MOTIVO = re.compile(r"(?i)\bmotivo\s*:\s*([^\r\n]*)")
# La Descripción del 432 es texto libre con datos de clientes (el monitor-meteo
# midió nombres, correos, teléfonos y RUT). El motivo se corta ANTES de
# cualquier cosa que parezca un dato personal, como defensa en profundidad.
_RX_FIN_MOTIVO = re.compile(r"\.(?:\s|$)|[;|]|\s-\s")
_RX_DATO_PERSONAL = re.compile(
    r"(?i)\b(?:cliente|clienta|sr|sra|srta|fono|tel[eé]fono|tel|cel|celular|nis|rut|"
    r"direcci[oó]n|contacto|nombre|correo|email|obs|observaci[oó]n)\b|\+?\d[\d\s.-]{4,}|[^\s@]*@")


def motivo_de_descripcion(desc: str) -> str:
    """'Motivo: Sin Suministro. Cliente…' -> 'Sin Suministro'."""
    m = _RX_MOTIVO.search(str(desc or ""))
    if not m:
        return ""
    s = _RX_FIN_MOTIVO.split(m.group(1), 1)[0]
    s = _RX_DATO_PERSONAL.split(s, 1)[0]
    s = limpio(s).strip(" ,:-")
    if len(s) > 60:
        s = s[:60].rsplit(" ", 1)[0]
    return s


def peligro_de(valor: Any) -> bool | None:
    """Con peligro -> True, Sin peligro -> False, Requiere inspección -> None."""
    if isinstance(valor, bool):
        return valor
    if es_nulo(valor):
        return None
    n = norm(str(valor))
    if "REQUIERE" in n or "INSPECC" in n:
        return None
    if n.startswith("SIN ") or "SIN PELIGRO" in n:
        return False
    if n.startswith("CON ") or "CON PELIGRO" in n or n == "PELIGRO":
        return True
    return parse_bool(valor)


def _texto(v: Any) -> str:
    if isinstance(v, (dict, list, tuple)):
        return ""
    return "" if es_nulo(v) else limpio(v)


def _nis(v: Any) -> str:
    if isinstance(v, bool) or isinstance(v, (dict, list)):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = _texto(v)
    return re.sub(r"^(\d+)\.0+$", r"\1", s)


def _caso(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if es_nulo(v) or isinstance(v, (bool, dict, list)):
        return ""
    c = normalizar_caso(v)
    # Una grilla puede mostrar la tarea en la columna "Caso": quedarse con el caso.
    return caso_de_tarea(c) or c


def _estado(v: Any) -> str:
    """Quita el sufijo ' [X]' del 432 ('En ejecución [X]' = 'En ejecución')."""
    return re.sub(r"\s*\[X\]\s*$", "", _texto(v), flags=re.IGNORECASE).strip()


_TAREA_CERRADA = {"FINALIZADA", "CANCELADA", "CANCELADA POR OTRA BRIGADA",
                  "FINALIZADA POR OTRA BRIGADA", "RECHAZADA"}
_RECLAMO_CERRADO = {"CERRADO", "CERRADA", "RESUELTO", "RESUELTA", "ANULADO", "ANULADA",
                    "FINALIZADO", "FINALIZADA"}


def _registro(fuente: str, v: dict[str, Any]) -> Registro | None:
    """Valores ya mapeados a canónicos -> Registro. None = fila sin caso."""
    tarea = _texto(v.get("tarea"))
    if fuente == "abiertas":
        caso = caso_de_tarea(tarea) or ""
        id_ = tarea
    else:
        caso = _caso(v.get("caso"))
        if not caso and tarea:
            caso = caso_de_tarea(tarea) or ""
        id_ = _texto(v.get("id")) or tarea
    if not caso:
        return None

    clientes = parse_entero(v.get("clientes"))
    if clientes is not None and clientes < 0:
        clientes = None
    estado = _estado(v.get("estado"))
    motivo = _texto(v.get("motivo"))
    if not motivo and fuente in ("abiertas", "grilla"):
        motivo = motivo_de_descripcion(v.get("descripcion") or "")
    origen = _texto(v.get("origen"))
    cierre = parse_fecha(v.get("cierre"))

    n_estado = norm(estado)
    if fuente == "casos":
        cerrado = cierre is not None          # sin fecha de cierre = caso abierto
    elif fuente == "abiertas":
        cerrado = n_estado in _TAREA_CERRADA
    elif fuente == "reclamos":
        cerrado = cierre is not None or n_estado in _RECLAMO_CERRADO
    else:
        cerrado = cierre is not None or n_estado in (_RECLAMO_CERRADO | _TAREA_CERRADA)

    return Registro(
        fuente=fuente,
        caso=caso,
        id=id_,
        alimentador=normalizar_alimentador(v.get("alimentador")),
        clientes=clientes,
        nis=_nis(v.get("nis")),
        direccion=_texto(v.get("direccion")),
        area=_texto(v.get("area")),
        comuna=_texto(v.get("comuna")),
        empresa=_texto(v.get("empresa")),
        motivo=motivo,
        causa=_texto(v.get("causa")),
        componente=_texto(v.get("componente")),
        origen=origen,
        tipo_instalacion=_texto(v.get("tipo_instalacion")),
        jerarquia=_texto(v.get("jerarquia")),
        sed=normalizar_sed(v.get("sed")),
        sensibilidad=_texto(v.get("sensibilidad")),
        peligro=peligro_de(v.get("peligro")),
        en_interrupcion=parse_bool(v.get("en_interrupcion")),
        estado=estado,
        cerrado=cerrado,
        inicio=parse_fecha(v.get("inicio")),
        cierre=cierre,
        # 'Programada' / 'Programada ' / 'PROGRAMADA - corte': trabajo programado.
        programada=fuente == "casos" and norm(origen).startswith("PROGRAMADA"),
    )


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

_SEPARADORES = (";", ",", "\t")
_MAX_LINEAS_PREAMBULO = 10
_RX_SALTO = re.compile(r"\r\n|\n|\r")


def _separador(linea: str) -> str:
    """El que más aparece en la línea; empate -> ';' (el de Centrality). Ninguno -> ','."""
    sep = max(_SEPARADORES, key=linea.count)
    return sep if linea.count(sep) else ","


def _celdas(linea: str, sep: str) -> list[str]:
    try:
        return next(csv.reader([linea], delimiter=sep))
    except (StopIteration, csv.Error):
        return []


def _lineas_candidatas(texto: str) -> list[tuple[int, str]]:
    """(offset, línea) de las primeras líneas no vacías, para buscar el encabezado."""
    out, pos = [], 0
    for m in _RX_SALTO.finditer(texto):
        linea = texto[pos:m.start()]
        if linea.strip():
            out.append((pos, linea))
            if len(out) >= _MAX_LINEAS_PREAMBULO:
                return out
        pos = m.end()
    if pos < len(texto) and texto[pos:].strip() and len(out) < _MAX_LINEAS_PREAMBULO:
        out.append((pos, texto[pos:]))
    return out


def _para_mensaje(encabezados: list[str]) -> str:
    """Primeros 12 encabezados para el mensaje de error, sin números largos.

    Si el archivo no tenía encabezado, la "primera línea" son datos: se tapan
    las secuencias de dígitos (NIS, teléfonos) antes de que el error llegue
    al log o a la extensión.
    """
    vistos = []
    for h in encabezados[:12]:
        h = re.sub(r"\d[\d\s.-]{3,}\d", "#", limpio(h))
        vistos.append(h[:30] + ("…" if len(h) > 30 else ""))
    return ", ".join(repr(h) for h in vistos) or "(ninguno)"


def _parece_dato(celdas: list[str]) -> bool:
    """¿La 'fila de encabezado' posicional es en realidad una fila de datos?"""
    return len(celdas) > 1 and bool(re.fullmatch(r"\d{6,}", normalizar_caso(celdas[1])))


def _claves_unicas(encabezados: list[str]) -> list[str]:
    out, vistos = [], set()
    for i, h in enumerate(encabezados):
        k = h.strip() or f"#{i + 1}"
        if k in vistos:
            k = f"{k} #{i + 1}"
        vistos.add(k)
        out.append(k)
    return out


def parse_csv(datos: bytes | str, fuente: str, recibido: datetime,
              ventana_desde: datetime | None = None,
              ventana_hasta: datetime | None = None) -> Snapshot:
    """CSV crudo de Centrality -> Snapshot de `fuente` (casos|reclamos|abiertas).

    Levanta ErrorContrato si el archivo no sirve; un encabezado válido con
    CERO filas es válido (no hay fallas) y devuelve un Snapshot vacío.
    """
    if fuente not in FUENTES_CSV:
        raise ValueError(f"fuente desconocida: {fuente!r}")
    texto, recuperados = decodificar(datos if datos is not None else b"")
    cuerpo = texto.strip()
    if not cuerpo or (not _RX_SALTO.search(cuerpo) and not any(s in cuerpo for s in _SEPARADORES)):
        raise ErrorContrato("respuesta vacía o no es CSV")

    clave = "tarea" if fuente == "abiertas" else "caso"
    candidatas = _lineas_candidatas(texto)
    elegido: tuple[int, int, str, dict[int, str]] | None = None   # (n_linea, offset, sep, mapa)
    posicional = False
    for n, (offset, linea) in enumerate(candidatas):
        sep = _separador(linea)
        mapa = mapear_encabezados(_celdas(linea, sep), fuente)
        if clave in mapa.values():
            elegido = (n, offset, sep, mapa)
            break
    if elegido is None and fuente == "reclamos":
        # La bandeja exporta íconos (📞 ⚠️ ⚡ 👥) como encabezado: se usa el orden
        # verificado de las 21 columnas.
        for n, (offset, linea) in enumerate(candidatas):
            sep = _separador(linea)
            if len(_celdas(linea, sep)) == len(COLUMNAS_POSICIONALES):
                mapa = {i: _POSICIONAL_A_CANONICO[nombre]
                        for i, nombre in enumerate(COLUMNAS_POSICIONALES)
                        if nombre in _POSICIONAL_A_CANONICO}
                elegido, posicional = (n, offset, sep, mapa), True
                break
    if elegido is None:
        primera = candidatas[0][1] if candidatas else ""
        vistos = _celdas(primera, _separador(primera))
        raise ErrorContrato(
            f"no se reconoce la columna '{clave}' en el encabezado de {fuente}; "
            f"encabezados vistos: {_para_mensaje(vistos)}")

    n_linea, offset, sep, mapa = elegido
    lector = csv.reader(io.StringIO(texto[offset:], newline=""), delimiter=sep)
    encabezados = next(lector, [])
    snap = Snapshot(fuente=fuente, recibido=recibido, completa=True,
                    ventana_desde=ventana_desde, ventana_hasta=ventana_hasta)
    snap.columnas = [h.strip() for h in encabezados]

    filas_previas: list[list[str]] = []
    if posicional:
        snap.advertencias.append(
            "encabezado no reconocido: se usó el orden posicional de 21 columnas")
        if _parece_dato(encabezados):
            filas_previas.append(encabezados)      # no había encabezado: es la 1ª fila
        claves = _claves_unicas(encabezados)
        snap.columnas_mapeadas = {claves[i]: c for i, c in sorted(mapa.items())}
    else:
        snap.columnas_mapeadas = {encabezados[i].strip(): c for i, c in sorted(mapa.items())}
    if n_linea:
        snap.advertencias.append(f"se omitieron {n_linea} líneas antes del encabezado")

    largo = len(encabezados)
    desparejas = 0
    try:
        for fila in [*filas_previas, *lector]:
            if not any(c.strip() for c in fila):
                continue                                # fila totalmente vacía
            snap.filas_leidas += 1
            if len(fila) != largo:
                desparejas += 1
            valores = {c: (fila[i] if i < len(fila) else "") for i, c in mapa.items()}
            reg = _registro(fuente, valores)
            if reg is None:
                snap.filas_descartadas += 1
            else:
                snap.registros.append(reg)
    except csv.Error as e:
        raise ErrorContrato(f"CSV mal formado cerca de la fila {snap.filas_leidas + 1}: {e}") from e

    canonicos = set(mapa.values())
    snap.faltantes = [c for c in CAMPOS_UTILES[fuente] if c not in canonicos]
    if desparejas:
        snap.advertencias.append(
            f"{desparejas} filas con un número de columnas distinto al encabezado ({largo})")
    if recuperados:
        snap.advertencias.append(f"{recuperados} bytes cp1252 recuperados")
    return snap


# --------------------------------------------------------------------------
# Grilla capturada por la extensión
# --------------------------------------------------------------------------


def _primitivo(v: Any) -> Any:
    return v if v is None or isinstance(v, (str, int, float, bool)) else None


def parse_grilla(payload: dict, recibido: datetime) -> Snapshot:
    """Payload de `leerGrillasExt` -> Snapshot "grilla" (completa=False).

    Toma TODAS las grillas que mapeen `caso` (o `tarea` con código de
    emergencia) e ignora el resto con una advertencia. Si ninguna sirve,
    ErrorContrato (la extensión lo trata como normal: la pestaña puede no
    estar en la bandeja).
    """
    if not isinstance(payload, dict):
        raise ErrorContrato("payload de grilla inválido: se esperaba un objeto JSON")
    if payload.get("error"):
        raise ErrorContrato(f"la extensión no pudo leer la grilla: {limpio(payload['error'])[:200]}")
    grillas = [g for g in (payload.get("grillas") or []) if isinstance(g, dict)]
    if not grillas:
        raise ErrorContrato("ninguna grilla sirve: el payload no trae grillas")

    snap = Snapshot(fuente="grilla", recibido=recibido, completa=False)
    ignoradas: list[str] = []
    canonicos: set[str] = set()
    vistas: set[str] = set()
    duplicadas = usadas = 0

    for g in grillas:
        nombre = limpio(g.get("titulo")) or limpio(g.get("id")) or "(sin título)"
        filas = [f for f in (g.get("filas") or []) if isinstance(f, dict)]
        # Claves = las de las filas + texto/dataIndex de las columnas (una grilla
        # vacía con columna Caso igual sirve: dice "no hay nada").
        claves: list[str] = []
        for f in filas:
            for k in f:
                if k not in claves:
                    claves.append(k)
        for col in g.get("columnas") or []:
            if isinstance(col, dict):
                for k in (col.get("texto"), col.get("dataIndex")):
                    if isinstance(k, str) and k and k not in claves:
                        claves.append(k)

        # A diferencia del CSV, aquí varias claves pueden ir al mismo canónico
        # (texto "Caso" y dataIndex "caso"): se toma la primera con valor.
        mapa = {}
        for k in claves:
            c, _ = _canonico(k, "grilla")
            if c:
                mapa[k] = c
        if "inicio" not in mapa.values():
            for k in claves:
                if slug(k) == _INICIO_RESPALDO:
                    mapa[k] = "inicio"
                    break
        tiene_caso = "caso" in mapa.values()
        tiene_tarea = "tarea" in mapa.values() and any(
            caso_de_tarea(_texto(f.get(k))) for f in filas for k, c in mapa.items() if c == "tarea")
        if not (tiene_caso or tiene_tarea):
            ignoradas.append(nombre)
            continue

        usadas += 1
        for k, c in mapa.items():
            snap.columnas_mapeadas.setdefault(k, c)
        canonicos.update(mapa.values())
        snap.columnas.extend(k for k in claves if k not in snap.columnas)
        total, cargadas = g.get("total"), g.get("cargadas")
        if isinstance(total, int) and isinstance(cargadas, int) and cargadas < total:
            snap.advertencias.append(
                f"grilla «{nombre}»: {cargadas} de {total} filas cargadas (paginada o filtrada)")

        for f in filas:
            fila = {k: _primitivo(v) for k, v in f.items()}
            if all(es_nulo(v) for v in fila.values()):
                continue
            # La misma grilla puede llegar desde dos frames: no contar dos veces.
            huella = json.dumps(fila, sort_keys=True, ensure_ascii=False, default=str)
            if huella in vistas:
                duplicadas += 1
                continue
            vistas.add(huella)
            snap.filas_leidas += 1
            valores: dict[str, Any] = {}
            for k, c in mapa.items():
                v = fila.get(k)
                if c not in valores or (es_nulo(valores[c]) and not es_nulo(v)):
                    valores[c] = v
            reg = _registro("grilla", valores)
            if reg is None:
                snap.filas_descartadas += 1
            else:
                snap.registros.append(reg)

    if not usadas:
        raise ErrorContrato(
            "ninguna grilla sirve (sin columna caso ni tarea de emergencia): "
            + ", ".join(ignoradas[:10]))
    if ignoradas:
        snap.advertencias.append("grillas ignoradas (sin columna caso): " + ", ".join(ignoradas[:10]))
    if duplicadas:
        snap.advertencias.append(f"{duplicadas} filas repetidas entre grillas omitidas")
    snap.faltantes = [c for c in CAMPOS_UTILES["grilla"] if c not in canonicos]
    return snap
