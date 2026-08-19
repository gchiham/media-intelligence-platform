"""Lecturas de prensa digital. Solo consultas: las escrituras pasan por
IngestaPrensaService."""
from sqlalchemy import text
from sqlalchemy.orm import Session

# LEFT JOIN no hace falta: un Articulo siempre tiene fuente y toda fuente tiene
# medio (ambas FK son NOT NULL). El limite existe porque la tabla crece ~500
# articulos por dia y la pagina se renderiza entera de una.
_LISTA_QUERY = text(
    """
    SELECT a.id,
           a.titulo,
           a.url,
           a.resumen,
           a.autor,
           a.publicado_at,
           (a.contenido_html IS NOT NULL) AS tiene_cuerpo,
           m.nombre AS medio_nombre
    FROM articulos a
    JOIN fuentes_web f ON f.id = a.fuente_id
    JOIN medios m ON m.id = f.medio_id
    ORDER BY a.publicado_at DESC
    LIMIT :limite
    """
)


class ArticuloRepository:
    def __init__(self, session: Session):
        self._session = session

    def listar_recientes(self, limite: int = 500) -> list[dict]:
        rows = self._session.execute(_LISTA_QUERY, {"limite": limite}).mappings()
        return [dict(row) for row in rows]
