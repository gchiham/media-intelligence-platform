from sqlalchemy import select, text

from src.infrastructure.db.repository import Repository
from src.modules.editorial.models import EstadoNoticia, MonitoringProfile, Noticia, NoticiaVersion

# Liviana a proposito: GET /news/dashboard manda esto para las ~3000+
# noticias de una sola vez, asi que NO incluye resumen/transcripcion/keywords
# (eso pesaba ~8MB de HTML con todas las noticias inline, muy pesado para
# celular/datos moviles) -- el detalle completo se carga bajo demanda, ver
# `obtener_detalle`, solo cuando el usuario abre esa noticia especifica.
_DASHBOARD_LIST_QUERY = text(
    """
    SELECT
        n.id,
        n.estado,
        n.created_at,
        v.titulo,
        m.nombre AS medio_nombre
    FROM noticias n
    LEFT JOIN noticia_versiones v ON v.id = n.version_actual_id
    LEFT JOIN grabaciones g ON g.id = n.grabacion_id
    LEFT JOIN programas p ON p.id = g.programa_id
    LEFT JOIN medios m ON m.id = p.medio_id
    ORDER BY n.created_at DESC
    """
)

_DETALLE_QUERY = text(
    """
    SELECT
        n.id,
        n.estado,
        n.created_at,
        n.clip_s3_uri,
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
    WHERE n.id = :id
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

    def listar_todas_resumen(self) -> list[dict]:
        """Lectura liviana para GET /news/dashboard -- todas las noticias
        (cualquier estado), mas reciente primero, solo lo necesario para la
        lista/tabs/busqueda. Ver `obtener_detalle` para el resto por noticia."""
        rows = self._session.execute(_DASHBOARD_LIST_QUERY).mappings()
        return [dict(row) for row in rows]

    def obtener_detalle(self, noticia_id) -> dict | None:
        """Detalle completo (resumen, transcripcion, keywords, clip) de una
        sola noticia -- cargado bajo demanda cuando el usuario abre esa fila
        en el dashboard, ver GET /news/{news_id}/detail."""
        row = self._session.execute(_DETALLE_QUERY, {"id": noticia_id}).mappings().first()
        return dict(row) if row else None

    def buscar_candidatos(self, terminos: list[str], limit: int = 150) -> list[dict]:
        """Busqueda de texto para GET /news/search: ILIKE por palabra sobre
        titulo/resumen/transcripcion/keywords/medio -- sin LLM, sin costo por
        busqueda, y a proposito sin pg_trgm/pgvector (solo coincidencia
        parcial de substring, no tolera errores ortograficos ni sinonimos)."""
        if not terminos:
            return []

        clausulas = []
        params: dict[str, str] = {}
        for i, termino in enumerate(terminos):
            key = f"t{i}"
            params[key] = f"%{termino}%"
            clausulas.append(
                f"(v.titulo ILIKE :{key} OR v.resumen ILIKE :{key} "
                f"OR v.transcripcion_texto ILIKE :{key} OR v.metadatos_ia::text ILIKE :{key} "
                f"OR m.nombre ILIKE :{key})"
            )
        where_sql = " OR ".join(clausulas)
        params["limit"] = limit

        query = text(
            f"""
            SELECT
                n.id, n.created_at, v.titulo, v.resumen, v.metadatos_ia,
                m.nombre AS medio_nombre
            FROM noticias n
            LEFT JOIN noticia_versiones v ON v.id = n.version_actual_id
            LEFT JOIN grabaciones g ON g.id = n.grabacion_id
            LEFT JOIN programas p ON p.id = g.programa_id
            LEFT JOIN medios m ON m.id = p.medio_id
            WHERE {where_sql}
            ORDER BY n.created_at DESC
            LIMIT :limit
            """
        )
        rows = self._session.execute(query, params).mappings()
        return [dict(row) for row in rows]


class NoticiaVersionRepository(Repository[NoticiaVersion]):
    """NewsVersion."""

    model = NoticiaVersion


class MonitoringProfileRepository(Repository[MonitoringProfile]):
    model = MonitoringProfile

    def get_by_tenant_id(self, tenant_id) -> MonitoringProfile | None:
        stmt = select(MonitoringProfile).where(MonitoringProfile.tenant_id == tenant_id)
        return self._session.scalars(stmt).first()
