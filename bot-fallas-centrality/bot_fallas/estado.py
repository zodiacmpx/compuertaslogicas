"""Persistencia del estado del bot: qué se avisó de cada caso y cuándo.

Por qué existe: el bot corre en el PC del despachador, que se reinicia, se
suspende o se cierra al cambiar de turno. Sin memoria de lo ya avisado, cada
arranque re-avisaría todas las fallas activas como "nuevas" (spam) y perdería
la base contra la que se miden los cambios ("1.240 → 2.300 clientes").

Qué se guarda: SÓLO lo que el modelo deja salir del equipo (conteos, nombres
de calle, comunas, el último `Falla.to_dict()`). Nunca NIS, nombres ni
teléfonos: esos viven en `Registro` y no llegan hasta aquí.

Formato: JSON UTF-8 legible (ensure_ascii=False, para que "Peñaflor" se lea
como tal si alguien abre el archivo). Las fechas van en ISO local ingenuo.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .modelo import Falla

log = logging.getLogger(__name__)

VERSION_ESTADO = 1

_FECHAS_SEGUIMIENTO = ("primera_vista", "ultima_vista", "ultimo_aviso",
                       "ausente_desde", "normalizada_en")


# --------------------------------------------------------------------------
# Conversión tolerante
# --------------------------------------------------------------------------
# El estado lo escribe el propio bot, pero puede venir de una versión anterior
# (campos que faltan) o posterior (campos que sobran). Se tolera ambas cosas
# campo a campo para no botar todo el historial por un detalle.


def _iso(v: datetime | None) -> str | None:
    # isoformat() completo: conserva microsegundos si los hay, así el
    # roundtrip devuelve exactamente la misma hora.
    return v.isoformat() if isinstance(v, datetime) else None


def _fecha(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _entero(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _lista_str(v: Any) -> list[str]:
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x) for x in v if x is not None]


def _json_default(v: Any) -> Any:
    # `fuentes` y `alertas` los llena servicio.py con lo que estime; si mete
    # una fecha o un set no debe reventar el guardado de todo el estado.
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (set, frozenset)):
        return sorted(v, key=str)
    raise TypeError(f"valor no serializable en el estado: {type(v).__name__}")


# --------------------------------------------------------------------------
# Modelo persistido
# --------------------------------------------------------------------------


@dataclass
class Seguimiento:
    """Lo que el bot sabe y ya avisó de UN caso."""

    caso: str
    primera_vista: datetime
    ultima_vista: datetime
    notificada: bool = False
    ultimo_aviso: datetime | None = None
    clientes_notificados: int | None = None
    max_clientes: int | None = None
    nivel_notificado: str = ""
    peligro_notificado: bool = False
    calles_notificadas: list[str] = field(default_factory=list)   # Calle.clave
    ausente_desde: datetime | None = None
    evaluaciones_ausente: int = 0
    normalizada: bool = False
    normalizada_en: datetime | None = None
    # Fuentes COMPLETAS que alguna vez trajeron el caso. Sólo la ausencia en
    # una de ellas cuenta como evidencia de normalización (ver detector.py).
    fuentes_completas: list[str] = field(default_factory=list)
    resumen: dict = field(default_factory=dict)                    # último Falla.to_dict()

    def falla(self) -> Falla:
        """La última foto de la falla (para avisar aunque ya no venga en los datos)."""
        d = dict(self.resumen or {})
        if not d.get("caso"):
            d["caso"] = self.caso
        return Falla.from_dict(d)

    def to_dict(self) -> dict:
        return {
            "caso": self.caso,
            "primera_vista": _iso(self.primera_vista),
            "ultima_vista": _iso(self.ultima_vista),
            "notificada": self.notificada,
            "ultimo_aviso": _iso(self.ultimo_aviso),
            "clientes_notificados": self.clientes_notificados,
            "max_clientes": self.max_clientes,
            "nivel_notificado": self.nivel_notificado,
            "peligro_notificado": self.peligro_notificado,
            "calles_notificadas": list(self.calles_notificadas),
            "ausente_desde": _iso(self.ausente_desde),
            "evaluaciones_ausente": self.evaluaciones_ausente,
            "normalizada": self.normalizada,
            "normalizada_en": _iso(self.normalizada_en),
            "fuentes_completas": list(self.fuentes_completas),
            "resumen": dict(self.resumen),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Seguimiento":
        if not isinstance(d, dict):
            raise ValueError("seguimiento no es un objeto")
        caso = str(d.get("caso") or "").strip()
        if not caso:
            raise ValueError("seguimiento sin caso")
        fechas = {k: _fecha(d.get(k)) for k in _FECHAS_SEGUIMIENTO}
        # primera/ultima vista son obligatorias para razonar ausencias; si una
        # falta se usa la otra, si faltan ambas el seguimiento no sirve.
        primera = fechas["primera_vista"] or fechas["ultima_vista"]
        ultima = fechas["ultima_vista"] or fechas["primera_vista"]
        if primera is None or ultima is None:
            raise ValueError(f"seguimiento {caso} sin fechas de vista")
        resumen = d.get("resumen")
        return cls(
            caso=caso,
            primera_vista=primera,
            ultima_vista=ultima,
            notificada=bool(d.get("notificada", False)),
            ultimo_aviso=fechas["ultimo_aviso"],
            clientes_notificados=_entero(d.get("clientes_notificados")),
            max_clientes=_entero(d.get("max_clientes")),
            nivel_notificado=str(d.get("nivel_notificado") or ""),
            peligro_notificado=bool(d.get("peligro_notificado", False)),
            calles_notificadas=_lista_str(d.get("calles_notificadas")),
            ausente_desde=fechas["ausente_desde"],
            evaluaciones_ausente=_entero(d.get("evaluaciones_ausente")) or 0,
            normalizada=bool(d.get("normalizada", False)),
            normalizada_en=fechas["normalizada_en"],
            fuentes_completas=_lista_str(d.get("fuentes_completas")),
            resumen=dict(resumen) if isinstance(resumen, dict) else {},
        )


@dataclass
class EstadoBot:
    version: int = VERSION_ESTADO
    # False hasta la primera evaluación con datos: esa evaluación NO avisa
    # fallas una por una, manda un único "resumen_inicio".
    iniciado: bool = False
    casos: dict[str, Seguimiento] = field(default_factory=dict)
    fuentes: dict[str, dict] = field(default_factory=dict)   # último resumen de ingesta por fuente
    alertas: dict[str, Any] = field(default_factory=dict)    # flags de alertas de salud ya enviadas
    telegram_offset: int = 0

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "iniciado": self.iniciado,
            "casos": {k: s.to_dict() for k, s in sorted(self.casos.items())},
            "fuentes": dict(self.fuentes),
            "alertas": dict(self.alertas),
            "telegram_offset": self.telegram_offset,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EstadoBot":
        if not isinstance(d, dict):
            raise ValueError("el estado no es un objeto JSON")
        casos: dict[str, Seguimiento] = {}
        crudos = d.get("casos") or {}
        if not isinstance(crudos, dict):
            raise ValueError("'casos' no es un objeto")
        for clave, crudo in crudos.items():
            # Un seguimiento dañado se descarta solo: perder un caso (que a lo
            # más se re-avisa) es mejor que perder el historial completo.
            try:
                if not isinstance(crudo, dict):
                    raise ValueError("no es un objeto")
                s = Seguimiento.from_dict(dict(crudo, caso=crudo.get("caso") or clave))
            except (ValueError, TypeError) as e:
                log.warning("Seguimiento del caso %s ilegible, se descarta: %s", clave, e)
                continue
            casos[s.caso] = s
        fuentes = d.get("fuentes")
        alertas = d.get("alertas")
        return cls(
            version=_entero(d.get("version")) or VERSION_ESTADO,
            iniciado=bool(d.get("iniciado", False)),
            casos=casos,
            fuentes=dict(fuentes) if isinstance(fuentes, dict) else {},
            alertas=dict(alertas) if isinstance(alertas, dict) else {},
            telegram_offset=_entero(d.get("telegram_offset")) or 0,
        )


# --------------------------------------------------------------------------
# Archivo
# --------------------------------------------------------------------------


def cargar(ruta: Path) -> EstadoBot:
    """Lee el estado. Inexistente → estado vacío.

    JSON corrupto → se RENOMBRA a `<nombre>.corrupto-<ts>.json` (no se borra:
    sirve para diagnosticar) y se parte con estado vacío. Partir vacío es
    seguro: `iniciado=False` hace que la primera evaluación mande un resumen
    en vez de re-avisar cada falla. Otros errores de E/S (permisos) se
    propagan: no son corrupción y renombrar no los arregla.
    """
    ruta = Path(ruta)
    try:
        crudo = ruta.read_bytes()
    except FileNotFoundError:
        return EstadoBot()
    try:
        return EstadoBot.from_dict(json.loads(crudo.decode("utf-8-sig")))
    except (ValueError, TypeError, AttributeError) as e:
        # JSONDecodeError y UnicodeDecodeError son ValueError.
        destino = _respaldar_corrupto(ruta)
        log.warning("Estado %s ilegible (%s: %s); respaldado como %s, se parte con estado vacío",
                    ruta.name, type(e).__name__, str(e)[:200],
                    destino.name if destino else "(no se pudo respaldar)")
        return EstadoBot()


def _respaldar_corrupto(ruta: Path) -> Path | None:
    # E/S, no lógica de negocio: aquí sí se usa la hora del equipo.
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = ruta.with_name(f"{ruta.stem}.corrupto-{ts}.json")
    n = 1
    while destino.exists():
        destino = ruta.with_name(f"{ruta.stem}.corrupto-{ts}-{n}.json")
        n += 1
    try:
        os.replace(ruta, destino)
    except OSError as e:
        log.error("No se pudo respaldar el estado corrupto %s: %s", ruta.name, e)
        return None
    return destino


def guardar(estado: EstadoBot, ruta: Path) -> None:
    """Guarda atómicamente: temporal en el MISMO directorio + os.replace.

    Por qué así: un corte de luz o un cierre de sesión a mitad de escritura
    dejaría un JSON truncado, y un JSON truncado = estado vacío = se pierde
    lo avisado. `os.replace` dentro del mismo volumen es atómico tanto en
    Windows como en Linux: queda el archivo viejo entero o el nuevo entero.
    Se serializa ANTES de abrir el temporal para que un error de
    serialización no toque nada en disco.
    """
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    contenido = json.dumps(estado.to_dict(), ensure_ascii=False, indent=1,
                           default=_json_default)
    fd, tmp = tempfile.mkstemp(prefix=f".{ruta.name}.", suffix=".tmp", dir=ruta.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(contenido)
            fh.flush()
            os.fsync(fh.fileno())
        _reemplazar(tmp, ruta)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _reemplazar(origen: str, destino: Path, intentos: int = 5) -> None:
    # En Windows os.replace falla con PermissionError si el antivirus o el
    # indexador tiene el archivo destino abierto un instante. Reintentar unas
    # décimas de segundo lo resuelve casi siempre; si persiste, se propaga.
    for i in range(intentos):
        try:
            os.replace(origen, destino)
            return
        except PermissionError:
            if i == intentos - 1:
                raise
            time.sleep(0.05 * (i + 1))
