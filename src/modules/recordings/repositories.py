from src.infrastructure.db.repository import Repository
from src.modules.recordings.models import Grabacion


class GrabacionRepository(Repository[Grabacion]):
    model = Grabacion
