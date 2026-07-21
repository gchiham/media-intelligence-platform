from sqlalchemy import select, text

from src.infrastructure.db.repository import Repository
from src.modules.editorial.models import EstadoNoticia, MonitoringProfile, Noticia, NoticiaVersion

_DASHBOARD_QUERY = text(
    """
    SELECT
        n.id,
        n.estado,
        n.created_at,
        v.titulo,
        v.resumen,
        v.transcripcion_texto,
        v.prioridad,
        v.ai_score,
        v.metadatos_ia,
        m.nombre AS medio_nombre,
        p.nombre AS programa_nombre
    FROM noticias n
    LEFT JOIN noticia_versiones v ON v.id = n.version_actual_id
    LEFT JOIN grabaciones g ON g.id = n.grabacion_id
    LEFT JOIN programas p ON p.id = g.programa_id
    LEFT JOIN medios m ON m.id = p.medio_id
    ORDER BY n.created_at DESC
    """
)


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

    def listar_todas_con_detalle(self) -> list[dict]:
        """Lectura de reporte para GET /news/dashboard -- todas las noticias
        (cualquier estado), mas reciente primero, con medio/programa via un
        solo query (evita N+1). No pasa por el modelo Noticia a proposito:
        es una proyeccion de solo lectura para UI, no una operacion de dominio."""
        rows = self._session.execute(_DASHBOARD_QUERY).mappings()
        return [dict(row) for row in rows]


class NoticiaVersionRepository(Repository[NoticiaVersion]):
    """NewsVersion."""

    model = NoticiaVersion


class MonitoringProfileRepository(Repository[MonitoringProfile]):
    model = MonitoringProfile

    def get_by_tenant_id(self, tenant_id) -> MonitoringProfile | None:
        stmt = select(MonitoringProfile).where(MonitoringProfile.tenant_id == tenant_id)
        return self._session.scalars(stmt).first()
