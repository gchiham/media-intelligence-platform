"""Reconciliacion S3 -> Postgres para grabaciones huerfanas: PROCESANDO
cuyo resultado ya esta en `s3://TRANSCRIBE_OUTPUT_BUCKET/transcripts/...`
pero nunca llego a Postgres porque el worker de chepita nunca publico (o
publico y el mensaje se perdio) el evento en `media-intel-transcription-done`.

A diferencia de `consume_transcription_results.py` (que consume la cola en
tiempo real), este script lista el bucket una sola vez y cruza contra
`grabaciones.estado=PROCESANDO` directamente -- pensado para correrse
on-demand cuando se sabe que hay un backlog de huerfanas (ver
docs/INGESTION_DESIGN.md), no como cron recurrente.

Soporta grabaciones sin `_words.json` (anteriores a 2026-07-19, antes de
`word_timestamps`): en ese caso usa el `.txt` plano como texto_completo,
con segmentos vacios.

Uso: python scripts/backfill_transcriptions_from_s3.py
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.recordings.models import EstadoGrabacion, Transcripcion  # noqa: E402
from src.modules.recordings.repositories import GrabacionRepository, TranscripcionRepository  # noqa: E402

PROVIDER_NAME = "faster-whisper-small"


def _list_s3_keys(s3, bucket: str) -> set[str]:
    keys: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="transcripts/"):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def _fetch_transcript(s3, bucket: str, station: str, hour_key: str, existing_keys: set[str]):
    """Devuelve (texto_completo, segmentos) o None si no esta en S3 todavia."""
    words_key = f"transcripts/{station}/{hour_key}_words.json"
    txt_key = f"transcripts/{station}/{hour_key}.txt"

    if words_key in existing_keys:
        obj = s3.get_object(Bucket=bucket, Key=words_key)
        words = json.loads(obj["Body"].read())
        texto = " ".join(w["word"] for w in words)
        return texto, {"words": words}

    if txt_key in existing_keys:
        obj = s3.get_object(Bucket=bucket, Key=txt_key)
        texto = obj["Body"].read().decode("utf-8")
        return texto, {}

    return None


def main() -> None:
    bucket = settings.transcribe_output_bucket
    s3 = boto3.client("s3", region_name=settings.aws_region)

    print(f"listando s3://{bucket}/transcripts/ ...", flush=True)
    existing_keys = _list_s3_keys(s3, bucket)
    print(f"objetos encontrados en s3: {len(existing_keys)}", flush=True)

    with Session(get_engine()) as session:
        grabaciones = GrabacionRepository(session)
        transcripciones = TranscripcionRepository(session)

        stmt = select(grabaciones.model).where(grabaciones.model.estado == EstadoGrabacion.PROCESANDO)
        candidatas = list(session.scalars(stmt))
        print(f"candidatas (estado=procesando): {len(candidatas)}", flush=True)

        # Se extraen los valores planos ANTES de entrar al thread pool -- los
        # objetos ORM quedan atados a esta Session (no es thread-safe), asi
        # que los hilos nunca deben tocar `grabacion` directamente, solo estos
        # valores capturados en el hilo principal.
        candidatas_planas = [
            (g.id, g.s3_key.split("/", 1)[0], g.fecha_inicio.strftime("%Y-%m-%dT%HZ"))
            for g in candidatas
        ]

        def _resolve(grabacion_id, station, hour_key):
            try:
                result = _fetch_transcript(s3, bucket, station, hour_key, existing_keys)
            except Exception as exc:  # noqa: BLE001 -- error de red/parseo puntual, no debe tumbar la corrida
                return grabacion_id, exc
            return grabacion_id, result

        backfilled = 0
        aun_pendientes = 0
        ya_existian = 0
        fallidas = 0

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {
                pool.submit(_resolve, gid, station, hour_key): gid
                for gid, station, hour_key in candidatas_planas
            }
            for i, future in enumerate(as_completed(futures), start=1):
                grabacion_id, result = future.result()

                if isinstance(result, Exception):
                    fallidas += 1
                    print(f"  [{i}] error resolviendo {grabacion_id}: {result}", flush=True)
                    continue

                if result is None:
                    aun_pendientes += 1
                    continue

                try:
                    if transcripciones.get_by_grabacion_id(grabacion_id) is not None:
                        ya_existian += 1
                        continue

                    texto, segmentos = result
                    transcripciones.add(Transcripcion(
                        grabacion_id=grabacion_id,
                        texto_completo=texto,
                        segmentos=segmentos,
                        proveedor=PROVIDER_NAME,
                    ))
                    grabacion = grabaciones.get_by_id(grabacion_id)
                    grabacion.estado = EstadoGrabacion.PROCESADA
                    grabaciones.commit()
                    backfilled += 1
                except Exception as exc:  # noqa: BLE001 -- una fila mala no debe tumbar el resto del backfill
                    session.rollback()
                    fallidas += 1
                    print(f"  [{i}] error guardando {grabacion_id}: {exc}", flush=True)
                    continue

                if backfilled % 200 == 0:
                    print(f"  ... {i}/{len(candidatas)} revisadas, {backfilled} backfilled hasta ahora", flush=True)

        print(
            f"listo: backfilled={backfilled} ya_existian={ya_existian} "
            f"aun_sin_resultado_en_s3={aun_pendientes} fallidas={fallidas}",
            flush=True,
        )


if __name__ == "__main__":
    main()
