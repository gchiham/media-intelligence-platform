"""Compara dos modelos de Destiller contra la MISMA validacion humana.

**Por que alcanza con validar un solo modelo.** La particion es completa: cada
palabra de la grabacion cae en exactamente un bloque, asi que validar los
bloques de un modelo produce una etiqueta verdadera por palabra, no por bloque.
Esa verdad esta atada a la linea de tiempo, no a los limites que propuso el
modelo validado -- y por eso sirve para puntuar a cualquier otro modelo sobre
las mismas grabaciones, sin pedirle al humano que revise el doble.

El bloque validado aporta su etiqueta a todas sus palabras; los tramos que
nadie valido quedan fuera del calculo en vez de asumirse correctos.

Uso:
    python scripts/destiller_comparar.py --validado "Qwen/Qwen2.5-7B-Instruct" \
        --contra gpt-4o-mini
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.destiller.models import BloqueValidacion, VeredictoValidacion  # noqa: E402

BASURA = {"publicidad", "religioso", "promo", "musica", "relleno"}
UMBRALES = [0.0, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]
PREFIJO = "destiller_eval"


def verdad_por_palabra(modelo_validado: str) -> dict[str, dict[int, str]]:
    """{grabacion: {indice_de_palabra: tipo_real}} segun los humanos."""
    with Session(get_engine()) as session:
        filas = session.execute(
            select(
                BloqueValidacion.grabacion_ref,
                BloqueValidacion.start_word,
                BloqueValidacion.end_word,
                VeredictoValidacion.ok,
                VeredictoValidacion.tipo_llm,
                VeredictoValidacion.tipo_real,
            )
            .join(VeredictoValidacion, VeredictoValidacion.bloque_id == BloqueValidacion.id)
            .where(BloqueValidacion.modelo == modelo_validado)
        ).all()

    verdad: dict[str, dict[int, str]] = defaultdict(dict)
    for f in filas:
        tipo = f.tipo_llm.value if f.ok else f.tipo_real.value
        for i in range(f.start_word, f.end_word + 1):
            # Si dos validadores discrepan sobre la misma palabra gana el
            # ultimo; el desacuerdo se reporta aparte en destiller_calibrate.py
            # y no se esconde promediando.
            verdad[f.grabacion_ref][i] = tipo
    return verdad


def bloques_de(etiqueta: str) -> dict[str, list[dict]]:
    s3 = boto3.client("s3", region_name=settings.aws_region)
    salida: dict[str, list[dict]] = {}
    paginador = s3.get_paginator("list_objects_v2")
    for pagina in paginador.paginate(
        Bucket=settings.transcribe_output_bucket, Prefix=f"{PREFIJO}/destilado/{etiqueta}/"
    ):
        for obj in pagina.get("Contents", []):
            try:
                d = json.loads(
                    s3.get_object(Bucket=settings.transcribe_output_bucket, Key=obj["Key"])[
                        "Body"
                    ].read()
                )
            except ClientError:
                continue
            salida[d["id"]] = d["bloques"]
    return salida


def puntuar(nombre: str, bloques: dict[str, list[dict]], verdad: dict[str, dict[int, str]]) -> None:
    print(f"\n{'=' * 62}\n{nombre}\n{'=' * 62}")

    cubiertas = aciertos = 0
    for grabacion, bs in bloques.items():
        v = verdad.get(grabacion, {})
        if not v:
            continue
        for b in bs:
            for i in range(b["start_word"], b["end_word"] + 1):
                real = v.get(i)
                if real is None:
                    continue
                cubiertas += 1
                if real == b["tipo"]:
                    aciertos += 1

    if not cubiertas:
        print("  sin palabras validadas todavia para este modelo")
        return
    print(f"  palabras con verdad humana: {cubiertas}")
    print(f"  acierto de tipo exacto: {100 * aciertos / cubiertas:.1f}%")

    print("\n  umbral   basura excluida   NOTICIA REAL PERDIDA")
    print("           (% de palabras)      (% de palabras)")
    for u in UMBRALES:
        basura_total = ok = noticia_total = perdida = 0
        for grabacion, bs in bloques.items():
            v = verdad.get(grabacion, {})
            for b in bs:
                excluye = b["tipo"] in BASURA and b["confidence"] >= u
                for i in range(b["start_word"], b["end_word"] + 1):
                    real = v.get(i)
                    if real is None:
                        continue
                    if real in BASURA:
                        basura_total += 1
                        ok += excluye
                    elif real == "noticia":
                        noticia_total += 1
                        perdida += excluye
        pb = 100 * ok / basura_total if basura_total else 0.0
        pn = 100 * perdida / noticia_total if noticia_total else 0.0
        print(f"   {u:4.2f}      {pb:9.1f}%            {pn:9.2f}%")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validado", required=True, help="modelo cuyos bloques valido el humano")
    parser.add_argument("--contra", nargs="+", required=True, help="etiquetas en S3 a puntuar")
    parser.add_argument("--etiqueta-validado", help="etiqueta en S3 del modelo validado")
    args = parser.parse_args()

    verdad = verdad_por_palabra(args.validado)
    total = sum(len(v) for v in verdad.values())
    if not total:
        raise SystemExit(
            f"nadie ha validado bloques de {args.validado} todavia -- "
            "el portal es el que produce la verdad contra la que se compara"
        )
    print(f"verdad humana: {total} palabras etiquetadas en {len(verdad)} grabaciones")

    etiquetas = list(args.contra)
    if args.etiqueta_validado:
        etiquetas.insert(0, args.etiqueta_validado)
    for etiqueta in etiquetas:
        puntuar(etiqueta, bloques_de(etiqueta), verdad)

    print("\nLa columna que decide es la tercera: es el periodismo real que se")
    print("borraria del producto. Gana el modelo que excluya mas basura a igual")
    print("perdida de noticia, no el que tenga mejor acierto promedio.")


if __name__ == "__main__":
    main()
