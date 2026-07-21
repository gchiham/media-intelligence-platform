from sqlalchemy import select

from src.infrastructure.db.repository import Repository
from src.modules.auth.models import Usuario


class UsuarioRepository(Repository[Usuario]):
    model = Usuario

    def get_by_email(self, email: str) -> Usuario | None:
        stmt = select(Usuario).where(Usuario.email == email)
        return self._session.scalars(stmt).first()
