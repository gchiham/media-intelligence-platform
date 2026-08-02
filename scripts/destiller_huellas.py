"""Cuanta de la discrepancia entre modelos se resuelve sin LLM, por huella.

La hipotesis: buena parte de lo que los modelos discuten es contenido grabado
que sale al aire muchas veces -- canciones, spots, cortinas, jingles. Eso no
necesita criterio: se detecta porque su transcripcion sale casi identica cada
vez. Ver el razonamiento en src/modules/ai/repeated_content.py, cuyo
`huellas_de_ventanas` se reutiliza aca tal cual para que lo medido sea el
detector real y no una reimplementacion parecida.

**Lo que la huella NO distingue:** cancion de anuncio. Ambos son grabaciones que
se repiten. Para el pipeline da igual -- los dos se excluyen -- pero conviene no
vender el numero como "canciones" cuando es "contenido recurrente".

Uso:
    python scripts/destiller_huellas.py --por-medio 25
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
from src.modules.ai.repeated_content import VENTANA, huellas_de_ventanas  # noqa: E402
from src.modules.ai.schemas import Word  # noqa: E402
from src.modules.destiller.prioridad import BASURA, mapa_por_palabra  # noqa: E402

PREFIJO = "destiller_eval/destilado"
BASE = "qwen2-5-7b-instruct"
OTROS = ["gpt-4o-mini", "gpt-5-mini"]


def _s3():
    return boto3.client("s3", region_name=settings.aws_region)


def cargar_destilado(s3, etiqueta: str) -> dict[str, list[dict]]:
    salida = {}
    for pag in s3.get_paginator("list_objects_v2").paginate(
        Bucket=settings.transcribe_output_bucket, Prefix=f"{PREFIJO}/{etiqueta}/"
    ):
        for obj in pag.get("Contents", []):
            d = json.loads(
                s3.get_object(Bucket=settings.transcribe_output_bucket, Key=obj["Key"])["Body"].read()
            )
            salida[d["id"]] = d["bloques"]
    return salida


def llaves_de_aprendizaje(s3, medios: set[str], por_medio: int, excluir: set[str]) -> list[str]:
    """Transcripciones de las mismas emisoras para que el indice aprenda que se
    repite. Una cancion se repite dentro de la misma emisora, asi que aprender
    de otras estaciones no ayudaria."""
    llaves = []
    for medio in sorted(medios):
        del_medio = []
        for pag in s3.get_paginator("list_objects_v2").paginate(
            Bucket=settings.transcribe_output_bucket, Prefix=f"transcripts/{medio}/"
        ):
            for obj in pag.get("Contents", []):
                k = obj["Key"]
                if k.endswith("_words.json") and Path(k).name[:-11] not in excluir:
                    del_medio.append(k)
        # Del final: las mas recientes, que es donde hay mas volumen capturado.
        llaves.extend(del_medio[-por_medio:])
    return llaves


def indexar(llaves: list[str]) -> dict[str, set[str]]:
    """{huella: {grabaciones donde aparece}}. Contar grabaciones distintas y no
    apariciones evita que un spot repetido dos veces en la misma hora se vea
    como contenido recurrente de la emisora."""
    indice: dict[str, set[str]] = defaultdict(set)

    def una(llave: str) -> None:
        s3 = _s3()
        try:
            cuerpo = s3.get_object(Bucket=settings.transcribe_output_bucket, Key=llave)["Body"].read()
            words = [Word.model_validate(w) for w in json.loads(cuerpo)]
        except Exception:  # noqa: BLE001 -- una transcripcion mala no debe tumbar el indice
            return
        for huella, _ in huellas_de_ventanas(words):
            indice[huella].add(llave)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(una, llaves))
    return indice


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--por-medio", type=int, default=25,
                        help="transcripciones por emisora para aprender que se repite")
    args = parser.parse_args()

    s3 = _s3()
    base = cargar_destilado(s3, BASE)
    mapas = {e: {g: mapa_por_palabra(bs) for g, bs in cargar_destilado(s3, e).items()}
             for e in OTROS}

    # Bloques en disputa binaria, mismo criterio que el analisis anterior.
    disputa = []
    for grabacion, bloques in base.items():
        for i, b in enumerate(bloques):
            rango = range(b["start_word"], b["end_word"] + 1)
            lados = [b["tipo"] in BASURA]
            for e in OTROS:
                m = mapas[e].get(grabacion, {})
                vistos = [m[w] for w in rango if w in m]
                if not vistos:
                    lados = None
                    break
                lados.append(Counter(vistos).most_common(1)[0][0] in BASURA)
            if lados and len(set(lados)) > 1:
                disputa.append({"grabacion": grabacion, "idx": i, **b})

    medios = {g.split("__")[0] for g in base}
    evaluadas = {g.split("__")[1] for g in base}
    print(f"{len(disputa)} bloques en disputa | {len(medios)} emisoras")

    llaves = llaves_de_aprendizaje(s3, medios, args.por_medio, evaluadas)
    print(f"aprendiendo de {len(llaves)} transcripciones ({args.por_medio} por emisora)...")
    indice = indexar(llaves)
    print(f"indice: {len(indice)} huellas distintas\n")

    # Se necesitan las palabras originales para huellar el tramo exacto del
    # bloque, no su texto reconstruido.
    words_por_grabacion = {}
    for grabacion in {d["grabacion"] for d in disputa}:
        medio, stem = grabacion.split("__")
        cuerpo = s3.get_object(
            Bucket=settings.transcribe_output_bucket,
            Key=f"transcripts/{medio}/{stem}_words.json",
        )["Body"].read()
        words_por_grabacion[grabacion] = [Word.model_validate(w) for w in json.loads(cuerpo)]

    cortos = 0
    resultados = []
    for d in disputa:
        words = [w for w in words_por_grabacion[d["grabacion"]]
                 if d["start_word"] <= w.index <= d["end_word"]]
        huellas = huellas_de_ventanas(words)
        if not huellas:
            cortos += 1
            continue
        for umbral in (2, 3, 5):
            conocidas = sum(1 for h, _ in huellas if len(indice.get(h, ())) >= umbral)
            d[f"cob{umbral}"] = conocidas / len(huellas)
        resultados.append(d)

    print(f"bloques demasiado cortos para huellar (<{VENTANA} palabras): {cortos} "
          f"de {len(disputa)} ({100 * cortos / len(disputa):.0f}%)")
    print(f"bloques evaluables: {len(resultados)}\n")

    print("bloques cuya mayoria de ventanas ya se vio en otras grabaciones")
    print(f"{'aparece en >= N grabaciones':32}{'cob>=50%':>10}{'cob>=75%':>10}")
    for umbral in (2, 3, 5):
        c50 = sum(1 for d in resultados if d[f"cob{umbral}"] >= 0.5)
        c75 = sum(1 for d in resultados if d[f"cob{umbral}"] >= 0.75)
        print(f"  N = {umbral:<28}{c50:>10}{c75:>10}")

    detectados = [d for d in resultados if d["cob3"] >= 0.5]
    print(f"\ncon N=3 y cobertura >=50%: {len(detectados)} bloques "
          f"({100 * len(detectados) / len(disputa):.0f}% de toda la disputa, "
          f"{100 * len(detectados) / max(len(resultados), 1):.0f}% de los evaluables)")

    if detectados:
        print("\n  como los etiqueto cada modelo:")
        for etq, get in (
            (BASE, lambda d: d["tipo"]),
            ("otros", lambda d: None),
        ):
            if get(detectados[0]) is None:
                continue
            print(f"    {etq:22} " + "  ".join(
                f"{k}={v}" for k, v in Counter(get(d) for d in detectados).most_common(5)))
        print(f"\n  emisoras: " + "  ".join(
            f"{m}={n}" for m, n in Counter(d["grabacion"].split("__")[0]
                                           for d in detectados).most_common(6)))
        print("\n  muestras:")
        for d in sorted(detectados, key=lambda x: -x["cob3"])[:8]:
            print(f"    [{d['tipo']:11}] cob={d['cob3']:.0%} {d['texto'][:88]}")


if __name__ == "__main__":
    main()
