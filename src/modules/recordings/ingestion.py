"""Ingestion Worker: lee el bucket S3 del sistema de captura externo (mediaCAP) y crea
filas en `grabaciones` para cada archivo de audio nuevo. Idempotente por s3_key (constraint
UNIQUE en la tabla) -- correr varias veces no duplica filas. Disenado para correr en lotes
programados (~6 veces/dia, NFR-001), no en tiempo real.

Soporta dos formatos de nombre de archivo que coexisten en el bucket real:
  - Legado (pre-cutover UTC, antes del 13-jun-2026): {codigo}/{YYYY}/{MM}/{YYYY-MM-DD}_{HH}h.mp3
    La hora es local GMT-6 (ver CLAUDE.md del repo mediaCAP); se convierte a UTC sumando 6h.
  - UTC v2 (post-cutover): {codigo}/{YYYY}/{MM}/{YYYY-MM-DDTHH}Z.mp3
    La hora ya viene en UTC.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import boto3
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.infrastructure.db.engine import get_engine
from src.modules.media.models import Medio, Programa
from src.modules.recordings.models import EstadoGrabacion, Grabacion

BUCKET = "mediadev-recordings"

_LEGACY_RE = re.compile(r"^(?P<codigo>[^/]+)/\d{4}/\d{2}/(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})_(?P<h>\d{2})h\.mp3$")
_UTC_RE = re.compile(r"^(?P<codigo>[^/]+)/\d{4}/\d{2}/(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})T(?P<h>\d{2})Z\.mp3$")

GMT_MINUS_6 = timedelta(hours=-6)


@dataclass
class ArchivoDetectado:
    codigo_medio: str
    s3_key: str
    fecha_inicio: datetime
    fecha_fin: datetime


def _parse_key(key: str) -> ArchivoDetectado | None:
    m = _UTC_RE.match(key)
    if m:
        inicio = datetime(
            int(m["y"]), int(m["m"]), int(m["d"]), int(m["h"]), tzinfo=timezone.utc
        )
        return ArchivoDetectado(m["codigo"], key, inicio, inicio + timedelta(hours=1))

    m = _LEGACY_RE.match(key)
    if m:
        hora_local = datetime(int(m["y"]), int(m["m"]), int(m["d"]), int(m["h"]))
        inicio = (hora_local - GMT_MINUS_6).replace(tzinfo=timezone.utc)
        return ArchivoDetectado(m["codigo"], key, inicio, inicio + timedelta(hours=1))

    return None


def _listar_objetos(s3_client, codigo_medio: str) -> list[str]:
    keys: list[str] = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"{codigo_medio}/"):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def sync_grabaciones(session: Session, s3_client=None) -> int:
    """Escanea S3 para todos los medios seed-eados y crea filas Grabacion faltantes.
    Retorna cuantas filas nuevas se crearon."""
    s3_client = s3_client or boto3.client("s3")

    medios = session.scalars(select(Medio)).all()
    programa_por_medio = {
        p.medio_id: p.id for p in session.scalars(select(Programa)).all()
    }

    nuevas = 0
    for medio in medios:
        programa_id = programa_por_medio.get(medio.id)
        if programa_id is None:
            print(f"[WARN] medio {medio.codigo} no tiene programa; se omite")
            continue

        for key in _listar_objetos(s3_client, medio.codigo):
            archivo = _parse_key(key)
            if archivo is None:
                continue  # objeto que no calza el patron esperado (ej. .json, carpetas de otro tipo)

            stmt = (
                pg_insert(Grabacion)
                .values(
                    programa_id=programa_id,
                    s3_key=archivo.s3_key,
                    fecha_inicio=archivo.fecha_inicio,
                    fecha_fin=archivo.fecha_fin,
                    estado=EstadoGrabacion.PENDIENTE,
                )
                .on_conflict_do_nothing(index_elements=["s3_key"])
                .returning(Grabacion.id)
            )
            result = session.execute(stmt)
            if result.first() is not None:
                nuevas += 1

    session.commit()
    return nuevas


if __name__ == "__main__":
    engine = get_engine()
    with Session(engine) as session:
        creadas = sync_grabaciones(session)
        print(f"Grabaciones nuevas creadas: {creadas}")
