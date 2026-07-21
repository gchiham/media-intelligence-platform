"""Endpoints de News (Editorial). Capa de transporte pura: valida el request,
resuelve dependencias, llama a NoticiaService, devuelve la respuesta. Toda la
logica de negocio vive en NoticiaService -- ver docs/EDITORIAL_DOMAIN.md y
docs/API.md.

Excepcion: GET /pending no pasa por NoticiaService porque no es una operacion
de negocio (no muta nada, no aplica ninguna regla) -- es una lectura directa
via NoticiaRepository. NoticiaService.siguiente_pendiente_con_lock() no sirve
para esto: usa SELECT FOR UPDATE y bloquearia filas solo por listarlas.
"""
import html
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse

from src.api.deps import (
    get_noticia_repository,
    get_noticia_service,
    get_noticia_version_repository,
)
from src.api.schemas.editorial import (
    ApproveRequest,
    NewsResponse,
    NewsSummaryResponse,
    NewsVersionResponse,
    RejectRequest,
    SaveDraftRequest,
    StartReviewRequest,
)
from src.infrastructure.config import settings
from src.modules.editorial.models import Noticia
from src.modules.editorial.repositories import NoticiaRepository, NoticiaVersionRepository
from src.modules.editorial.services import NoticiaService

router = APIRouter(prefix="/news", tags=["news"])

_ESTADO_COLORS = {
    "pendiente": "#9ca3af",
    "en_revision": "#f59e0b",
    "aprobada": "#10b981",
    "rechazada": "#ef4444",
    "publicada": "#3b82f6",
}


def _dashboard_row_html(row: dict) -> str:
    titulo = html.escape(row["titulo"] or "(sin titulo)")
    resumen = html.escape(row["resumen"] or "")
    estado = row["estado"] or ""
    color = _ESTADO_COLORS.get(estado, "#9ca3af")
    created = row["created_at"]
    created_str = created.strftime("%Y-%m-%d %H:%M:%S") if created else "-"
    medio = html.escape(row["medio_nombre"] or "-")
    programa = html.escape(row["programa_nombre"] or "-")
    prioridad = html.escape(row["prioridad"] or "-")
    ai_score = row["ai_score"] if row["ai_score"] is not None else "-"

    meta = row["metadatos_ia"] or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    keywords = meta.get("keywords") or []
    keywords_html = "".join(f'<span class="tag">{html.escape(str(k))}</span>' for k in keywords[:8])

    return f"""
    <article class="card">
      <div class="card-header">
        <span class="badge" style="background:{color}">{html.escape(estado)}</span>
        <span class="date">{created_str}</span>
      </div>
      <h2>{titulo}</h2>
      <p class="meta">{medio} &middot; {programa} &middot; prioridad: {prioridad} &middot; ai_score: {ai_score}</p>
      <p class="resumen">{resumen}</p>
      <div class="tags">{keywords_html}</div>
    </article>
    """


_DASHBOARD_PAGE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Dashboard de Noticias</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    max-width: 900px;
    margin: 0 auto;
    padding: 24px;
    background: #0b0f14;
    color: #e5e7eb;
  }}
  h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .subtitle {{ color: #9ca3af; margin-top: 0; margin-bottom: 24px; }}
  .card {{
    background: #131922;
    border: 1px solid #232b36;
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 14px;
  }}
  .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
  .badge {{
    color: #0b0f14;
    font-weight: 600;
    font-size: 0.75rem;
    padding: 2px 10px;
    border-radius: 999px;
    text-transform: uppercase;
  }}
  .date {{ color: #6b7280; font-size: 0.85rem; }}
  h2 {{ font-size: 1.1rem; margin: 6px 0; }}
  .meta {{ color: #9ca3af; font-size: 0.85rem; margin: 4px 0; }}
  .resumen {{ font-size: 0.95rem; line-height: 1.4; color: #d1d5db; }}
  .tags {{ margin-top: 8px; }}
  .tag {{
    display: inline-block;
    background: #1f2937;
    color: #9ca3af;
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 6px;
    margin-right: 6px;
    margin-bottom: 4px;
  }}
  .count {{ color: #6b7280; font-size: 0.85rem; }}
</style>
</head>
<body>
  <h1>Dashboard de Noticias</h1>
  <p class="subtitle">Generado {generated_at} &middot; <span class="count">{count} noticias</span> &middot; ordenadas de mas reciente a mas vieja &middot; se refresca solo cada 60s</p>
  {cards}
</body>
</html>
"""


def _to_news_response(noticia: Noticia, versiones: NoticiaVersionRepository) -> NewsResponse:
    version = versiones.get_by_id(noticia.version_actual_id) if noticia.version_actual_id else None
    return NewsResponse(
        id=noticia.id,
        status=noticia.estado.value,
        assigned_to=noticia.asignado_a,
        clip_start_seconds=noticia.clip_inicio_seg,
        clip_end_seconds=noticia.clip_fin_seg,
        title=version.titulo if version else None,
        summary=version.resumen if version else None,
    )


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    status_code=status.HTTP_200_OK,
    summary="Dashboard temporal de todas las noticias",
    description=(
        "Pagina HTML de solo lectura con todas las noticias (cualquier estado), mas "
        "reciente primero. Protegida por `token` en la query string (no es auth de "
        "usuario -- ver `DASHBOARD_TOKEN` en `.env`); responde 404 si no coincide, "
        "para no filtrar si el endpoint existe."
    ),
)
def news_dashboard(
    token: str,
    noticias: NoticiaRepository = Depends(get_noticia_repository),
) -> HTMLResponse:
    if not settings.dashboard_token or token != settings.dashboard_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    rows = noticias.listar_todas_con_detalle()
    cards = "\n".join(_dashboard_row_html(row) for row in rows)
    page = _DASHBOARD_PAGE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        count=len(rows),
        cards=cards,
    )
    return HTMLResponse(content=page)


@router.get(
    "/pending",
    response_model=list[NewsSummaryResponse],
    status_code=status.HTTP_200_OK,
    summary="Listar noticias pendientes",
    description="Lista, en orden FIFO, las noticias en estado PENDIENTE (sin bloquear ninguna).",
)
def list_pending_news(
    noticias: NoticiaRepository = Depends(get_noticia_repository),
    versiones: NoticiaVersionRepository = Depends(get_noticia_version_repository),
) -> list[NewsSummaryResponse]:
    pendientes = noticias.listar_pendientes()
    resultado = []
    for noticia in pendientes:
        version = versiones.get_by_id(noticia.version_actual_id) if noticia.version_actual_id else None
        resultado.append(
            NewsSummaryResponse(
                id=noticia.id,
                status=noticia.estado.value,
                title=version.titulo if version else None,
                created_at=noticia.created_at,
            )
        )
    return resultado


@router.post(
    "/start-review",
    response_model=NewsResponse,
    status_code=status.HTTP_200_OK,
    summary="Tomar la siguiente noticia de la cola",
    description=(
        "Toma la noticia PENDIENTE mas antigua de la cola FIFO, la bloquea y la asigna al "
        "editor. Si la cola esta vacia, responde 204 No Content."
    ),
)
def start_review(
    body: StartReviewRequest,
    service: NoticiaService = Depends(get_noticia_service),
    versiones: NoticiaVersionRepository = Depends(get_noticia_version_repository),
) -> NewsResponse:
    noticia = service.start_review(body.editor_id)
    return _to_news_response(noticia, versiones)


@router.post(
    "/{news_id}/draft",
    response_model=NewsVersionResponse,
    status_code=status.HTTP_200_OK,
    summary="Guardar un borrador (nueva version)",
    description=(
        "Crea una NoticiaVersion nueva con los campos editados -- nunca sobrescribe la "
        "version anterior. Requiere que `editor_id` tenga la noticia bloqueada (EN_REVISION "
        "y asignada a el)."
    ),
)
def save_draft(
    news_id: uuid.UUID,
    body: SaveDraftRequest,
    service: NoticiaService = Depends(get_noticia_service),
) -> NewsVersionResponse:
    version = service.save_draft(
        news_id,
        body.editor_id,
        titulo=body.title,
        resumen=body.summary,
        transcripcion_texto=body.transcription_text,
    )
    return NewsVersionResponse(
        news_id=version.noticia_id,
        version_number=version.numero_version,
        title=version.titulo,
        summary=version.resumen,
        transcription_text=version.transcripcion_texto,
        is_ai_generated=version.es_generada_por_ia,
        edited_by=version.editado_por,
        created_at=version.created_at,
    )


@router.post(
    "/{news_id}/approve",
    response_model=NewsResponse,
    status_code=status.HTTP_200_OK,
    summary="Aprobar una noticia",
    description=(
        "Aprueba la noticia completa (FR-056, una sola accion) y libera el bloqueo. "
        "Requiere que `editor_id` tenga la noticia bloqueada. No publica al cliente "
        "todavia -- fuera de alcance de esta fase."
    ),
)
def approve_news(
    news_id: uuid.UUID,
    body: ApproveRequest,
    service: NoticiaService = Depends(get_noticia_service),
    versiones: NoticiaVersionRepository = Depends(get_noticia_version_repository),
) -> NewsResponse:
    noticia = service.approve(news_id, body.editor_id)
    return _to_news_response(noticia, versiones)


@router.post(
    "/{news_id}/reject",
    response_model=NewsResponse,
    status_code=status.HTTP_200_OK,
    summary="Rechazar una noticia",
    description="Rechaza la noticia con un motivo opcional, y libera el bloqueo.",
)
def reject_news(
    news_id: uuid.UUID,
    body: RejectRequest,
    service: NoticiaService = Depends(get_noticia_service),
    versiones: NoticiaVersionRepository = Depends(get_noticia_version_repository),
) -> NewsResponse:
    noticia = service.reject(news_id, body.editor_id, motivo=body.reason)
    return _to_news_response(noticia, versiones)
