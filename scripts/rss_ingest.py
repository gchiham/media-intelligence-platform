"""Ingesta de prensa digital: una pasada sobre las fuentes activas.

No es un loop infinito -- pensado para cron, igual que
scripts/consume_transcription_results.py. Cada 15 minutos alcanza y sobra: la
fuente con menos colchon (hch.tv) cubre 2.9 h con los 10 items que trae, o sea
casi 12x de margen. Ver docs/PRENSA_DIGITAL.md.

Uso:
    python scripts/rss_ingest.py                       # todas las fuentes activas
    python scripts/rss_ingest.py --medio criterio_hn --medio la_prensa
    python scripts/rss_ingest.py --listar               # que hay dado de alta y como le fue

Sale con codigo 1 si alguna fuente fallo, para que el cron lo pueda detectar.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.db import registry  # noqa: F401,E402 -- registra todos los modelos
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.prensa.services import IngestaPrensaService  # noqa: E402


def listar(service: IngestaPrensaService) -> None:
    print(f"{'medio':26} {'tipo':13} {'ultimo poll':20} resultado")
    for fuente in service.fuentes_activas():
        medio = fuente.url.split("/")[2]
        poll = fuente.ultimo_poll_at.strftime("%Y-%m-%d %H:%M UTC") if fuente.ultimo_poll_at else "-"
        aviso = f"  [{fuente.errores_consecutivos} errores seguidos]" if fuente.errores_consecutivos else ""
        print(f"{medio:26} {fuente.tipo.value:13} {poll:20} {fuente.ultimo_resultado or '-'}{aviso}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--medio",
        action="append",
        default=[],
        help="codigo de Medio; repetible. Vacio = todas las fuentes activas",
    )
    p.add_argument("--listar", action="store_true", help="solo mostrar el estado, sin pollear")
    a = p.parse_args()

    with Session(get_engine()) as session:
        service = IngestaPrensaService(session)
        if a.listar:
            listar(service)
            return 0

        resultados = service.procesar_todas(a.medio or None)

    if not resultados:
        print("no hay fuentes activas (correr scripts/seed_fuentes_prensa.py)")
        return 0

    nuevos = sum(r.nuevos for r in resultados)
    errores = [r for r in resultados if r.estado == "error"]
    sin_cambios = sum(1 for r in resultados if r.estado == "sin_cambios")

    for r in sorted(resultados, key=lambda r: (r.estado != "error", -r.nuevos)):
        if r.estado == "sin_cambios":
            continue  # el caso aburrido y mayoritario; se resume abajo
        extra = f" ({r.previos_al_corte} previos al corte)" if r.previos_al_corte else ""
        print(f"{r.medio:26} {r.resumen()}{extra}")

    print(
        f"\n{len(resultados)} fuentes | {nuevos} articulos nuevos | "
        f"{sin_cambios} sin cambios (304) | {len(errores)} con error"
    )
    return 1 if errores else 0


if __name__ == "__main__":
    raise SystemExit(main())
