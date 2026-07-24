"""Alimenta el indice de contenido repetido con las transcripciones ya
existentes, para que el filtro de publicidad tenga algo que reconocer.

Sin esta pasada previa, `RepeatedContentIndex.debe_saltarse()` nunca dispara:
la primera vez que ve un spot lo cuenta como contenido nuevo. Correr esto
sobre el historico deja el indice "entrenado" antes de mandar el backlog al
LLM. Ver docs/EFFICIENCY_REVIEW.md §3.

Uso: python scripts/learn_repeated_content.py --limit 2000
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.ai.models import ContenidoRepetido  # noqa: E402
from src.modules.ai.repeated_content import RepeatedContentIndex, UMBRAL_APARICIONES  # noqa: E402
from src.modules.ai.schemas import Word  # noqa: E402
from src.modules.media.models import Medio, Programa  # noqa: E402
from src.modules.recordings.models import Grabacion, Transcripcion  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    with Session(get_engine()) as session:
        indice = RepeatedContentIndex(session)

        stmt = (
            select(Transcripcion, Medio.codigo)
            .join(Grabacion, Grabacion.id == Transcripcion.grabacion_id)
            .join(Programa, Programa.id == Grabacion.programa_id)
            .join(Medio, Medio.id == Programa.medio_id)
            .order_by(Grabacion.fecha_inicio.desc())
            .limit(args.limit)
        )

        procesadas = 0
        for transcripcion, codigo in session.execute(stmt):
            words = [Word(**w) for w in (transcripcion.segmentos or {}).get("words", [])]
            if not words:
                continue
            indice.registrar(words, medio_codigo=codigo)
            procesadas += 1
            # Commit periodico: una corrida sobre miles de transcripciones no
            # debe mantener una transaccion gigante abierta contra la misma
            # Postgres que sirve la API (ver el incidente del 2026-07-22).
            if procesadas % 50 == 0:
                session.commit()
                print(f"  ... {procesadas} transcripciones procesadas", flush=True)

        session.commit()

        total = session.scalar(select(func.count()).select_from(ContenidoRepetido))
        publicidad = session.scalar(
            select(func.count())
            .select_from(ContenidoRepetido)
            .where(ContenidoRepetido.veces_visto >= UMBRAL_APARICIONES)
        )

    print(
        f"listo: {procesadas} transcripciones | {total} bloques indexados | "
        f"{publicidad} superan el umbral de {UMBRAL_APARICIONES} apariciones"
    )


if __name__ == "__main__":
    main()
