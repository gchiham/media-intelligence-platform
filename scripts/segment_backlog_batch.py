"""Segmenta el backlog usando la Batch API de Anthropic (50% mas barato).

Ver src/modules/ai/batch.py para el diseño y docs/EFFICIENCY_REVIEW.md §4
para la motivacion. Resumen: al 2026-07-22 quedaban ~14,200 grabaciones sin
segmentar (~91,000 llamadas al LLM); mandarlas por batch en vez de
sincronicamente es la reduccion de costo mas grande disponible.

Este script NO genera Noticias -- solo llena `segmentation_cache`. El clipping
y la persistencia los sigue haciendo el pipeline normal leyendo esa cache
(ver scripts/process_cached_segments.py), asi que toda la logica de mapeo a
tiempo, corte de audio, versionado e idempotencia queda intacta.

Uso:
    python scripts/segment_backlog_batch.py --submit --limit 200
    python scripts/segment_backlog_batch.py --status
    python scripts/segment_backlog_batch.py --collect
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from anthropic import Anthropic  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.ai.batch import (  # noqa: E402
    BatchSegmentationClient,
    build_chunk_requests,
    build_custom_id,
)
from src.modules.ai.models import (  # noqa: E402
    EstadoSegmentationBatch,
    SegmentationBatch,
    SegmentationCache,
)
from src.modules.ai.repeated_content import RepeatedContentIndex  # noqa: E402
from src.modules.ai.schemas import Word  # noqa: E402
from src.modules.recordings.models import EstadoGrabacion, Grabacion, Transcripcion  # noqa: E402


def _pendientes(session: Session, limit: int) -> list[tuple[Grabacion, Transcripcion]]:
    """Grabaciones transcritas que todavia no tienen segmentos cacheados."""
    ya_cacheadas = select(SegmentationCache.grabacion_id)
    stmt = (
        select(Grabacion, Transcripcion)
        .join(Transcripcion, Transcripcion.grabacion_id == Grabacion.id)
        .where(Grabacion.estado == EstadoGrabacion.PROCESADA)
        .where(Grabacion.id.notin_(ya_cacheadas))
        .order_by(Grabacion.fecha_inicio.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).all())


def _words(transcripcion: Transcripcion) -> list[Word]:
    return [Word(**w) for w in (transcripcion.segmentos or {}).get("words", [])]


def submit(limit: int, saltar_publicidad: bool) -> None:
    if not settings.anthropic_api_key:
        raise SystemExit("falta ANTHROPIC_API_KEY")

    client = BatchSegmentationClient(
        Anthropic(api_key=settings.anthropic_api_key.get_secret_value()),
        model=settings.anthropic_model,
    )

    with Session(get_engine()) as session:
        filas = _pendientes(session, limit)
        if not filas:
            print("no hay grabaciones pendientes de segmentar")
            return

        indice = RepeatedContentIndex(session) if saltar_publicidad else None
        peticiones = []
        saltados = 0
        for grabacion, transcripcion in filas:
            words = _words(transcripcion)
            if not words:
                continue
            for p in build_chunk_requests(
                str(grabacion.id), words, model=settings.anthropic_model
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
                modelo=settings.anthropic_model,
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
    client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
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
            remoto = client.messages.batches.retrieve(batch.anthropic_batch_id)
            print(
                f"{batch.anthropic_batch_id}  {remoto.processing_status:12} "
                f"requests={batch.total_requests}  counts={remoto.request_counts}"
            )


def collect() -> None:
    anthropic_client = Anthropic(api_key=settings.anthropic_api_key.get_secret_value())
    client = BatchSegmentationClient(anthropic_client)

    with Session(get_engine()) as session:
        abiertos = session.scalars(
            select(SegmentationBatch).where(
                SegmentationBatch.estado == EstadoSegmentationBatch.ENVIADO
            )
        ).all()

        for batch in abiertos:
            if not client.is_ended(batch.anthropic_batch_id):
                print(f"{batch.anthropic_batch_id}: todavia procesando, se omite")
                continue

            rangos = {k: (v[0], v[1]) for k, v in (batch.rangos or {}).items()}
            resultados = client.collect(batch.anthropic_batch_id, rangos)

            guardadas = 0
            for grabacion_id, segmentos in resultados.por_grabacion.items():
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
    args = parser.parse_args()

    if args.submit:
        submit(args.limit, saltar_publicidad=not args.sin_filtro_publicidad)
    elif args.status:
        status()
    elif args.collect:
        collect()
    else:
        parser.error("elegi --submit, --status o --collect")


if __name__ == "__main__":
    main()
