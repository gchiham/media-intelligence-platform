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
import json
import os
import uuid
import sys
import time
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
from src.modules.prensa import extraccion, pdf as pdflib  # noqa: E402
from src.modules.prensa.models import (  # noqa: E402
    Edicion,
    NotaImpresa,
    NotaTraduccion,
)

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


# --------------------------------------------------------------- extraccion
PREFIJO_BATCH = "prensa_pdf/batches"


def _cliente_openai(cuenta: str):
    from openai import OpenAI
    if cuenta == "1":
        return OpenAI(api_key=settings.openai_api_key.get_secret_value())
    k = os.environ.get("OPENAI_API_KEY_2")
    if not k:
        ruta = Path(__file__).parent.parent / ".env"
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            if linea.startswith("OPENAI_API_KEY_2="):
                k = linea.split("=", 1)[1].strip()
    return OpenAI(api_key=k)


def _subir_y_crear(cliente, jsonl: bytes, etiqueta: str) -> str:
    """Sube el JSONL, crea el batch y CONFIRMA que paso la validacion.

    `batches.create()` devuelve el batch en `validating`: que no lance no
    significa que vaya a correr. Sin esperar, se reporta enviado un batch que
    fallo -- el mismo problema que tenia la segmentacion de audio.
    """
    subida = cliente.files.create(file=(f"{etiqueta}.jsonl", jsonl), purpose="batch")
    for _ in range(30):
        if cliente.files.retrieve(subida.id).status == "processed":
            break
        time.sleep(2)
    time.sleep(3)
    b = cliente.batches.create(
        input_file_id=subida.id, endpoint="/v1/chat/completions", completion_window="24h"
    )
    limite = time.monotonic() + 45
    estado = "validating"
    while time.monotonic() < limite:
        actual = cliente.batches.retrieve(b.id)
        estado = actual.status
        if estado != "validating":
            break
        time.sleep(3)
    if estado in ("failed", "expired", "cancelled"):
        datos = getattr(getattr(actual, "errors", None), "data", None) or []
        motivo = f"{datos[0].code}: {datos[0].message}" if datos else "sin detalle"
        raise RuntimeError(f"batch {b.id} quedo en {estado} -- {motivo}")
    return b.id


def extraer(session: Session, directorio: Path, limite: int | None) -> None:
    """Manda a extraer las ediciones que todavia no tienen notas.

    Una request por edicion (no por chunk): un periodico entero son ~16K tokens
    de entrada en markdown, comodo dentro de la ventana, y partirlo obligaria a
    recomponer notas cortadas en el limite.
    """
    s3 = _s3()
    pendientes = list(session.scalars(
        select(Edicion).where(Edicion.extraido_at.is_(None)).order_by(Edicion.fecha_edicion.desc())
    ))
    if limite:
        pendientes = pendientes[:limite]
    if not pendientes:
        log("no hay ediciones pendientes de extraer")
        return

    peticiones, mapa = [], {}
    for ed in pendientes:
        ruta = directorio / Path(ed.s3_pdf_key).parent.name / Path(ed.s3_pdf_key).name
        if not ruta.exists():
            log(f"falta el PDF local, se salta: {ruta.name}")
            continue
        md = pdflib.leer(ruta).markdown
        if not md.strip():
            log(f"sin texto extraible, se salta: {ruta.name}")
            continue
        peticiones.append(extraccion.peticion_extraccion(str(ed.id), md))
        mapa[str(ed.id)] = ruta.name
    if not peticiones:
        log("nada que mandar")
        return

    cuentas = ["1", "2"]
    mitad = (len(peticiones) + 1) // 2
    grupos = [peticiones[:mitad], peticiones[mitad:]]
    for cuenta, grupo in zip(cuentas, grupos):
        if not grupo:
            continue
        cliente = _cliente_openai(cuenta)
        bid = _subir_y_crear(cliente, extraccion.a_jsonl(grupo), f"extraccion_{cuenta}")
        s3.put_object(
            Bucket=BUCKET, Key=f"{PREFIJO_BATCH}/{bid}.json",
            Body=json.dumps({
                "tipo": "extraccion", "cuenta": cuenta, "batch_id": bid,
                "modelo": extraccion.MODELO_EXTRACCION,
                "prompt_version": extraccion.PROMPT_VERSION,
                "ediciones": {p.custom_id.split("|")[1]: mapa[p.custom_id.split("|")[1]] for p in grupo},
            }, ensure_ascii=False).encode(),
            ContentType="application/json",
        )
        log(f"batch extraccion enviado (cuenta {cuenta}): {bid} | {len(grupo)} ediciones")


def recolectar(session: Session) -> None:
    """Levanta los batches terminados y guarda las notas.

    Idempotente por (edicion_id, indice): re-recolectar el mismo batch no
    duplica. El manifiesto en S3 se borra solo cuando todo se guardo bien, asi
    que un fallo a mitad se reintenta en la corrida siguiente.
    """
    s3 = _s3()
    manifiestos = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{PREFIJO_BATCH}/").get("Contents", [])
    if not manifiestos:
        log("no hay batches pendientes de recolectar")
        return

    for obj in manifiestos:
        man = json.loads(s3.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read())
        cliente = _cliente_openai(man["cuenta"])
        b = cliente.batches.retrieve(man["batch_id"])
        if b.status not in ("completed", "failed", "expired", "cancelled"):
            log(f"{man['batch_id'][:24]} aun {b.status} ({b.request_counts.completed}/{b.request_counts.total})")
            continue
        if b.status != "completed":
            log(f"ATENCION {man['batch_id'][:24]} termino en {b.status}; se deja el manifiesto para revisar")
            continue

        crudo = cliente.files.content(b.output_file_id).text
        guardadas = fallidas = 0
        for linea in crudo.splitlines():
            if not linea.strip():
                continue
            r = json.loads(linea)
            tipo, ident = r["custom_id"].split("|", 1)
            cuerpo = r.get("response", {}).get("body", {})
            msg = (cuerpo.get("choices") or [{}])[0].get("message", {}).get("content")
            if not msg:
                fallidas += 1
                continue
            datos = json.loads(msg)
            if tipo == "ext":
                guardadas += _guardar_notas(session, ident, datos, man, cuerpo.get("usage", {}))
            elif tipo == "trad":
                guardadas += _guardar_traduccion(session, ident, datos, man)
        session.commit()
        log(f"{man['batch_id'][:24]} {man['tipo']}: {guardadas} guardadas, {fallidas} sin respuesta")
        s3.put_object(Bucket=BUCKET, Key=obj["Key"].replace(PREFIJO_BATCH, PREFIJO_BATCH + "/hechos"),
                      Body=json.dumps(man).encode(), ContentType="application/json")


def _guardar_notas(session: Session, edicion_id: str, datos: dict, man: dict, usage: dict) -> int:
    ed = session.get(Edicion, uuid.UUID(edicion_id))
    if ed is None:
        return 0
    ya = {n.indice for n in session.scalars(
        select(NotaImpresa).where(NotaImpresa.edicion_id == ed.id))}
    n = 0
    for i, nota in enumerate(datos.get("notas", [])):
        if i in ya:
            continue
        session.add(NotaImpresa(
            edicion_id=ed.id, indice=i,
            titulo=nota["titulo"][:2000], sumario=nota.get("sumario"),
            cuerpo=nota["cuerpo"], seccion=(nota.get("seccion") or None),
            paginas=nota.get("paginas") or [],
        ))
        n += 1
    ed.extraido_at = datetime.now(timezone.utc)
    ed.modelo = man.get("modelo")
    ed.prompt_version = man.get("prompt_version")
    ed.tokens_entrada = usage.get("prompt_tokens")
    ed.tokens_salida = usage.get("completion_tokens")
    return n


def _guardar_traduccion(session: Session, nota_id: str, datos: dict, man: dict) -> int:
    nota = session.get(NotaImpresa, uuid.UUID(nota_id))
    if nota is None:
        return 0
    existe = session.scalar(select(NotaTraduccion).where(
        NotaTraduccion.nota_id == nota.id, NotaTraduccion.idioma == "en"))
    if existe:
        return 0
    session.add(NotaTraduccion(
        nota_id=nota.id, idioma="en",
        titulo=datos["title"], sumario=datos.get("summary"), cuerpo=datos.get("body"),
        modelo=man.get("modelo"), traducido_at=datetime.now(timezone.utc),
    ))
    return 1


def traducir(session: Session, limite: int | None) -> None:
    """Manda a traducir TODAS las notas que aun no tienen version en ingles."""
    s3 = _s3()
    ya = select(NotaTraduccion.nota_id).where(NotaTraduccion.idioma == "en")
    pend = list(session.scalars(
        select(NotaImpresa).where(NotaImpresa.id.notin_(ya)).limit(limite or 5000)))
    if not pend:
        log("no hay notas pendientes de traducir")
        return
    peticiones = [extraccion.peticion_traduccion(str(n.id), n.titulo, n.cuerpo) for n in pend]
    mitad = (len(peticiones) + 1) // 2
    for cuenta, grupo in zip(["1", "2"], [peticiones[:mitad], peticiones[mitad:]]):
        if not grupo:
            continue
        cliente = _cliente_openai(cuenta)
        bid = _subir_y_crear(cliente, extraccion.a_jsonl(grupo), f"traduccion_{cuenta}")
        s3.put_object(
            Bucket=BUCKET, Key=f"{PREFIJO_BATCH}/{bid}.json",
            Body=json.dumps({"tipo": "traduccion", "cuenta": cuenta, "batch_id": bid,
                             "modelo": extraccion.MODELO_TRADUCCION}).encode(),
            ContentType="application/json")
        log(f"batch traduccion enviado (cuenta {cuenta}): {bid} | {len(grupo)} notas")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dir", type=Path, default=Path("/data/telegram_pdfs"))
    p.add_argument("--registrar", action="store_true")
    p.add_argument("--render", action="store_true")
    p.add_argument("--estado", action="store_true")
    p.add_argument("--extraer", action="store_true")
    p.add_argument("--recolectar", action="store_true")
    p.add_argument("--traducir", action="store_true")
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
        if a.extraer:
            extraer(session, a.dir, a.limite)
        if a.recolectar:
            recolectar(session)
        if a.traducir:
            traducir(session, a.limite)
        if a.estado:
            estado(session)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
