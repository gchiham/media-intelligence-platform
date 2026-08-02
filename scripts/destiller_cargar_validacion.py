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
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.destiller.models import BloqueValidacion  # noqa: E402
from src.modules.destiller.prioridad import clasificar, mapa_por_palabra  # noqa: E402

SALIDA = Path("data/destiller_eval")
PREFIJO_AUDIO = "destiller_eval/audio"
PREFIJO_DESTILADO = "destiller_eval/destilado"

NOMBRE_PRIORIDAD = {
    1: "binario, mayoria discrepa",
    2: "binario, frontera",
    3: "muestra de control",
    4: "subtipo distinto",
    5: "acuerdo exacto",
}


def bloques_comparados(s3, etiqueta: str) -> dict[str, dict[int, str]]:
    """{grabacion: {palabra: tipo}} del modelo contra el que se compara."""
    salida: dict[str, dict[int, str]] = {}
    paginador = s3.get_paginator("list_objects_v2")
    for pagina in paginador.paginate(
        Bucket=settings.transcribe_output_bucket, Prefix=f"{PREFIJO_DESTILADO}/{etiqueta}/"
    ):
        for obj in pagina.get("Contents", []):
            d = json.loads(
                s3.get_object(Bucket=settings.transcribe_output_bucket, Key=obj["Key"])[
                    "Body"
                ].read()
            )
            salida[d["id"]] = mapa_por_palabra(d["bloques"])
    return salida


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


#: Segundos que toma validar un bloque en promedio: leer el texto y escuchar
#: unos segundos de audio, no la duracion completa del bloque. Es un supuesto
#: declarado, no una medicion -- se recalibra con el ritmo real del portal.
SEG_POR_BLOQUE = 20


def _hhmm(segundos: float) -> str:
    h, m = divmod(int(segundos // 60), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def reporte(filas: list[dict], comparado: str | None) -> None:
    if not comparado:
        print("\n(sin --comparar-con: todos los bloques quedaron en prioridad 5)")
        return

    por_p = defaultdict(list)
    for f in filas:
        por_p[f["prioridad"]].append(f)
    total = len(filas)

    print(f"\n{'=' * 74}\nPRIORIDAD DE LA COLA  (base: {filas[0]['modelo_base']} | "
          f"comparado: {comparado})\n{'=' * 74}")
    print(f"{'':4}{'criterio':28}{'bloques':>9}{'%':>7}{'desac.':>9}{'tiempo':>10}")
    for p in sorted(por_p):
        fs = por_p[p]
        desac = sum(f["desacuerdo_binario"] for f in fs) / len(fs)
        print(
            f"P{p:<3}{NOMBRE_PRIORIDAD[p]:28}{len(fs):>9}{100 * len(fs) / total:>6.1f}%"
            f"{desac:>9.2f}{_hhmm(len(fs) * SEG_POR_BLOQUE):>10}"
        )
    print(f"{'':4}{'TOTAL':28}{total:>9}{100.0:>6.1f}%{'':>9}"
          f"{_hhmm(total * SEG_POR_BLOQUE):>10}")

    altas = sum(len(por_p[p]) for p in (1, 2))
    print(f"\n  P1+P2 (donde esta la informacion): {altas} bloques, "
          f"{100 * altas / total:.0f}% del set, {_hhmm(altas * SEG_POR_BLOQUE)}")
    print(f"  palabras por bloque (promedio): "
          f"{sum(f['end_word'] - f['start_word'] + 1 for f in filas) / total:.0f}")

    print("\nPOR EMISORA")
    print(f"{'medio':22}{'P1':>5}{'P2':>5}{'P3':>5}{'P4':>5}{'P5':>5}{'total':>7}{'%alta':>7}")
    por_medio = defaultdict(list)
    for f in filas:
        por_medio[f["medio"]].append(f)
    for medio in sorted(por_medio, key=lambda m: -sum(
        1 for f in por_medio[m] if f["prioridad"] <= 2) / len(por_medio[m])):
        fs = por_medio[medio]
        c = Counter(f["prioridad"] for f in fs)
        alta = 100 * sum(c[p] for p in (1, 2)) / len(fs)
        print(f"{medio:22}" + "".join(f"{c[p]:>5}" for p in range(1, 6))
              + f"{len(fs):>7}{alta:>6.0f}%")

    print("\nPOR GRABACION  (las 8 con mas desacuerdo)")
    por_grab = defaultdict(list)
    for f in filas:
        por_grab[f["grabacion_ref"]].append(f)
    ranking = sorted(
        por_grab.items(),
        key=lambda kv: -sum(1 for f in kv[1] if f["prioridad"] <= 2) / len(kv[1]),
    )
    for grab, fs in ranking[:8]:
        alta = 100 * sum(1 for f in fs if f["prioridad"] <= 2) / len(fs)
        print(f"  {grab:34}{len(fs):>5} bloques{alta:>6.0f}% en P1+P2")


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
    parser.add_argument(
        "--comparar-con",
        help="etiqueta del otro modelo en S3 (ej. gpt-4o-mini). Sin esto todos los "
        "bloques quedan en prioridad 5 y la cola sale en orden cronologico.",
    )
    parser.add_argument(
        "--version-comparacion",
        default="v1",
        help="etiqueta de trazabilidad de esta comparacion",
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
    comparado = bloques_comparados(s3, args.comparar_con) if args.comparar_con else {}
    if args.comparar_con and not comparado:
        raise SystemExit(f"no hay destilados de '{args.comparar_con}' en S3")

    filas = []
    for g in bundle["grabaciones"]:
        clave = (
            f"{PREFIJO_AUDIO}/{g['id']}.mp3"
            if (args.desde_s3 and not args.sin_audio)
            else (None if args.sin_audio else subir_audio(s3, g["id"]))
        )
        if clave:
            print(f"  audio {g['id']} -> s3://{settings.clips_bucket}/{clave}")
        otro = comparado.get(g["id"], {})
        for i, b in enumerate(g["bloques"]):
            prioridad, desacuerdo, acuerdo = (
                clasificar(b["tipo"], b["start_word"], b["end_word"], otro, g["id"], i)
                if comparado
                else (5, 0.0, 0.0)
            )
            filas.append(
                {
                    "prioridad": prioridad,
                    "desacuerdo_binario": desacuerdo,
                    "porcentaje_acuerdo": acuerdo,
                    "modelo_base": modelo,
                    "modelo_comparado": args.comparar_con if otro else None,
                    "version_comparacion": args.version_comparacion if otro else None,
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
                        "prioridad": stmt.excluded.prioridad,
                        "desacuerdo_binario": stmt.excluded.desacuerdo_binario,
                        "porcentaje_acuerdo": stmt.excluded.porcentaje_acuerdo,
                        "modelo_base": stmt.excluded.modelo_base,
                        "modelo_comparado": stmt.excluded.modelo_comparado,
                        "version_comparacion": stmt.excluded.version_comparacion,
                    },
                )
            )
        session.commit()

    print(f"\n{len(filas)} bloques de {len(bundle['grabaciones'])} grabaciones | modelo {modelo}")
    reporte(filas, args.comparar_con)
    print("\nportal: https://<host>/api/v1/destiller/portal?token=<DASHBOARD_TOKEN>")


if __name__ == "__main__":
    main()
