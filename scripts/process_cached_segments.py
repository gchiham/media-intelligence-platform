"""Convierte lo que dejo el batch en `segmentation_cache` en `Noticia` +
`NoticiaVersion` + `ClienteNoticia`. Es el eslabon que faltaba: hasta ahora el
batch llenaba la cache y nadie la leia (las Noticias existentes venian de
scripts/clipper_ingest.py, que lee JSONs de otro camino).

**El audio es opcional (`--con-clips`).** Sin el flag solo se escribe texto y
`Noticia.clip_s3_uri` queda NULL -- valido a proposito ("la noticia y su texto
ya son validos sin el audio") y suficiente para generar informes. Con el flag
tambien baja el audio de origen, corta con ffmpeg y sube el clip a S3, que es
lo que hace falta para el portal. Los tiempos de inicio/fin se calculan
siempre, asi que se puede correr primero sin clips y volver despues a
cortarlos sin repetir la inferencia.

`ClienteNoticia` se crea en estado SUGERIDA -- que es exactamente para lo que
existe ese estado ("propuesta por el matching automatico de IA"): la noticia
queda propuesta para el cliente, pendiente de que un periodista la confirme o
la descarte. El `rationale` y el `matched_on` del LLM se guardan en
`NoticiaVersion.metadatos_ia` para poder auditar despues por que se sugirio.

**Paralelismo por sharding estatico** (`--shard i --de N`), igual criterio que
clipper_ingest.py: cada instancia procesa las grabaciones cuyo indice cumple
`idx % N == i`. Sin cola ni coordinacion; N instancias identicas con distinto
shard cubren el total exactamente una vez, y como cada grabacion valida su
`PipelineRun` antes de empezar, reintentar es seguro.

Idempotente por `PipelineRun` completado, ademas marca
`segmentation_cache.consumido`.

Uso:
    python scripts/process_cached_segments.py --limit 500
    python scripts/process_cached_segments.py --con-clips --shard 0 --de 6
"""
import argparse
import os
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.ai.clipping import clip_audio  # noqa: E402
from src.modules.ai.mapping import map_words_to_time  # noqa: E402
from src.modules.ai.models import SegmentationCache  # noqa: E402
from src.modules.ai.schemas import NewsSegment, Word  # noqa: E402
from src.modules.editorial.models import (  # noqa: E402
    ClienteNoticia,
    EstadoClienteNoticia,
    EstadoNoticia,
    Noticia,
    NoticiaVersion,
)
from src.modules.pipeline.models import EstadoPipelineRun, PipelineRun  # noqa: E402
from src.modules.recordings.models import Grabacion, Transcripcion  # noqa: E402

PADDING = 2.0
CORTES_PARALELOS = 4  # ffmpeg (CPU) + S3 (red) por grabacion; en serie se usa 1 de 4 vCPU


def limpiar(t):
    """Quita bytes NUL: Postgres los rechaza y un solo caracter aborta el
    INSERT y envenena la sesion. Mismo criterio que clipper_ingest."""
    return t.replace("\x00", "") if isinstance(t, str) else t


def texto_de(words: list[Word], desde: int, hasta: int) -> str:
    return limpiar(" ".join(w.word for w in words if desde <= w.index <= hasta))


def tiempos_de(words: list[Word], desde: int, hasta: int) -> tuple[float, float] | None:
    ventana = [w for w in words if desde <= w.index <= hasta]
    if not ventana:
        return None
    return ventana[0].start, ventana[-1].end


def _ya_procesada(session: Session, grabacion_id) -> bool:
    return session.scalar(
        select(PipelineRun.id)
        .where(PipelineRun.grabacion_id == grabacion_id)
        .where(PipelineRun.estado == EstadoPipelineRun.COMPLETADO)
        .limit(1)
    ) is not None


def _ids_pendientes(session: Session, limit: int, fecha_desde, fecha_hasta,
                    shard: int = 0, de: int = 1) -> list:
    """Solo los ids de grabacion, sin traer JSONB.

    El sharding se hace en Python (no se puede en SQL sin una columna de
    posicion), asi que hay que listar TODO el universo antes de filtrar. Traer
    las filas completas aqui significaria cargar las transcripciones enteras
    de 1,200 grabaciones -- cientos de MB de palabras con timestamps que en su
    mayoria se van a descartar. Con solo los ids la consulta es trivial y cada
    grabacion se carga despues, de a una."""
    stmt = (
        select(Grabacion.id)
        .join(SegmentationCache, SegmentationCache.grabacion_id == Grabacion.id)
        .join(Transcripcion, Transcripcion.grabacion_id == Grabacion.id)
        .where(SegmentationCache.consumido.is_(False))
    )
    if fecha_desde is not None:
        stmt = stmt.where(Grabacion.fecha_inicio >= fecha_desde)
    if fecha_hasta is not None:
        stmt = stmt.where(Grabacion.fecha_inicio < fecha_hasta)
    # El orden tiene que ser estable entre instancias o dos shards podrian
    # quedarse con la misma grabacion: `fecha_inicio` sola no desempata las
    # ~18 grabaciones que arrancan a la misma hora en distintos medios.
    ids = list(session.scalars(stmt.order_by(Grabacion.fecha_inicio.desc(), Grabacion.id)))
    if de > 1:
        ids = [x for i, x in enumerate(ids) if i % de == shard]
    return ids[:limit]


def _cargar_una(session: Session, grabacion_id):
    """Las tres filas de UNA grabacion. Se llama por iteracion para que la
    memoria no crezca con el tamano del backlog."""
    return session.execute(
        select(SegmentationCache, Transcripcion, Grabacion)
        .join(Transcripcion, Transcripcion.grabacion_id == SegmentationCache.grabacion_id)
        .join(Grabacion, Grabacion.id == SegmentationCache.grabacion_id)
        .where(Grabacion.id == grabacion_id)
    ).first()


def _cortar_y_subir(s3, audio: Path, tmpd: Path, clave: str, run_id, segmentos: list[dict],
                    words: list[Word], clips_bucket: str) -> dict[int, str]:
    """Corta y sube en paralelo; devuelve indice -> uri del clip.

    No toca la BD: la sesion de SQLAlchemy no es thread-safe, asi que la
    persistencia queda para el hilo principal (mismo criterio que
    clipper_ingest.py)."""
    def cortar(par):
        i, n = par
        seg = NewsSegment.model_validate(
            {k: v for k, v in n.items() if k in NewsSegment.model_fields}
        )
        t = map_words_to_time(seg, words, padding=PADDING)
        salida = tmpd / f"clip_{i:04d}.mp3"
        clip_audio(audio, salida, t.start_time, t.end_time)
        key = f"clips/{clave}/{run_id}/{i:04d}.mp3"
        s3.upload_file(str(salida), clips_bucket, key)
        salida.unlink(missing_ok=True)
        return i, f"s3://{clips_bucket}/{key}"

    uris: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=CORTES_PARALELOS) as ex:
        futuros = {ex.submit(cortar, p): p[0] for p in enumerate(segmentos)}
        for fut in as_completed(futuros):
            try:
                i, uri = fut.result()
                uris[i] = uri
            except Exception as exc:  # noqa: BLE001 -- un clip fallido no tumba la nota
                print(f"    clip {futuros[fut]} fallo: {type(exc).__name__}: {str(exc)[:100]}")
    return uris


def procesar(limit: int, fecha_desde, fecha_hasta, dry_run: bool,
             con_clips: bool = False, shard: int = 0, de: int = 1) -> None:
    s3 = None
    clips_bucket = capture_bucket = None
    if con_clips:
        import boto3

        s3 = boto3.client("s3", region_name=settings.aws_region)
        clips_bucket = settings.clips_bucket
        capture_bucket = settings.capture_bucket

    with Session(get_engine()) as session:
        ids = _ids_pendientes(session, limit, fecha_desde, fecha_hasta, shard, de)
        print(f"{len(ids)} grabaciones con segmentos cacheados sin consumir"
              + (f" (shard {shard}/{de})" if de > 1 else "")
              + (" | con corte de audio" if con_clips else " | solo texto"), flush=True)

        total_noticias = total_sugerencias = saltadas = 0
        total_clips = procesadas = 0
        filas = ids  # solo para los mensajes de progreso

        for grabacion_id in ids:
            cargada = _cargar_una(session, grabacion_id)
            if cargada is None:
                saltadas += 1
                continue
            cache, transcripcion, grabacion = cargada
            if _ya_procesada(session, grabacion.id):
                cache.consumido = True
                saltadas += 1
                continue

            words = [Word(**w) for w in (transcripcion.segmentos or {}).get("words", [])]
            if not words or not cache.segmentos:
                cache.consumido = True
                saltadas += 1
                continue

            if dry_run:
                relevantes = sum(len(s.get("client_relevance") or []) for s in cache.segmentos)
                print(f"  {grabacion.id}: {len(cache.segmentos)} noticias, {relevantes} sugerencias")
                total_noticias += len(cache.segmentos)
                total_sugerencias += relevantes
                continue

            run = PipelineRun(
                grabacion_id=grabacion.id,
                estado=EstadoPipelineRun.EN_PROGRESO,
                iniciado_at=datetime.now(timezone.utc),
            )
            session.add(run)
            session.flush()

            # El audio se baja UNA vez por grabacion y de ahi salen todos sus
            # clips: la descarga (~35 MB) domina el tiempo, cortar diez notas
            # de la misma hora cuesta casi lo mismo que cortar una.
            uris: dict[int, str] = {}
            tmpdir = None
            if con_clips:
                try:
                    tmpdir = tempfile.TemporaryDirectory(prefix="clipper_")
                    tmpd = Path(tmpdir.name)
                    audio = tmpd / "fuente.ts"
                    s3.download_file(capture_bucket, grabacion.s3_key, str(audio))
                    clave = grabacion.s3_key.rsplit(".", 1)[0].replace("/", "_")
                    uris = _cortar_y_subir(s3, audio, tmpd, clave, run.id,
                                           cache.segmentos, words, clips_bucket)
                except Exception as exc:  # noqa: BLE001
                    # Sin audio la nota sigue siendo valida: se persiste el
                    # texto y el clip queda pendiente para otra corrida.
                    print(f"  {grabacion.id}: audio no disponible "
                          f"({type(exc).__name__}: {str(exc)[:80]}), se guarda solo texto")

            creadas = sugeridas = 0
            for idx_seg, seg in enumerate(cache.segmentos):
                # Savepoint por noticia: si una falla, se revierte solo esa y
                # la sesion sigue viva para las demas.
                try:
                    with session.begin_nested():
                        tiempos = tiempos_de(words, seg["start_word"], seg["end_word"])
                        if tiempos is None:
                            continue
                        inicio, fin = tiempos

                        noticia = Noticia(
                            grabacion_id=grabacion.id,
                            pipeline_run_id=run.id,
                            estado=EstadoNoticia.PENDIENTE,
                            clip_inicio_seg=inicio,
                            clip_fin_seg=fin,
                            clip_s3_uri=uris.get(idx_seg),  # None si no se cortó audio
                        )
                        session.add(noticia)
                        session.flush()

                        relevancia = seg.get("client_relevance") or []
                        version = NoticiaVersion(
                            noticia_id=noticia.id,
                            numero_version=1,
                            titulo=limpiar(seg["title"])[:500],
                            resumen=limpiar(seg["summary"]),
                            transcripcion_texto=texto_de(words, seg["start_word"], seg["end_word"]),
                            confianza={"overall": seg.get("confidence")},
                            metadatos_ia={
                                "news_type": seg.get("news_type"),
                                "keywords": [limpiar(k) for k in seg.get("keywords", [])],
                                "people": [limpiar(p) for p in seg.get("people", [])],
                                "organizations": [limpiar(o) for o in seg.get("organizations", [])],
                                "locations": [limpiar(l) for l in seg.get("locations", [])],
                                # Se guarda tal cual para poder auditar despues
                                # por que se le sugirio esta noticia a cada
                                # cliente -- es la unica traza del criterio.
                                "client_relevance": relevancia,
                                "origen": f"batch_openai::{cache.modelo}",
                            },
                            es_generada_por_ia=True,
                        )
                        session.add(version)
                        session.flush()
                        noticia.version_actual_id = version.id

                        # Dedup por cliente: `ClienteNoticia` tiene
                        # UniqueConstraint(tenant_id, noticia_id), y si el
                        # modelo devuelve dos veces el mismo client_id para
                        # una noticia el INSERT duplicado revienta el
                        # savepoint y se pierde la noticia entera, no solo la
                        # sugerencia repetida.
                        ya_sugeridos = set()
                        for r in relevancia:
                            if not r.get("relevant"):
                                continue
                            try:
                                tenant_id = uuid.UUID(r["client_id"])
                            except (ValueError, KeyError, TypeError):
                                continue  # client_id inventado o malformado
                            if tenant_id in ya_sugeridos:
                                continue
                            ya_sugeridos.add(tenant_id)
                            session.add(
                                ClienteNoticia(
                                    tenant_id=tenant_id,
                                    noticia_id=noticia.id,
                                    estado=EstadoClienteNoticia.SUGERIDA,
                                )
                            )
                            sugeridas += 1
                    creadas += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"    noticia de {grabacion.id} fallo: {type(exc).__name__}: {str(exc)[:120]}")

            run.estado = EstadoPipelineRun.COMPLETADO
            run.finalizado_at = datetime.now(timezone.utc)
            run.noticias_generadas = creadas
            run.metadatos = {
                "origen": "process_cached_segments",
                "clips_generados": len(uris),
                "padding_seconds": PADDING if con_clips else None,
            }
            cache.consumido = True
            session.commit()
            if tmpdir is not None:
                tmpdir.cleanup()
            # Sin esto la identity map de SQLAlchemy retiene cada
            # transcripcion ya procesada y la memoria crece con el backlog.
            session.expunge_all()

            total_noticias += creadas
            total_clips += len(uris)
            total_sugerencias += sugeridas
            procesadas += 1
            if procesadas % 10 == 0:
                print(f"  [{procesadas}/{len(filas)}] {total_noticias} noticias, "
                      f"{total_clips} clips", flush=True)

        if not dry_run:
            session.commit()

        print(
            f"\n{total_noticias} noticias creadas | {total_sugerencias} sugerencias a clientes"
            + (f" | {saltadas} saltadas" if saltadas else "")
            + (" (DRY RUN, nada se guardo)" if dry_run else "")
        )


def _fecha(valor):
    """Rechaza fechas sin zona horaria: un naive contra timestamptz se
    interpreta en la TZ de la sesion de Postgres, no en la del operador
    (mismo criterio que informe_cliente.py y segmentar_ventana_sync.py)."""
    if not valor:
        return None
    dt = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise SystemExit(
            f"fecha {valor!r} sin zona horaria -- usa Z u offset explicito "
            f"(ej. {valor}Z para UTC, {valor}-06:00 para hora de Honduras)"
        )
    return dt


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=100000)
    p.add_argument("--fecha-desde", type=str, default=None, help="ISO 8601 UTC")
    p.add_argument("--fecha-hasta", type=str, default=None, help="ISO 8601 UTC, exclusivo")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--con-clips", action="store_true",
                   help="baja el audio, corta con ffmpeg y sube el clip a S3")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--de", type=int, default=1, help="numero total de shards")
    a = p.parse_args()
    procesar(a.limit, _fecha(a.fecha_desde), _fecha(a.fecha_hasta), a.dry_run,
             con_clips=a.con_clips, shard=a.shard, de=a.de)


if __name__ == "__main__":
    main()
