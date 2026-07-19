"""Endpoints de News (Editorial). Capa de transporte pura: valida el request,
resuelve dependencias, llama a NoticiaService, devuelve la respuesta. Toda la
logica de negocio vive en NoticiaService -- ver docs/EDITORIAL_DOMAIN.md y
docs/API.md.

Excepcion: GET /pending no pasa por NoticiaService porque no es una operacion
de negocio (no muta nada, no aplica ninguna regla) -- es una lectura directa
via NoticiaRepository. NoticiaService.siguiente_pendiente_con_lock() no sirve
para esto: usa SELECT FOR UPDATE y bloquearia filas solo por listarlas.
"""
import uuid

from fastapi import APIRouter, Depends, status

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
from src.modules.editorial.models import Noticia
from src.modules.editorial.repositories import NoticiaRepository, NoticiaVersionRepository
from src.modules.editorial.services import NoticiaService

router = APIRouter(prefix="/news", tags=["news"])


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
