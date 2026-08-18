"""Corre el filtro de JOH en sincrono (no Batch API), en paralelo, repartido
entre las dos cuentas de OpenAI. Nace de que la Batch API estaba
inusualmente lenta para un volumen chico (23 chunks) -- ver
docs/OPENAI_BATCH_DOS_CUENTAS.md para el patron de batch normal; esto es la
variante sincrona para cuando la latencia importa mas que el descuento del
50%.

Uso:
    python scripts/filtro_joh_sync_paralelo.py --entrada data/export_hoy_medios.txt \\
        --salida data/filtro_joh_hoy_medios --periodo "13 de agosto de 2026 (...)"
"""
import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import OpenAI  # noqa: E402

from scripts.filtro_joh_batch_openai import (  # noqa: E402
    MODELO_OPENAI,
    PRESUPUESTO_CHARS,
    SYSTEM_PROMPT,
    _leer_env_var,
    armar_chunks,
    leer_bloques,
)
from src.infrastructure.config import settings  # noqa: E402

NOTICIA_RE = re.compile(
    r"=+\s*\nNOTICIA\s+\d+\s*\n(.*?)(?=\n=+\s*\nNOTICIA\s+\d+|\Z)", re.S
)


def _clientes() -> list[OpenAI]:
    return [
        OpenAI(api_key=settings.openai_api_key.get_secret_value()),
        OpenAI(api_key=_leer_env_var("OPENAI_API_KEY_2")),
    ]


def _procesar_chunk(cliente: OpenAI, idx: int, texto: str) -> tuple[int, str | None, str | None]:
    try:
        resp = cliente.chat.completions.create(
            model=MODELO_OPENAI,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": texto},
            ],
        )
        return idx, resp.choices[0].message.content, None
    except Exception as exc:  # noqa: BLE001
        return idx, None, str(exc)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--entrada", type=Path, required=True)
    p.add_argument("--salida", type=Path, required=True)
    p.add_argument("--periodo", type=str, required=True)
    p.add_argument("--concurrencia", type=int, default=6)
    a = p.parse_args()

    import scripts.filtro_joh_batch_openai as m
    m.ENTRADA = a.entrada
    bloques = leer_bloques()
    chunks = armar_chunks(bloques, PRESUPUESTO_CHARS)
    print(f"{len(bloques)} noticias | {len(chunks)} chunks | concurrencia {a.concurrencia}")

    clientes = _clientes()
    resultados: dict[int, str] = {}
    errores: list[str] = []

    with ThreadPoolExecutor(max_workers=a.concurrencia) as pool:
        futuros = {
            pool.submit(_procesar_chunk, clientes[i % 2], i, "\n".join(chunk)): i
            for i, chunk in enumerate(chunks)
        }
        for fut in as_completed(futuros):
            idx, texto, err = fut.result()
            if err:
                errores.append(f"chunk_{idx:04d}: {err}")
                print(f"  chunk_{idx:04d}: ERROR {err}")
            else:
                resultados[idx] = texto
                print(f"  chunk_{idx:04d}: ok ({len(texto)} chars)")

    noticias = []
    for idx in range(len(chunks)):
        texto = resultados.get(idx, "")
        if not texto or "SIN NOTICIAS RELEVANTES" in texto[:80]:
            continue
        for m_ in NOTICIA_RE.finditer(texto):
            noticias.append(m_.group(1).strip())

    total_analizadas = len(bloques)
    total_seleccionadas = len(noticias)

    a.salida.mkdir(parents=True, exist_ok=True)
    (a.salida / "resultados_sync.json").write_text(
        json.dumps(resultados, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    delim = "=" * 50
    partes = [
        delim,
        "EXTRACTO DE NOTICIAS RELEVANTES",
        "INFORME ESTRATÉGICO — JUAN ORLANDO HERNÁNDEZ",
        "PERÍODO:",
        a.periodo,
        "TOTAL DE NOTICIAS ANALIZADAS:",
        str(total_analizadas),
        "TOTAL DE NOTICIAS SELECCIONADAS:",
        str(total_seleccionadas),
        delim,
        "",
    ]
    for i, cuerpo in enumerate(noticias, start=1):
        partes.append(delim)
        partes.append(f"NOTICIA {i}")
        partes.append(cuerpo)
        partes.append(delim)
        partes.append("")

    final = a.salida / "noticias_relevantes_hoy.txt"
    final.write_text("\n".join(partes), encoding="utf-8")

    print(f"\n{total_seleccionadas} noticias relevantes de {total_analizadas} analizadas")
    if errores:
        print(f"{len(errores)} errores:")
        for e in errores:
            print(f"  {e}")
    print(f"escrito: {final}")


if __name__ == "__main__":
    main()
