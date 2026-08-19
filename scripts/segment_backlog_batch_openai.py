"""Segmenta el backlog usando la Batch API de OpenAI (50% mas barato) --
mismo diseño que scripts/segment_backlog_batch.py (Anthropic), ver
src/modules/ai/openai_batch.py para el detalle del formato.

Reusa la tabla `segmentation_batches` tal cual existe hoy en produccion (sin
migracion nueva): el batch id de OpenAI se guarda en la columna
`anthropic_batch_id` pese al nombre -- es solo un string, y las dos APIs no
comparten formato pero tampoco chocan (`batch_...` de OpenAI vs `msgbatch_...`
de Anthropic). Ver la nota en docs/ o preguntar antes de "corregir" el nombre
de la columna con una migracion: la DB de prod tiene una migracion pendiente
sin correr (b8e4f21a7c30) y no es el momento de sumar otra.

Este script NO genera Noticias -- solo llena `segmentation_cache`. El
clipping y la persistencia los sigue haciendo el pipeline normal leyendo esa
cache (ver scripts/process_cached_segments.py).

Uso:
    python scripts/segment_backlog_batch_openai.py --submit --limit 200
    python scripts/segment_backlog_batch_openai.py --status
    python scripts/segment_backlog_batch_openai.py --collect
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import OpenAI  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.ai.models import (  # noqa: E402
    EstadoSegmentationBatch,
    SegmentationBatch,
    SegmentationCache,
)
from src.modules.ai.openai_batch import OpenAIBatchSegmentationClient, build_chunk_requests
from src.modules.ai.batch import build_custom_id  # noqa: E402
from src.modules.ai.client_profiles import cargar_perfiles  # noqa: E402
from src.modules.ai.repeated_content import RepeatedContentIndex  # noqa: E402
from src.modules.ai.schemas import Word  # noqa: E402
from src.modules.recordings.models import EstadoGrabacion, Grabacion, Transcripcion  # noqa: E402


def _pendientes(
    session: Session,
    limit: int,
    fecha_desde: datetime | None = None,
    fecha_hasta: datetime | None = None,
    offset: int = 0,
) -> list[tuple[Grabacion, Transcripcion]]:
    """Grabaciones transcritas que todavia no tienen segmentos cacheados.

    Sin `--fecha-desde`/`--fecha-hasta` toma las mas recientes primero, igual
    que segment_backlog_batch.py (Anthropic) -- pero el backlog global de
    segmentacion es mucho mas grande que cualquier ventana puntual (se vio en
    la practica: 19,806 grabaciones sin segmentar contra 1,331 de un rango de
    3 dias), asi que sin fecha el orden `desc()` se come backlog nuevo antes
    de tocar el rango que se quiso procesar. Los filtros de fecha son en UTC,
    igual que enqueue_transcriptions.py -- convertir desde hora local antes de
    llamar."""
    ya_cacheadas = select(SegmentationCache.grabacion_id)
    stmt = (
        select(Grabacion, Transcripcion)
        .join(Transcripcion, Transcripcion.grabacion_id == Grabacion.id)
        .where(Grabacion.estado == EstadoGrabacion.PROCESADA)
        .where(Grabacion.id.notin_(ya_cacheadas))
    )
    if fecha_desde is not None:
        stmt = stmt.where(Grabacion.fecha_inicio >= fecha_desde)
    if fecha_hasta is not None:
        stmt = stmt.where(Grabacion.fecha_inicio < fecha_hasta)
    # `offset` existe para poder tener dos batches en vuelo a la vez (una por
    # key/organizacion, que es donde aplica el limite de tokens encolados de
    # OpenAI). Sin esto las dos tandas eligen las MISMAS grabaciones: el filtro
    # de "ya cacheadas" solo ve lo que se recolecto, y al enviar todavia no hay
    # nada recolectado. Paso en produccion: de 950 requests de la segunda tanda
    # solo 5 grabaciones eran nuevas, el resto trabajo duplicado ya pagado.
    stmt = stmt.order_by(Grabacion.fecha_inicio.desc()).offset(offset).limit(limit)
    return list(session.execute(stmt).all())


def _words(transcripcion: Transcripcion) -> list[Word]:
    return [Word(**w) for w in (transcripcion.segmentos or {}).get("words", [])]


def _parse_fecha(valor: str | None) -> datetime | None:
    if valor is None:
        return None
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


def _api_key() -> str:
    """OPENAI_API_KEY se quedo sin credito (credit_balance_exhausted, visto
    en produccion el 2026-08-13) -- OPENAI_API_KEY_2 es una cuenta/org
    distinta con credito disponible. No es parte de Settings (pydantic
    ignora env vars extra, igual que en scripts/filtro_joh_batch_openai.py),
    asi que se lee directo del entorno. Un batch abierto con una key no se
    puede consultar/recolectar con la otra (son orgs distintas) -- por eso
    hay que terminar de recolectar todo lo abierto de la key vieja antes de
    mandar el primer submit con la nueva."""
    key = os.environ.get("OPENAI_API_KEY_2")
    if key:
        return key
    if not settings.openai_api_key:
        raise SystemExit("falta OPENAI_API_KEY")
    return settings.openai_api_key.get_secret_value()


def submit(
    limit: int,
    saltar_publicidad: bool,
    fecha_desde: datetime | None = None,
    fecha_hasta: datetime | None = None,
    offset: int = 0,
) -> None:
    client = OpenAIBatchSegmentationClient(OpenAI(api_key=_api_key()))

    with Session(get_engine()) as session:
        filas = _pendientes(session, limit, fecha_desde, fecha_hasta, offset)
        if not filas:
            print("no hay grabaciones pendientes de segmentar")
            return

        indice = RepeatedContentIndex(session) if saltar_publicidad else None
        # Los perfiles viajan en el mensaje `user` de cada chunk: la relevancia
        # por cliente sale en la misma llamada que la segmentacion, sin una
        # segunda pasada ni una segunda ventana de batch. Si no hay ninguno
        # configurado, el prompt y el esquema son los de siempre.
        perfiles = cargar_perfiles(session)
        if perfiles:
            print(f"{len(perfiles)} perfiles de cliente: " + ", ".join(p.nombre for p in perfiles))

        peticiones = []
        saltados = 0
        for grabacion, transcripcion in filas:
            words = _words(transcripcion)
            if not words:
                continue
            for p in build_chunk_requests(
                str(grabacion.id), words, model=settings.openai_model, perfiles=perfiles
            ):
                # El filtro de publicidad se aplica aca, antes de pagar la
                # llamada -- es el unico punto donde el ahorro es real.
                if indice is not None:
                    chunk = words[p.chunk_index * 600 : (p.chunk_index + 1) * 600]
                    if indice.debe_saltarse(chunk):
                        saltados += 1
                        continue
                peticiones.append(p)

        if not peticiones:
            print(f"todo el contenido candidato quedo filtrado ({saltados} chunks de publicidad)")
            return

        batch_id = client.submit(peticiones)
        session.add(
            SegmentationBatch(
                anthropic_batch_id=batch_id,
                estado=EstadoSegmentationBatch.ENVIADO,
                modelo=settings.openai_model,
                total_requests=len(peticiones),
                rangos={
                    build_custom_id(p.grabacion_id, p.chunk_index): [p.lo, p.hi]
                    for p in peticiones
                },
            )
        )
        session.commit()

    print(
        f"batch enviado: {batch_id} | {len(peticiones)} chunks de {len(filas)} grabaciones"
        + (f" | {saltados} chunks saltados por publicidad" if saltados else "")
    )


def status() -> None:
    openai_client = OpenAI(api_key=_api_key())
    with Session(get_engine()) as session:
        abiertos = session.scalars(
            select(SegmentationBatch).where(
                SegmentationBatch.estado == EstadoSegmentationBatch.ENVIADO
            )
        ).all()
        if not abiertos:
            print("no hay batches abiertos")
            return
        for batch in abiertos:
            remoto = openai_client.batches.retrieve(batch.anthropic_batch_id)
            print(
                f"{batch.anthropic_batch_id}  {remoto.status:12} "
                f"requests={batch.total_requests}  counts={remoto.request_counts}"
            )


def collect() -> None:
    openai_client = OpenAI(api_key=_api_key())
    client = OpenAIBatchSegmentationClient(openai_client)

    with Session(get_engine()) as session:
        abiertos = session.scalars(
            select(SegmentationBatch).where(
                SegmentationBatch.estado == EstadoSegmentationBatch.ENVIADO
            )
        ).all()

        for batch in abiertos:
            # Las dos keys son organizaciones distintas y el limite de tokens
            # encolados es por organizacion, asi que se usan en paralelo para
            # duplicar el throughput. La contra: un batch enviado con una key
            # es invisible para la otra, y `retrieve` falla. Se omite en vez de
            # reventar, para que correr --collect con cualquiera de las dos
            # recolecte lo suyo y deje lo ajeno intacto para la otra pasada.
            try:
                terminado = client.is_ended(batch.anthropic_batch_id)
            except Exception as exc:  # noqa: BLE001
                print(f"{batch.anthropic_batch_id}: no visible con esta key ({type(exc).__name__}), se omite")
                continue
            if not terminado:
                print(f"{batch.anthropic_batch_id}: todavia procesando, se omite")
                continue

            rangos = {k: (v[0], v[1]) for k, v in (batch.rangos or {}).items()}
            # Se valida contra los perfiles de AHORA, no contra los del envio:
            # si un cliente se dio de baja mientras el batch corria, sus
            # veredictos no deben entrar. Un cliente nuevo tampoco aparece,
            # porque no estaba en el prompt que se mando.
            client_ids = {p.client_id for p in cargar_perfiles(session)}
            resultados = client.collect(
                batch.anthropic_batch_id, rangos, client_ids_validos=client_ids or None
            )

            guardadas = 0
            for grabacion_id, segmentos in resultados.por_grabacion.items():
                # Grabacion con chunks fallidos: no cachear (ver comentario en
                # segment_backlog_batch.py -- mismo criterio para ambos caminos).
                if grabacion_id in resultados.grabaciones_con_error:
                    continue
                existente = session.scalar(
                    select(SegmentationCache).where(
                        SegmentationCache.grabacion_id == grabacion_id
                    )
                )
                if existente is not None:
                    continue
                session.add(
                    SegmentationCache(
                        grabacion_id=grabacion_id,
                        segmentos=[s.model_dump(mode="json") for s in segmentos],
                        modelo=batch.modelo,
                        batch_id=batch.id,
                    )
                )
                guardadas += 1

            batch.estado = EstadoSegmentationBatch.COMPLETADO
            if resultados.errores:
                batch.error_mensaje = "; ".join(resultados.errores[:20])
            session.commit()

            print(
                f"{batch.anthropic_batch_id}: {guardadas} grabaciones cacheadas, "
                f"{len(resultados.grabaciones_con_error)} con error se reintentaran, "
                f"{len(resultados.errores)} chunks con error, {resultados.expirados} expirados"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--limit", type=int, default=200, help="grabaciones por batch")
    parser.add_argument(
        "--sin-filtro-publicidad",
        action="store_true",
        help="manda todos los chunks, sin saltar contenido repetido conocido",
    )
    parser.add_argument(
        "--fecha-desde", type=str, default=None, help="ISO 8601 UTC, ej. 2026-08-08T06:00:00Z"
    )
    parser.add_argument(
        "--fecha-hasta", type=str, default=None, help="ISO 8601 UTC, exclusivo"
    )
    parser.add_argument(
        "--offset", type=int, default=0,
        help="salta las primeras N grabaciones; permite dos tandas en vuelo sin solaparse",
    )
    args = parser.parse_args()

    if args.submit:
        submit(
            args.limit,
            saltar_publicidad=not args.sin_filtro_publicidad,
            fecha_desde=_parse_fecha(args.fecha_desde),
            fecha_hasta=_parse_fecha(args.fecha_hasta),
            offset=args.offset,
        )
    elif args.status:
        status()
    elif args.collect:
        collect()
    else:
        parser.error("elegi --submit, --status o --collect")


if __name__ == "__main__":
    main()
