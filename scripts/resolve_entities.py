"""Resuelve las menciones crudas de `NoticiaVersion.metadatos_ia` contra el
catalogo `Entidad`, y las enlaza via `NoticiaVersionEntidad`.

Hasta ahora el LLM guardaba people/organizations/locations como texto libre y
nadie poblaba `entidades` -- o sea que la pregunta que da valor al producto
("¿cuantas veces mencionaron a X?") no se podia responder. Ver
docs/EFFICIENCY_REVIEW.md §6 y src/modules/ai/entity_resolution.py.

Idempotente: una version ya enlazada no se vuelve a procesar.

Uso: python scripts/resolve_entities.py --limit 5000
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.ai.entity_resolution import EntityResolver  # noqa: E402
from src.modules.ai.models import Entidad, TipoEntidad  # noqa: E402
from src.modules.editorial.models import NoticiaVersion, NoticiaVersionEntidad  # noqa: E402

# Como mapea cada lista de metadatos_ia a un tipo del catalogo. `organizations`
# se resuelve como INSTITUCION: el LLM no distingue institucion de empresa de
# forma confiable, y separarlas mal fragmenta el catalogo. Un humano puede
# reclasificar despues; fusionar dos tipos mal asignados es mas facil que
# descubrir que "Banco Central" quedo partido en dos entidades.
_CAMPOS = {
    "people": TipoEntidad.PERSONA,
    "organizations": TipoEntidad.INSTITUCION,
    "locations": TipoEntidad.LUGAR,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()

    with Session(get_engine()) as session:
        resolver = EntityResolver(session)

        ya_enlazadas = select(NoticiaVersionEntidad.noticia_version_id).distinct()
        versiones = session.scalars(
            select(NoticiaVersion)
            .where(NoticiaVersion.id.notin_(ya_enlazadas))
            .limit(args.limit)
        ).all()

        procesadas = enlaces = 0
        for version in versiones:
            metadatos = version.metadatos_ia or {}
            vistos: set = set()

            for campo, tipo in _CAMPOS.items():
                for mencion in metadatos.get(campo, []) or []:
                    if not isinstance(mencion, str) or not mencion.strip():
                        continue
                    entidad = resolver.resolve(mencion, tipo)
                    if entidad is None:
                        continue
                    # El id de una entidad recien creada no existe hasta el
                    # flush; hace falta para el FK de la tabla puente.
                    if entidad.id is None:
                        session.flush()
                    if entidad.id in vistos:
                        continue
                    vistos.add(entidad.id)
                    session.add(
                        NoticiaVersionEntidad(
                            noticia_version_id=version.id, entidad_id=entidad.id
                        )
                    )
                    enlaces += 1

            procesadas += 1
            if procesadas % 100 == 0:
                session.commit()
                print(f"  ... {procesadas} versiones procesadas", flush=True)

        session.commit()

        total_entidades = session.scalar(select(func.count()).select_from(Entidad))
        top = session.scalars(
            select(Entidad).order_by(Entidad.menciones.desc()).limit(10)
        ).all()

    print(f"listo: {procesadas} versiones | {enlaces} enlaces | {total_entidades} entidades")
    if top:
        print("\nmas mencionadas:")
        for e in top:
            print(f"  {e.menciones:5}  {e.tipo.value:12} {e.nombre}")


if __name__ == "__main__":
    main()
