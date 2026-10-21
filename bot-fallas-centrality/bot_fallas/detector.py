"""Detector: decide QUÉ avisar, comparando las fallas de ahora con lo ya avisado.

Entra la foto agregada (`dict[caso, Falla]`) y el `EstadoBot`; salen
`Evento`s. Toda la memoria vive en el estado (persistido), así que el
detector es sólo reglas y se puede re-crear en cada arranque.

Las tres reglas que más cuidan al despachador:

1. Sin spam al arrancar: la primera evaluación con datos NO avisa falla por
   falla; manda un único "resumen_inicio" y marca todo como ya avisado.
2. Sin falsas normalizaciones: "ya no aparece" sólo cuenta si la fuente que
   dejó de mostrarla es COMPLETA (un reporte, no una grilla que el despachador
   filtra o pagina), está FRESCA, la había mostrado antes, y la falla no pudo
   haber salido simplemente por la ventana de fechas del reporte. Además
   exige minutos Y evaluaciones seguidas de ausencia: un reporte que una vez
   viene cortado no normaliza nada.
3. Actualizaciones con freno: cambios chicos no avisan; cambios grandes se
   difieren (no se pierden) hasta cumplir el intervalo mínimo, salvo que sean
   urgentes (pasa a masivo o aparece peligro).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .estado import EstadoBot, Seguimiento
from .modelo import FUENTES, Evento, Falla, Snapshot, rango_nivel
from .texto import norm

# Un reporte por periodo ("casos de las últimas 72 h") deja de traer una falla
# vieja cuando su inicio sale de la ventana, aunque siga abierta. Con este
# margen, una falla que empezó cerca del borde de la ventana no se considera
# ausente en esa fuente.
_MARGEN_VENTANA = timedelta(hours=1)

_NOMBRE_FUENTE = {
    "casos": "informe de casos",
    "reclamos": "reclamos",
    "abiertas": "tareas abiertas",
    "grilla": "bandeja capturada",
}

# Las fuentes de clientes que son un TOTAL declarado. "nis_reclamos" es un
# mínimo (sólo quienes llamaron): nunca prueba que los clientes bajaron.
_CLIENTES_TOTAL = ("informe_casos", "grilla")


def _ccd(v) -> str:
    """'ccd_sur' / 'CCD Sur' / 'SUR' / 'sur' -> 'SUR' (idem NORTE).

    Equivalente a maestros.normalizar_ccd, duplicado a propósito para que el
    detector no dependa de los maestros. Un valor que no es SUR/NORTE se
    devuelve normalizado (no vacío): así un filtro mal escrito ("CENTRO")
    excluye en vez de desactivarse en silencio.
    """
    palabras = [p for p in norm(str(v or "")).replace("_", " ").replace("-", " ").split()
                if p != "CCD"]
    if "SUR" in palabras:
        return "SUR"
    if "NORTE" in palabras:
        return "NORTE"
    return " ".join(palabras)


def _fuentes_legibles(fuentes) -> str:
    orden = [f for f in FUENTES if f in fuentes] + sorted(f for f in fuentes if f not in FUENTES)
    nombres: list[str] = []
    for f in orden:
        n = _NOMBRE_FUENTE.get(f, f)
        if n not in nombres:
            nombres.append(n)
    if not nombres:
        return "las fuentes"
    if len(nombres) == 1:
        return nombres[0]
    return ", ".join(nombres[:-1]) + " y " + nombres[-1]


def _orden_clientes(f: Falla) -> tuple:
    # Clientes desc; sin dato al final; desempate estable por caso.
    return (-(f.clientes if f.clientes is not None else -1), f.caso)


def _como_lista(v) -> list:
    if not v:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


class Detector:
    def __init__(self, estado: EstadoBot, cfg: dict):
        self.estado = estado
        self.cfg = cfg

    # ------------------------------------------------------------------
    # Evaluación
    # ------------------------------------------------------------------

    def evaluar(self, fallas: dict[str, Falla], completas: dict[str, Snapshot],
                ahora: datetime) -> list[Evento]:
        """Actualiza el estado y devuelve los eventos a avisar, en orden:
        normalizadas, nuevas (clientes desc), actualizadas, resumen_inicio.

        `completas` = snapshots frescas de fuentes con `cuenta_ausencia` y
        `completa=True` (las únicas que permiten inferir una ausencia).
        """
        est = self.estado
        av = self.cfg["avisos"]
        primera = not est.iniciado

        # Arranque sin ningún dato (el timer de recálculo puede correr antes
        # de la primera ingesta): si se "gastara" la primera evaluación aquí,
        # la primera ingesta real avisaría cada falla activa como nueva.
        if primera and not fallas and not completas:
            return []

        normalizadas: list[Evento] = []
        nuevas: list[Evento] = []
        actualizadas: list[Evento] = []
        al_iniciar: list[Falla] = []

        for caso in sorted(fallas):
            f = fallas[caso]
            s = est.casos.get(caso)
            if s is None:
                s = Seguimiento(caso=caso, primera_vista=ahora, ultima_vista=ahora)
                est.casos[caso] = s
            s.ultima_vista = ahora
            s.ausente_desde = None
            s.evaluaciones_ausente = 0
            s.resumen = f.to_dict()
            if f.clientes is not None:
                s.max_clientes = f.clientes if s.max_clientes is None else max(s.max_clientes, f.clientes)
            for fu in f.fuentes:
                if fu in completas and fu not in s.fuentes_completas:
                    s.fuentes_completas.append(fu)

            reapertura = False
            if s.normalizada and f.abierta:
                # Sólo se titula "reabierta" si el despachador alguna vez supo
                # de ella; una que se normalizó en silencio se avisa como nueva.
                reapertura = s.notificada
                s.normalizada = False
                s.normalizada_en = None
                s.notificada = False

            if not f.abierta:
                # Cierre explícito de la fuente: vale más que cualquier ausencia.
                if not s.normalizada:
                    if s.notificada and av["avisar_normalizadas"]:
                        normalizadas.append(Evento(
                            tipo="normalizada", caso=caso, falla=f,
                            anterior=self._anterior(s),
                            motivo=f"cierre informado por {_fuentes_legibles(f.fuentes)}",
                            ts=ahora))
                    # Sin aviso previo se marca en silencio: avisar "normalizada"
                    # algo que nunca se avisó sólo confunde. No se refresca
                    # normalizada_en en evaluaciones siguientes (si no, el caso
                    # cerrado que sigue en el reporte por periodo nunca se purga).
                    s.normalizada = True
                    s.normalizada_en = ahora
                continue

            if not self.pasa_filtros(f):
                continue   # se sigue; si después cumple, se avisa como nueva

            if not s.notificada:
                self._marcar_notificada(s, f, ahora)
                if primera:
                    al_iniciar.append(f)
                else:
                    nuevas.append(Evento(
                        tipo="nueva", caso=caso, falla=f,
                        motivo="reapertura" if reapertura
                        else f"detectada en {_fuentes_legibles(f.fuentes)}",
                        ts=ahora))
                continue

            ev = self._cambios(s, f, ahora)
            if ev is not None:
                actualizadas.append(ev)

        self._ausencias(set(fallas), completas, ahora, normalizadas)
        self._purgar(ahora)

        resumen: list[Evento] = []
        if primera:
            est.iniciado = True
            if al_iniciar and av["resumen_al_iniciar"]:
                al_iniciar.sort(key=_orden_clientes)
                resumen.append(Evento(
                    tipo="resumen_inicio", fallas=al_iniciar, ts=ahora,
                    motivo=f"{len(al_iniciar)} fallas activas al iniciar el bot"))

        nuevas.sort(key=lambda e: _orden_clientes(e.falla))
        return normalizadas + nuevas + actualizadas + resumen

    # ------------------------------------------------------------------
    # Filtros
    # ------------------------------------------------------------------

    def pasa_filtros(self, f: Falla) -> bool:
        fl = self.cfg["filtros"]
        if f.programada and not fl["incluir_programadas"]:
            return False
        if f.clientes_fuente in _CLIENTES_TOTAL:
            if (f.clientes or 0) < fl["min_clientes"]:
                return False
        elif (f.clientes_reclamando or 0) < fl["min_reclamos_sin_clientes"]:
            # Sin total del DMS se exige un mínimo de clientes reclamando:
            # un reclamo aislado suele ser un problema de un solo empalme.
            return False

        si_desconocido = fl["incluir_si_desconocido"]
        listas = (
            (fl.get("ccd"), {_ccd(f.ccd)}, _ccd),
            (fl.get("zonales"), {norm(z) for z in f.zonales}, norm),
            (fl.get("comunas"), {norm(c) for c in f.comunas}, norm),
            (fl.get("empresas"), {norm(f.empresa)}, norm),
        )
        for lista, valores, normalizar in listas:
            pedidos = {normalizar(v) for v in _como_lista(lista)} - {""}
            if not pedidos:
                continue
            valores = valores - {""}
            if not valores:
                # Sin maestros, ccd/zonal no se conocen: la config decide si
                # se avisa igual (mejor un aviso de más que una falla perdida).
                if not si_desconocido:
                    return False
                continue
            if not valores & pedidos:
                return False
        return True

    # ------------------------------------------------------------------
    # Internos
    # ------------------------------------------------------------------

    @staticmethod
    def _marcar_notificada(s: Seguimiento, f: Falla, ahora: datetime) -> None:
        s.notificada = True
        s.ultimo_aviso = ahora
        s.clientes_notificados = f.clientes
        s.nivel_notificado = f.nivel
        s.peligro_notificado = bool(f.peligro)
        claves: list[str] = []
        for c in f.calles:
            if c.clave not in claves:
                claves.append(c.clave)
        s.calles_notificadas = claves

    @staticmethod
    def _anterior(s: Seguimiento) -> dict:
        return {
            "clientes": s.clientes_notificados,
            "nivel": s.nivel_notificado,
            "calles": list(s.calles_notificadas),
            "max_clientes": s.max_clientes,
        }

    def _cambios(self, s: Seguimiento, f: Falla, ahora: datetime) -> Evento | None:
        av = self.cfg["avisos"]
        prev = s.clientes_notificados
        cur = f.clientes
        nivel_actual = f.nivel or s.nivel_notificado

        # Si el informe de casos se pone viejo, el agregador cae a contar NIS
        # con reclamo: 1.240 pasa a "≥ 5". Ese 5 es un MÍNIMO, no una baja
        # real; tomarlo como tal mandaría una "reposición parcial" falsa. Un
        # mínimo menor que lo ya avisado no aporta: se mantiene la base.
        minimo_bajo = (f.clientes_fuente == "nis_reclamos" and prev is not None
                       and cur is not None and cur < prev)
        if minimo_bajo:
            cur = prev
            nivel_actual = s.nivel_notificado

        delta = cur - prev if (cur is not None and prev is not None) else None
        dabs = av["delta_clientes_abs"]
        drel = av["delta_clientes_rel"]
        crece = ((prev is None and cur is not None and cur > 0)
                 or (delta is not None and delta > 0 and delta >= dabs and delta >= drel * prev))
        baja = (delta is not None and delta < 0 and -delta >= dabs and -delta >= drel * prev
                and cur > 0)
        sube_nivel = rango_nivel(nivel_actual) > rango_nivel(s.nivel_notificado)

        vistas = set(s.calles_notificadas)
        nuevas = []
        for c in f.calles:
            if c.clave not in vistas:
                vistas.add(c.clave)
                nuevas.append(c)
        hay_calles = len(nuevas) >= av["min_calles_nuevas"]
        peligro_nuevo = bool(f.peligro) and not s.peligro_notificado

        if not (crece or baja or sube_nivel or hay_calles or peligro_nuevo):
            return None

        urgente = (sube_nivel and nivel_actual == "masivo") or peligro_nuevo
        intervalo = timedelta(minutes=av["min_intervalo_actualizacion_min"])
        if not urgente and s.ultimo_aviso is not None and ahora - s.ultimo_aviso < intervalo:
            # Se DIFIERE: el estado no se toca, así el mismo cambio vuelve a
            # detectarse en la primera evaluación después del intervalo.
            return None

        tipo = "reposicion_parcial" if (baja and not crece and not sube_nivel) else "actualizada"
        if tipo == "reposicion_parcial" and not av["avisar_reposicion_parcial"]:
            # Se mueve sólo la base de clientes/nivel. Calles y peligro quedan
            # pendientes para que, si eran novedad, se avisen igual después.
            s.clientes_notificados = cur
            s.nivel_notificado = nivel_actual
            return None

        razones = []
        if crece or baja:
            razones.append(f"clientes {prev if prev is not None else 's/d'} → {cur}")
        if sube_nivel or (nivel_actual != s.nivel_notificado and (crece or baja)):
            razones.append(f"nivel {s.nivel_notificado or 's/d'} → {nivel_actual or 's/d'}")
        if nuevas:
            razones.append(f"{len(nuevas)} calles nuevas")
        if peligro_nuevo:
            razones.append("reclamo con peligro")

        anterior = {
            "clientes": prev,
            "nivel": s.nivel_notificado,
            "calles": list(s.calles_notificadas),
            "nuevas_calles": [c.nombre for c in nuevas],
        }
        s.clientes_notificados = cur if cur is not None else prev
        s.nivel_notificado = nivel_actual
        s.peligro_notificado = s.peligro_notificado or bool(f.peligro)
        s.calles_notificadas = s.calles_notificadas + [c.clave for c in nuevas]
        s.ultimo_aviso = ahora
        return Evento(tipo=tipo, caso=s.caso, falla=f, anterior=anterior,
                      motivo="; ".join(razones), ts=ahora)

    def _candidatas(self, s: Seguimiento, completas: dict[str, Snapshot]) -> set[str]:
        """Fuentes completas y frescas cuya ausencia SÍ dice algo de este caso."""
        candidatas = set(s.fuentes_completas) & set(completas)
        if not candidatas:
            return candidatas
        inicio = s.falla().inicio
        for fu in list(candidatas):
            desde = completas[fu].ventana_desde
            # Sin inicio conocido no se puede descartar que haya salido por la
            # ventana: ante la duda no se normaliza (expira sola si nadie la ve).
            if desde is not None and (inicio is None or inicio < desde + _MARGEN_VENTANA):
                candidatas.discard(fu)
        return candidatas

    def _ausencias(self, presentes: set[str], completas: dict[str, Snapshot],
                   ahora: datetime, normalizadas: list[Evento]) -> None:
        est = self.estado
        av = self.cfg["avisos"]
        expira = timedelta(minutes=av["expiracion_sin_datos_min"])
        minimo = timedelta(minutes=av["minutos_ausencia_normalizar"])
        for caso in sorted(est.casos):
            s = est.casos[caso]
            if caso in presentes or s.normalizada:
                continue
            candidatas = self._candidatas(s, completas)
            if not candidatas:
                # Nadie que pueda dar fe de que terminó (sólo la vio una
                # grilla, o sus fuentes están viejas): no se avisa nada y, tras
                # mucho rato sin verla, se olvida.
                if ahora - s.ultima_vista > expira:
                    del est.casos[caso]
                continue
            if s.ausente_desde is None:
                s.ausente_desde = ahora
            s.evaluaciones_ausente += 1
            if (ahora - s.ausente_desde >= minimo
                    and s.evaluaciones_ausente >= av["evaluaciones_ausencia_normalizar"]):
                if s.notificada and av["avisar_normalizadas"]:
                    minutos = int((ahora - s.ultima_vista).total_seconds() // 60)
                    normalizadas.append(Evento(
                        tipo="normalizada", caso=caso, falla=s.falla(),
                        anterior=self._anterior(s), ts=ahora,
                        motivo=(f"dejó de aparecer en {_fuentes_legibles(candidatas)} "
                                f"hace {minutos} min (no hay cierre explícito)")))
                s.normalizada = True
                s.normalizada_en = ahora

    def _purgar(self, ahora: datetime) -> None:
        retener = timedelta(hours=self.cfg["avisos"]["retener_normalizadas_h"])
        for caso in list(self.estado.casos):
            s = self.estado.casos[caso]
            if s.normalizada and (s.normalizada_en is None or ahora - s.normalizada_en > retener):
                del self.estado.casos[caso]
