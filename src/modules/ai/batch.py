"""Segmentacion por la Batch API de Anthropic -- camino asincronico para el
backlog, separado del camino sincronico de `POST /pipeline/process`.

Por que existe: la Batch API cuesta **50% menos** que la llamada sincronica y
esta carga no necesita respuesta inmediata (ver docs/EFFICIENCY_REVIEW.md §4).
Al 2026-07-22 quedaban ~14,200 grabaciones sin segmentar (~91,000 llamadas al
LLM): correrlas por batch en vez de sincronicamente es la diferencia mas
grande de costo del proyecto, y no requiere degradar el modelo ni el prompt.

**No reemplaza al camino sincronico.** `POST /pipeline/process` sigue usando
`AnthropicAnalysisProvider` porque un batch puede tardar hasta 24 h y ese
endpoint es interactivo. Los dos comparten `build_request_params`/
`parse_segments` justamente para que no se desincronicen las reglas.

Flujo de uso (ver scripts/segment_backlog_batch.py):

    submit  -> se arman los chunks de N grabaciones, se manda un solo batch,
               se guarda el batch_id en `segmentation_batches`.
    collect -> cuando el batch termina, se bajan los resultados y se guardan
               los segmentos en `segmentation_cache`, uno por grabacion.

El clipping/persistencia de Noticia NO ocurre aca: eso lo sigue haciendo el
pipeline normal, leyendo los segmentos ya cacheados (ver
PrecomputedAnalysisProvider). Asi el batch solo saca la llamada al LLM del
camino critico, sin duplicar la logica de mapeo a tiempo, corte de audio,
versionado ni idempotencia.
"""
import json
from dataclasses import dataclass, field

from src.modules.ai.chunking import chunk_words
from src.modules.ai.providers.anthropic_provider import (
    _TOOL_NAME,
    build_request_params,
    parse_segments,
)
from src.modules.ai.schemas import NewsSegment, Word
from src.shared.logging_utils import get_logger

logger = get_logger("ai_batch")

# Anthropic limita custom_id a 64 caracteres. Un UUID son 36, asi que
# "<uuid>__<indice>" entra con margen incluso con indices de 3 digitos.
_SEPARADOR = "__"


def build_custom_id(grabacion_id: str, chunk_index: int) -> str:
    return f"{grabacion_id}{_SEPARADOR}{chunk_index}"


def parse_custom_id(custom_id: str) -> tuple[str, int]:
    grabacion_id, _, idx = custom_id.rpartition(_SEPARADOR)
    return grabacion_id, int(idx)


@dataclass
class ChunkRequest:
    """Un chunk listo para mandar, con el rango de palabras que cubre.

    `lo`/`hi` se guardan porque al recolectar el resultado hay que validar que
    el modelo no haya inventado indices fuera del chunk que vio -- y para ese
    entonces ya no tenemos el chunk original en memoria.
    """

    grabacion_id: str
    chunk_index: int
    lo: int
    hi: int
    params: dict


@dataclass
class BatchResults:
    por_grabacion: dict[str, list[NewsSegment]] = field(default_factory=dict)
    errores: list[str] = field(default_factory=list)
    expirados: int = 0


def build_chunk_requests(
    grabacion_id: str, words: list[Word], model: str, chunk_size: int = 600
) -> list[ChunkRequest]:
    peticiones: list[ChunkRequest] = []
    for idx, chunk in enumerate(c for c in chunk_words(words, chunk_size) if c):
        peticiones.append(
            ChunkRequest(
                grabacion_id=grabacion_id,
                chunk_index=idx,
                lo=chunk[0].index,
                hi=chunk[-1].index,
                params=build_request_params(chunk, model),
            )
        )
    return peticiones


class BatchSegmentationClient:
    def __init__(self, client, model: str = "claude-sonnet-5"):
        self._client = client
        self._model = model

    def submit(self, peticiones: list[ChunkRequest]) -> str:
        """Manda todos los chunks como un solo batch y devuelve su id."""
        if not peticiones:
            raise ValueError("no hay chunks que mandar")

        requests = [
            {
                "custom_id": build_custom_id(p.grabacion_id, p.chunk_index),
                "params": p.params,
            }
            for p in peticiones
        ]
        batch = self._client.messages.batches.create(requests=requests)
        logger.info(
            "batch enviado",
            extra={"extra_fields": {"batch_id": batch.id, "requests": len(requests)}},
        )
        return batch.id

    def is_ended(self, batch_id: str) -> bool:
        batch = self._client.messages.batches.retrieve(batch_id)
        return batch.processing_status == "ended"

    def collect(self, batch_id: str, rangos: dict[str, tuple[int, int]]) -> BatchResults:
        """Baja los resultados y los agrupa por grabacion.

        `rangos` mapea custom_id -> (lo, hi) para poder validar los indices
        devueltos, igual que hace el camino sincronico.

        Un chunk que falle no invalida la grabacion entera: se registra en
        `errores` y los demas chunks de esa misma grabacion se conservan. Es
        deliberado -- perder un chunk de 600 palabras degrada esa hora, pero
        descartar las otras 6 llamadas ya pagadas seria peor.
        """
        salida = BatchResults()

        for entrada in self._client.messages.batches.results(batch_id):
            custom_id = entrada.custom_id
            grabacion_id, _ = parse_custom_id(custom_id)
            salida.por_grabacion.setdefault(grabacion_id, [])

            tipo = entrada.result.type
            if tipo == "expired":
                salida.expirados += 1
                salida.errores.append(f"{custom_id}: expirado")
                continue
            if tipo != "succeeded":
                salida.errores.append(f"{custom_id}: {tipo}")
                continue

            raw = self._extract_tool_input(entrada.result.message)
            if raw is None:
                salida.errores.append(f"{custom_id}: sin tool call valida")
                continue

            lo, hi = rangos.get(custom_id, (0, 10**9))
            try:
                salida.por_grabacion[grabacion_id].extend(parse_segments(raw, lo, hi))
            except Exception as exc:  # noqa: BLE001 -- un chunk malformado no tumba el resto
                salida.errores.append(f"{custom_id}: {exc}")

        return salida

    @staticmethod
    def _extract_tool_input(message) -> dict | None:
        for block in message.content:
            if block.type == "tool_use" and block.name == _TOOL_NAME:
                entrada = block.input
                if isinstance(entrada, str):
                    entrada = json.loads(entrada)
                if "news" in entrada:
                    return entrada
        return None
