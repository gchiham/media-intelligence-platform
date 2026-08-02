"""Compara N corridas de Destiller sobre las mismas grabaciones.

No toca la base ni la cola de validacion: lee los destilados de S3 y reporta.
Sirve para decidir si un modelo nuevo aporta informacion o si repite lo que ya
dice otro -- dos modelos que coinciden en todo cuestan el doble y no agregan
nada como jueces independientes.

Todo se mide **por palabra**, no por bloque: cada modelo corta la transmision
donde quiere (sobre las mismas 20 grabaciones, Qwen dio 815 bloques y
gpt-4o-mini 662), asi que comparar bloque contra bloque obligaria a elegir en
silencio con cual de los solapados comparar. La palabra es la unidad que todos
comparten.

Uso:
    python scripts/destiller_benchmark.py --modelos qwen2-5-7b-instruct gpt-4o-mini gpt-5-mini
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.modules.destiller.prioridad import BASURA, mapa_por_palabra  # noqa: E402

PREFIJO = "destiller_eval/destilado"
TIPOS = ["noticia", "publicidad", "religioso", "promo", "musica", "relleno", "desconocido"]


def cargar(s3, etiqueta: str) -> dict[str, list[dict]]:
    salida: dict[str, list[dict]] = {}
    for pagina in s3.get_paginator("list_objects_v2").paginate(
        Bucket=settings.transcribe_output_bucket, Prefix=f"{PREFIJO}/{etiqueta}/"
    ):
        for obj in pagina.get("Contents", []):
            d = json.loads(
                s3.get_object(Bucket=settings.transcribe_output_bucket, Key=obj["Key"])[
                    "Body"
                ].read()
            )
            salida[d["id"]] = d["bloques"]
    return salida


def perfil(etiqueta: str, corrida: dict[str, list[dict]]) -> None:
    bloques = [b for bs in corrida.values() for b in bs]
    palabras = Counter()
    for b in bloques:
        palabras[b["tipo"]] += b["end_word"] - b["start_word"] + 1
    total = sum(palabras.values())
    basura = sum(v for k, v in palabras.items() if k in BASURA)

    print(f"\n{etiqueta}")
    print(f"  grabaciones {len(corrida)} | bloques {len(bloques)} | "
          f"palabras/bloque {total / len(bloques):.0f}")
    print(f"  noticia {100 * palabras['noticia'] / total:.1f}%  |  "
          f"basura {100 * basura / total:.1f}%  |  "
          f"desconocido {100 * palabras['desconocido'] / total:.1f}%")
    print("  " + "  ".join(f"{t}={100 * palabras[t] / total:.1f}%" for t in TIPOS if palabras[t]))


def par(a: str, ma: dict[str, dict[int, str]], b: str, mb: dict[str, dict[int, str]]) -> dict:
    exacto = binario = comparadas = 0
    matriz: Counter = Counter()
    por_medio: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    for g in set(ma) & set(mb):
        medio = g.split("__")[0]
        for i in set(ma[g]) & set(mb[g]):
            ta, tb = ma[g][i], mb[g][i]
            comparadas += 1
            exacto += ta == tb
            mismo_lado = (ta in BASURA) == (tb in BASURA)
            binario += mismo_lado
            por_medio[medio][0] += 1
            por_medio[medio][1] += mismo_lado
            if ta != tb:
                matriz[(ta, tb)] += 1

    return {
        "a": a, "b": b, "comparadas": comparadas,
        "exacto": exacto / comparadas if comparadas else 0.0,
        "binario": binario / comparadas if comparadas else 0.0,
        "matriz": matriz,
        "por_medio": {m: v[1] / v[0] for m, v in por_medio.items() if v[0]},
    }


def imprimir_par(r: dict) -> None:
    print(f"\n{'-' * 70}\n{r['a']}  vs  {r['b']}   ({r['comparadas']} palabras comparables)")
    print(f"  acuerdo exacto  : {100 * r['exacto']:.1f}%")
    print(f"  acuerdo binario : {100 * r['binario']:.1f}%   <- lo unico que el pipeline usa")

    if r["matriz"]:
        print(f"\n  principales diferencias ({r['a']} -> {r['b']}):")
        for (x, y), n in r["matriz"].most_common(6):
            cruza = "  [cruza la linea basura/noticia]" if (x in BASURA) != (y in BASURA) else ""
            print(f"    {x:12} -> {y:12} {n:7} palabras{cruza}")

    peores = sorted(r["por_medio"].items(), key=lambda kv: kv[1])[:5]
    print("\n  emisoras donde mas discrepan (acuerdo binario):")
    for medio, v in peores:
        print(f"    {medio:22} {100 * v:5.1f}%")


def tres_vias(mapas: dict[str, dict[str, dict[int, str]]]) -> None:
    etiquetas = list(mapas)
    if len(etiquetas) < 3:
        return
    print(f"\n{'=' * 70}\nLOS {len(etiquetas)} A LA VEZ\n{'=' * 70}")

    todos = unanime_bin = unanime_exacto = 0
    solo_uno: Counter = Counter()
    grabaciones = set.intersection(*(set(m) for m in mapas.values()))
    for g in grabaciones:
        indices = set.intersection(*(set(mapas[e][g]) for e in etiquetas))
        for i in indices:
            tipos = [mapas[e][g][i] for e in etiquetas]
            lados = [t in BASURA for t in tipos]
            todos += 1
            unanime_exacto += len(set(tipos)) == 1
            if all(lados) or not any(lados):
                unanime_bin += 1
            else:
                # Quien queda solo contra los otros dos: sirve para ver si un
                # modelo es el raro sistematicamente o si el desacuerdo se
                # reparte (lo segundo significa que el caso es genuinamente
                # ambiguo, no que un modelo este mal).
                for k, e in enumerate(etiquetas):
                    if lados.count(lados[k]) == 1:
                        solo_uno[e] += 1

    print(f"  palabras comparables: {todos}")
    print(f"  los tres con el tipo exacto igual : {100 * unanime_exacto / todos:.1f}%")
    print(f"  los tres del mismo lado (binario) : {100 * unanime_bin / todos:.1f}%")
    print("\n  cuando uno queda solo contra los otros dos:")
    for e in etiquetas:
        n = solo_uno[e]
        print(f"    {e:24} {n:7} palabras  ({100 * n / max(todos - unanime_bin, 1):.0f}% "
              "de los casos en disputa)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modelos", nargs="+", required=True, help="etiquetas en S3")
    args = parser.parse_args()

    s3 = boto3.client("s3", region_name=settings.aws_region)
    corridas = {}
    for etq in args.modelos:
        c = cargar(s3, etq)
        if not c:
            raise SystemExit(f"no hay destilados de '{etq}' en S3")
        corridas[etq] = c

    print("=" * 70)
    print("PERFIL DE CADA CORRIDA")
    print("=" * 70)
    for etq, c in corridas.items():
        perfil(etq, c)

    mapas = {
        etq: {g: mapa_por_palabra(bs) for g, bs in c.items()} for etq, c in corridas.items()
    }

    print(f"\n{'=' * 70}\nCOMPARACIONES POR PAR\n{'=' * 70}")
    resultados = []
    for a, b in combinations(args.modelos, 2):
        r = par(a, mapas[a], b, mapas[b])
        resultados.append(r)
        imprimir_par(r)

    tres_vias(mapas)

    print(f"\n{'=' * 70}\nRESUMEN\n{'=' * 70}")
    print(f"{'par':52}{'exacto':>9}{'binario':>9}")
    for r in sorted(resultados, key=lambda x: -x["binario"]):
        print(f"{r['a'] + '  vs  ' + r['b']:52}{100 * r['exacto']:>8.1f}%{100 * r['binario']:>8.1f}%")


if __name__ == "__main__":
    main()
