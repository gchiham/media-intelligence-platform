"""Importa todos los modelos de todos los modulos para que Base.metadata quede completo.

Alembic (env.py) importa este archivo antes de correr autogenerate. Sin esto, cualquier
modulo que no se haya importado en algun punto del arranque quedaria invisible para las
migraciones automaticas.
"""

from src.infrastructure.db.base import Base  # noqa: F401
from src.modules.ai import models as ai_models  # noqa: F401
from src.modules.auth import models as auth_models  # noqa: F401
from src.modules.editorial import models as editorial_models  # noqa: F401
from src.modules.media import models as media_models  # noqa: F401
from src.modules.pipeline import models as pipeline_models  # noqa: F401
from src.modules.recordings import models as recordings_models  # noqa: F401
from src.modules.reports import models as reports_models  # noqa: F401
from src.shared import audit as audit_models  # noqa: F401
