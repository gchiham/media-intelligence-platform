"""Ingesta de periodicos en PDF: registro, renderizado y extraccion.

Pipeline en fases separadas a proposito. Las deterministas (registrar, render)
no cuestan tokens y se pueden repetir sin miedo; las que llaman al modelo
(extraer, traducir) son las caras y van por Batch API, que es 50% mas barato y
no es sensible a latencia porque esto es un backfill.

    registrar -> render -> extraer -> recolectar -> traducir -> recolectar-trad

Cada fase es idempotente: `registrar` deduplica por sha256, `render` salta las
paginas que ya estan en S3, `extraer` ignora ediciones ya extraidas. Se puede
re-correr cualquiera sin ensuciar nada.

Uso:
    python scripts/prensa_pdf_ingest.py --registrar --dir /data/telegram_pdfs
    python scripts/prensa_pdf_ingest.py --render [--limite N]
    python scripts/prensa_pdf_ingest.py --estado
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.media.models import Medio  # noqa: E402
from src.modules.prensa import pdf as pdflib  # noqa: E402
from src.modules.prensa.models import Edicion  # noqa: E402

BUCKET = settings.clips_bucket
PREFIJO_PDF = "prensa_pdf"
PREFIJO_PAGINAS = "prensa_pdf/paginas"


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}  {msg}", flush=True)


def _s3():
    return boto3.client("s3", region_name=settings.aws_region)


def registrar(session: Session, directorio: Path, dry_run: bool) -> int:
    """Da de alta cada PDF como `Edicion`, saltando los repetidos.

    La dedup es por sha256 y no por (medio, fecha): el canal republica el mismo
    archivo con otro nombre y a veces en otro dia, asi que la clave natural no
    los agarra.
    """
    medios = {m.codigo: m.id for m in session.scalars(select(Medio))}
    vistos = set(session.scalars(select(Edicion.sha256)))
    nuevos = saltados = sin_medio = sin_fecha = 0

    for ruta in sorted(directorio.rglob("*.pdf")):
        ident = pdflib.identificar(ruta.name)
        if not ident.medio:
            log(f"SIN MEDIO reconocible, se salta: {ruta.name}")
            sin_medio += 1
            continue
        if ident.medio not in medios:
            log(f"FALTA el Medio '{ident.medio}' en la tabla medios: {ruta.name}")
            sin_medio += 1
            continue
        if not ident.fecha:
            # El nombre no trae el dia (pasa con La Patrulla Grafica). No se
            # inventa: queda fuera para revision manual.
            log(f"SIN FECHA en el nombre, se salta: {ruta.name}")
            sin_fecha += 1
            continue

        h = pdflib.sha256(ruta)
        if h in vistos:
            saltados += 1
            continue

        lectura = pdflib.leer(ruta)
        vistos.add(h)
        nuevos += 1
        log(
            f"{ident.medio:17} {ident.fecha}  {lectura.paginas_total:3} pags "
            f"({len(lectura.paginas_sin_texto)} sin texto)  {ruta.name[:44]}"
        )
        if dry_run:
            continue
        session.add(
            Edicion(
                medio_id=medios[ident.medio],
                fecha_edicion=ident.fecha,
                sha256=h,
                s3_pdf_key=f"{PREFIJO_PDF}/{ruta.parent.name}/{ruta.name}",
                paginas_total=lectura.paginas_total,
                paginas_sin_texto=lectura.paginas_sin_texto or None,
                origen_url=_origen(ruta.name),
            )
        )
    if not dry_run:
        session.commit()
    log(
        f"registradas: {nuevos} | repetidas (sha256): {saltados} | "
        f"sin medio: {sin_medio} | sin fecha: {sin_fecha}"
        + ("  [DRY RUN, no se escribio]" if dry_run else "")
    )
    return nuevos


def _origen(nombre: str) -> str | None:
    """El nombre empieza con el id del mensaje de Telegram."""
    ident = nombre.split("_", 1)[0]
    canal = "Hondurasperiodico"
    return f"https://t.me/{canal}/{ident}" if ident.isdigit() else None


def render(session: Session, directorio: Path, limite: int | None) -> None:
    """Sube cada pagina como JPEG para que el front pueda mostrarla.

    Renderiza TODAS las paginas, no solo las que tienen notas: el usuario que
    abre la pagina 5 va a querer hojear la 4 y la 6, y el costo en S3 es
    despreciable frente a tener que volver a abrir el PDF.
    """
    s3 = _s3()
    ediciones = list(session.scalars(select(Edicion).order_by(Edicion.fecha_edicion.desc())))
    if limite:
        ediciones = ediciones[:limite]

    for ed in ediciones:
        ruta = directorio / Path(ed.s3_pdf_key).parent.name / Path(ed.s3_pdf_key).name
        if not ruta.exists():
            log(f"no esta el archivo local, se salta: {ruta.name}")
            continue
        hechas = saltadas = 0
        for n in range(1, ed.paginas_total + 1):
            key = f"{PREFIJO_PAGINAS}/{ed.id}/{n:03d}.jpg"
            try:
                s3.head_object(Bucket=BUCKET, Key=key)
                saltadas += 1
                continue
            except ClientError:
                pass
            s3.put_object(
                Bucket=BUCKET, Key=key,
                Body=pdflib.render_pagina(ruta, n),
                ContentType="image/jpeg",
            )
            hechas += 1
        log(f"{ed.fecha_edicion} {ruta.name[:40]:42} {hechas} paginas nuevas, {saltadas} ya estaban")


def estado(session: Session) -> None:
    total = session.scalar(select(Edicion).with_only_columns(Edicion.id).exists().select()) or False
    ediciones = list(session.scalars(select(Edicion).order_by(Edicion.fecha_edicion)))
    if not ediciones:
        print("no hay ediciones registradas")
        return
    medios = {m.id: m.codigo for m in session.scalars(select(Medio))}
    print(f"{len(ediciones)} ediciones | {sum(e.paginas_total for e in ediciones)} paginas")
    print(f"{'fecha':12} {'medio':18} {'pags':>5} {'sin texto':>10}  extraida")
    for e in ediciones:
        sin = len(e.paginas_sin_texto or [])
        print(
            f"{str(e.fecha_edicion):12} {medios.get(e.medio_id, '?'):18} "
            f"{e.paginas_total:5} {sin:10}  {e.extraido_at or '-'}"
        )
    _ = total


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", type=Path, default=Path("/data/telegram_pdfs"))
    p.add_argument("--registrar", action="store_true")
    p.add_argument("--render", action="store_true")
    p.add_argument("--estado", action="store_true")
    p.add_argument("--limite", type=int, default=None)
    p.add_argument("--dry-run", action="store_true", help="con --registrar: no escribe")
    a = p.parse_args()

    with Session(get_engine()) as session:
        if a.registrar:
            registrar(session, a.dir, a.dry_run)
        if a.render:
            render(session, a.dir, a.limite)
        if a.estado:
            estado(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
