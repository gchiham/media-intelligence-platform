from sqlalchemy import select

from src.infrastructure.db.repository import Repository
from src.modules.editorial.models import EstadoNoticia, Noticia, NoticiaVersion


class NoticiaRepository(Repository[Noticia]):
    """News. Ver nota de naming en docs/BACKEND_ARCHITECTURE.md."""

    model = Noticia

    def siguiente_pendiente_con_lock(self) -> Noticia | None:
        """Cola FIFO (FR-050): la noticia PENDIENTE mas antigua. `FOR UPDATE
        SKIP LOCKED` hace el dequeue seguro con multiples periodistas
        llamando `start_review()` al mismo tiempo -- cada uno se salta las
        filas que otra transaccion concurrente ya tiene bloqueadas, en vez de
        esperar o de recibir la misma noticia que otro."""
        stmt = (
            select(Noticia)
            .where(Noticia.estado == EstadoNoticia.PENDIENTE)
            .order_by(Noticia.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def listar_pendientes(self) -> list[Noticia]:
        """Lectura de solo consulta (GET /news/pending) -- no toca estado ni
        hace locking, a diferencia de `siguiente_pendiente_con_lock`."""
        stmt = (
            select(Noticia)
            .where(Noticia.estado == EstadoNoticia.PENDIENTE)
            .order_by(Noticia.created_at.asc())
        )
        return list(self._session.scalars(stmt))


class NoticiaVersionRepository(Repository[NoticiaVersion]):
    """NewsVersion."""

    model = NoticiaVersion
