"""Carga un bundle destilado al portal de validacion: sube el audio a S3 y
mete los bloques en Postgres.

El audio se sube ya recomprimido (mono 32 kbps, lo que produjo
`destiller_eval.py --paso armar`) y se sirve al navegador con URLs prefirmadas
-- nunca por el backend. Ver la nota en src/api/routers/destiller.py: la
instancia de produccion aloja Postgres y ya se cayo una vez por saturacion.

Idempotente: reejecutarlo no duplica bloques ni borra veredictos existentes.

Uso:
    python scripts/destiller_cargar_validacion.py --etiqueta qwen2-5-7b-instruct
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.destiller.models import BloqueValidacion  # noqa: E402

SALIDA = Path("data/destiller_eval")
PREFIJO_AUDIO = "destiller_eval/audio"


def subir_audio(s3, grabacion_id: str) -> str | None:
    local = SALIDA / "audio" / f"{grabacion_id}.mp3"
    if not local.exists():
        return None
    clave = f"{PREFIJO_AUDIO}/{grabacion_id}.mp3"
    try:
        s3.head_object(Bucket=settings.clips_bucket, Key=clave)
        return clave  # ya estaba: no re-subir 14 MB por gusto
    except Exception:
        pass
    s3.upload_file(
        str(local), settings.clips_bucket, clave, ExtraArgs={"ContentType": "audio/mpeg"}
    )
    return clave


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--etiqueta", required=True, help="ej. qwen2-5-7b-instruct")
    parser.add_argument("--sin-audio", action="store_true", help="no sube los mp3 a S3")
    parser.add_argument(
        "--desde-s3",
        action="store_true",
        help="lee el bundle de S3 en vez del disco -- para correrlo en el host de produccion, "
        "que no tiene el data/ local",
    )
    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=settings.aws_region)

    if args.desde_s3:
        clave = f"destiller_eval/bundle_{args.etiqueta}.json"
        bundle = json.loads(
            s3.get_object(Bucket=settings.clips_bucket, Key=clave)["Body"].read()
        )
    else:
        bundle_path = SALIDA / f"bundle_{args.etiqueta}.json"
        if not bundle_path.exists():
            raise SystemExit(f"no existe {bundle_path} -- corre primero --paso armar")
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    modelo = bundle.get("modelo", args.etiqueta)
    filas = []
    for g in bundle["grabaciones"]:
        clave = (
            f"{PREFIJO_AUDIO}/{g['id']}.mp3"
            if (args.desde_s3 and not args.sin_audio)
            else (None if args.sin_audio else subir_audio(s3, g["id"]))
        )
        if clave:
            print(f"  audio {g['id']} -> s3://{settings.clips_bucket}/{clave}")
        for i, b in enumerate(g["bloques"]):
            filas.append(
                {
                    "modelo": modelo,
                    "grabacion_ref": g["id"],
                    "medio": g["medio"],
                    "fecha": g["fecha"],
                    "hora_local": g["hora_local"],
                    "audio_key": clave,
                    "bloque_idx": i,
                    "start_word": b["start_word"],
                    "end_word": b["end_word"],
                    "inicio_seg": b["inicio"],
                    "fin_seg": b["fin"],
                    "tipo_llm": b["tipo"],
                    "confidence": b["confidence"],
                    "motivo": (b.get("motivo") or "")[:500],
                    "texto": b["texto"],
                }
            )

    with Session(get_engine()) as session:
        for i in range(0, len(filas), 500):
            lote = filas[i : i + 500]
            stmt = insert(BloqueValidacion).values(lote)
            # Actualiza el contenido si el bloque ya existia (recarga tras
            # cambiar el prompt) sin tocar la asignacion ni borrar veredictos:
            # los veredictos cuelgan por FK y sobreviven a la recarga.
            session.execute(
                stmt.on_conflict_do_update(
                    constraint="uq_destiller_bloque",
                    set_={
                        "tipo_llm": stmt.excluded.tipo_llm,
                        "confidence": stmt.excluded.confidence,
                        "motivo": stmt.excluded.motivo,
                        "texto": stmt.excluded.texto,
                        "audio_key": stmt.excluded.audio_key,
                        "inicio_seg": stmt.excluded.inicio_seg,
                        "fin_seg": stmt.excluded.fin_seg,
                    },
                )
            )
        session.commit()

    print(f"\n{len(filas)} bloques de {len(bundle['grabaciones'])} grabaciones | modelo {modelo}")
    print("portal: https://<host>/api/v1/destiller/portal?token=<DASHBOARD_TOKEN>")


if __name__ == "__main__":
    main()
