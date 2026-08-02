"""Corre el destilado en la instancia GPU (`media-intel-destiller`), contra el
vLLM que sirve el modelo propio en localhost.

Vive del lado de la instancia por la misma razon que los workers de chepita: el
security group no expone puertos y abrirlos seria peor -- el usuario IAM del
proyecto tiene `AuthorizeSecurityGroupIngress` pero no `Revoke`, asi que un
puerto abierto lo queda para siempre. El trabajo viaja a la GPU, no al reves.

Idempotente: si ya existe el destilado de una grabacion en S3 no la vuelve a
procesar, para poder relanzarlo tras un corte sin pagar de nuevo lo ya hecho.

Uso (en la instancia, ver docs/INFRASTRUCTURE.md):
    DESTILLER_BASE_URL=http://127.0.0.1:8000/v1 python3 destiller_worker.py
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.modules.ai.destiller import BloqueDestilado, Destiller  # noqa: E402
from src.modules.ai.schemas import Word  # noqa: E402

PREFIJO = "destiller_eval"


def _texto(words: list[Word], b: BloqueDestilado) -> str:
    return " ".join(w.word for w in words if b.start_word <= w.index <= b.end_word)


def _tiempos(words: list[Word], b: BloqueDestilado) -> tuple[float, float]:
    dentro = [w for w in words if b.start_word <= w.index <= b.end_word]
    return (dentro[0].start, dentro[-1].end) if dentro else (0.0, 0.0)


def _etiqueta(modelo: str) -> str:
    """Slug del modelo, para que los destilados de proveedores distintos no se
    pisen en S3 y se puedan comparar entre si sobre las mismas grabaciones."""
    return modelo.split("/")[-1].lower().replace(".", "-")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rehacer", action="store_true", help="reprocesa lo ya destilado")
    parser.add_argument("--etiqueta", help="subcarpeta en S3; por defecto, el slug del modelo")
    parser.add_argument(
        "--seleccion",
        default=f"{PREFIJO}/seleccion.json",
        help="llave S3 de la lista de grabaciones a destilar",
    )
    parser.add_argument(
        "--destino",
        default=f"{PREFIJO}/destilado",
        help="prefijo S3 donde escribir; la etiqueta del modelo va como subcarpeta",
    )
    parser.add_argument(
        "--particion",
        default="0/1",
        metavar="i/n",
        help="reparte la lista entre n instancias; esta procesa la particion i. "
        "Sin esto, varias instancias sobre la misma lista se duplican el trabajo: "
        "el salto por `head_object` solo evita reprocesar lo YA escrito, no que dos "
        "arranquen a la vez por la misma grabacion.",
    )
    parser.add_argument(
        "--modelo",
        help="pisa el modelo configurado. Todo lo demas -- prompt, esquema, chunking, "
        "parametros -- queda igual, que es lo que hace comparables las corridas.",
    )
    args = parser.parse_args()

    # Mismo criterio que scripts/destiller_eval.py: la instancia propia gana si
    # esta configurada, y si no se cae a la API de OpenAI. Asi el mismo worker
    # corre en la GPU o localmente sin tocar codigo.
    if settings.destiller_base_url:
        modelo = args.modelo or settings.destiller_model
        destiller = Destiller(api_key="", model=modelo, base_url=settings.destiller_base_url)
    elif settings.openai_api_key:
        modelo = args.modelo or settings.openai_model
        destiller = Destiller(
            api_key=settings.openai_api_key.get_secret_value(), model=modelo
        )
    else:
        raise SystemExit("configura DESTILLER_BASE_URL o OPENAI_API_KEY")

    etiqueta = args.etiqueta or _etiqueta(modelo)
    s3 = boto3.client("s3", region_name=settings.aws_region)
    bucket = settings.transcribe_output_bucket

    seleccion = json.loads(s3.get_object(Bucket=bucket, Key=args.seleccion)["Body"].read())
    i_part, n_part = (int(x) for x in args.particion.split("/"))
    if n_part > 1:
        seleccion = [c for k, c in enumerate(seleccion) if k % n_part == i_part]
    print(f"{len(seleccion)} grabaciones (particion {args.particion}) | modelo {modelo} "
          f"| etiqueta {etiqueta}", flush=True)
    t0 = time.monotonic()

    for i, c in enumerate(seleccion, 1):
        cid = f"{c['medio']}__{c['stem']}"
        destino = f"{args.destino}/{etiqueta}/{cid}.json"

        if not args.rehacer:
            try:
                s3.head_object(Bucket=bucket, Key=destino)
                print(f"[{i}/{len(seleccion)}] {cid}: ya estaba", flush=True)
                continue
            except ClientError:
                pass

        words = [
            Word.model_validate(w)
            for w in json.loads(s3.get_object(Bucket=bucket, Key=c["llave"])["Body"].read())
        ]
        if not words:
            print(f"[{i}/{len(seleccion)}] {cid}: transcripcion vacia, se omite", flush=True)
            continue

        try:
            bloques = destiller.destilar(words)
        except Exception as exc:  # noqa: BLE001 -- una grabacion mala no debe tumbar la corrida
            print(f"[{i}/{len(seleccion)}] {cid}: FALLO {exc}", flush=True)
            continue

        salida = []
        for n, b in enumerate(bloques):
            inicio, fin = _tiempos(words, b)
            salida.append(
                {
                    "id": f"{cid}#{n}",
                    "start_word": b.start_word,
                    "end_word": b.end_word,
                    "tipo": b.tipo.value,
                    "confidence": round(b.confidence, 3),
                    "motivo": b.motivo,
                    "inicio": round(inicio, 2),
                    "fin": round(fin, 2),
                    "texto": _texto(words, b),
                }
            )

        s3.put_object(
            Bucket=bucket,
            Key=destino,
            Body=json.dumps(
                {
                    "id": cid,
                    "modelo": modelo,
                    "palabras": len(words),
                    "bloques": salida,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        print(
            f"[{i}/{len(seleccion)}] {cid}: {len(bloques)} bloques de {len(words)} palabras",
            flush=True,
        )

    u = destiller.uso
    print(
        f"listo | {u.llamadas} llamadas | {u.tokens_entrada} tokens entrada | "
        f"{u.tokens_salida} tokens salida | {time.monotonic() - t0:.0f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
