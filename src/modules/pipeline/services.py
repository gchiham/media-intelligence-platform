"""Logica de negocio de Pipeline Runs -- se implementa en la siguiente fase.
Esta clase por ahora solo declara la dependencia (constructor) para que la
capa API tenga algo concreto que inyectar. Ver docs/BACKEND_ARCHITECTURE.md."""
from src.modules.pipeline.repositories import PipelineRunRepository


class PipelineRunService:
    def __init__(self, repository: PipelineRunRepository):
        self._repository = repository
