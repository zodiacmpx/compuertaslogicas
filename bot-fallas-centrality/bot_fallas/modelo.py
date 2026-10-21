"""Modelo canónico del bot de fallas.

Todas las piezas hablan este idioma:

    CSV / grilla de Centrality
        -> contrato.py   -> Registro      (una fila normalizada, cualquier fuente)
        -> Snapshot      (la foto completa de UNA fuente en UN momento)
        -> agregador.py  -> Falla         (un evento de red = un CASO)
        -> detector.py   -> Evento        (nueva / actualizada / normalizada ...)
        -> mensajes.py   -> Mensaje       (texto listo para Telegram / Teams)

Regla de privacidad que este modelo hace cumplir por construcción:
  - `Registro.nis` y `Registro.direccion` viven SÓLO en memoria. Sirven para
    contar clientes distintos y sacar el nombre de la calle.
  - `Falla` no tiene ni NIS, ni nombres, ni teléfonos, ni números de casa:
    sólo nombres de calle, comunas y conteos. Es lo único que se persiste y lo
    único que sale del equipo.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

FUENTES_CSV = ("casos", "reclamos", "abiertas")
FUENTES = FUENTES_CSV + ("grilla",)

# Orden de severidad. "" = sin dato de clientes.
NIVELES = ("", "baja", "media", "alta", "masivo")


def rango_nivel(nivel: str) -> int:
    try:
        return NIVELES.index(nivel or "")
    except ValueError:
        return 0


def _iso(v: Any) -> Any:
    return v.isoformat(timespec="seconds") if isinstance(v, datetime) else v


def _fecha(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


@dataclass
class Registro:
    """Una fila normalizada de cualquier fuente.

    `clientes` es el conteo que DECLARA la fuente para esa fila (en el informe
    de casos, una fila = un alimentador del caso). No es un conteo de filas.
    """

    fuente: str                       # "casos" | "reclamos" | "abiertas" | "grilla"
    caso: str                         # clave de unión entre fuentes
    id: str = ""                      # id de la fila de origen (ticket, tarea)
    alimentador: str = ""             # código normalizado, p.ej. "A260"
    clientes: int | None = None
    nis: str = ""                     # SÓLO memoria: contar clientes distintos
    direccion: str = ""               # SÓLO memoria: se reduce a nombre de calle
    area: str = ""
    comuna: str = ""
    empresa: str = ""
    motivo: str = ""
    causa: str = ""
    componente: str = ""
    origen: str = ""
    tipo_instalacion: str = ""
    jerarquia: str = ""
    sed: str = ""
    sensibilidad: str = ""
    peligro: bool | None = None
    en_interrupcion: bool | None = None
    estado: str = ""
    cerrado: bool = False             # la fuente dice que esta fila ya está cerrada
    inicio: datetime | None = None
    cierre: datetime | None = None
    programada: bool = False


@dataclass
class Snapshot:
    """La foto de una fuente. Reemplaza entera a la anterior de esa fuente."""

    fuente: str
    recibido: datetime
    registros: list[Registro] = field(default_factory=list)
    filas_leidas: int = 0
    filas_descartadas: int = 0
    columnas: list[str] = field(default_factory=list)          # encabezados vistos
    columnas_mapeadas: dict[str, str] = field(default_factory=dict)  # encabezado -> canónico
    faltantes: list[str] = field(default_factory=list)         # canónicos útiles ausentes
    advertencias: list[str] = field(default_factory=list)
    # True = es el universo completo de la fuente (un reporte). Sólo una foto
    # completa permite inferir que un caso "desapareció". Una grilla que el
    # despachador puede filtrar o paginar NUNCA es completa.
    completa: bool = True
    ventana_desde: datetime | None = None   # si el reporte es por periodo
    ventana_hasta: datetime | None = None

    def resumen(self) -> dict:
        return {
            "fuente": self.fuente,
            "recibido": _iso(self.recibido),
            "registros": len(self.registros),
            "filas_leidas": self.filas_leidas,
            "filas_descartadas": self.filas_descartadas,
            "columnas_mapeadas": dict(self.columnas_mapeadas),
            "faltantes": list(self.faltantes),
            "advertencias": list(self.advertencias),
            "completa": self.completa,
            "ventana_desde": _iso(self.ventana_desde),
            "ventana_hasta": _iso(self.ventana_hasta),
        }


@dataclass
class Calle:
    nombre: str                       # para mostrar: "AV. MATTA"
    comuna: str = ""                  # para mostrar: "Osorno"
    reclamos: int = 0                 # clientes/registros con reclamo en esa calle
    clientes: int | None = None       # sólo si viene de un maestro de calles
    fuente: str = "reclamos"          # "reclamos" | "maestro_sed" | "maestro_alimentador"

    @property
    def clave(self) -> str:
        """Clave estable para comparar entre ciclos ("calles nuevas")."""
        from .texto import norm
        return f"{norm(self.nombre)}|{norm(self.comuna)}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Calle":
        return cls(**{k: d.get(k) for k in ("nombre", "comuna", "reclamos", "clientes", "fuente")
                      if k in d})


@dataclass
class Falla:
    """Un evento de red. La unidad es el CASO de Centrality, no el ticket."""

    caso: str
    abierta: bool = True
    programada: bool = False
    clientes: int | None = None
    # de dónde salió `clientes`, en orden de confianza:
    #   "informe_casos"  Clientes Interrumpidos del DMS (suma por alimentador)
    #   "grilla"         columna de clientes de la bandeja capturada
    #   "nis_reclamos"   NIS distintos con reclamo -> es un MÍNIMO, no el total
    clientes_fuente: str = ""
    clientes_reclamando: int = 0
    reclamos: int = 0
    calles: list[Calle] = field(default_factory=list)
    calles_fuente: str = ""           # "reclamos" | "maestro_sed" | "maestro_alimentador" | ""
    sin_calle: int = 0                # registros sin calle utilizable
    comunas: list[str] = field(default_factory=list)
    alimentadores: list[str] = field(default_factory=list)      # para mostrar
    alimentadores_cod: list[str] = field(default_factory=list)  # códigos normalizados
    seds: list[str] = field(default_factory=list)
    jerarquia: str = ""
    empresa: str = ""
    motivo: str = ""
    causa: str = ""
    componente: str = ""
    origen: str = ""
    peligro: bool = False
    ccd: str = ""
    zonales: list[str] = field(default_factory=list)
    inicio: datetime | None = None
    cierre: datetime | None = None
    fuentes: list[str] = field(default_factory=list)
    nivel: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["calles"] = [c.to_dict() for c in self.calles]
        for k in ("inicio", "cierre"):
            d[k] = _iso(d[k])
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Falla":
        f = cls(caso=str(d.get("caso", "")))
        for k, v in d.items():
            if k == "calles":
                f.calles = [Calle.from_dict(c) for c in (v or [])]
            elif k in ("inicio", "cierre"):
                setattr(f, k, _fecha(v))
            elif hasattr(f, k) and k != "caso":
                setattr(f, k, v)
        return f


@dataclass
class Evento:
    tipo: str          # "nueva" | "actualizada" | "reposicion_parcial" | "normalizada"
                       # | "resumen_inicio" | "sin_datos" | "datos_recuperados" | "sesion_caducada"
    caso: str = ""
    falla: Falla | None = None
    anterior: dict | None = None      # lo último que se notificó: clientes, nivel, calles
    motivo: str = ""                  # por qué se generó (auditable)
    ts: datetime | None = None
    fallas: list[Falla] = field(default_factory=list)   # para "resumen_inicio"

    def to_dict(self) -> dict:
        return {
            "tipo": self.tipo,
            "caso": self.caso,
            "motivo": self.motivo,
            "ts": _iso(self.ts),
            "falla": self.falla.to_dict() if self.falla else None,
            "anterior": self.anterior,
            "fallas": [f.caso for f in self.fallas],
        }


@dataclass
class Mensaje:
    """Un aviso ya redactado. Cada canal elige el formato que entiende."""

    titulo: str
    html: str          # Telegram parse_mode=HTML
    texto: str         # texto plano / markdown simple (Teams, archivo, consola)
    tipo: str = ""
    caso: str = ""
