"""Snapshots de todas las fuentes -> una Falla por CASO.

La unidad es el caso de Centrality, no el ticket ni la fila: en la bandeja
real 563 tickets eran 237 casos (despacho_automatico, CLAUDE.md §4.2) y en el
informe de casos 2.568 filas eran 2.385 casos (una fila por alimentador).

Qué se puede afirmar y con qué confianza:
  - `clientes` del informe de casos es el conteo del DMS: se SUMA por
    alimentador (cada fila es un alimentador distinto del mismo caso) y si el
    mismo (caso, alimentador) se repite se toma el MÁXIMO, no se suma dos veces.
  - Sin informe de casos, los NIS distintos que reclamaron son un MÍNIMO
    ("≥ N clientes"), nunca el total.
  - Las calles salen de quienes llamaron: son una MUESTRA, no el universo. Si
    hay un maestro de calles por SED/alimentador, ese reemplaza a la muestra.

Privacidad: aquí se usan NIS y direcciones completas, pero la Falla que sale
sólo lleva nombres de calle SIN número, comunas y conteos.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from typing import Iterable

from .direcciones import Direccion, clave_calle, nombre_calle, parse_direccion
from .maestros import Maestros, clave_comuna, normalizar_alimentador, normalizar_sed
from .modelo import FUENTES, Calle, Falla, Registro, Snapshot
from .texto import limpio, norm, titulo

__all__ = ["construir_fallas", "nivel_de"]

_CORTES_DEF = {"masivo": 1000, "alta": 300, "media": 50}
_FUENTES_RECLAMO = ("reclamos", "abiertas", "grilla")
_RATIO_DEF = 0.8
# Best-effort: la jerarquía de red a veces nombra la SED ("… > SED 12345 > …").
_RX_SED = re.compile(r"\bSED\s*[:#Nº°-]?\s*(\d{3,})", re.IGNORECASE)


def nivel_de(clientes: int | None, cortes: dict) -> str:
    """Criticidad por clientes interrumpidos. Cortes del usuario sobre p50=135,
    p75=423, p90=1213: masivo ≥1000, alta ≥300, media ≥50, baja <50."""
    if clientes is None:
        return ""
    c = {**_CORTES_DEF, **{k: v for k, v in (cortes or {}).items() if v is not None}}
    if clientes >= c["masivo"]:
        return "masivo"
    if clientes >= c["alta"]:
        return "alta"
    if clientes >= c["media"]:
        return "media"
    return "baja"


def construir_fallas(snapshots: dict[str, Snapshot], maestros: Maestros, cfg: dict) -> dict[str, Falla]:
    """Une los registros de todas las snapshots (ya filtradas por frescura)."""
    maestros = maestros if maestros is not None else Maestros()
    cfg = cfg or {}
    # Orden FUENTES: así "el primero visto" (desempates, variantes) es estable y
    # privilegia el informe de casos y la bandeja sobre la grilla.
    orden = [f for f in FUENTES if f in snapshots] + [f for f in snapshots if f not in FUENTES]
    por_caso: dict[str, list[Registro]] = {}
    for fuente in orden:
        snap = snapshots.get(fuente)
        if snap is None:
            continue
        for r in snap.registros:
            if r.caso:
                por_caso.setdefault(r.caso, []).append(r)
    return {caso: _falla(caso, regs, maestros, cfg) for caso, regs in por_caso.items()}


# --------------------------------------------------------------------------
# Helpers de conteo
# --------------------------------------------------------------------------


class _Frecuencia:
    """Cuenta valores agrupando variantes (por una clave) y recuerda cómo se escribió.

    `mas_frecuentes()` devuelve, por grupo, la variante más escrita; los grupos
    van por frecuencia desc y, en empate, por orden de aparición.
    """

    def __init__(self, clave=norm):
        self._clave = clave
        self._grupos: dict[str, Counter] = {}

    def agregar(self, valor: str, n: int = 1) -> None:
        valor = limpio(valor)
        k = self._clave(valor) if valor else ""
        if k:
            self._grupos.setdefault(k, Counter())[valor] += n

    def mas_frecuentes(self) -> list[str]:
        grupos = sorted(self._grupos.values(), key=lambda c: -sum(c.values()))
        return [c.most_common(1)[0][0] for c in grupos]

    def moda(self) -> str:
        top = self.mas_frecuentes()
        return top[0] if top else ""


def _moda_unica(valores: Iterable[str]) -> str:
    """Valor más frecuente; si hay empate entre distintos, '' (no se adivina)."""
    c = Counter(v for v in valores if v)
    if not c:
        return ""
    (v1, n1), *resto = c.most_common(2)
    if resto and resto[0][1] == n1:
        return ""
    return v1


def _clave_cliente(r: Registro, i: int) -> tuple:
    """Un cliente = un NIS; sin NIS, el ticket/tarea; sin id, la fila."""
    if r.nis:
        return ("nis", r.nis)
    if r.id:
        return ("id", r.id)
    return ("fila", i)


# --------------------------------------------------------------------------
# Calles
# --------------------------------------------------------------------------


def _calles_de_reclamos(recl: list[Registro], comunas: _Frecuencia) -> tuple[list[Calle], int]:
    """Calles según quienes reclamaron, contando CLIENTES distintos por calle.

    Un mismo cliente que llamó tres veces (o que tiene reclamo y tarea) cuenta
    una vez: si no, una calle con un cliente insistente parece la más afectada.
    """
    por_cliente: dict[tuple, list[Registro]] = {}
    for i, r in enumerate(recl):
        if r.direccion:
            por_cliente.setdefault(_clave_cliente(r, i), []).append(r)

    grupos: dict[tuple[str, str], dict] = {}
    sin_calle = 0
    for regs in por_cliente.values():
        elegida: Direccion | None = None
        primera: Direccion | None = None
        for r in regs:
            d = parse_direccion(r.direccion, r.area or r.comuna)
            primera = primera or d
            if d.tiene_calle():
                elegida = d
                break
        d = elegida or primera
        if d is not None and d.comuna:
            comunas.agregar(d.comuna)
        nombre = nombre_calle(elegida.calle) if elegida else ""
        k = clave_calle(nombre) if nombre else ""
        if not k:
            sin_calle += 1
            continue
        g = grupos.setdefault((k, clave_comuna(d.comuna)),
                              {"nombres": Counter(), "comunas": Counter(), "n": 0})
        g["nombres"][nombre] += 1
        if d.comuna:
            g["comunas"][limpio(d.comuna)] += 1
        g["n"] += 1

    calles = []
    for g in grupos.values():
        comuna = g["comunas"].most_common(1)[0][0] if g["comunas"] else ""
        calles.append(Calle(nombre=g["nombres"].most_common(1)[0][0], comuna=titulo(comuna),
                            reclamos=g["n"], fuente="reclamos"))
    calles.sort(key=lambda c: (-c.reclamos, c.nombre))
    return calles, sin_calle


def _clave_par(c: Calle) -> tuple[str, str]:
    return clave_calle(c.nombre), clave_comuna(c.comuna)


def _fusionar(del_maestro: list[Calle], de_reclamos: list[Calle],
              agregar_sobrantes: bool = False) -> list[Calle]:
    """Calles del maestro + conteo de reclamos por clave de calle.

    Si el maestro no trae comuna para una calle, calza con la calle de cualquier
    comuna; si el reclamo no traía comuna, calza con la del maestro.
    """
    recl = {_clave_par(c): c for c in de_reclamos}
    out: dict[tuple[str, str], Calle] = {}
    for c in del_maestro:
        k = _clave_par(c)
        if k in out:
            # La misma calle en dos SED/alimentadores de la falla: sumar clientes.
            previa = out[k]
            if c.clientes is not None:
                previa.clientes = (previa.clientes or 0) + c.clientes
            continue
        out[k] = replace(c, reclamos=0)

    usados: set[tuple[str, str]] = set()
    for (calle, comuna), c in out.items():
        if (calle, comuna) in recl:
            pares = [(calle, comuna)]
        elif not comuna:
            pares = [p for p in recl if p[0] == calle]
        else:
            pares = [(calle, "")] if (calle, "") in recl else []
        pares = [p for p in pares if p not in usados]
        c.reclamos = sum(recl[p].reclamos for p in pares)
        usados.update(pares)

    lista = sorted(out.values(), key=lambda c: (-c.reclamos, -(c.clientes or 0), c.nombre))
    if agregar_sobrantes:
        lista += [c for p, c in recl.items() if p not in usados]
    return lista


# --------------------------------------------------------------------------
# Falla
# --------------------------------------------------------------------------


def _falla(caso: str, regs: list[Registro], maestros: Maestros, cfg: dict) -> Falla:
    casos = [r for r in regs if r.fuente == "casos"]
    recl = [r for r in regs if r.fuente in _FUENTES_RECLAMO]
    otros = [r for r in regs if r.fuente != "casos"]
    f = Falla(caso=caso)

    # ---- clientes y reclamos
    por_alim: dict[str, int] = {}
    for r in casos:
        if r.clientes is not None:
            k = normalizar_alimentador(r.alimentador)
            por_alim[k] = max(por_alim.get(k, 0), r.clientes)

    nis, tickets = set(), set()
    for i, r in enumerate(recl):
        if r.nis or r.direccion:
            nis.add(_clave_cliente(r, i))
        tickets.add((r.fuente, r.id) if r.id else ("fila", i))
    f.clientes_reclamando = len(nis)
    f.reclamos = len(tickets)

    de_grilla = [r.clientes for r in regs if r.fuente == "grilla" and r.clientes is not None]
    if por_alim:
        f.clientes, f.clientes_fuente = sum(por_alim.values()), "informe_casos"
    elif de_grilla:
        f.clientes, f.clientes_fuente = max(de_grilla), "grilla"
    elif f.clientes_reclamando > 0:
        f.clientes, f.clientes_fuente = f.clientes_reclamando, "nis_reclamos"

    # ---- estado y tiempos
    # El informe de casos manda sobre el cierre: un reclamo se cierra por
    # trámite, a veces horas después de reponer.
    decisores = casos or otros
    if casos:
        f.abierta = any(r.cierre is None for r in casos)
    else:
        f.abierta = any(not r.cerrado for r in otros)
    if not f.abierta:
        cierres = [r.cierre for r in decisores if r.cierre]
        f.cierre = max(cierres) if cierres else None
    inicios = [r.inicio for r in regs if r.inicio]
    f.inicio = min(inicios) if inicios else None
    f.programada = any(r.programada for r in casos)

    # ---- red: alimentadores, SED, jerarquía
    for r in regs:
        a = normalizar_alimentador(r.alimentador)
        if a and a not in f.alimentadores_cod:
            f.alimentadores_cod.append(a)
    infos = {a: maestros.alimentador(a) for a in f.alimentadores_cod}
    f.alimentadores = [infos[a].para_mostrar() if infos[a] else a for a in f.alimentadores_cod]

    for r in regs:
        candidatos = [normalizar_sed(r.sed)] + _RX_SED.findall(r.jerarquia or "")
        for s in candidatos:
            s = normalizar_sed(s)
            if s and s not in f.seds:
                f.seds.append(s)

    jer = _Frecuencia(clave=lambda v: v)
    for r in regs:
        jer.agregar(limpio(r.jerarquia)[:80])
    f.jerarquia = jer.moda()

    for campo in ("motivo", "causa", "componente", "origen", "empresa"):
        fr = _Frecuencia()
        for r in regs:
            fr.agregar(getattr(r, campo))
        setattr(f, campo, fr.moda())
    f.peligro = any(r.peligro is True for r in regs)

    # ---- calles y comunas
    comunas = _Frecuencia(clave=clave_comuna)
    calles, f.sin_calle = _calles_de_reclamos(recl, comunas)
    f.calles, f.calles_fuente = calles, ("reclamos" if calles else "")
    for r in regs:
        if not r.direccion:
            comunas.agregar(r.comuna or r.area)
    for a in f.alimentadores_cod:
        if infos[a] and infos[a].comuna:
            comunas.agregar(infos[a].comuna)
    f.comunas = [titulo(c) for c in comunas.mas_frecuentes()]

    _aplicar_maestro_calles(f, calles, por_alim, infos, maestros, cfg)

    # ---- CCD y zonales
    ccd = _moda_unica(i.ccd for i in infos.values() if i)
    pares = [p for c in f.comunas for p in maestros.zonales_de_comuna(c)]
    if not ccd:
        ccd = _moda_unica(p[1] for p in pares)
    f.ccd = ccd
    for z, _ in pares:
        if z not in f.zonales:
            f.zonales.append(z)

    f.fuentes = [x for x in FUENTES if any(r.fuente == x for r in regs)]
    f.nivel = nivel_de(f.clientes, cfg.get("niveles") or {})
    return f


def _aplicar_maestro_calles(f: Falla, de_reclamos: list[Calle], por_alim: dict[str, int],
                            infos: dict, maestros: Maestros, cfg: dict) -> None:
    """Reemplaza la muestra de reclamos por el maestro de calles, si aplica.

    1. SED: el maestro por SED es preciso (una SED = pocas calles) -> se usa.
    2. Alimentador: un alimentador abarca muchas calles; sólo se usa si el
       informe de casos dice que cayó casi entero (clientes del caso en ese
       alimentador / clientes_total ≥ ratio_alimentador_completo). Se evalúa
       POR alimentador: si la falla tiene además alimentadores parciales, las
       calles con reclamo que el maestro no cubre se conservan al final para
       no perder la evidencia de esos tramos.
    """
    calles_sed = [c for s in f.seds for c in maestros.calles_de_sed(s)]
    if calles_sed:
        f.calles, f.calles_fuente = _fusionar(calles_sed, de_reclamos), "maestro_sed"
        return
    if f.clientes_fuente != "informe_casos":
        return

    ratio_min = (cfg.get("maestros") or {}).get("ratio_alimentador_completo", _RATIO_DEF)
    if ratio_min is None:
        ratio_min = _RATIO_DEF
    completos: list[Calle] = []
    hay_parciales = False
    for a in dict.fromkeys([*f.alimentadores_cod, *por_alim]):
        info = infos.get(a) or (maestros.alimentador(a) if a else None)
        calles = maestros.calles_de_alimentador(a) if a else []
        total = info.clientes_total if info else None
        n = por_alim.get(a)
        if calles and total and n is not None and n / total >= ratio_min:
            completos.extend(calles)
        else:
            hay_parciales = True
    if completos:
        f.calles = _fusionar(completos, de_reclamos, agregar_sobrantes=hay_parciales)
        f.calles_fuente = "maestro_alimentador"
