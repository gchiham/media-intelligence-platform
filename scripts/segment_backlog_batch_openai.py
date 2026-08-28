"""Segmenta el backlog usando la Batch API de OpenAI (50% mas barato) --
mismo diseño que scripts/segment_backlog_batch.py (Anthropic), ver
src/modules/ai/openai_batch.py para el detalle del formato.

Reusa la tabla `segmentation_batches` tal cual existe hoy en produccion (sin
migracion nueva): el batch id de OpenAI se guarda en la columna
`anthropic_batch_id` pese al nombre -- es solo un string, y las dos APIs no
comparten formato pero tampoco chocan (`batch_...` de OpenAI vs `msgbatch_...`
de Anthropic). Ver la nota en docs/ o preguntar antes de "corregir" el nombre
de la columna con una migracion: la DB de prod tiene una migracion pendiente
sin correr (b8e4f21a7c30) y no es el momento de sumar otra.

Este script NO genera Noticias -- solo llena `segmentation_cache`. El
clipping y la persistencia los sigue haciendo el pipeline normal leyendo esa
cache (ver scripts/process_cached_segments.py).

Uso:
    python scripts/segment_backlog_batch_openai.py --submit --limit 200
    python scripts/segment_backlog_batch_openai.py --status
    python scripts/segment_backlog_batch_openai.py --collect
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import OpenAI, OpenAIError  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.ai.models import (  # noqa: E402
    EstadoSegmentationBatch,
    SegmentationBatch,
    SegmentationCache,
)
from src.modules.ai.openai_batch import (  # noqa: E402
    BatchRechazado,
    OpenAIBatchSegmentationClient,
    build_chunk_requests,
)
from src.modules.ai.batch import build_custom_id  # noqa: E402
from src.modules.ai.client_profiles import cargar_perfiles  # noqa: E402
from src.modules.ai.repeated_content import RepeatedContentIndex  # noqa: E402
from src.modules.ai.schemas import Word  # noqa: E402
from src.modules.recordings.models import EstadoGrabacion, Grabacion, Transcripcion  # noqa: E402


def _pendientes(
    session: Session,
    limit: int,
    fecha_desde: datetime | None = None,
    fecha_hasta: datetime | None = None,
    offset: int = 0,
) -> list[tuple[Grabacion, Transcripcion]]:
    """Grabaciones transcritas que todavia no tienen segmentos cacheados.

    Sin `--fecha-desde`/`--fecha-hasta` toma las mas recientes primero, igual
    que segment_backlog_batch.py (Anthropic) -- pero el backlog global de
    segmentacion es mucho mas grande que cualquier ventana puntual (se vio en
    la practica: 19,806 grabaciones sin segmentar contra 1,331 de un rango de
    3 dias), asi que sin fecha el orden `desc()` se come backlog nuevo antes
    de tocar el rango que se quiso procesar. Los filtros de fecha son en UTC,
    igual que enqueue_transcriptions.py -- convertir desde hora local antes de
    llamar."""
    ya_cacheadas = select(SegmentationCache.grabacion_id)
    stmt = (
        select(Grabacion, Transcripcion)
        .join(Transcripcion, Transcripcion.grabacion_id == Grabacion.id)
        .where(Grabacion.estado == EstadoGrabacion.PROCESADA)
        .where(Grabacion.id.notin_(ya_cacheadas))
    )
    if fecha_desde is not None:
        stmt = stmt.where(Grabacion.fecha_inicio >= fecha_desde)
    if fecha_hasta is not None:
        stmt = stmt.where(Grabacion.fecha_inicio < fecha_hasta)
    # `offset` existe para poder tener dos batches en vuelo a la vez (una por
    # key/organizacion, que es donde aplica el limite de tokens encolados de
    # OpenAI). Sin esto las dos tandas eligen las MISMAS grabaciones: el filtro
    # de "ya cacheadas" solo ve lo que se recolecto, y al enviar todavia no hay
    # nada recolectado. Paso en produccion: de 950 requests de la segunda tanda
    # solo 5 grabaciones eran nuevas, el resto trabajo duplicado ya pagado.
    stmt = stmt.order_by(Grabacion.fecha_inicio.desc()).offset(offset).limit(limit)
    return list(session.execute(stmt).all())


def _words(transcripcion: Transcripcion) -> list[Word]:
    return [Word(**w) for w in (transcripcion.segmentos or {}).get("words", [])]


def _parse_fecha(valor: str | None) -> datetime | None:
    if valor is None:
        return None
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


def _key(cuenta: str) -> str | None:
    """Devuelve la key de la cuenta pedida ("1" o "2"), o None si no existe.

    Son organizaciones distintas de OpenAI, no dos keys de la misma: cada una
    tiene su propia cuota de tokens encolados, que es lo que permite mandar
    dos batches en paralelo y duplicar el throughput. La contra es que un
    batch abierto con una cuenta es INVISIBLE para la otra (`retrieve` falla),
    por eso `segmentation_batches.cuenta` registra con cual salio cada uno.
    """
    if cuenta == "1":
        return settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if cuenta == "2":
        env = os.environ.get("OPENAI_API_KEY_2")
        if env:
            return env
        # El contenedor no siempre trae KEY_2 en el entorno; el .env del repo si.
        ruta = Path(__file__).parent.parent / ".env"
        if ruta.exists():
            for linea in ruta.read_text(encoding="utf-8").splitlines():
                if linea.strip().startswith("OPENAI_API_KEY_2="):
                    return linea.split("=", 1)[1].strip()
    return None


def _cuentas_disponibles() -> list[str]:
    return [c for c in ("1", "2") if _key(c)]


def submit(
    limit: int,
    saltar_publicidad: bool,
    fecha_desde: datetime | None = None,
    fecha_hasta: datetime | None = None,
    offset: int = 0,
) -> None:
    """Arma los chunks pendientes y los reparte entre TODAS las cuentas de
    OpenAI disponibles, un batch por cuenta. Cada cuenta tiene su propia cuota
    de tokens encolados, asi que dos batches en paralelo avanzan al doble de
    velocidad que uno solo con el doble de chunks."""
    cuentas = _cuentas_disponibles()
    if not cuentas:
        raise SystemExit("no hay ninguna cuenta de OpenAI configurada")

    with Session(get_engine()) as session:
        filas = _pendientes(session, limit, fecha_desde, fecha_hasta, offset)
        if not filas:
            print("no hay grabaciones pendientes de segmentar")
            return

        indice = RepeatedContentIndex(session) if saltar_publicidad else None
        # Los perfiles viajan en el mensaje `user` de cada chunk: la relevancia
        # por cliente sale en la misma llamada que la segmentacion, sin una
        # segunda pasada ni una segunda ventana de batch. Si no hay ninguno
        # configurado, el prompt y el esquema son los de siempre.
        perfiles = cargar_perfiles(session)
        if perfiles:
            print(f"{len(perfiles)} perfiles de cliente: " + ", ".join(p.nombre for p in perfiles))

        peticiones = []
        saltados = 0
        for grabacion, transcripcion in filas:
            words = _words(transcripcion)
            if not words:
                continue
            for p in build_chunk_requests(
                str(grabacion.id), words, model=settings.openai_model, perfiles=perfiles
            ):
                # El filtro de publicidad se aplica aca, antes de pagar la
                # llamada -- es el unico punto donde el ahorro es real.
                if indice is not None:
                    chunk = words[p.chunk_index * 600 : (p.chunk_index + 1) * 600]
                    if indice.debe_saltarse(chunk):
                        saltados += 1
                        continue
                peticiones.append(p)

        if not peticiones:
            print(f"todo el contenido candidato quedo filtrado ({saltados} chunks de publicidad)")
            return

        # Reparto por GRABACION, no por chunk: los chunks de una misma
        # grabacion tienen que volver juntos para que el stitching pueda
        # fusionar las noticias partidas en el limite entre chunks contiguos.
        # Partirlos entre dos cuentas los haria volver en collects distintos y
        # las noticias del borde quedarian cortadas.
        por_cuenta: dict[str, list] = {c: [] for c in cuentas}
        grabaciones_vistas: list[str] = []
        for p in peticiones:
            if p.grabacion_id not in grabaciones_vistas:
                grabaciones_vistas.append(p.grabacion_id)
            idx = grabaciones_vistas.index(p.grabacion_id)
            por_cuenta[cuentas[idx % len(cuentas)]].append(p)

        enviados = []
        rechazados = []
        for cuenta in cuentas:
            grupo = por_cuenta[cuenta]
            if not grupo:
                continue
            client = OpenAIBatchSegmentationClient(OpenAI(api_key=_key(cuenta)))
            # Una cuenta bloqueada (sin creditos, tope de gasto) no puede
            # tumbar el batch de la otra: sin esto, la excepcion abortaba
            # antes del commit y se perdia tambien lo que si se envio.
            try:
                batch_id = client.submit(grupo)
            except (BatchRechazado, OpenAIError) as e:
                rechazados.append((cuenta, len(grupo), str(e)[:200]))
                continue
            session.add(
                SegmentationBatch(
                    anthropic_batch_id=batch_id,
                    estado=EstadoSegmentationBatch.ENVIADO,
                    modelo=settings.openai_model,
                    total_requests=len(grupo),
                    cuenta=cuenta,
                    rangos={
                        build_custom_id(p.grabacion_id, p.chunk_index): [p.lo, p.hi]
                        for p in grupo
                    },
                )
            )
            enviados.append((cuenta, batch_id, len(grupo)))
        session.commit()

    for cuenta, batch_id, n in enviados:
        print(f"batch enviado (cuenta {cuenta}): {batch_id} | {n} chunks")
    for cuenta, n, motivo in rechazados:
        print(f"ATENCION -- cuenta {cuenta} RECHAZO {n} chunks: {motivo}", file=sys.stderr)
    if rechazados and not enviados:
        # Ninguna cuenta acepto: que el cron lo note por codigo de salida, en
        # vez de dejar la segmentacion detenida en silencio.
        raise SystemExit(1)
    print(
        f"total: {len(peticiones)} chunks de {len(filas)} grabaciones "
        f"repartidos en {len(enviados)} batch(es)"
        + (f" | {saltados} chunks saltados por publicidad" if saltados else "")
    )


def _cliente_de(batch: SegmentationBatch) -> OpenAI | None:
    """El cliente de la cuenta con la que se mando ESE batch. Los batches
    viejos no tienen `cuenta` registrada: para ellos se prueba la 1 y despues
    la 2, que es el comportamiento que habia antes de la columna."""
    if batch.cuenta:
        key = _key(batch.cuenta)
        return OpenAI(api_key=key) if key else None
    for cuenta in _cuentas_disponibles():
        cliente = OpenAI(api_key=_key(cuenta))
        try:
            cliente.batches.retrieve(batch.anthropic_batch_id)
            return cliente
        except Exception:  # noqa: BLE001 -- invisible para esa cuenta, probar la otra
            continue
    return None


def status() -> None:
    with Session(get_engine()) as session:
        abiertos = session.scalars(
            select(SegmentationBatch).where(
                SegmentationBatch.estado == EstadoSegmentationBatch.ENVIADO
            )
        ).all()
        if not abiertos:
            print("no hay batches abiertos")
            return
        for batch in abiertos:
            cliente = _cliente_de(batch)
            if cliente is None:
                print(f"{batch.anthropic_batch_id}  (cuenta {batch.cuenta or '?'} no configurada)")
                continue
            remoto = cliente.batches.retrieve(batch.anthropic_batch_id)
            print(
                f"{batch.anthropic_batch_id}  cuenta={batch.cuenta or '?'}  {remoto.status:12} "
                f"requests={batch.total_requests}  counts={remoto.request_counts}"
            )


def collect() -> None:
    with Session(get_engine()) as session:
        abiertos = session.scalars(
            select(SegmentationBatch).where(
                SegmentationBatch.estado == EstadoSegmentationBatch.ENVIADO
            )
        ).all()

        for batch in abiertos:
            # Cada batch se recolecta con la cuenta que lo mando (registrada en
            # `cuenta`). Antes habia que correr --collect una vez por cuenta,
            # con env vars distintas, y un batch quedaba sin recolectar si
            # alguien se olvidaba de la segunda pasada.
            cliente = _cliente_de(batch)
            if cliente is None:
                print(f"{batch.anthropic_batch_id}: cuenta {batch.cuenta or '?'} no disponible, se omite")
                continue
            client = OpenAIBatchSegmentationClient(cliente)
            try:
                terminado = client.is_ended(batch.anthropic_batch_id)
            except Exception as exc:  # noqa: BLE001
                print(f"{batch.anthropic_batch_id}: no visible ({type(exc).__name__}), se omite")
                continue
            if not terminado:
                print(f"{batch.anthropic_batch_id}: todavia procesando, se omite")
                continue

            rangos = {k: (v[0], v[1]) for k, v in (batch.rangos or {}).items()}
            # Se valida contra los perfiles de AHORA, no contra los del envio:
            # si un cliente se dio de baja mientras el batch corria, sus
            # veredictos no deben entrar. Un cliente nuevo tampoco aparece,
            # porque no estaba en el prompt que se mando.
            client_ids = {p.client_id for p in cargar_perfiles(session)}
            resultados = client.collect(
                batch.anthropic_batch_id, rangos, client_ids_validos=client_ids or None
            )

            guardadas = 0
            for grabacion_id, segmentos in resultados.por_grabacion.items():
                # Grabacion con chunks fallidos: no cachear (ver comentario en
                # segment_backlog_batch.py -- mismo criterio para ambos caminos).
                if grabacion_id in resultados.grabaciones_con_error:
                    continue
                existente = session.scalar(
                    select(SegmentationCache).where(
                        SegmentationCache.grabacion_id == grabacion_id
                    )
                )
                if existente is not None:
                    continue
                session.add(
                    SegmentationCache(
                        grabacion_id=grabacion_id,
                        segmentos=[s.model_dump(mode="json") for s in segmentos],
                        modelo=batch.modelo,
                        batch_id=batch.id,
                    )
                )
                guardadas += 1

            batch.estado = EstadoSegmentationBatch.COMPLETADO
            if resultados.errores:
                batch.error_mensaje = "; ".join(resultados.errores[:20])
            session.commit()

            print(
                f"{batch.anthropic_batch_id}: {guardadas} grabaciones cacheadas, "
                f"{len(resultados.grabaciones_con_error)} con error se reintentaran, "
                f"{len(resultados.errores)} chunks con error, {resultados.expirados} expirados"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--limit", type=int, default=200, help="grabaciones por batch")
    parser.add_argument(
        "--sin-filtro-publicidad",
        action="store_true",
        help="manda todos los chunks, sin saltar contenido repetido conocido",
    )
    parser.add_argument(
        "--fecha-desde", type=str, default=None, help="ISO 8601 UTC, ej. 2026-08-08T06:00:00Z"
    )
    parser.add_argument(
        "--fecha-hasta", type=str, default=None, help="ISO 8601 UTC, exclusivo"
    )
    parser.add_argument(
        "--offset", type=int, default=0,
        help="salta las primeras N grabaciones; permite dos tandas en vuelo sin solaparse",
    )
    args = parser.parse_args()

    if args.submit:
        submit(
            args.limit,
            saltar_publicidad=not args.sin_filtro_publicidad,
            fecha_desde=_parse_fecha(args.fecha_desde),
            fecha_hasta=_parse_fecha(args.fecha_hasta),
            offset=args.offset,
        )
    elif args.status:
        status()
    elif args.collect:
        collect()
    else:
        parser.error("elegi --submit, --status o --collect")


if __name__ == "__main__":
    main()
