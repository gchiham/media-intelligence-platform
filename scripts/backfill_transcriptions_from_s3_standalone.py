"""Version standalone de backfill_transcriptions_from_s3.py, pensada para
correr FUERA del host de Postgres (ej. desde una instancia de chepita, que
tiene CPU dedicada en vez de burstable) -- no depende del paquete `src` del
repo ni de un checkout completo, solo `psycopg` + `boto3` puros.

Por que existe una version separada: la primera corrida de
backfill_transcriptions_from_s3.py se hizo desde la misma instancia t3.small
que aloja Postgres, con 20 hilos y un commit por fila -- saturó esa
instancia chica lo suficiente para tumbar la API de producción (ver
incidente 2026-07-22). Esta version:
  - No corre en el mismo host que Postgres (se ejecuta en chepita).
  - Concurrencia mas baja (default 6, no 20).
  - Batch de commits (default 100 filas por commit, no 1) para reducir
    fsyncs de WAL contra Postgres.

Uso:
  export DATABASE_URL_RAW="postgresql://postgres:PASSWORD@172.31.5.81:5433/media_intelligence"
  python3 backfill_transcriptions_from_s3_standalone.py
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
import psycopg

BUCKET = os.environ.get("TRANSCRIBE_OUTPUT_BUCKET", "media-intel-transcribe-050871635829")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_WORKERS = int(os.environ.get("BACKFILL_MAX_WORKERS", "6"))
BATCH_SIZE = int(os.environ.get("BACKFILL_BATCH_SIZE", "100"))
PROVIDER_NAME = "faster-whisper-small"


def _list_s3_keys(s3, bucket: str) -> set[str]:
    keys: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="transcripts/"):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def _fetch_transcript(s3, bucket, station, hour_key, existing_keys):
    words_key = f"transcripts/{station}/{hour_key}_words.json"
    txt_key = f"transcripts/{station}/{hour_key}.txt"

    if words_key in existing_keys:
        obj = s3.get_object(Bucket=bucket, Key=words_key)
        words = json.loads(obj["Body"].read())
        texto = " ".join(w["word"] for w in words)
        return texto, json.dumps({"words": words})

    if txt_key in existing_keys:
        obj = s3.get_object(Bucket=bucket, Key=txt_key)
        texto = obj["Body"].read().decode("utf-8")
        return texto, json.dumps({})

    return None


def main() -> None:
    dsn = os.environ.get("DATABASE_URL_RAW")
    if not dsn:
        raise SystemExit("falta DATABASE_URL_RAW en el entorno (dsn plano de psycopg, no el de sqlalchemy)")

    s3 = boto3.client("s3", region_name=AWS_REGION)

    print(f"listando s3://{BUCKET}/transcripts/ ...", flush=True)
    existing_keys = _list_s3_keys(s3, BUCKET)
    print(f"objetos encontrados en s3: {len(existing_keys)}", flush=True)

    conn = psycopg.connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, s3_key, fecha_inicio FROM grabaciones WHERE estado = 'procesando'")
            candidatas = cur.fetchall()
        print(f"candidatas (estado=procesando): {len(candidatas)}", flush=True)

        planas = [
            (str(gid), s3_key.split("/", 1)[0], fecha_inicio.strftime("%Y-%m-%dT%HZ"))
            for gid, s3_key, fecha_inicio in candidatas
        ]

        def _resolve(grabacion_id, station, hour_key):
            try:
                result = _fetch_transcript(s3, BUCKET, station, hour_key, existing_keys)
            except Exception as exc:  # noqa: BLE001 -- error puntual de red/parseo, no debe tumbar la corrida
                return grabacion_id, exc
            return grabacion_id, result

        backfilled = 0
        aun_pendientes = 0
        fallidas = 0
        pendientes_de_commit = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(_resolve, gid, station, hour): gid for gid, station, hour in planas}
            for i, future in enumerate(as_completed(futures), start=1):
                grabacion_id, result = future.result()

                if isinstance(result, Exception):
                    fallidas += 1
                    print(f"  [{i}] error resolviendo {grabacion_id}: {result}", flush=True)
                    continue

                if result is None:
                    aun_pendientes += 1
                    continue

                texto, segmentos_json = result
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO transcripciones (id, grabacion_id, texto_completo, segmentos, proveedor,
                                                          created_at, updated_at)
                            VALUES (gen_random_uuid(), %s, %s, %s::jsonb, %s, now(), now())
                            ON CONFLICT (grabacion_id) DO NOTHING
                            """,
                            (grabacion_id, texto, segmentos_json, PROVIDER_NAME),
                        )
                        cur.execute(
                            "UPDATE grabaciones SET estado = 'procesada' WHERE id = %s AND estado = 'procesando'",
                            (grabacion_id,),
                        )
                    backfilled += 1
                    pendientes_de_commit += 1
                except Exception as exc:  # noqa: BLE001 -- una fila mala no debe tumbar el resto del backfill
                    conn.rollback()
                    fallidas += 1
                    print(f"  [{i}] error guardando {grabacion_id}: {exc}", flush=True)
                    continue

                if pendientes_de_commit >= BATCH_SIZE:
                    conn.commit()
                    pendientes_de_commit = 0
                    print(f"  ... {i}/{len(planas)} revisadas, {backfilled} backfilled hasta ahora", flush=True)

        conn.commit()
        print(
            f"listo: backfilled={backfilled} aun_sin_resultado_en_s3={aun_pendientes} fallidas={fallidas}",
            flush=True,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
