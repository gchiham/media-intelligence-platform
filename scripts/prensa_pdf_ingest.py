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
from concurrent.futures import ProcessPoolExecutor
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
        # Commit por edicion, no uno solo al final: leer 70 PDF tarda ~20 min y
        # el proceso ya murio por OOM a la novena. Con un commit global se
        # perdia TODO el trabajo; asi lo hecho queda, y re-correr salta lo que
        # ya esta por sha256.
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


def _render_una(args: tuple[str, str, int]) -> tuple[str, int, int, str]:
    """Renderiza y sube las paginas de UNA edicion. Corre en su propio proceso.

    Recibe y devuelve solo tipos simples porque tiene que viajar por pickle
    entre procesos; abre su propio cliente de S3 por lo mismo.
    """
    edicion_id, ruta_str, paginas_total = args
    ruta = Path(ruta_str)
    try:
        s3 = _s3()
        hechas = saltadas = 0
        for n in range(1, paginas_total + 1):
            key = f"{PREFIJO_PAGINAS}/{edicion_id}/{n:03d}.jpg"
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
        return (ruta.name, hechas, saltadas, "")
    except Exception as e:  # noqa: BLE001
        # Un PDF que falle no puede tumbar el lote entero: con 70 archivos, que
        # el numero 9 reviente y se pierda todo lo demas es inaceptable.
        return (ruta.name, 0, 0, f"{type(e).__name__}: {str(e)[:90]}")


def render(session: Session, directorio: Path, limite: int | None, procesos: int) -> None:
    """Sube cada pagina como JPEG para que el front pueda mostrarla.

    Renderiza TODAS las paginas, no solo las que tienen notas: el usuario que
    abre la pagina 5 va a querer hojear la 4 y la 6, y el costo en S3 es
    despreciable frente a tener que volver a abrir el PDF.

    **`--procesos` usa PROCESOS y no hilos, a proposito.** pdfplumber (via
    pypdfium2) no es thread-safe: con 8 hilos abriendo PDF a la vez el estado
    se corrompe entre ellos y tira `MalformedPDFException: Data format error`,
    que hace creer que el archivo esta danado cuando no lo esta -- los mismos
    70 PDF abren perfecto de a uno. Paso el 2026-08-29.

    El default es 1 porque el backend de produccion es un t3.small de 2 vCPU
    que suele tener los creditos de CPU en cero y ya corre el clipper: subirle
    el paralelismo ahi degrada el pipeline. Para el backlog conviene una
    instancia temporal grande (con 8 procesos, 2.166 paginas tardaron 4 min).
    """
    ediciones = list(session.scalars(select(Edicion).order_by(Edicion.fecha_edicion.desc())))
    if limite:
        ediciones = ediciones[:limite]

    trabajos: list[tuple[str, str, int]] = []
    for ed in ediciones:
        ruta = directorio / Path(ed.s3_pdf_key).parent.name / Path(ed.s3_pdf_key).name
        if not ruta.exists():
            log(f"no esta el archivo local, se salta: {ruta.name}")
            continue
        trabajos.append((str(ed.id), str(ruta), ed.paginas_total))

    log(f"{len(trabajos)} ediciones a renderizar, {procesos} proceso(s)")
    if procesos > 1:
        with ProcessPoolExecutor(procesos) as ex:
            resultados = list(ex.map(_render_una, trabajos))
    else:
        resultados = [_render_una(t) for t in trabajos]

    nuevas = errores = 0
    for nombre, hechas, saltadas, error in resultados:
        if error:
            errores += 1
            log(f"ERROR {nombre[:40]:42} {error}")
        else:
            nuevas += hechas
            log(f"{nombre[:40]:42} {hechas} paginas nuevas, {saltadas} ya estaban")
    log(f"paginas subidas: {nuevas} | ediciones con error: {errores}")


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
    p.add_argument(
        "--procesos", type=int, default=1,
        help="procesos para --render. 1 en produccion (t3.small de 2 vCPU sin "
             "creditos); subirlo solo en una instancia temporal grande",
    )
    p.add_argument("--dry-run", action="store_true", help="con --registrar: no escribe")
    a = p.parse_args()

    with Session(get_engine()) as session:
        if a.registrar:
            registrar(session, a.dir, a.dry_run)
        if a.render:
            render(session, a.dir, a.limite, a.procesos)
        if a.estado:
            estado(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
