from src.infrastructure.db.repository import Repository
from src.modules.auth.models import Usuario


class UsuarioRepository(Repository[Usuario]):
    model = Usuario
