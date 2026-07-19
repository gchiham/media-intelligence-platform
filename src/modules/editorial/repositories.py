from src.infrastructure.db.repository import Repository
from src.modules.editorial.models import Noticia, NoticiaVersion


class NoticiaRepository(Repository[Noticia]):
    """News. Ver nota de naming en docs/BACKEND_ARCHITECTURE.md."""

    model = Noticia


class NoticiaVersionRepository(Repository[NoticiaVersion]):
    """NewsVersion."""

    model = NoticiaVersion
