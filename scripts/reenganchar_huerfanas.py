"""Devuelve a PENDIENTE las Grabacion que quedaron atascadas en PROCESANDO
sin transcripcion -- huerfanas que nada recupera solo.

POR QUE EXISTEN. Una Grabacion pasa a PROCESANDO cuando QueueService publica
su mensaje en SQS. Si ese mensaje desaparece sin que nadie lo procese, la fila
queda en PROCESANDO para siempre: `enqueue_transcriptions.py` solo mira
PENDIENTE, asi que no la reintenta nunca. Dos formas de perderlo, ambas vistas
en produccion:

  1. PURGA DE LA COLA. Documentado en docs/BENCHMARK_TURBO_20260808.md: la cola
     se purgo el 8-ago para aislar una medicion y "el backlog previo (~2,117
     mensajes) quedo representado solo en Postgres como procesando".
  2. RETENTION DE SQS. La cola tiene MessageRetentionPeriod=345600 (4 DIAS).
     Todo mensaje que no se consuma en ese plazo se borra solo, sin aviso. Con
     ~400 grabaciones/dia encolandose, un backlog que tarde mas de 4 dias en
     drenarse pierde su cola por el extremo viejo mientras sigue creciendo por
     el nuevo.

Al 2026-08-19 habia 4,619 huerfanas, todas entre el 27-jul y el 8-ago: el
corte es limpio porque desde el 9-ago el fleet se puso al dia y ningun mensaje
volvio a expirar.

NO CONFUNDIR con las que estan legitimamente en vuelo: una Grabacion recien
encolada tambien esta en PROCESANDO sin transcripcion. Por eso `--antiguedad`
(default 24 h) exige que la grabacion sea vieja antes de tocarla.

Uso:
    python scripts/reenganchar_huerfanas.py --dry-run
    python scripts/reenganchar_huerfanas.py --limit 500
    python scripts/reenganchar_huerfanas.py --fecha-desde 2026-07-27 --fecha-hasta 2026-08-09
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402

# Se selecciona por SQL directo y no por el repositorio porque el criterio
# ("PROCESANDO sin transcripcion y vieja") es de reparacion, no del dominio:
# no vale la pena agregarlo a GrabacionRepository para un script one-off.
_HUERFANAS = """
    SELECT g.id, g.fecha_inicio, m.codigo
    FROM grabaciones g
    JOIN programas p ON p.id = g.programa_id
    JOIN medios m ON m.id = p.medio_id
    LEFT JOIN transcripciones t ON t.grabacion_id = g.id
    WHERE g.estado = 'procesando'
      AND t.id IS NULL
      AND g.fecha_inicio < :tope
      {filtros}
    ORDER BY g.fecha_inicio ASC
    LIMIT :limit
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--antiguedad", type=int, default=24,
                   help="horas: no tocar grabaciones mas nuevas (pueden estar en vuelo)")
    p.add_argument("--fecha-desde", default=None, help="YYYY-MM-DD (UTC)")
    p.add_argument("--fecha-hasta", default=None, help="YYYY-MM-DD (UTC), exclusivo")
    p.add_argument("--medio", default=None, help="codigo del Medio")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    tope = datetime.now(timezone.utc) - timedelta(hours=a.antiguedad)
    filtros, params = "", {"tope": tope, "limit": a.limit}
    if a.fecha_desde:
        filtros += " AND g.fecha_inicio >= :fd"
        params["fd"] = datetime.fromisoformat(a.fecha_desde).replace(tzinfo=timezone.utc)
    if a.fecha_hasta:
        filtros += " AND g.fecha_inicio < :fh"
        params["fh"] = datetime.fromisoformat(a.fecha_hasta).replace(tzinfo=timezone.utc)
    if a.medio:
        filtros += " AND m.codigo = :medio"
        params["medio"] = a.medio

    with Session(get_engine()) as s:
        filas = list(s.execute(text(_HUERFANAS.format(filtros=filtros)), params))
        if not filas:
            print("no hay huerfanas que reenganchar con esos filtros")
            return

        por_medio: dict[str, int] = {}
        for _, _, codigo in filas:
            por_medio[codigo] = por_medio.get(codigo, 0) + 1
        print(f"{len(filas)} huerfanas ({filas[0][1]:%Y-%m-%d} -> {filas[-1][1]:%Y-%m-%d})")
        for codigo, n in sorted(por_medio.items(), key=lambda x: -x[1])[:10]:
            print(f"    {codigo:20} {n}")

        if a.dry_run:
            print("\nDRY RUN -- no se modifico nada")
            return

        # A PENDIENTE, no encolar directo: asi las toma el cron de
        # enqueue_transcriptions con su propio limite y ritmo, en vez de meter
        # miles de mensajes de golpe en una cola cuyo retention es de 4 dias.
        ids = [f[0] for f in filas]
        s.execute(
            text("UPDATE grabaciones SET estado = 'pendiente' WHERE id = ANY(:ids)"),
            {"ids": ids},
        )
        s.commit()
        print(f"\n{len(ids)} grabaciones devueltas a PENDIENTE")
        print("el cron de enqueue_transcriptions las ira encolando (cada 5 min, 500 por vez)")


if __name__ == "__main__":
    main()
