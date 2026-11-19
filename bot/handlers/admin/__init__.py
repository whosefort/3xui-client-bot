"""Admin handlers share one allowlisted router and register in legacy order."""

from .shared import router

# Import order matters for overlapping Telegram filters.
# isort: off
from . import menu as menu
from . import clients as clients
from . import settings as settings
from . import broadcast as broadcast
from . import commands as commands
from . import servers as servers
from . import reality as reality
from . import warp as warp
from . import routing as routing
# isort: on

__all__ = ["router"]
