from src.infrastructure.db.repository import Repository
from src.modules.pipeline.models import PipelineRun


class PipelineRunRepository(Repository[PipelineRun]):
    model = PipelineRun
