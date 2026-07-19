"""DTOs HTTP del modulo Pipeline. Nunca se reutiliza `PipelineRun` (entidad de
dominio) directamente como modelo de respuesta -- estos son objetos propios de
la capa de transporte."""
import uuid

from pydantic import BaseModel, Field


class ProcessRecordingRequest(BaseModel):
    recording_id: uuid.UUID = Field(description="ID de la Grabacion a procesar")


class ProcessRecordingResponse(BaseModel):
    pipeline_run_id: uuid.UUID
    status: str
    news_generated: int
