"""Convierte los veredictos humanos de review.html en las dos cosas que hacen
falta para decidir sobre Destiller: si clasifica bien, y con que umbral correrlo.

La metrica que manda no es la precision global sino **cuanta noticia real se
perderia** al excluir por debajo de cada umbral. Los dos errores no cuestan lo
mismo: dejar pasar publicidad cuesta unos centavos de tokens en el paso de
segmentacion (que ademas ya la filtra por su cuenta, commit 7f4f5b0), mientras
que excluir una noticia real la borra del producto del cliente sin dejar rastro.

Se pondera por palabras y no por bloques porque los bloques no son comparables
entre si: perder una nota de 300 palabras no es el mismo dano que perder una
muletilla de 8.

Uso:
    python scripts/destiller_calibrate.py --veredictos ~/Downloads/veredictos.json
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

TIPOS = ["noticia", "publicidad", "religioso", "promo", "musica", "relleno", "desconocido"]
BASURA = {"publicidad", "religioso", "promo", "musica", "relleno"}
UMBRALES = [0.0, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99]


def cargar(bundle_path: Path, veredictos_path: Path) -> list[dict]:
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    veredictos = json.loads(veredictos_path.read_text(encoding="utf-8"))["veredictos"]

    filas = []
    for g in bundle["grabaciones"]:
        for b in g["bloques"]:
            v = veredictos.get(b["id"])
            if not v:
                continue  # sin validar: no aporta ni a favor ni en contra
            filas.append(
                {
                    "medio": g["medio"],
                    "tipo_llm": b["tipo"],
                    "tipo_real": b["tipo"] if v["ok"] else v["tipo_real"],
                    "confidence": b["confidence"],
                    "palabras": b["end_word"] - b["start_word"] + 1,
                }
            )
    return filas


def cargar_db(modelo: str) -> list[dict]:
    """Veredictos del portal compartido (varias personas validando a la vez).

    Un bloque puede tener veredictos de mas de un validador. Aca se toman todos
    como filas separadas a proposito: si dos personas discrepan sobre el mismo
    bloque, eso NO se promedia ni se resuelve en silencio -- se ve en la matriz
    y en `desacuerdos()`, porque un desacuerdo humano alto significa que la
    categoria esta mal definida y ningun modelo va a poder acertarla.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.infrastructure.db import registry  # noqa: F401
    from src.infrastructure.db.engine import get_engine
    from src.modules.destiller.models import BloqueValidacion, VeredictoValidacion

    with Session(get_engine()) as session:
        filas = session.execute(
            select(
                BloqueValidacion.medio,
                BloqueValidacion.grabacion_ref,
                BloqueValidacion.bloque_idx,
                BloqueValidacion.confidence,
                BloqueValidacion.start_word,
                BloqueValidacion.end_word,
                VeredictoValidacion.validador,
                VeredictoValidacion.ok,
                VeredictoValidacion.tipo_llm,
                VeredictoValidacion.tipo_real,
            )
            .join(VeredictoValidacion, VeredictoValidacion.bloque_id == BloqueValidacion.id)
            .where(BloqueValidacion.modelo == modelo)
        ).all()

    return [
        {
            "medio": f.medio,
            "bloque": f"{f.grabacion_ref}#{f.bloque_idx}",
            "validador": f.validador,
            "tipo_llm": f.tipo_llm.value,
            "tipo_real": f.tipo_llm.value if f.ok else f.tipo_real.value,
            "confidence": f.confidence,
            "palabras": f.end_word - f.start_word + 1,
        }
        for f in filas
    ]


def desacuerdos(filas: list[dict]) -> None:
    """Bloques donde dos validadores dijeron cosas distintas.

    Es la medida de que tan confiable es la verdad contra la que se mide todo
    lo demas. Si es alta, el problema no es el modelo sino la definicion de las
    categorias, y ningun umbral la arregla.
    """
    por_bloque: dict[str, set[str]] = defaultdict(set)
    for f in filas:
        if f.get("validador"):
            por_bloque[f["bloque"]].add(f["tipo_real"])
    multiples = {b: t for b, t in por_bloque.items() if len(t) > 1}
    revisados_por_varios = sum(
        1 for b in por_bloque if sum(1 for f in filas if f["bloque"] == b) > 1
    )
    if not revisados_por_varios:
        return
    print(f"\nACUERDO ENTRE VALIDADORES  ({revisados_por_varios} bloques con mas de una opinion)")
    print(f"  discrepan en {len(multiples)} ({100 * len(multiples) / revisados_por_varios:.0f}%)")
    for bloque, tipos in list(multiples.items())[:10]:
        print(f"    {bloque}: {' vs '.join(sorted(tipos))}")


def matriz(filas: list[dict]) -> None:
    conteo: dict[tuple[str, str], int] = defaultdict(int)
    for f in filas:
        conteo[(f["tipo_llm"], f["tipo_real"])] += 1

    presentes = [t for t in TIPOS if any(f["tipo_llm"] == t or f["tipo_real"] == t for f in filas)]
    ancho = max(len(t) for t in presentes) + 2

    print("\nMATRIZ  (fila = lo que dijo el LLM, columna = lo que era en realidad)")
    print(" " * ancho + "".join(t[:6].rjust(8) for t in presentes) + "     acierto")
    for llm in presentes:
        fila = [conteo[(llm, real)] for real in presentes]
        total = sum(fila)
        ok = conteo[(llm, llm)]
        pct = f"{100 * ok / total:5.1f}%" if total else "    --"
        print(llm.ljust(ancho) + "".join(str(n).rjust(8) for n in fila) + "    " + pct)


def binario(filas: list[dict]) -> None:
    """Lo que de verdad decide el pipeline: basura si / basura no."""
    vp = sum(1 for f in filas if f["tipo_llm"] in BASURA and f["tipo_real"] in BASURA)
    fp = sum(1 for f in filas if f["tipo_llm"] in BASURA and f["tipo_real"] not in BASURA)
    fn = sum(1 for f in filas if f["tipo_llm"] not in BASURA and f["tipo_real"] in BASURA)
    vn = sum(1 for f in filas if f["tipo_llm"] not in BASURA and f["tipo_real"] not in BASURA)

    prec = vp / (vp + fp) if vp + fp else 0.0
    rec = vp / (vp + fn) if vp + fn else 0.0
    print("\nBASURA vs NOTICIA (sin umbral, tomando toda decision del LLM)")
    print(f"  precision {prec:.3f}   recall {rec:.3f}")
    print(f"  basura bien detectada {vp}   basura que se colo {fn}")
    print(f"  NOTICIA REAL marcada como basura: {fp}   noticia bien conservada {vn}")


def curva(filas: list[dict]) -> None:
    palabras_basura = sum(f["palabras"] for f in filas if f["tipo_real"] in BASURA)
    palabras_noticia = sum(f["palabras"] for f in filas if f["tipo_real"] == "noticia")

    print("\nCURVA DE UMBRAL  (excluir bloque si el LLM dijo basura y confidence >= umbral)")
    print("  umbral   basura excluida   NOTICIA REAL PERDIDA   bloques")
    print("           (% de palabras)      (% de palabras)    excluidos")
    for u in UMBRALES:
        excluidos = [f for f in filas if f["tipo_llm"] in BASURA and f["confidence"] >= u]
        basura_ok = sum(f["palabras"] for f in excluidos if f["tipo_real"] in BASURA)
        noticia_mal = sum(f["palabras"] for f in excluidos if f["tipo_real"] == "noticia")
        pb = 100 * basura_ok / palabras_basura if palabras_basura else 0.0
        pn = 100 * noticia_mal / palabras_noticia if palabras_noticia else 0.0
        print(f"   {u:4.2f}      {pb:9.1f}%            {pn:9.2f}%          {len(excluidos):5d}")

    print("\n  El umbral se elige por la tercera columna, no por la segunda: es el")
    print("  porcentaje de periodismo real que Destiller borraria del producto.")


def por_medio(filas: list[dict]) -> None:
    print("\nPOR MEDIO  (% de palabras que el LLM considera basura)")
    agrupado: dict[str, list[dict]] = defaultdict(list)
    for f in filas:
        agrupado[f["medio"]].append(f)
    for medio in sorted(agrupado):
        fs = agrupado[medio]
        total = sum(f["palabras"] for f in fs)
        basura = sum(f["palabras"] for f in fs if f["tipo_real"] in BASURA)
        errores = sum(1 for f in fs if f["tipo_llm"] != f["tipo_real"])
        pct = 100 * basura / total if total else 0.0
        print(f"  {medio:22} {pct:5.1f}%   bloques {len(fs):4d}   mal clasificados {errores:3d}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modelo", help="lee del portal en Postgres (ej. Qwen/Qwen2.5-7B-Instruct)")
    parser.add_argument("--bundle", type=Path, default=Path("data/destiller_eval/bundle.json"))
    parser.add_argument("--veredictos", type=Path, help="JSON exportado del HTML local")
    args = parser.parse_args()

    if args.modelo:
        filas = cargar_db(args.modelo)
        origen = f"portal ({args.modelo})"
    elif args.veredictos:
        filas = cargar(args.bundle, args.veredictos)
        origen = str(args.veredictos)
    else:
        parser.error("pasa --modelo (portal) o --veredictos (HTML local)")

    if not filas:
        raise SystemExit(f"no hay bloques validados todavia en {origen}")

    print(f"{len(filas)} veredictos desde {origen}")

    desacuerdos(filas)
    matriz(filas)
    binario(filas)
    curva(filas)
    por_medio(filas)


if __name__ == "__main__":
    main()
