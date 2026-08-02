"""Prueba de regresion: ¿Destiller habria borrado una noticia que produccion
ya entrego al cliente?

Es la contraparte automatica de la validacion humana. Aquella mide *verdad*
sobre pocos bloques; esta mide *riesgo de regresion contra el producto actual*
sobre miles de noticias, sin consumir una hora de nadie.

**Lo que este numero NO es.** Las noticias contra las que se compara son salida
de `segment_news`, o sea de otro LLM con sus propios errores. Si `segment_news`
extrajo un anuncio como noticia y Destiller lo excluye, aparece aca como
impacto cuando en realidad Destiller acerto. Por eso el resultado es una **cota
superior del daño**, no un conteo de perdidas reales: si da cero no hay nada
que discutir, y si da distinto de cero cada caso hay que mirarlo.

**Padding.** `clip_inicio_seg`/`clip_fin_seg` traen el padding que agrega
`map_words_to_time` (2 s por lado por defecto). Se encoge antes de comparar: un
solape contra el padding no es una noticia perdida, es relleno del corte.

Uso:
    python scripts/destiller_regresion.py --politica conservadora-v1 \
        --modelos qwen2-5-7b-instruct gpt-4o-mini
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.modules.ai.schemas import Word  # noqa: E402
from src.modules.destiller.politica import CATALOGO  # noqa: E402

PREFIJO = "destiller_eval/regresion"
PADDING = 2.0


def _s3():
    return boto3.client("s3", region_name=settings.aws_region)


def cargar_destilado(s3, etiqueta: str) -> dict[str, list[dict]]:
    salida = {}
    for pag in s3.get_paginator("list_objects_v2").paginate(
        Bucket=settings.transcribe_output_bucket, Prefix=f"{PREFIJO}/destilado/{etiqueta}/"
    ):
        for obj in pag.get("Contents", []):
            d = json.loads(
                s3.get_object(Bucket=settings.transcribe_output_bucket, Key=obj["Key"])["Body"].read()
            )
            salida[d["id"]] = d["bloques"]
    return salida


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--politica", default="conservadora-v1", choices=sorted(CATALOGO))
    parser.add_argument("--modelos", nargs="+", required=True)
    parser.add_argument("--padding", type=float, default=PADDING)
    args = parser.parse_args()

    politica = CATALOGO[args.politica]
    s3 = _s3()

    objetivo = json.loads(
        s3.get_object(Bucket=settings.clips_bucket, Key=f"{PREFIJO}/objetivo.json")["Body"].read()
    )
    noticias = defaultdict(list)
    for s3_key, ini, fin in objetivo["noticias"]:
        medio, stem = s3_key.split("/")[0], s3_key.split("/")[-1].rsplit(".", 1)[0]
        noticias[f"{medio}__{stem}"].append((ini, fin))

    corridas = {e: cargar_destilado(s3, e) for e in args.modelos}
    faltan = [e for e, c in corridas.items() if not c]
    if faltan:
        raise SystemExit(f"sin destilados para: {', '.join(faltan)}")

    comunes = set.intersection(*(set(c) for c in corridas.values())) & set(noticias)
    print(f"politica: {politica.nombre} ({politica.motivo})")
    print(f"modelos:  {', '.join(args.modelos)} (minimo {politica.minimo_modelos})")
    print(f"grabaciones evaluables: {len(comunes)} | noticias en ellas: "
          f"{sum(len(noticias[g]) for g in comunes)}\n")

    def una(grabacion: str) -> dict:
        cli = _s3()
        medio, stem = grabacion.split("__")
        words = [
            Word.model_validate(w)
            for w in json.loads(
                cli.get_object(
                    Bucket=settings.transcribe_output_bucket,
                    Key=f"transcripts/{medio}/{stem}_words.json",
                )["Body"].read()
            )
        ]
        excl = set()
        for lo, hi in politica.rangos([corridas[e][grabacion] for e in args.modelos]):
            excl.update(range(lo, hi + 1))

        impactos = []
        for ini, fin in noticias[grabacion]:
            # Encoger el padding: solapar con el relleno del clip no es perder noticia.
            a, b = ini + args.padding, fin - args.padding
            if b <= a:
                continue
            dentro = [w for w in words if a <= w.start and w.end <= b]
            if not dentro:
                continue
            tocadas = sum(1 for w in dentro if w.index in excl)
            if tocadas:
                impactos.append(
                    {
                        "grabacion": grabacion,
                        "palabras_noticia": len(dentro),
                        "palabras_tocadas": tocadas,
                        "fraccion": tocadas / len(dentro),
                        "texto": " ".join(
                            w.word for w in dentro if w.index in excl
                        )[:160],
                    }
                )
        return {"noticias": len(noticias[grabacion]), "impactos": impactos,
                "palabras_excluidas": len(excl), "palabras": len(words)}

    with ThreadPoolExecutor(max_workers=10) as pool:
        res = list(pool.map(una, sorted(comunes)))

    total_noticias = sum(r["noticias"] for r in res)
    impactos = [i for r in res for i in r["impactos"]]
    aire = sum(r["palabras"] for r in res)
    excluidas = sum(r["palabras_excluidas"] for r in res)

    print("=" * 70)
    print("RESULTADO")
    print("=" * 70)
    print(f"  aire excluido por la politica : {100 * excluidas / aire:.1f}% "
          f"({excluidas} de {aire} palabras)")
    print(f"  noticias evaluadas            : {total_noticias}")
    print(f"  noticias TOCADAS              : {len(impactos)} "
          f"({100 * len(impactos) / max(total_noticias, 1):.2f}%)")

    if impactos:
        graves = [i for i in impactos if i["fraccion"] >= 0.5]
        leves = [i for i in impactos if i["fraccion"] < 0.1]
        print(f"    con >=50% de sus palabras excluidas : {len(graves)}")
        print(f"    con <10%  (probable borde de bloque): {len(leves)}")
        print(f"\n  por emisora: " + "  ".join(
            f"{m}={n}" for m, n in Counter(i["grabacion"].split("__")[0]
                                           for i in impactos).most_common(6)))
        print("\n  peores casos:")
        for i in sorted(impactos, key=lambda x: -x["fraccion"])[:10]:
            print(f"    {i['fraccion']:5.0%} de {i['palabras_noticia']:4} palabras "
                  f"| {i['grabacion'][:26]:26} | {i['texto'][:70]}")
        Path("data/destiller_eval").mkdir(parents=True, exist_ok=True)
        Path("data/destiller_eval/regresion_impactos.json").write_text(
            json.dumps(impactos, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\n  -> data/destiller_eval/regresion_impactos.json")
    else:
        print("\n  Ninguna noticia de produccion se veria afectada.")

    n = total_noticias
    print(f"\n  Cota superior del FPR al 95% (regla de tres, n={n}): "
          f"{300 / n:.2f}%" if not impactos else
          f"\n  FPR observado: {100 * len(impactos) / max(n, 1):.2f}%")
    print("\n  Recordar: las noticias de referencia son salida de segment_news,")
    print("  no verdad humana. Este numero es una COTA SUPERIOR del daño.")


if __name__ == "__main__":
    main()
