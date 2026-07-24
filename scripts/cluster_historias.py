"""Agrupa Noticias en Historias: la misma noticia cubierta por varias emisoras
queda bajo un solo evento.

Es la implementacion del hallazgo principal de docs/EFFICIENCY_REVIEW.md §2 --
medido sobre 145 grabaciones, un mismo evento aparecia hasta 8 veces con
titulos distintos, y un periodista lo curaba 8 veces.

Idempotente: una Noticia que ya tiene `historia_id` no se reprocesa.

Uso:
    python scripts/cluster_historias.py --limit 2000
    python scripts/cluster_historias.py --umbral 0.86
    python scripts/cluster_historias.py --dry-run      # no escribe, solo reporta
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.ai.embeddings import OpenAIEmbeddingProvider  # noqa: E402
from src.modules.editorial.dedup import (  # noqa: E402
    UMBRAL_SIMILITUD,
    VENTANA_HORAS,
    HistoriaClusterer,
    texto_para_embedding,
)
from src.modules.editorial.models import Historia, Noticia, NoticiaVersion  # noqa: E402
from src.modules.media.models import Medio, Programa  # noqa: E402
from src.modules.recordings.models import Grabacion  # noqa: E402

# Cuantos textos se embeben por llamada a la API.
LOTE_EMBEDDING = 100


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--umbral", type=float, default=UMBRAL_SIMILITUD)
    parser.add_argument("--ventana-horas", type=int, default=VENTANA_HORAS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not settings.openai_api_key:
        raise SystemExit("falta OPENAI_API_KEY (se usa para los embeddings)")

    embeddings = OpenAIEmbeddingProvider(settings.openai_api_key.get_secret_value())

    with Session(get_engine()) as session:
        # Se ordena por fecha ascendente a proposito: agrupar en orden
        # cronologico hace que la primera aparicion sea la que abre la
        # historia, que es lo que uno espera al leer "primera_aparicion".
        stmt = (
            select(Noticia, NoticiaVersion, Grabacion.fecha_inicio, Medio.id)
            .join(NoticiaVersion, NoticiaVersion.id == Noticia.version_actual_id)
            .join(Grabacion, Grabacion.id == Noticia.grabacion_id)
            .join(Programa, Programa.id == Grabacion.programa_id)
            .join(Medio, Medio.id == Programa.medio_id)
            .where(Noticia.historia_id.is_(None))
            .order_by(Grabacion.fecha_inicio.asc())
            .limit(args.limit)
        )
        filas = list(session.execute(stmt).all())
        if not filas:
            print("no hay noticias sin agrupar")
            return

        print(f"agrupando {len(filas)} noticias (umbral={args.umbral}, ventana={args.ventana_horas}h)")

        clusterer = HistoriaClusterer(
            session, embeddings, umbral=args.umbral, ventana_horas=args.ventana_horas
        )

        nuevas = agrupadas = 0
        for inicio in range(0, len(filas), LOTE_EMBEDDING):
            lote = filas[inicio : inicio + LOTE_EMBEDDING]
            vectores = embeddings.embed(
                [texto_para_embedding(v.titulo, v.resumen) for _, v, _, _ in lote]
            )

            for (noticia, version, fecha, medio_id), vector in zip(lote, vectores):
                asignacion = clusterer.asignar(
                    titulo=version.titulo,
                    resumen=version.resumen,
                    momento=fecha,
                    medio_id=medio_id,
                    embedding=vector,
                )
                if asignacion.es_nueva:
                    nuevas += 1
                else:
                    agrupadas += 1
                noticia.historia_id = asignacion.historia.id

            if args.dry_run:
                session.rollback()
            else:
                session.commit()
            print(f"  ... {min(inicio + LOTE_EMBEDDING, len(filas))}/{len(filas)}", flush=True)

        if args.dry_run:
            session.rollback()
            print("\n[dry-run] nada se escribio")
        else:
            session.commit()

        print(f"\nlisto: {nuevas} historias nuevas, {agrupadas} noticias agrupadas en existentes")

        if not args.dry_run:
            top = session.execute(
                select(Historia.titulo_canonico, Historia.total_apariciones)
                .where(Historia.total_apariciones > 1)
                .order_by(Historia.total_apariciones.desc())
                .limit(10)
            ).all()
            if top:
                print("\nhistorias con mas cobertura:")
                for titulo, veces in top:
                    print(f"  {veces:3} apariciones  {titulo[:70]}")

            sin_agrupar = session.scalar(
                select(func.count()).select_from(Historia).where(Historia.total_apariciones == 1)
            )
            print(f"\nhistorias de una sola aparicion: {sin_agrupar}")


if __name__ == "__main__":
    main()
