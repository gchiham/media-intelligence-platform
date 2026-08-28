"""Segmentacion por la Batch API de OpenAI -- mismo diseño que
src/modules/ai/batch.py (Anthropic), pero hablando el formato de OpenAI
(archivo JSONL subido + /v1/chat/completions por linea, en vez de un array de
requests con tool-use).

`ChunkRequest`, `BatchResults`, `build_custom_id`/`parse_custom_id` son
genericos (no dependen del proveedor) y se reusan tal cual de
src/modules/ai/batch.py -- solo el cliente que habla con la API y el armado
del cuerpo de cada request cambian.

El clipping/persistencia de Noticia sigue sin ocurrir aca, igual que en el
camino de Anthropic: esto solo llena `segmentation_cache` (ver
scripts/segment_backlog_batch_openai.py); el pipeline normal, leyendo esa
cache via PrecomputedAnalysisProvider, hace el resto.
"""
import json
import time

from src.modules.ai.batch import BatchResults, ChunkRequest, build_custom_id, parse_custom_id
from src.modules.ai.chunking import chunk_words
from src.modules.ai.providers.prompts import (
    BOUNDARY_CLIENT_RESPONSE_SCHEMA,
    BOUNDARY_RESPONSE_SCHEMA,
    BOUNDARY_SYSTEM_PROMPT,
    CONTEXTO_PALABRAS,
    MAX_KEYWORDS,
    render_chunk_con_contexto,
    render_chunk_con_contexto_y_perfiles,
)
from src.modules.ai.schemas import NewsSegment, PerfilCliente, Word
from src.modules.ai.stitching import stitch_por_chunk
from src.shared.logging_utils import get_logger

logger = get_logger("ai_openai_batch")

# `batches.create()` devuelve un batch en estado `validating`: que no lance no
# significa que el batch vaya a correr. La validacion pasa a `failed` segundos
# despues si OpenAI no puede leer el JSONL (tope de gasto alcanzado, cuota de
# tokens encolados, archivo no visible). Sin esperarla, el submit reporta
# "batch enviado" para batches que nunca procesaron nada -- y la segmentacion
# se detiene en silencio. Paso el 2026-08-28: 4 batches seguidos "enviados",
# los 4 en failed, 141 grabaciones sin segmentar sin una sola linea de error.
ESPERA_VALIDACION_SEG = 45
INTERVALO_VALIDACION_SEG = 3


class BatchRechazado(RuntimeError):
    """El batch se creo pero OpenAI lo rechazo al validarlo."""


def _build_request_body_boundary(
    chunk: list[Word],
    contexto: list[Word],
    model: str,
    perfiles: list[PerfilCliente] | None = None,
) -> dict:
    """Como openai_provider.build_request_body pero con deteccion de corte en
    el limite del chunk y, si se pasan perfiles, relevancia por cliente en la
    misma llamada.

    Es una funcion aparte y no un flag de aquella porque el camino sincronico
    (Claude primario) no debe cambiar: nada de esto esta validado ahi.

    El system prompt es el mismo con o sin perfiles -- lo unico que cambia es
    el mensaje `user` y el esquema de salida. Asi dar de alta un cliente no
    puede degradar la segmentacion ni el stitching medidos."""
    if perfiles:
        contenido = render_chunk_con_contexto_y_perfiles(chunk, contexto, perfiles)
        schema = BOUNDARY_CLIENT_RESPONSE_SCHEMA
    else:
        contenido = render_chunk_con_contexto(chunk, contexto)
        schema = BOUNDARY_RESPONSE_SCHEMA

    return {
        "model": model,
        "messages": [
            {"role": "system", "content": BOUNDARY_SYSTEM_PROMPT},
            {"role": "user", "content": contenido},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "news_segments", "schema": schema, "strict": True},
        },
    }


def build_chunk_requests(
    grabacion_id: str,
    words: list[Word],
    model: str,
    chunk_size: int = 600,
    perfiles: list[PerfilCliente] | None = None,
) -> list[ChunkRequest]:
    chunks = [c for c in chunk_words(words, chunk_size) if c]
    peticiones: list[ChunkRequest] = []
    for idx, chunk in enumerate(chunks):
        # Cada chunk ve las ultimas CONTEXTO_PALABRAS del anterior para poder
        # darse cuenta de que arranca a mitad de una noticia -- sin eso no lo
        # detecta nunca (ver src/modules/ai/stitching.py). El primero no tiene
        # anterior y va con contexto vacio.
        contexto = chunks[idx - 1][-CONTEXTO_PALABRAS:] if idx > 0 else []
        peticiones.append(
            ChunkRequest(
                grabacion_id=grabacion_id,
                chunk_index=idx,
                lo=chunk[0].index,
                hi=chunk[-1].index,
                params=_build_request_body_boundary(chunk, contexto, model, perfiles),
            )
        )
    return peticiones


def _parse_con_boundary(
    raw: dict, lo: int, hi: int, client_ids_validos: set[str] | None = None
) -> list[dict]:
    """Como openai_provider.parse_segments, pero conserva boundary_status /
    boundary_confidence, que `NewsSegment` no modela (son transitorios: se
    usan para decidir la fusion y se descartan antes de cachear).
    `client_relevance` si es parte de `NewsSegment`, asi que sobrevive solo.

    El filtro de rango sigue siendo el mismo y ademas cumple una funcion
    nueva: descarta cualquier indice que el modelo haya sacado del bloque de
    CONTEXTO PREVIO en vez del texto que le tocaba segmentar.

    `client_ids_validos` cumple el papel equivalente para la relevancia:
    descarta veredictos sobre un cliente que no se mando en el prompt (el
    modelo a veces devuelve un id ligeramente distinto o inventado). Es el
    mismo criterio defensivo que con los rangos: ante algo que no se pidio,
    tirarlo en silencio antes de que llegue a la base.
    """
    salida = []
    for item in raw.get("news", []):
        item = dict(item)
        item["keywords"] = item.get("keywords", [])[:MAX_KEYWORDS]
        if client_ids_validos is not None:
            item["client_relevance"] = [
                cr
                for cr in item.get("client_relevance", [])
                if cr.get("client_id") in client_ids_validos
            ]
        seg = NewsSegment.model_validate(
            {k: v for k, v in item.items() if k in NewsSegment.model_fields}
        )
        if not (lo <= seg.start_word <= seg.end_word <= hi):
            continue
        d = seg.model_dump(mode="json")
        d["boundary_status"] = item.get("boundary_status", "complete")
        d["boundary_confidence"] = item.get("boundary_confidence", 0.0)
        salida.append(d)
    return salida


def _sin_nul(valor):
    """Postgres/JSONB no acepta el byte NUL dentro de texto
    ("unsupported Unicode escape sequence") -- visto en produccion: el
    modelo a veces mete un \\u0000 suelto en medio de un titulo. Eso llega
    como texto escapado en el JSON crudo y solo se vuelve un caracter NUL
    real despues de json.loads, asi que hay que limpiar el resultado ya
    parseado, no el string crudo."""
    if isinstance(valor, str):
        return valor.replace("\x00", "")
    if isinstance(valor, list):
        return [_sin_nul(v) for v in valor]
    if isinstance(valor, dict):
        return {k: _sin_nul(v) for k, v in valor.items()}
    return valor


class OpenAIBatchSegmentationClient:
    def __init__(self, client):
        self._client = client

    def submit(self, peticiones: list[ChunkRequest]) -> str:
        """Sube un JSONL con todos los chunks y manda un solo batch. Devuelve
        el batch id de OpenAI (formato `batch_...`, no relacionado con
        Anthropic pese al nombre de la columna donde se guarda -- ver
        scripts/segment_backlog_batch_openai.py)."""
        if not peticiones:
            raise ValueError("no hay chunks que mandar")

        lineas = [
            json.dumps(
                {
                    "custom_id": build_custom_id(p.grabacion_id, p.chunk_index),
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": p.params,
                },
                ensure_ascii=False,
            )
            for p in peticiones
        ]
        jsonl = ("\n".join(lineas) + "\n").encode("utf-8")

        subido = self._client.files.create(file=("batch.jsonl", jsonl), purpose="batch")
        batch = self._client.batches.create(
            input_file_id=subido.id,
            endpoint="/v1/chat/completions",
            completion_window="24h",
        )
        self._esperar_validacion(batch.id, len(peticiones))
        return batch.id

    def _esperar_validacion(self, batch_id: str, n_peticiones: int) -> None:
        """Confirma que el batch paso la validacion antes de darlo por enviado.

        Se queda esperando mientras siga en `validating`, hasta
        ESPERA_VALIDACION_SEG. Si termina en `failed` lanza BatchRechazado con
        el motivo real de OpenAI; si sigue validando al agotarse la espera lo
        loguea como advertencia y lo da por bueno (`collect` lo va a revisar
        igual), en vez de bloquear el cron indefinidamente.
        """
        limite = time.monotonic() + ESPERA_VALIDACION_SEG
        estado = "validating"
        while time.monotonic() < limite:
            batch = self._client.batches.retrieve(batch_id)
            estado = batch.status
            if estado != "validating":
                break
            time.sleep(INTERVALO_VALIDACION_SEG)

        if estado in ("failed", "expired", "cancelled"):
            errores = getattr(batch, "errors", None)
            datos = getattr(errores, "data", None) or []
            motivo = f"{datos[0].code}: {datos[0].message}" if datos else "sin detalle"
            logger.error(
                "batch RECHAZADO en validacion",
                extra={"extra_fields": {
                    "batch_id": batch_id, "requests": n_peticiones,
                    "estado": estado, "motivo": motivo,
                }},
            )
            raise BatchRechazado(f"batch {batch_id} quedo en '{estado}' -- {motivo}")

        if estado == "validating":
            logger.warning(
                "batch aun validando al agotarse la espera; se da por enviado",
                extra={"extra_fields": {"batch_id": batch_id, "requests": n_peticiones}},
            )
            return

        logger.info(
            "batch enviado",
            extra={"extra_fields": {
                "batch_id": batch_id, "requests": n_peticiones, "estado": estado,
            }},
        )

    def is_ended(self, batch_id: str) -> bool:
        batch = self._client.batches.retrieve(batch_id)
        return batch.status in ("completed", "failed", "expired", "cancelled")

    def collect(
        self,
        batch_id: str,
        rangos: dict[str, tuple[int, int]],
        client_ids_validos: set[str] | None = None,
    ) -> BatchResults:
        """Baja los resultados y los agrupa por grabacion, igual que el
        cliente de Anthropic. Un chunk que falle no invalida la grabacion
        entera -- se registra en `errores` y los demas chunks se conservan."""
        salida = BatchResults()
        batch = self._client.batches.retrieve(batch_id)

        if batch.status != "completed":
            # failed/expired/cancelled: no hay output_file_id que leer.
            salida.errores.append(f"batch {batch_id}: status={batch.status}, sin resultados")
            return salida

        # OpenAI separa exitos y fallos en dos archivos distintos --
        # output_file_id trae solo las lineas que llegaron a respuesta HTTP,
        # error_file_id trae las que fallaron antes de eso (rate limit, error
        # de la API). Si solo se lee output_file_id, un chunk fallido
        # simplemente no aparece en ningun lado: no cuenta como error, no
        # se reintenta, desaparece sin dejar rastro (visto en produccion:
        # request_counts.failed=11 pero el collect original reportaba "0
        # chunks con error" porque nunca miraba el archivo de errores).
        if batch.error_file_id:
            crudo_errores = self._client.files.content(batch.error_file_id).text
            for linea in crudo_errores.splitlines():
                if not linea.strip():
                    continue
                entrada = json.loads(linea)
                cid = entrada.get("custom_id", "?")
                salida.errores.append(f"{cid}: {entrada.get('error')}")
                # El chunk fallido no aparece en output_file_id: si no se marca
                # aca, su grabacion se cachea incompleta y nunca se reintenta.
                if cid != "?":
                    salida.grabaciones_con_error.add(parse_custom_id(cid)[0])

        # Se agrupa por (grabacion, indice de chunk) en vez de aplanar de una:
        # el stitching necesita saber que segmento vino de que chunk para
        # fusionar solo pares contiguos, y el JSONL de resultados no viene en
        # orden.
        por_grabacion_por_chunk: dict[str, dict[int, list[dict]]] = {}

        crudo = self._client.files.content(batch.output_file_id).text
        for linea in crudo.splitlines():
            if not linea.strip():
                continue
            entrada = json.loads(linea)
            custom_id = entrada["custom_id"]
            grabacion_id, chunk_index = parse_custom_id(custom_id)
            por_grabacion_por_chunk.setdefault(grabacion_id, {}).setdefault(chunk_index, [])

            if entrada.get("error"):
                salida.errores.append(f"{custom_id}: {entrada['error']}")
                salida.grabaciones_con_error.add(grabacion_id)
                continue

            cuerpo = (entrada.get("response") or {}).get("body") or {}
            if entrada.get("response", {}).get("status_code") != 200:
                salida.errores.append(f"{custom_id}: http {entrada.get('response', {}).get('status_code')}")
                salida.grabaciones_con_error.add(grabacion_id)
                continue

            try:
                contenido = cuerpo["choices"][0]["message"]["content"]
                raw = _sin_nul(json.loads(contenido))
                lo, hi = rangos.get(custom_id, (0, 10**9))
                por_grabacion_por_chunk[grabacion_id][chunk_index].extend(
                    _parse_con_boundary(raw, lo, hi, client_ids_validos)
                )
            except Exception as exc:  # noqa: BLE001 -- un chunk malformado no tumba el resto
                salida.errores.append(f"{custom_id}: {exc}")
                salida.grabaciones_con_error.add(grabacion_id)

        # Fusion de noticias partidas + vuelta al contrato que espera quien
        # llama: NewsSegment sin los campos de boundary, que son transitorios
        # y no se cachean.
        fusionadas = 0
        for grabacion_id, por_chunk in por_grabacion_por_chunk.items():
            segmentos = stitch_por_chunk(por_chunk)
            fusionadas += sum(len(v) for v in por_chunk.values()) - len(segmentos)
            salida.por_grabacion[grabacion_id] = [
                NewsSegment.model_validate(
                    {k: v for k, v in s.items() if k in NewsSegment.model_fields}
                )
                for s in segmentos
            ]
        if fusionadas:
            logger.info(
                "noticias reconstruidas en limite de chunk",
                extra={"extra_fields": {"batch_id": batch_id, "fusiones": fusionadas}},
            )

        # OpenAI no distingue "expired" a nivel de linea individual como
        # Anthropic -- un batch expirado entero cae en el `if` de arriba.
        return salida
