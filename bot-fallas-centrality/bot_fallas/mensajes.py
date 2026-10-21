"""Redacción de avisos: Evento / Falla -> Mensaje (HTML de Telegram + texto plano).

Reglas que este módulo hace cumplir:

- TODO valor dinámico pasa por `escape_html`. Un nombre de calle con "<" o
  "&" (pasa: "LOS AROMOS & CIA", "PJE <SIN NOMBRE>") hace que Telegram
  rechace el mensaje entero con "can't parse entities"; peor, un valor sin
  escapar podría inyectar etiquetas. Para que no dependa de acordarse, cada
  línea se arma con `_linea(...)`, que escapa todo lo que recibe.
- Máximo 4096 caracteres (límite de Telegram). Se arma POR LÍNEAS y cada
  línea abre y cierra sus propias etiquetas: al recortar se descartan líneas
  completas, así nunca queda un "<b>" sin cerrar ni un "&am" a medias.
- El texto plano (Teams, archivo, consola) sale de las MISMAS líneas, sin
  etiquetas: los dos formatos dicen exactamente lo mismo.
- No hay datos personales que filtrar: `Falla` ya no trae NIS, nombres ni
  números de casa (ver modelo.py). Este módulo sólo muestra lo que recibe.
"""

from __future__ import annotations

from datetime import datetime

from .modelo import Calle, Evento, Falla, Mensaje
from .texto import escape_html, fmt_duracion, fmt_hora, fmt_miles

EMOJI_NIVEL = {"masivo": "🟣", "alta": "🔴", "media": "🟠", "baja": "🟡", "": "⚪"}

LIMITE_TELEGRAM = 4096

_NOMBRE_FUENTE = {
    "informe_casos": "informe de casos",
    "casos": "informe de casos",
    "grilla": "bandeja capturada",
    "nis_reclamos": "reclamos",
    "reclamos": "reclamos",
    "abiertas": "tareas abiertas",
}

_SUFIJO_CALLES = {
    "maestro_sed": "según maestro de red (SED)",
    "maestro_alimentador": ("alimentador completo según maestro "
                            "(puede sobrestimar si la falla es parcial)"),
}

_SALUD = {
    "sin_datos": ("⚠️", "Sin datos de Centrality",
                  "El bot no está recibiendo reportes: los avisos de fallas están detenidos."),
    "datos_recuperados": ("✅", "Datos de Centrality recuperados",
                          "El bot vuelve a recibir reportes."),
    "sesion_caducada": ("🔑", "Sesión de Centrality caducada",
                        "Vuelve a iniciar sesión en Centrality en el navegador; "
                        "la extensión retoma sola."),
    "prueba": ("🧪", "Mensaje de prueba",
               "Si lees esto, el canal del bot de fallas funciona."),
}

# Topes por campo: con cada línea acotada, el recorte por largo sólo tiene que
# quitar líneas enteras (nunca partir una). 60 cubre de sobra los nombres de
# calle reales; lo que pase de ahí es basura de la fuente.
_MAX_CALLE = 60
_MAX_COMUNA = 40
_MAX_CAMPO = 80
_MAX_CASO = 40
_MAX_COMUNAS = 6
_MAX_ALIMENTADORES = 5
_MAX_SEDS = 5
_MAX_CALLES_NUEVAS = 10
_MAX_LINEAS_RESUMEN_INICIO = 25
_MAX_LINEAS_RESUMEN = 40
_MAX_CALLES_DETALLE = 40
_MAX_CALLES_NORMALIZADA = 5

Linea = tuple[str, str]   # (html, texto)


# --------------------------------------------------------------------------
# Piezas
# --------------------------------------------------------------------------


def _b(s) -> tuple[str, str]:
    return ("b", s)


def _i(s) -> tuple[str, str]:
    return ("i", s)


def _code(s) -> tuple[str, str]:
    return ("code", s)


def _linea(*partes) -> Linea:
    """Arma una línea escapando TODO. Una parte es texto o (etiqueta, texto)."""
    html: list[str] = []
    texto: list[str] = []
    for p in partes:
        if p is None:
            continue
        if isinstance(p, tuple):
            etiqueta, valor = p
            valor = "" if valor is None else str(valor)
            html.append(f"<{etiqueta}>{escape_html(valor)}</{etiqueta}>")
            texto.append(valor)
        else:
            html.append(escape_html(p))
            texto.append(str(p))
    return "".join(html), "".join(texto)


def _corto(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _largo(s: str) -> int:
    # Telegram mide en unidades UTF-16 (un emoji cuenta 2). Medir así el HTML
    # (que además incluye las etiquetas) deja siempre margen.
    return len(s.encode("utf-16-le")) // 2


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or singular + "s")


def _cl(n: int | None) -> str:
    return f"{fmt_miles(n)} {_plural(n or 0, 'cliente')}"


def _emoji(f: Falla) -> str:
    return EMOJI_NIVEL.get(f.nivel or "", "⚪")


def _lista_corta(valores, maximo: int, largo: int) -> str:
    vals = [_corto(v, largo) for v in valores if v]
    if len(vals) > maximo:
        return ", ".join(vals[:maximo]) + f" y {len(vals) - maximo} más"
    return ", ".join(vals)


def _hace(inicio: datetime | None, ahora: datetime) -> str:
    if inicio is None or inicio > ahora:
        return ""
    return f"hace {fmt_duracion(ahora - inicio)}"


def _nombre_fuentes(claves) -> str:
    nombres: list[str] = []
    for k in claves:
        n = _NOMBRE_FUENTE.get(k, k)
        if n and n not in nombres:
            nombres.append(n)
    return " + ".join(nombres)


# --------------------------------------------------------------------------
# Líneas de una falla
# --------------------------------------------------------------------------


def _linea_titulo(f: Falla, emoji: str, etiqueta: str) -> Linea:
    return _linea("🗓️ PROGRAMADA · " if f.programada else None,
                  f"{emoji} ", _b(etiqueta), " · Caso ", _code(_corto(f.caso, _MAX_CASO)))


def _partes_clientes(f: Falla) -> list:
    if f.clientes is None:
        return ["👥 Clientes: sin dato"]
    if f.clientes_fuente == "nis_reclamos":
        return ["👥 ", _b(f"≥ {_cl(f.clientes)}"), " (con reclamo; el DMS aún no informa total)"]
    return ["👥 ", _b(_cl(f.clientes)), " " + _plural(f.clientes, "afectado")]


def _linea_clientes(f: Falla) -> Linea:
    partes = _partes_clientes(f)
    # Con clientes por NIS el nivel sale de un mínimo: mostrarlo engaña.
    if f.nivel and f.clientes is not None and f.clientes_fuente != "nis_reclamos":
        partes.append(f" · nivel {f.nivel.upper()}")
    return _linea(*partes)


def _linea_comunas(f: Falla) -> Linea | None:
    txt = _lista_corta(f.comunas, _MAX_COMUNAS, _MAX_COMUNA)
    return _linea("📍 ", txt) if txt else None


def _linea_red(f: Falla) -> Linea | None:
    partes = []
    alim = _lista_corta(f.alimentadores or f.alimentadores_cod, _MAX_ALIMENTADORES, _MAX_CAMPO)
    if alim:
        partes.append(alim)
    seds = _lista_corta(f.seds, _MAX_SEDS, 20)
    if seds:
        partes.append(f"SED {seds}")
    return _linea("⚡ ", " · ".join(partes)) if partes else None


def _linea_inicio(f: Falla, ahora: datetime) -> Linea | None:
    if f.inicio is None:
        return None
    hace = _hace(f.inicio, ahora)
    return _linea(f"🕒 Inicio {fmt_hora(f.inicio)}", f" ({hace})" if hace else None)


def _linea_motivo(f: Falla) -> Linea | None:
    partes: list = []
    detalle = [x for x in (_corto(f.motivo, _MAX_CAMPO),
                           f"causa: {_corto(f.causa, _MAX_CAMPO)}" if f.causa else "") if x]
    if detalle:
        partes.append("📝 " + " · ".join(detalle))
    if f.peligro:
        if partes:
            partes.append(" · ")
        partes += ["⚠️ ", _b("CON PELIGRO")]
    return _linea(*partes) if partes else None


def _linea_calle(c: Calle) -> Linea:
    nombre = _corto(c.nombre, _MAX_CALLE)
    comuna = f" — {_corto(c.comuna, _MAX_COMUNA)}" if c.comuna else ""
    if c.clientes is not None:
        cuenta = f" ({fmt_miles(c.clientes)} {_plural(c.clientes, 'cliente')})"
    elif c.reclamos:
        cuenta = f" ({fmt_miles(c.reclamos)})"
    else:
        cuenta = ""
    return _linea(f"• {nombre}{comuna}{cuenta}")


def _mas_calles(n: int) -> Linea:
    return _linea(f"…y {fmt_miles(n)} {_plural(n, 'calle')} más")


def _mas(n: int) -> Linea:
    return _linea(f"…y {fmt_miles(n)} más")


def _bloque_calles(f: Falla) -> tuple[Linea, list[Linea]]:
    """(cabecera, líneas de calle). Sin calles la cabecera lo dice todo."""
    if not f.calles:
        extra = ""
        if f.sin_calle:
            extra = f" ({fmt_miles(f.sin_calle)} {_plural(f.sin_calle, 'reclamo')} sin calle)"
        return _linea(f"🛣️ Calles: sin dirección utilizable aún{extra}"), []
    sufijo = _SUFIJO_CALLES.get(f.calles_fuente, "según reclamos")
    cab = _linea("🛣️ ", _b(f"Calles ({fmt_miles(len(f.calles))})"), f" — {sufijo}:")
    return cab, [_linea_calle(c) for c in f.calles]


def _linea_pie(f: Falla, ahora: datetime) -> Linea:
    fuentes = _nombre_fuentes(f.fuentes or ([f.clientes_fuente] if f.clientes_fuente else []))
    txt = f"Fuente: {fuentes} · {ahora:%H:%M}" if fuentes else f"Actualizado {ahora:%H:%M}"
    return _linea(_i(txt))


def _clientes_titulo(f: Falla) -> str:
    if f.clientes is None:
        return "clientes s/d"
    if f.clientes_fuente == "nis_reclamos":
        return f"≥ {_cl(f.clientes)}"
    return _cl(f.clientes)


def _sin_nulos(lineas) -> list[Linea]:
    return [x for x in lineas if x is not None]


# --------------------------------------------------------------------------
# Composición con límite de largo
# --------------------------------------------------------------------------


def _unir(lineas: list[Linea]) -> tuple[str, str]:
    return "\n".join(h for h, _ in lineas), "\n".join(t for _, t in lineas)


def _recortar(lineas: list[Linea]) -> list[Linea]:
    """Última defensa: deja las líneas que caben y cierra con '…'."""
    fin = ("…", "…")
    salida: list[Linea] = []
    for ln in lineas:
        if _largo("\n".join(h for h, _ in [*salida, ln, fin])) > LIMITE_TELEGRAM:
            break
        salida.append(ln)
    if not salida and lineas:
        # Ni la primera línea cabe (no debería pasar con los topes por campo):
        # se cae a su texto plano, que se vuelve a escapar entero, así no
        # queda ninguna etiqueta ni entidad partida.
        txt = lineas[0][1]
        n = len(txt)
        while n > 0 and _largo(escape_html(txt[:n])) > LIMITE_TELEGRAM - 2:
            n = int(n * 0.9)
        salida = [(escape_html(txt[:n]), txt[:n])]
    return salida + [fin]


def _componer(cabeza: list[Linea], items: list[Linea], pie: list[Linea],
              max_items: int, linea_mas) -> tuple[str, str]:
    """Cabeza + hasta `max_items` ítems (+ "…y N más") + pie, dentro de 4096.

    Si no cabe, se sacan ítems del final (y el "…y N más" se recalcula) antes
    de tocar la cabeza o el pie: el título y la fuente importan más que la
    calle número 38.
    """
    k = max(0, min(max_items, len(items)))
    while True:
        cuerpo = list(items[:k])
        if len(items) > k:
            cuerpo.append(linea_mas(len(items) - k))
        lineas = [*cabeza, *cuerpo, *pie]
        html, texto = _unir(lineas)
        if _largo(html) <= LIMITE_TELEGRAM or k == 0:
            break
        k -= 1
    if _largo(html) > LIMITE_TELEGRAM:
        html, texto = _unir(_recortar(lineas))
    return html, texto


def _max_calles(cfg: dict) -> int:
    try:
        return int((cfg or {}).get("avisos", {}).get("max_calles_mensaje", 15))
    except (TypeError, ValueError, AttributeError):
        return 15


# --------------------------------------------------------------------------
# Mensajes por tipo
# --------------------------------------------------------------------------


def _msg_falla(f: Falla, ahora: datetime, etiqueta: str, max_calles: int,
               extra: list[Linea | None] = ()) -> tuple[str, str]:
    cab_calles, calles = _bloque_calles(f)
    cabeza = _sin_nulos([
        _linea_titulo(f, _emoji(f), etiqueta),
        _linea_clientes(f),
        _linea_comunas(f),
        _linea_red(f),
        _linea_inicio(f, ahora),
        _linea_motivo(f),
        *extra,
        cab_calles,
    ])
    return _componer(cabeza, calles, [_linea_pie(f, ahora)], max_calles, _mas_calles)


def _msg_nueva(ev: Evento, cfg: dict, ahora: datetime) -> Mensaje:
    f = ev.falla or Falla(caso=ev.caso)
    reabierta = (ev.motivo or "").startswith("reapertura")
    etiqueta = "FALLA REABIERTA" if reabierta else "FALLA NUEVA"
    html, texto = _msg_falla(f, ahora, etiqueta, _max_calles(cfg))
    titulo = f"{'Falla reabierta' if reabierta else 'Falla nueva'} · Caso {f.caso} · {_clientes_titulo(f)}"
    return Mensaje(titulo=titulo, html=html, texto=texto, tipo=ev.tipo, caso=f.caso)


def _msg_cambio(ev: Evento, cfg: dict, ahora: datetime) -> Mensaje:
    """actualizada y reposicion_parcial: lo mismo, cambia el titular."""
    f = ev.falla or Falla(caso=ev.caso)
    ant = ev.anterior or {}
    prev = ant.get("clientes")
    cur = f.clientes
    minimo = f.clientes_fuente == "nis_reclamos"
    reposicion = ev.tipo == "reposicion_parcial"

    # Un mínimo por NIS menor que lo avisado no es una baja (ver detector):
    # en ese caso no se muestra flecha de clientes ni de nivel.
    cambio = (prev is not None and cur is not None and cur != prev
              and not (minimo and cur < prev))
    if cambio:
        d = cur - prev
        partes = ["👥 ", _b(f"{fmt_miles(prev)} → {_cl(cur)}"),
                  f" ({'+' if d > 0 else '−'}{fmt_miles(abs(d))})"]
        if minimo:
            partes.append(" (con reclamo; el DMS aún no informa total)")
    else:
        partes = _partes_clientes(f)
    nivel_ant = ant.get("nivel") or ""
    if f.nivel and not minimo and f.clientes is not None:
        if nivel_ant and nivel_ant != f.nivel:
            partes.append(f" · nivel {nivel_ant.upper()} → {f.nivel.upper()}")
        else:
            partes.append(f" · nivel {f.nivel.upper()}")

    nuevas = [n for n in (ant.get("nuevas_calles") or []) if n]
    linea_nuevas = None
    if nuevas:
        linea_nuevas = _linea("🆕 Calles nuevas: ",
                              _lista_corta(nuevas, _MAX_CALLES_NUEVAS, _MAX_CALLE))

    emoji = "🔵" if reposicion else _emoji(f)
    etiqueta = "REPOSICIÓN PARCIAL" if reposicion else "FALLA ACTUALIZADA"
    cab_calles, calles = _bloque_calles(f)
    cabeza = _sin_nulos([
        _linea_titulo(f, emoji, etiqueta),
        _linea(*partes),
        linea_nuevas,
        _linea_comunas(f),
        _linea_red(f),
        _linea_inicio(f, ahora),
        _linea_motivo(f),
        cab_calles,
    ])
    html, texto = _componer(cabeza, calles, [_linea_pie(f, ahora)], _max_calles(cfg), _mas_calles)

    if cambio:
        cl_titulo = f"{fmt_miles(prev)} → {_cl(cur)}"
    else:
        cl_titulo = _clientes_titulo(f)
    nombre = "Reposición parcial" if reposicion else "Falla actualizada"
    return Mensaje(titulo=f"{nombre} · Caso {f.caso} · {cl_titulo}",
                   html=html, texto=texto, tipo=ev.tipo, caso=f.caso)


def _msg_normalizada(ev: Evento, cfg: dict, ahora: datetime) -> Mensaje:
    f = ev.falla or Falla(caso=ev.caso)
    ant = ev.anterior or {}
    fin = f.cierre or ahora
    duracion = fmt_duracion(fin - f.inicio) if f.inicio else ""
    if duracion == "s/d":
        duracion = ""
    candidatos = [v for v in (ant.get("max_clientes"), ant.get("clientes"), f.clientes)
                  if isinstance(v, int) and not isinstance(v, bool)]
    maximo = max(candidatos) if candidatos else None

    lineas = [_linea_titulo(f, "✅", "FALLA NORMALIZADA")]
    if duracion:
        lineas.append(_linea(f"⏱️ Duración {duracion}",
                             f" · inicio {fmt_hora(f.inicio)}"))
    if maximo is not None:
        lineas.append(_linea(f"👥 Máx. clientes: {fmt_miles(maximo)}",
                             " (con reclamo)" if f.clientes_fuente == "nis_reclamos" else None))
    lineas += _sin_nulos([_linea_comunas(f), _linea_red(f)])
    calles: list[Linea] = []
    if f.calles:
        lineas.append(_linea("🛣️ ", _b(f"Calles ({fmt_miles(len(f.calles))})"), ":"))
        calles = [_linea_calle(c) for c in f.calles]
    motivo = _corto(ev.motivo, 300)
    pie = _linea(_i(f"{motivo} · {ahora:%H:%M}" if motivo else f"{ahora:%H:%M}"))
    html, texto = _componer(lineas, calles, [pie], _MAX_CALLES_NORMALIZADA, _mas_calles)
    titulo = f"Falla normalizada · Caso {f.caso}" + (f" · {duracion}" if duracion else "")
    return Mensaje(titulo=titulo, html=html, texto=texto, tipo=ev.tipo, caso=f.caso)


def _linea_corta(f: Falla, ahora: datetime) -> Linea:
    """'🔴 40808515 · 1.240 cl · Osorno · 8 calles · hace 12 min'."""
    if f.clientes is None:
        cl = "s/d cl"
    elif f.clientes_fuente == "nis_reclamos":
        cl = f"≥ {fmt_miles(f.clientes)} cl"
    else:
        cl = f"{fmt_miles(f.clientes)} cl"
    resto = [cl]
    if f.comunas:
        comuna = _corto(f.comunas[0], _MAX_COMUNA)
        resto.append(comuna + (f" +{len(f.comunas) - 1}" if len(f.comunas) > 1 else ""))
    if f.calles:
        resto.append(f"{fmt_miles(len(f.calles))} {_plural(len(f.calles), 'calle')}")
    hace = _hace(f.inicio, ahora)
    if hace:
        resto.append(hace)
    marcas = ("🗓️" if f.programada else "") + ("⚠️" if f.peligro else "")
    return _linea(f"{_emoji(f)} ", _code(_corto(f.caso, _MAX_CASO)), " · " + " · ".join(resto),
                  f" {marcas}" if marcas else None)


def _msg_resumen_inicio(ev: Evento, cfg: dict, ahora: datetime) -> Mensaje:
    fallas = list(ev.fallas)
    n = len(fallas)
    cab = _linea("🤖 ", _b("Bot de fallas iniciado"),
                 f" — {fmt_miles(n)} {_plural(n, 'falla activa', 'fallas activas')} "
                 f"({'no se re-notifica' if n == 1 else 'no se re-notifican'}):")
    items = [_linea_corta(f, ahora) for f in fallas]
    html, texto = _componer([cab], items, [_linea(_i(f"{ahora:%d-%m %H:%M}"))],
                            _MAX_LINEAS_RESUMEN_INICIO, _mas)
    return Mensaje(titulo=f"Bot de fallas iniciado · {n} {_plural(n, 'falla activa', 'fallas activas')}",
                   html=html, texto=texto, tipo=ev.tipo, caso="")


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------


def mensaje_evento(ev: Evento, cfg: dict, ahora: datetime) -> Mensaje:
    tipo = ev.tipo
    if tipo == "nueva":
        return _msg_nueva(ev, cfg, ahora)
    if tipo in ("actualizada", "reposicion_parcial"):
        return _msg_cambio(ev, cfg, ahora)
    if tipo == "normalizada":
        return _msg_normalizada(ev, cfg, ahora)
    if tipo == "resumen_inicio":
        return _msg_resumen_inicio(ev, cfg, ahora)
    if tipo in _SALUD:
        return mensaje_salud(tipo, ev.motivo, ahora)
    # Tipo desconocido (versión futura): se avisa genérico antes que callar.
    lineas = [_linea("ℹ️ ", _b(_corto(tipo, _MAX_CAMPO) or "Evento"),
                     *((" · Caso ", _code(_corto(ev.caso, _MAX_CASO))) if ev.caso else ()))]
    if ev.motivo:
        lineas.append(_linea(_corto(ev.motivo, 500)))
    html, texto = _componer(lineas, [], [], 0, _mas)
    return Mensaje(titulo=f"{tipo}" + (f" · Caso {ev.caso}" if ev.caso else ""),
                   html=html, texto=texto, tipo=tipo, caso=ev.caso)


def mensaje_resumen(fallas: list[Falla], cfg: dict, ahora: datetime,
                    titulo: str = "Fallas activas") -> Mensaje:
    """Para /fallas: una línea por falla, la más grande primero."""
    if not fallas:
        html, texto = _unir([_linea("✅ Sin fallas activas que cumplan los filtros"),
                             _linea(_i(f"{ahora:%d-%m %H:%M}"))])
        return Mensaje(titulo=f"{titulo} · 0", html=html, texto=texto, tipo="resumen")
    orden = sorted(fallas, key=lambda f: (-(f.clientes if f.clientes is not None else -1), f.caso))
    n = len(orden)
    cab = _linea("📋 ", _b(_corto(titulo, _MAX_CAMPO)), f" — {fmt_miles(n)} · {ahora:%d-%m %H:%M}")
    items = [_linea_corta(f, ahora) for f in orden]
    html, texto = _componer([cab], items, [], _MAX_LINEAS_RESUMEN, _mas)
    return Mensaje(titulo=f"{titulo} · {n}", html=html, texto=texto, tipo="resumen")


def mensaje_detalle(f: Falla, cfg: dict, ahora: datetime) -> Mensaje:
    """Para /caso: como "nueva" pero con más calles y datos de contexto."""
    extra: list[Linea | None] = []
    if f.empresa:
        extra.append(_linea("🏢 ", _corto(f.empresa, _MAX_CAMPO)))
    zona = []
    if f.ccd:
        zona.append(f"CCD {_corto(f.ccd, 20)}")
    if f.zonales:
        zona.append("Zonal " + _lista_corta(f.zonales, 3, _MAX_CAMPO))
    if zona:
        extra.append(_linea("🧭 " + " · ".join(zona)))
    if not f.abierta:
        fin = f.cierre
        dur = fmt_duracion(fin - f.inicio) if (fin and f.inicio) else ""
        extra.append(_linea("✅ Cerrada",
                            f" {fmt_hora(fin)}" if fin else None,
                            f" · duración {dur}" if dur and dur != "s/d" else None))
    html, texto = _msg_falla(f, ahora, "DETALLE", _MAX_CALLES_DETALLE, extra)
    return Mensaje(titulo=f"Detalle · Caso {f.caso} · {_clientes_titulo(f)}",
                   html=html, texto=texto, tipo="detalle", caso=f.caso)


def mensaje_salud(tipo: str, detalle: str, ahora: datetime) -> Mensaje:
    """Avisos del propio bot (sin_datos / datos_recuperados / sesion_caducada / prueba)."""
    emoji, nombre, explicacion = _SALUD.get(tipo, ("ℹ️", _corto(tipo, _MAX_CAMPO) or "Aviso", ""))
    lineas = [_linea(f"{emoji} ", _b(nombre))]
    if explicacion:
        lineas.append(_linea(explicacion))
    if detalle:
        lineas.append(_linea(_corto(detalle, 600)))
    lineas.append(_linea(_i(f"Bot de fallas · {ahora:%d-%m %H:%M}")))
    html, texto = _componer(lineas, [], [], 0, _mas)
    return Mensaje(titulo=nombre, html=html, texto=texto, tipo=tipo, caso="")
