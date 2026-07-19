"""DTOs HTTP del modulo Editorial. Nunca se reutilizan `Noticia`/`NoticiaVersion`
(entidades de dominio) como modelos de respuesta.

Nota: `save_draft`/`approve`/`reject` en el dominio (`NoticiaService`)
requieren `editor_id` para verificar el bloqueo (FR-051) -- como esta fase no
implementa autenticacion, no hay otra forma de saber quien esta llamando, asi
que `editor_id` se agrego explicito en los bodies de estos tres endpoints
(el pedido original solo lo mostraba en start-review). Ver docs/API.md.
"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class StartReviewRequest(BaseModel):
    editor_id: uuid.UUID = Field(description="Usuario que toma la siguiente noticia de la cola")


class NewsSummaryResponse(BaseModel):
    id: uuid.UUID
    status: str
    title: str | None
    created_at: datetime


class NewsResponse(BaseModel):
    id: uuid.UUID
    status: str
    assigned_to: uuid.UUID | None
    clip_start_seconds: float
    clip_end_seconds: float
    title: str | None
    summary: str | None


class SaveDraftRequest(BaseModel):
    editor_id: uuid.UUID = Field(description="Editor que tiene la noticia bloqueada (FR-051)")
    title: str | None = None
    summary: str | None = None
    transcription_text: str | None = None


class NewsVersionResponse(BaseModel):
    news_id: uuid.UUID
    version_number: int
    title: str
    summary: str
    transcription_text: str
    is_ai_generated: bool
    edited_by: uuid.UUID | None
    created_at: datetime


class ApproveRequest(BaseModel):
    editor_id: uuid.UUID = Field(description="Editor que tiene la noticia bloqueada (FR-051)")


class RejectRequest(BaseModel):
    editor_id: uuid.UUID = Field(description="Editor que tiene la noticia bloqueada (FR-051)")
    reason: str | None = None
