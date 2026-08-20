"""DiscoveryService: escanea el S3 de captura externo y crea una Grabacion
(estado=PENDIENTE) por cada archivo de audio horario nuevo. Idempotente por
`s3_key` (unico) -- correr esto dos veces sobre el mismo backlog no duplica
filas. Ver docs/INGESTION_DESIGN.md.

No conoce SQS ni chepita -- solo hace S3 -> Postgres. El siguiente paso
(encolar lo PENDIENTE) es responsabilidad de QueueService.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.modules.media.repositories import MedioRepository, ProgramaRepository
from src.modules.recordings.models import EstadoGrabacion, Grabacion
from src.modules.recordings.repositories import GrabacionRepository
from src.shared.logging_utils import get_logger

logger = get_logger("discovery_service")

# "<station>/<year>/<month>/<YYYY-MM-DDTHHZ>.mp3" -- ver docs/INFRASTRUCTURE.md
_KEY_PATTERN = re.compile(
    r"^(?P<station>[^/]+)/(?P<year>\d{4})/(?P<month>\d{2})/"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2})Z\.mp3$"
)


# Estaciones dadas de baja del monitoreo: no se ingesta NADA de ellas, ni
# historico ni nuevo. Lo comparten los dos caminos de descubrimiento
# (DiscoveryService por S3 y CoverageDiscoveryService por recording_coverage),
# porque ambos corren en paralelo y tapar uno solo no sirve de nada.
#
# Por que estas cinco (medido sobre 2026-08-03 al 19, 5.723 grabaciones
# segmentadas y 66.197 noticias):
#
#   estacion       grabaciones  noticias  not/grab  menciones de cliente
#   fm_941                 264        75      0.28   1
#   suave_fm_teg           249       178      0.71   0
#   radio_valle            274       206      0.75   1
#   super_100              261       629      2.41   6
#   xy_sps                 260       771      2.97   4
#
# Contra canal_10, que saca 43.4 noticias por grabacion y 749 menciones. Las
# tres primeras son emisoras musicales (94.1 FM, Suave FM): 787 horas de audio
# en 17 dias para DOS menciones de cliente. Cada hora ingestada se paga tres
# veces -- GPU de transcripcion, tokens de segmentacion y clipping.
#
# Decision del 2026-08-20. Para reactivar una, sacarla de aca: su historico
# sigue en la base intacto (no se borro nada) y la ingesta se reanuda sola en
# la siguiente corrida del cron.
#
# OJO: esto corta la INGESTA, no la CAPTURA. mediaCAP (repo aparte) las sigue
# grabando y subiendo a S3; para dejar de pagar ese almacenamiento hay que
# sacarlas tambien de su config/stations.json.
EXCLUIDAS: frozenset[str] = frozenset({
    "fm_941",
    "suave_fm_teg",
    "radio_valle",
    "super_100",
    "xy_sps",
})


class EstacionNoRegistrada(Exception):
    """El archivo esta en S3 pero su carpeta de estacion no tiene un Medio
    sembrado todavia (ver scripts/seed_medios.py)."""

    def __init__(self, station: str):
        super().__init__(f"estacion '{station}' sin Medio registrado -- correr el seeder primero")
        self.station = station


@dataclass
class DiscoveryResult:
    creadas: int
    ya_existian: int
    ignoradas_no_reconocidas: int
    estaciones_sin_medio: set[str]
    omitidas_por_exclusion: int = 0


class DiscoveryService:
    def __init__(
        self,
        grabaciones: GrabacionRepository,
        medios: MedioRepository,
        programas: ProgramaRepository,
        s3_client,
        bucket: str,
    ):
        self._grabaciones = grabaciones
        self._medios = medios
        self._programas = programas
        self._s3 = s3_client
        self._bucket = bucket

    def discover(self) -> DiscoveryResult:
        creadas = 0
        ya_existian = 0
        ignoradas = 0
        excluidas = 0
        estaciones_sin_medio: set[str] = set()

        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                match = _KEY_PATTERN.match(key)
                if not match:
                    ignoradas += 1
                    continue

                # La exclusion se chequea ANTES del get_by_s3_key: si no, cada
                # archivo de una estacion dada de baja cuesta una consulta a la
                # base, y son cientos por corrida del cron (cada 5 min).
                station = match.group("station")
                if station in EXCLUIDAS:
                    excluidas += 1
                    continue

                if self._grabaciones.get_by_s3_key(key) is not None:
                    ya_existian += 1
                    continue

                try:
                    programa_id = self._resolve_programa_id(station)
                except EstacionNoRegistrada:
                    estaciones_sin_medio.add(station)
                    continue

                fecha_inicio = datetime.strptime(match.group("ts"), "%Y-%m-%dT%H").replace(
                    tzinfo=timezone.utc
                )
                grabacion = Grabacion(
                    programa_id=programa_id,
                    s3_key=key,
                    fecha_inicio=fecha_inicio,
                    fecha_fin=fecha_inicio + timedelta(hours=1),
                    estado=EstadoGrabacion.PENDIENTE,
                )
                self._grabaciones.add(grabacion)
                creadas += 1

        if creadas:
            self._grabaciones.commit()

        if estaciones_sin_medio:
            logger.error(
                "estaciones sin Medio registrado, archivos ignorados",
                extra={"extra_fields": {"estaciones": sorted(estaciones_sin_medio)}},
            )

        return DiscoveryResult(
            creadas=creadas,
            ya_existian=ya_existian,
            ignoradas_no_reconocidas=ignoradas,
            estaciones_sin_medio=estaciones_sin_medio,
            omitidas_por_exclusion=excluidas,
        )

    def _resolve_programa_id(self, station: str):
        medio = self._medios.get_by_codigo(station)
        if medio is None:
            raise EstacionNoRegistrada(station)
        programa = self._programas.get_first_by_medio_id(medio.id)
        if programa is None:
            raise EstacionNoRegistrada(station)
        return programa.id
