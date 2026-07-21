"""CoverageDiscoveryService: crea Grabacion(estado=PENDIENTE) leyendo
recording_coverage -- la tabla del sistema capturador (Destroyer, DB externa,
solo lectura) que registra cada archivo subido a S3 -- en vez de escanear
S3 directamente como hace DiscoveryService.

Por que existe ademas de DiscoveryService: el regex de DiscoveryService
(`_KEY_PATTERN` en discovery.py) solo reconoce archivos `.mp3`, pero el
capturador ahora sube `.ts` -- todo lo reciente quedaba en
"ignoradas (no reconocidas)" en silencio. recording_coverage ya trae el
s3_key exacto, sin necesidad de adivinar el formato del nombre de archivo
por regex, y evita tener que mantener ese regex sincronizado con el
capturador cada vez que cambian de formato.

Corre en paralelo a DiscoveryService por ahora (no lo reemplaza) para poder
comparar resultados antes de decidir cual es la fuente de verdad.

Idempotente por `s3_key` (unico en Grabacion), igual que DiscoveryService --
correr esto dos veces no duplica filas. No hace falta trackear un checkpoint
de `updated_at`: la tabla recording_coverage es pequena por ahora (miles de
filas, no millones como el listado de S3), y el filtro por `s3_key` ya
existente resuelve la idempotencia igual que en DiscoveryService.
"""
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.modules.media.repositories import MedioRepository, ProgramaRepository
from src.modules.recordings.discovery import EstacionNoRegistrada
from src.modules.recordings.models import EstadoGrabacion, Grabacion
from src.modules.recordings.repositories import GrabacionRepository
from src.shared.logging_utils import get_logger

logger = get_logger("coverage_discovery_service")

_QUERY = text(
    """
    SELECT stream_id, period_start_utc, period_end_utc, s3_key
    FROM recording_coverage
    WHERE media_type = 'audio' AND status = 'uploaded'
    ORDER BY period_start_utc ASC
    """
)


@dataclass
class CoverageDiscoveryResult:
    creadas: int
    ya_existian: int
    estaciones_sin_medio: set[str]


class CoverageDiscoveryService:
    def __init__(
        self,
        grabaciones: GrabacionRepository,
        medios: MedioRepository,
        programas: ProgramaRepository,
        coverage_session: Session,
    ):
        self._grabaciones = grabaciones
        self._medios = medios
        self._programas = programas
        self._coverage = coverage_session

    def discover(self) -> CoverageDiscoveryResult:
        creadas = 0
        ya_existian = 0
        estaciones_sin_medio: set[str] = set()

        for stream_id, period_start_utc, period_end_utc, s3_key in self._coverage.execute(_QUERY):
            if self._grabaciones.get_by_s3_key(s3_key) is not None:
                ya_existian += 1
                continue

            try:
                programa_id = self._resolve_programa_id(stream_id)
            except EstacionNoRegistrada:
                estaciones_sin_medio.add(stream_id)
                continue

            grabacion = Grabacion(
                programa_id=programa_id,
                s3_key=s3_key,
                fecha_inicio=period_start_utc,
                fecha_fin=period_end_utc,
                estado=EstadoGrabacion.PENDIENTE,
            )
            self._grabaciones.add(grabacion)
            creadas += 1

        if creadas:
            self._grabaciones.commit()

        if estaciones_sin_medio:
            logger.error(
                "estaciones sin Medio registrado, filas de recording_coverage ignoradas",
                extra={"extra_fields": {"estaciones": sorted(estaciones_sin_medio)}},
            )

        return CoverageDiscoveryResult(
            creadas=creadas,
            ya_existian=ya_existian,
            estaciones_sin_medio=estaciones_sin_medio,
        )

    def _resolve_programa_id(self, stream_id: str):
        medio = self._medios.get_by_codigo(stream_id)
        if medio is None:
            raise EstacionNoRegistrada(stream_id)
        programa = self._programas.get_first_by_medio_id(medio.id)
        if programa is None:
            raise EstacionNoRegistrada(stream_id)
        return programa.id
