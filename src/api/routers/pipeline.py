"""Endpoints de Pipeline. Capa de transporte pura: valida el request, resuelve
dependencias, llama a PipelineRunService, devuelve la respuesta. Toda la
logica de negocio vive en PipelineRunService/MediaProcessingOrchestrator --
ver docs/BACKEND_ARCHITECTURE.md y docs/API.md."""
from fastapi import APIRouter, Depends, status

from src.api.deps import get_pipeline_run_service, get_recording_resolver
from src.api.schemas.pipeline import ProcessRecordingRequest, ProcessRecordingResponse
from src.application.orchestrator import ProcessAudioJob
from src.modules.pipeline.resolvers import RecordingResolver
from src.modules.pipeline.services import PipelineRunService

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


@router.post(
    "/process",
    response_model=ProcessRecordingResponse,
    status_code=status.HTTP_200_OK,
    summary="Procesar una grabacion",
    description=(
        "Ejecuta el pipeline de segmentacion + clipping (MediaProcessingOrchestrator) "
        "sobre una Grabacion ya transcrita, y persiste el PipelineRun junto con las "
        "Noticia/NoticiaVersion resultantes. No dispara la transcripcion -- requiere que "
        "el words.json y el audio de la grabacion ya esten disponibles "
        "(ver RecordingResolver en docs/API.md)."
    ),
)
def process_recording(
    body: ProcessRecordingRequest,
    pipeline_service: PipelineRunService = Depends(get_pipeline_run_service),
    resolver: RecordingResolver = Depends(get_recording_resolver),
) -> ProcessRecordingResponse:
    resources = resolver.resolve(body.recording_id)
    try:
        job = ProcessAudioJob(
            words_json_path=resources.words_json_path,
            audio_path=resources.audio_path,
            output_dir=resources.output_dir,
        )
        pipeline_run = pipeline_service.run(body.recording_id, job)
    finally:
        # Siempre, incluso si el pipeline fallo -- sin esto el audio
        # descargado (S3RecordingResolver) se queda para siempre y llena el
        # disco del backend.
        resolver.cleanup(resources)

    return ProcessRecordingResponse(
        pipeline_run_id=pipeline_run.id,
        status=pipeline_run.estado.value,
        news_generated=pipeline_run.noticias_generadas,
    )
