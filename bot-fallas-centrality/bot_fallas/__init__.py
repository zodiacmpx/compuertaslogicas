"""Bot de fallas Centrality: avisa calles y clientes afectados por Telegram/Teams.

La extensión Chrome (carpeta `extension/`) baja los reportes desde la sesión
del despachador y los manda a este servicio local en 127.0.0.1; aquí se unen
por caso, se detectan los cambios y se avisa. Ver README.md.
"""

from __future__ import annotations

__version__ = "1.0.0"
