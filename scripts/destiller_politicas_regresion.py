"""Compara POLITICAS de exclusion contra las noticias que produccion ya entrego.

No compara modelos: compara reglas. La pregunta que responde es una sola --
¿el consenso reduce el riesgo lo suficiente para justificar una exclusion
automatica?

Contra que se mide: `clip_inicio_seg`/`clip_fin_seg` de cada Noticia real en
Postgres, mapeados a indice de palabra y encogidos por el padding de 2 s que
agrega `map_words_to_time` (un solape con el padding no es una noticia perdida,
es relleno del corte).

**Cota superior, no verdad.** Las noticias de referencia son salida de
`segment_news`, o sea de otro LLM. Si extrajo un anuncio como noticia y la
politica lo excluye, cuenta como daño aunque la politica haya acertado. Sirve
igual: para una regla que se va a automatizar, lo que importa es si cambia lo
que el cliente ya recibe.

Uso:
    python scripts/destiller_politicas_regresion.py
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
from src.modules.ai.destiller import TipoBloque  # noqa: E402
from src.modules.ai.schemas import Word  # noqa: E402
from src.modules.destiller.politica import EXCLUIBLES_V1, PoliticaExclusion  # noqa: E402

PREFIJO = "destiller_eval/regresion"
PADDING = 2.0
BASE = "qwen2-5-7b-instruct"
OTRO = "gpt-4o-mini"


def _s3():
    return boto3.client("s3", region_name=settings.aws_region)


def cargar(s3, etiqueta: str) -> dict[str, list[dict]]:
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
    parser.add_argument("--padding", type=float, default=PADDING)
    args = parser.parse_args()

    s3 = _s3()
    corridas = {BASE: cargar(s3, BASE), OTRO: cargar(s3, OTRO)}
    objetivo = json.loads(
        s3.get_object(Bucket=settings.clips_bucket, Key=f"{PREFIJO}/objetivo.json")["Body"].read()
    )
    noticias = defaultdict(list)
    for k, ini, fin in objetivo["noticias"]:
        medio, stem = k.split("/")[0], k.split("/")[-1].rsplit(".", 1)[0]
        noticias[f"{medio}__{stem}"].append((ini, fin))

    comunes = sorted(set(corridas[BASE]) & set(corridas[OTRO]) & set(noticias))
    print(f"grabaciones con las dos corridas y con noticias: {len(comunes)}")

    POLITICAS = {
        "Qwen solo": (PoliticaExclusion("qwen-solo", EXCLUIBLES_V1, 1), [BASE]),
        "gpt-4o-mini solo": (PoliticaExclusion("4o-solo", EXCLUIBLES_V1, 1), [OTRO]),
        "CONSENSO Qwen + gpt-4o-mini": (
            PoliticaExclusion("consenso", EXCLUIBLES_V1, 2), [BASE, OTRO]),
    }

    def evaluar(grabacion: str) -> dict:
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
        # Categoria por palabra segun el modelo base, para atribuir el daño.
        cat = {}
        for b in corridas[BASE][grabacion]:
            for i in range(b["start_word"], b["end_word"] + 1):
                cat[i] = b["tipo"]

        salida = {}
        for nombre, (pol, modelos) in POLITICAS.items():
            excl = set()
            for lo, hi in pol.rangos([corridas[m][grabacion] for m in modelos]):
                excl.update(range(lo, hi + 1))

            m = {"aire": len(words), "excl_aire": len(excl), "medio": medio,
                 "n": 0, "tocadas": 0, "graves": 0, "totales": 0,
                 "pal_noticia": 0, "pal_borradas": 0,
                 "por_cat": Counter(), "por_medio": Counter()}
            for ini, fin in noticias[grabacion]:
                a, b = ini + args.padding, fin - args.padding
                dentro = [w for w in words if a <= w.start and w.end <= b]
                if not dentro:
                    continue
                m["n"] += 1
                m["pal_noticia"] += len(dentro)
                tocadas = [w for w in dentro if w.index in excl]
                if not tocadas:
                    continue
                frac = len(tocadas) / len(dentro)
                m["tocadas"] += 1
                m["pal_borradas"] += len(tocadas)
                m["graves"] += frac >= 0.5
                m["totales"] += frac >= 0.999
                m["por_medio"][medio] += 1
                for w in tocadas:
                    m["por_cat"][cat.get(w.index, "?")] += 1
            salida[nombre] = m
        return salida

    with ThreadPoolExecutor(max_workers=10) as pool:
        todos = list(pool.map(evaluar, comunes))

    print()
    print("=" * 88)
    print("RIESGO DE CADA POLITICA CONTRA LAS NOTICIAS YA ENTREGADAS")
    print("=" * 88)
    enc = (f"{'politica':30}{'aire excl':>10}{'tocadas':>10}{'>=50%':>9}"
           f"{'100%':>8}{'pal.borr':>10}")
    print(enc)
    print("-" * 88)

    resumen = {}
    for nombre in POLITICAS:
        agg = {"aire": 0, "excl_aire": 0, "n": 0, "tocadas": 0, "graves": 0,
               "totales": 0, "pal_noticia": 0, "pal_borradas": 0}
        cats, medios = Counter(), Counter()
        for t in todos:
            m = t[nombre]
            for k in agg:
                agg[k] += m[k]
            cats += m["por_cat"]
            medios += m["por_medio"]
        n = max(agg["n"], 1)
        resumen[nombre] = (agg, cats, medios)
        print(f"{nombre:30}{100 * agg['excl_aire'] / agg['aire']:>9.1f}%"
              f"{100 * agg['tocadas'] / n:>9.1f}%{100 * agg['graves'] / n:>8.1f}%"
              f"{100 * agg['totales'] / n:>7.1f}%"
              f"{100 * agg['pal_borradas'] / max(agg['pal_noticia'], 1):>9.1f}%")

    print(f"\n  (noticias evaluadas: {resumen[list(POLITICAS)[0]][0]['n']})")
    print("  tocadas = al menos una palabra excluida | pal.borr = % de palabras")
    print("  de noticia que la politica elimina")

    for nombre in POLITICAS:
        agg, cats, medios = resumen[nombre]
        print(f"\n{'-' * 88}\n{nombre}")
        if not agg["tocadas"]:
            print("  sin impacto: ninguna noticia tocada")
            continue
        tot_cat = sum(cats.values()) or 1
        print("  categorias responsables (por palabra borrada, segun el modelo base):")
        print("    " + "  ".join(f"{k}={100 * v / tot_cat:.0f}%" for k, v in cats.most_common(5)))
        print("  emisoras responsables (noticias tocadas):")
        print("    " + "  ".join(f"{k}={v}" for k, v in medios.most_common(6)))

    base_agg = resumen["Qwen solo"][0]
    cons_agg = resumen["CONSENSO Qwen + gpt-4o-mini"][0]
    n = max(base_agg["n"], 1)
    print(f"\n{'=' * 88}\nREDUCCION QUE APORTA EL CONSENSO\n{'=' * 88}")
    for etiqueta, clave in (("noticias tocadas", "tocadas"), ("con >=50% borrado", "graves"),
                            ("borradas al 100%", "totales")):
        a, b = base_agg[clave], cons_agg[clave]
        factor = f"{a / b:.1f}x menos" if b else "a cero"
        print(f"  {etiqueta:22} {100 * a / n:6.2f}%  ->  {100 * b / n:6.2f}%   ({factor})")
    print(f"  {'aire excluido':22} {100 * base_agg['excl_aire'] / base_agg['aire']:6.2f}%  ->  "
          f"{100 * cons_agg['excl_aire'] / cons_agg['aire']:6.2f}%")
    print("\n  CRITERIO: apta si el daño grave (>=50%) queda por debajo de 0.5%.")


if __name__ == "__main__":
    main()
