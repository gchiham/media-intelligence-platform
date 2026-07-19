from src.infrastructure.db.repository import Repository
from src.modules.media.models import Medio, Programa


class MedioRepository(Repository[Medio]):
    """MediaSource."""

    model = Medio


class ProgramaRepository(Repository[Programa]):
    """Program."""

    model = Programa
