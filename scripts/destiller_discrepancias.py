"""Por que discrepan los tres modelos. Analisis estadistico, no ejemplos sueltos.

No toca produccion, ni la cola, ni prompts, ni umbrales: lee los destilados de
S3 y reporta.

**Unidad de analisis: el bloque de Qwen.** No es arbitrario -- es lo que esta
cargado en el portal, asi que los 50 casos que este script selecciona se pueden
validar directamente sin recargar nada. La etiqueta de los otros dos modelos
para ese bloque es la mayoritaria sobre sus palabras, porque cada modelo corta
en lugares distintos y no hay correspondencia 1:1 entre bloques.

**"Los tres diferentes" no puede existir en la decision binaria.** Solo hay dos
lados (basura / no basura), asi que con tres modelos siempre hay al menos dos
del mismo lado. Ese grupo se reporta sobre la categoria exacta, donde si es
posible.

Uso:
    python scripts/destiller_discrepancias.py
"""
import argparse
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.modules.destiller.prioridad import BASURA, mapa_por_palabra  # noqa: E402

PREFIJO = "destiller_eval/destilado"
BASE = "qwen2-5-7b-instruct"
OTROS = ["gpt-4o-mini", "gpt-5-mini"]
SALIDA = Path("data/destiller_eval")

_VACIAS = set(
    """de la que el en y a los del se las por un para con no una su al lo como mas
    pero sus le ya o este si porque esta entre cuando muy sin sobre tambien me hasta
    hay donde quien desde todo nos durante todos uno les ni contra otros ese eso ante
    ellos e esto mi antes algunos que unos yo otro otras otra el tanto esa estos mucho
    quienes nada muchos cual poco ella estar estas algunas algo nosotros es son fue
    ser han ha habia era esos aqui asi bien ahora dos tres vamos vamos va van hacer
    tiene tienen dice dijo puede pueden hoy solo cada toda todas""".split()
)
_NO_ALFA = re.compile(r"[^\w\s]", re.UNICODE)


def _norm(texto: str) -> str:
    sin = "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")
    return _NO_ALFA.sub(" ", sin.lower())


def tokens(texto: str) -> list[str]:
    return [t for t in _norm(texto).split() if len(t) > 3 and t not in _VACIAS]


# ------------------------------------------------------------------ carga

def cargar(s3, etiqueta: str) -> dict[str, list[dict]]:
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


def construir(s3) -> list[dict]:
    base = cargar(s3, BASE)
    mapas = {e: {g: mapa_por_palabra(bs) for g, bs in cargar(s3, e).items()} for e in OTROS}

    filas = []
    for grabacion, bloques in base.items():
        medio, stem = grabacion.split("__")
        # stem = "2026-07-26T12Z": la hora UTC son las posiciones 11-12.
        hora_local = (int(stem[11:13]) - 6) % 24
        for i, b in enumerate(bloques):
            rango = range(b["start_word"], b["end_word"] + 1)
            etiquetas = {BASE: b["tipo"]}
            for e in OTROS:
                m = mapas[e].get(grabacion, {})
                vistos = [m[w] for w in rango if w in m]
                etiquetas[e] = Counter(vistos).most_common(1)[0][0] if vistos else None
            if any(v is None for v in etiquetas.values()):
                continue
            filas.append(
                {
                    "grabacion": grabacion,
                    "medio": medio,
                    "hora_local": hora_local,
                    "idx": i,
                    "palabras": b["end_word"] - b["start_word"] + 1,
                    "duracion": b["fin"] - b["inicio"],
                    "confidence": b["confidence"],
                    "texto": b["texto"],
                    "tipos": etiquetas,
                }
            )
    return filas


def grupo_binario(f: dict) -> str:
    lados = {e: (t in BASURA) for e, t in f["tipos"].items()}
    if len(set(lados.values())) == 1:
        return "consenso"
    solo = [e for e in lados if list(lados.values()).count(lados[e]) == 1]
    return f"solo:{solo[0]}"


# ------------------------------------------------------------------ 1 y 2

def seccion_grupos(filas: list[dict]) -> dict[str, list[dict]]:
    print("=" * 78)
    print("1. GRUPOS DE (DES)ACUERDO  -- unidad: bloque de Qwen")
    print("=" * 78)

    grupos = defaultdict(list)
    for f in filas:
        grupos[grupo_binario(f)].append(f)

    total = len(filas)
    etiqueta = {
        "consenso": "consenso total (los tres del mismo lado)",
        f"solo:{BASE}": f"gpt-4o-mini + gpt-5-mini  vs  {BASE}",
        "solo:gpt-4o-mini": f"{BASE} + gpt-5-mini  vs  gpt-4o-mini",
        "solo:gpt-5-mini": f"{BASE} + gpt-4o-mini  vs  gpt-5-mini",
    }
    print(f"\n{'grupo':52}{'bloques':>9}{'%':>7}")
    for k in ("consenso", f"solo:{BASE}", "solo:gpt-4o-mini", "solo:gpt-5-mini"):
        n = len(grupos.get(k, []))
        print(f"{etiqueta[k]:52}{n:>9}{100 * n / total:>6.1f}%")
    print(f"{'TOTAL':52}{total:>9}{100.0:>6.1f}%")

    tres = [f for f in filas if len(set(f["tipos"].values())) == 3]
    print(f"\n  'los tres diferentes' en la decision binaria: imposible por construccion")
    print(f"  (solo hay dos lados; con tres modelos siempre hay dos juntos)")
    print(f"  Sobre la CATEGORIA EXACTA si existe: {len(tres)} bloques "
          f"({100 * len(tres) / total:.1f}%) con tres etiquetas distintas")

    print("\n" + "=" * 78)
    print("2. PATRONES POR GRUPO")
    print("=" * 78)
    print(f"\n{'grupo':22}{'bloques':>8}{'palabras':>10}{'dur(s)':>9}{'conf':>7}  emisoras dominantes")
    for k in ("consenso", f"solo:{BASE}", "solo:gpt-4o-mini", "solo:gpt-5-mini"):
        fs = grupos.get(k, [])
        if not fs:
            continue
        pal = sum(f["palabras"] for f in fs) / len(fs)
        dur = sum(f["duracion"] for f in fs) / len(fs)
        conf = sum(f["confidence"] for f in fs) / len(fs)
        top = Counter(f["medio"] for f in fs).most_common(3)
        corto = k.replace("solo:", "solo ")
        print(f"{corto:22}{len(fs):>8}{pal:>10.0f}{dur:>9.0f}{conf:>7.2f}  "
              + ", ".join(f"{m}({n})" for m, n in top))

    print(f"\n  franja horaria (GMT-6) donde mas cae cada grupo:")
    for k in (f"solo:{BASE}", "solo:gpt-4o-mini", "solo:gpt-5-mini"):
        fs = grupos.get(k, [])
        if not fs:
            continue
        horas = Counter(f["hora_local"] for f in fs).most_common(3)
        print(f"    {k.replace('solo:', 'solo '):22} "
              + ", ".join(f"{h:02d}h({n})" for h, n in horas))
    print("\n  (no hay metadata de PROGRAMA en el set de evaluacion -- la franja")
    print("   horaria es el proxy disponible, ver docs/INGESTION_DESIGN.md)")

    print(f"\n  categorias que Qwen puso donde el otro quedo solo:")
    for k in ("solo:gpt-4o-mini", "solo:gpt-5-mini"):
        fs = grupos.get(k, [])
        if not fs:
            continue
        pares = Counter(
            (f["tipos"][BASE], f["tipos"][k.split(":")[1]]) for f in fs
        ).most_common(5)
        print(f"    {k.replace('solo:', 'solo ')}:")
        for (a, b), n in pares:
            print(f"      qwen={a:12} otro={b:12} {n:5} bloques")
    return grupos


# ------------------------------------------------------------------ 3

def seccion_relleno(filas: list[dict]) -> None:
    print("\n" + "=" * 78)
    print("3. LA CATEGORIA 'relleno'")
    print("=" * 78)

    marcan = {e: [f for f in filas if f["tipos"][e] == "relleno"] for e in [BASE] + OTROS}
    print(f"\n  bloques marcados relleno:  "
          + "  ".join(f"{e}={len(v)}" for e, v in marcan.items()))

    print("\n  cuando gpt-4o-mini dice 'relleno', que dicen los otros:")
    for otro in [BASE, "gpt-5-mini"]:
        c = Counter(f["tipos"][otro] for f in marcan["gpt-4o-mini"])
        tot = sum(c.values()) or 1
        print(f"    {otro:22} " + "  ".join(
            f"{k}={100 * v / tot:.0f}%" for k, v in c.most_common(4)))

    print("\n  emisoras donde gpt-4o-mini marca relleno (top 8):")
    por_medio = Counter(f["medio"] for f in marcan["gpt-4o-mini"])
    tot_medio = Counter(f["medio"] for f in filas)
    for medio, n in por_medio.most_common(8):
        print(f"    {medio:22}{n:>5} de {tot_medio[medio]:>4} bloques  "
              f"({100 * n / tot_medio[medio]:.0f}%)")

    print("\n  franja horaria (GMT-6):")
    horas = Counter(f["hora_local"] for f in marcan["gpt-4o-mini"])
    print("    " + "  ".join(f"{h:02d}h={n}" for h, n in sorted(horas.items())))

    disputa = [f for f in marcan["gpt-4o-mini"] if f["tipos"][BASE] == "noticia"]
    print(f"\n  NUCLEO DE LA DISPUTA: {len(disputa)} bloques que gpt-4o-mini llama "
          f"relleno y Qwen llama noticia")
    if disputa:
        pal = sum(f["palabras"] for f in disputa) / len(disputa)
        conf = sum(f["confidence"] for f in disputa) / len(disputa)
        print(f"    palabras promedio {pal:.0f} | confianza de Qwen {conf:.2f}")

        frec = Counter()
        for f in disputa:
            frec.update(set(tokens(f["texto"])))
        print(f"    palabras mas frecuentes (en cuantos de los {len(disputa)} bloques):")
        for w, n in frec.most_common(18):
            print(f"      {w:18}{n:4}  ({100 * n / len(disputa):.0f}%)")

        ngramas = Counter()
        for f in disputa:
            ts = _norm(f["texto"]).split()
            ngramas.update(" ".join(ts[i : i + 4]) for i in range(len(ts) - 3))
        repetidas = [(g, n) for g, n in ngramas.most_common(40) if n >= 3]
        print(f"\n    frases de 4 palabras repetidas en >=3 bloques: {len(repetidas)}")
        for g, n in repetidas[:10]:
            print(f"      {n:3}x  \"{g}\"")
        if not repetidas:
            print("      ninguna -- no es contenido recurrente tipo cortina o muletilla")


# ------------------------------------------------------------------ 4

def seccion_top50(filas: list[dict]) -> list[dict]:
    print("\n" + "=" * 78)
    print("4. LOS 50 BLOQUES MAS INFORMATIVOS PARA VALIDAR")
    print("=" * 78)

    def puntaje(f: dict) -> float:
        tipos = f["tipos"]
        p = 0.0
        if len(set(tipos.values())) == 3:
            p += 3.0                                   # los tres discrepan
        g = grupo_binario(f)
        if g != "consenso":
            p += 2.0
            if g == "solo:gpt-4o-mini":
                p += 1.5                               # la hipotesis viva
        # Alta confianza + desacuerdo = alguien esta seguro y equivocado; eso
        # es mas informativo que un desacuerdo entre dudas.
        p += f["confidence"] * 1.5
        # Mas texto = mas contexto para que el humano decida sin adivinar.
        p += min(math.log10(max(f["palabras"], 1)), 3.0)
        return p

    ranked = sorted(filas, key=puntaje, reverse=True)[:50]
    print(f"\n{'#':>3} {'medio':16}{'qwen':11}{'4o-mini':11}{'5-mini':11}{'conf':>5}{'pal':>6}")
    for n, f in enumerate(ranked[:25], 1):
        t = f["tipos"]
        print(f"{n:>3} {f['medio']:16}{t[BASE]:11}{t['gpt-4o-mini']:11}"
              f"{t['gpt-5-mini']:11}{f['confidence']:>5.2f}{f['palabras']:>6}")
    print(f"    ... y 25 mas (los 50 completos en el JSON)")

    SALIDA.mkdir(parents=True, exist_ok=True)
    destino = SALIDA / "top50_discrepancias.json"
    destino.write_text(
        json.dumps(
            [
                {
                    "grabacion_ref": f["grabacion"], "bloque_idx": f["idx"],
                    "medio": f["medio"], "palabras": f["palabras"],
                    "confidence": f["confidence"], "tipos": f["tipos"],
                    "texto": f["texto"][:400],
                }
                for f in ranked
            ],
            ensure_ascii=False, indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n  -> {destino}  (grabacion_ref + bloque_idx identifican el bloque en el portal)")
    return ranked


# ------------------------------------------------------------------ 5

def seccion_sesgos(filas: list[dict], corridas: dict[str, dict[str, list[dict]]]) -> None:
    print("\n" + "=" * 78)
    print("5. SESGOS CUANTIFICADOS")
    print("=" * 78)

    print(f"\n{'modelo':22}{'bloques':>9}{'pal/bloq':>10}{'noticia%':>10}"
          f"{'relleno%':>10}{'desc.%':>9}{'cambios':>9}")
    for etiqueta, corrida in corridas.items():
        bloques = [b for bs in corrida.values() for b in bs]
        pal = Counter()
        cambios = 0
        for bs in corrida.values():
            for j, b in enumerate(bs):
                pal[b["tipo"]] += b["end_word"] - b["start_word"] + 1
                if j and bs[j - 1]["tipo"] != b["tipo"]:
                    cambios += 1
        tot = sum(pal.values())
        print(f"{etiqueta:22}{len(bloques):>9}{tot / len(bloques):>10.0f}"
              f"{100 * pal['noticia'] / tot:>9.1f}%{100 * pal['relleno'] / tot:>9.1f}%"
              f"{100 * pal['desconocido'] / tot:>8.1f}%{cambios:>9}")

    print("\n  'cambios' = veces que el tipo cambia entre bloques consecutivos.")
    print("  Mas cambios sobre el mismo audio = particion mas fragmentada.")

    print(f"\n  confianza declarada (solo Qwen la reporta por bloque en este set):")
    por_tipo = defaultdict(list)
    for f in filas:
        por_tipo[f["tipos"][BASE]].append(f["confidence"])
    for t, cs in sorted(por_tipo.items(), key=lambda kv: -len(kv[1])):
        print(f"    {t:14}{len(cs):>5} bloques  conf media {sum(cs) / len(cs):.2f}")

    acierta = [f for f in filas if grupo_binario(f) == "consenso"]
    falla = [f for f in filas if grupo_binario(f) == f"solo:{BASE}"]
    if acierta and falla:
        ca = sum(f["confidence"] for f in acierta) / len(acierta)
        cf = sum(f["confidence"] for f in falla) / len(falla)
        print(f"\n  Qwen en consenso: conf {ca:.2f}  |  Qwen solo contra los otros dos: {cf:.2f}")
        print("  Si son parecidas, su confianza NO discrimina cuando se equivoca:")
        print("  eso limita directamente cuanto puede servir el umbral.")


# ------------------------------------------------------------------ 6

def seccion_clusters(filas: list[dict], umbral: float = 0.22) -> None:
    print("\n" + "=" * 78)
    print("6. TIPOS DE DISCREPANCIA (clustering por similitud textual)")
    print("=" * 78)

    disputa = [f for f in filas if grupo_binario(f) != "consenso"]
    if not disputa:
        return

    docs = [set(tokens(f["texto"])) for f in disputa]
    df = Counter()
    for d in docs:
        df.update(d)
    n = len(docs)
    # TF-IDF binario sobre tokens, sin dependencias: pesa mas lo que distingue
    # un bloque de los demas y aplasta el vocabulario comun del noticiero.
    pesos = [{t: math.log(n / df[t]) for t in d if df[t] > 1} for d in docs]
    normas = [math.sqrt(sum(v * v for v in p.values())) or 1.0 for p in pesos]

    def sim(i: int, j: int) -> float:
        a, b = pesos[i], pesos[j]
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(t, 0.0) for t, v in a.items()) / (normas[i] * normas[j])

    asignado = [-1] * n
    clusters: list[list[int]] = []
    for i in range(n):
        if asignado[i] != -1:
            continue
        grupo = [i]
        asignado[i] = len(clusters)
        for j in range(i + 1, n):
            if asignado[j] == -1 and sim(i, j) >= umbral:
                asignado[j] = len(clusters)
                grupo.append(j)
        clusters.append(grupo)

    grandes = sorted((c for c in clusters if len(c) >= 3), key=len, reverse=True)
    sueltos = sum(1 for c in clusters if len(c) < 3)
    print(f"\n  {len(disputa)} bloques en disputa -> {len(clusters)} grupos")
    print(f"  {len(grandes)} grupos con 3+ bloques (patrones repetitivos) | "
          f"{sueltos} casos aislados")

    print(f"\n{'#':>3}{'n':>5}  patron de etiquetas / vocabulario dominante")
    for k, c in enumerate(grandes[:15], 1):
        fs = [disputa[i] for i in c]
        patron = Counter(
            f"{f['tipos'][BASE]}|{f['tipos']['gpt-4o-mini']}|{f['tipos']['gpt-5-mini']}"
            for f in fs
        ).most_common(1)[0][0]
        vocab = Counter()
        for f in fs:
            vocab.update(set(tokens(f["texto"])))
        top = ", ".join(w for w, _ in vocab.most_common(6))
        medios = Counter(f["medio"] for f in fs).most_common(2)
        print(f"{k:>3}{len(c):>5}  {patron}")
        print(f"        {top}")
        print(f"        medios: " + ", ".join(f"{m}({x})" for m, x in medios))


def main() -> None:
    argparse.ArgumentParser().parse_args()
    s3 = boto3.client("s3", region_name=settings.aws_region)
    filas = construir(s3)
    corridas = {e: cargar(s3, e) for e in [BASE] + OTROS}
    print(f"{len(filas)} bloques de Qwen con etiqueta comparable en los tres modelos\n")

    seccion_grupos(filas)
    seccion_relleno(filas)
    seccion_top50(filas)
    seccion_sesgos(filas, corridas)
    seccion_clusters(filas)


if __name__ == "__main__":
    main()
