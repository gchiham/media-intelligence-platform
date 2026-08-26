"""Metricas de avance del pipeline de audio, etapa por etapa, para el
dashboard de operacion (`GET /dash`).

Las cuatro etapas son las cuatro escrituras que el pipeline hace sobre una
misma Grabacion, en orden estricto -- cada una solo puede correr si la anterior
ya dejo su fila:

  1. **Grabaciones** -- `discover_grabaciones_coverage.py` (cron cada 5 min)
     crea la fila al detectar el archivo horario en el S3 de captura.
  2. **CHEPITA** -- la flota GPU transcribe y `consume_transcription_results.py`
     (cron cada minuto) guarda la `Transcripcion`.
  3. **Segmentacion LLM** -- `segment_backlog_batch_openai.py --submit/--collect`
     (cron cada 15/10 min, Batch API de OpenAI en dos cuentas) llena
     `segmentation_cache`.
  4. **Clipper** -- `process_cached_segments.py` (cron cada 10 min) corta el
     audio, crea las `Noticia` y marca `segmentation_cache.consumido`.

El eje de todas las consultas es `grabaciones.fecha_inicio` -- la hora que
salio al aire -- y no `created_at`: la pregunta que contesta el dashboard es
"hasta que hora del dia esta cubierta cada etapa", no "cuando corrio el cron".

Las consultas viven en el repositorio; `resumir()` es pura (recibe las filas ya
leidas) para poder probar el armado del payload sin base de datos.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.modules.ai.models import EstadoSegmentationBatch, SegmentationBatch, SegmentationCache
from src.modules.editorial.models import Noticia
from src.modules.media.models import Medio, Programa
from src.modules.recordings.models import EstadoGrabacion, Grabacion, Transcripcion

# Ventana por defecto del dashboard. 24 h cubre el dia de operacion completo sin
# arrastrar las 22k grabaciones historicas a una consulta que se repite cada minuto.
HORAS_DEFECTO = 24
HORAS_MAX = 24 * 14

# Las horas se presentan en horario de Honduras; el backend es UTC de punta a
# punta (mismo criterio que editorial.py y prensa.py).
GMT6 = timezone(timedelta(hours=-6))

# Un medio se marca atrasado cuando su ultima hora cubierta queda mas de esto por
# detras del medio mas adelantado. Con el descubrimiento cada 5 min y la Batch API
# de OpenAI en el camino, 2 h de diferencia es ruido normal; mas que eso ya es una
# etapa trabada o una fuente que dejo de publicar.
TOLERANCIA_ATRASO_HORAS = 2

# Un batch de OpenAI que lleva mas de esto en ENVIADO no va a llegar a tiempo para
# el informe del dia. La Batch API promete 24 h, pero en operacion normal estos
# batches cierran en 10-40 min (ver docs/OPENAI_BATCH_DOS_CUENTAS.md).
BATCH_LENTO_HORAS = 2

# (clave en las filas, nombre visible, icono Lucide). El orden es el del pipeline
# y es el que usa el embudo del dashboard.
ETAPAS = (
    ("grabaciones", "Grabaciones", "radio"),
    ("transcritas", "CHEPITA · Transcripción", "audio-lines"),
    ("segmentadas", "Segmentación LLM · OpenAI", "scissors"),
    ("clipeadas", "Clipper", "film"),
)


def inicio_ventana(horas: int, ahora: datetime | None = None) -> datetime:
    """Arranque de la ventana, alineado a la hora en punto -- las grabaciones son
    horarias, asi que un `desde` a media hora partiria el primer balde."""
    ahora = ahora or datetime.now(timezone.utc)
    return (ahora - timedelta(hours=horas)).replace(minute=0, second=0, microsecond=0)


class MetricasPipelineRepository:
    """Solo lecturas agregadas. No hereda de `Repository[ModelT]` a proposito: no
    hay un unico modelo detras, cada consulta cruza las cuatro tablas del pipeline."""

    def __init__(self, session: Session):
        self._session = session

    def por_hora(self, desde: datetime) -> list[dict]:
        """Un balde por hora de emision con el conteo de cada etapa.

        `transcripciones` y `segmentation_cache` son unique por `grabacion_id`, asi
        que los LEFT JOIN no multiplican filas y `count(col)` cuenta grabaciones, no
        coincidencias. `noticias` SI es 1:N -- por eso va aparte, en
        `_noticias_por_hora()`.
        """
        hora = func.date_trunc("hour", Grabacion.fecha_inicio).label("hora")
        stmt = (
            select(
                hora,
                func.count().label("grabaciones"),
                func.count(Transcripcion.grabacion_id).label("transcritas"),
                func.count(SegmentationCache.grabacion_id).label("segmentadas"),
                func.count(SegmentationCache.grabacion_id)
                .filter(SegmentationCache.consumido.is_(True))
                .label("clipeadas"),
                func.count(Grabacion.id)
                .filter(Grabacion.estado == EstadoGrabacion.ERROR)
                .label("errores"),
            )
            .select_from(Grabacion)
            .outerjoin(Transcripcion, Transcripcion.grabacion_id == Grabacion.id)
            .outerjoin(SegmentationCache, SegmentationCache.grabacion_id == Grabacion.id)
            .where(Grabacion.fecha_inicio >= desde)
            .group_by(hora)
            .order_by(hora)
        )
        noticias = self._noticias_por_hora(desde)
        filas = []
        for fila in self._session.execute(stmt):
            n_noticias, n_clips = noticias.get(fila.hora, (0, 0))
            filas.append({
                "hora": fila.hora,
                "grabaciones": fila.grabaciones,
                "transcritas": fila.transcritas,
                "segmentadas": fila.segmentadas,
                "clipeadas": fila.clipeadas,
                "errores": fila.errores,
                "noticias": n_noticias,
                "clips": n_clips,
            })
        return filas

    def _noticias_por_hora(self, desde: datetime) -> dict[datetime, tuple[int, int]]:
        """Noticias creadas por el Clipper, por hora de emision de su grabacion.
        `clip_s3_uri` NULL es valido (noticia sin audio cortado, ver el docstring de
        `scripts/process_cached_segments.py`), asi que van las dos cifras."""
        hora = func.date_trunc("hour", Grabacion.fecha_inicio).label("hora")
        stmt = (
            select(
                hora,
                func.count().label("noticias"),
                func.count(Noticia.clip_s3_uri).label("clips"),
            )
            .select_from(Noticia)
            .join(Grabacion, Grabacion.id == Noticia.grabacion_id)
            .where(Grabacion.fecha_inicio >= desde)
            .group_by(hora)
        )
        return {f.hora: (f.noticias, f.clips) for f in self._session.execute(stmt)}

    def por_medio(self, desde: datetime) -> list[dict]:
        """Hasta que hora llego cada etapa en cada medio, y cuanto le falta.

        Es la vista que contesta "que se atoro y donde": si `ultima_grabacion` va
        tan atrasada como `ultima_clipeada`, no falta procesar nada -- lo que falta
        es la captura aguas arriba.
        """
        stmt = (
            select(
                Medio.codigo,
                Medio.nombre,
                Medio.tipo,
                func.max(Grabacion.fecha_inicio).label("ultima_grabacion"),
                func.max(Grabacion.fecha_inicio)
                .filter(Transcripcion.grabacion_id.isnot(None))
                .label("ultima_transcrita"),
                func.max(Grabacion.fecha_inicio)
                .filter(SegmentationCache.grabacion_id.isnot(None))
                .label("ultima_segmentada"),
                func.max(Grabacion.fecha_inicio)
                .filter(SegmentationCache.consumido.is_(True))
                .label("ultima_clipeada"),
                func.count().label("grabaciones"),
                func.count(Transcripcion.grabacion_id).label("transcritas"),
                func.count(SegmentationCache.grabacion_id).label("segmentadas"),
                func.count(SegmentationCache.grabacion_id)
                .filter(SegmentationCache.consumido.is_(True))
                .label("clipeadas"),
            )
            .select_from(Grabacion)
            .join(Programa, Programa.id == Grabacion.programa_id)
            .join(Medio, Medio.id == Programa.medio_id)
            .outerjoin(Transcripcion, Transcripcion.grabacion_id == Grabacion.id)
            .outerjoin(SegmentationCache, SegmentationCache.grabacion_id == Grabacion.id)
            .where(Grabacion.fecha_inicio >= desde)
            .group_by(Medio.codigo, Medio.nombre, Medio.tipo)
            .order_by(Medio.codigo)
        )
        return [
            {
                "codigo": f.codigo,
                "nombre": f.nombre,
                "tipo": getattr(f.tipo, "value", f.tipo),
                "ultima_grabacion": f.ultima_grabacion,
                "ultima_transcrita": f.ultima_transcrita,
                "ultima_segmentada": f.ultima_segmentada,
                "ultima_clipeada": f.ultima_clipeada,
                "grabaciones": f.grabaciones,
                "transcritas": f.transcritas,
                "segmentadas": f.segmentadas,
                "clipeadas": f.clipeadas,
            }
            for f in self._session.execute(stmt)
        ]

    def batches_en_vuelo(self) -> list[dict]:
        """Batches de OpenAI todavia en ENVIADO, por cuenta. Es el unico trabajo del
        pipeline que no se ve en las tablas de arriba: ya salio del backlog pero
        todavia no escribio `segmentation_cache`."""
        stmt = (
            select(
                SegmentationBatch.cuenta,
                func.count().label("batches"),
                func.sum(SegmentationBatch.total_requests).label("requests"),
                func.min(SegmentationBatch.created_at).label("mas_viejo"),
            )
            .where(SegmentationBatch.estado == EstadoSegmentationBatch.ENVIADO)
            .group_by(SegmentationBatch.cuenta)
            .order_by(SegmentationBatch.cuenta)
        )
        return [
            {
                "cuenta": f.cuenta or "1",
                "batches": f.batches,
                "requests": int(f.requests or 0),
                "mas_viejo": f.mas_viejo,
            }
            for f in self._session.execute(stmt)
        ]


def _iso(valor: datetime | None) -> str | None:
    return valor.astimezone(timezone.utc).isoformat() if valor else None


def _maximo(filas: list[dict], clave: str) -> datetime | None:
    valores = [f[clave] for f in filas if f.get(clave)]
    return max(valores) if valores else None


def resumir(
    por_hora: list[dict],
    por_medio: list[dict],
    batches: list[dict],
    horas: int,
    ahora: datetime | None = None,
) -> dict:
    """Arma el payload que consume la pagina. Funcion pura: todo lo que necesita
    ya viene leido, asi que se prueba sin base de datos."""
    ahora = ahora or datetime.now(timezone.utc)
    totales = {
        clave: sum(f[clave] for f in por_hora)
        for clave in ("grabaciones", "transcritas", "segmentadas", "clipeadas", "noticias", "clips", "errores")
    }
    base = totales["grabaciones"]

    # El embudo: cada etapa contra el total de grabaciones de la ventana, y lo que
    # le falta contra la etapa inmediatamente anterior (que es su unica entrada).
    etapas = []
    previo = base
    ultimas = {
        "grabaciones": "ultima_grabacion",
        "transcritas": "ultima_transcrita",
        "segmentadas": "ultima_segmentada",
        "clipeadas": "ultima_clipeada",
    }
    for clave, nombre, icono in ETAPAS:
        hecho = totales[clave]
        etapas.append({
            "clave": clave,
            "nombre": nombre,
            "icono": icono,
            "total": hecho,
            "pct": round(100 * hecho / base, 1) if base else 0.0,
            "pendientes": max(previo - hecho, 0),
            "ultima_hora": _iso(_maximo(por_medio, ultimas[clave])),
        })
        previo = hecho

    # Un medio esta atrasado respecto del mas adelantado de todos, no respecto del
    # reloj: a las 09:50 lo normal es que la ultima hora cerrada sea la de las 08:00.
    frente = _maximo(por_medio, "ultima_grabacion")
    limite = frente - timedelta(hours=TOLERANCIA_ATRASO_HORAS) if frente else None

    medios = []
    for m in por_medio:
        atrasado = bool(limite and (m["ultima_grabacion"] is None or m["ultima_grabacion"] < limite))
        medios.append({
            **{k: m[k] for k in ("codigo", "nombre", "tipo", "grabaciones", "transcritas", "segmentadas", "clipeadas")},
            "ultima_grabacion": _iso(m["ultima_grabacion"]),
            "ultima_transcrita": _iso(m["ultima_transcrita"]),
            "ultima_segmentada": _iso(m["ultima_segmentada"]),
            "ultima_clipeada": _iso(m["ultima_clipeada"]),
            "atrasado": atrasado,
            # Sin grabaciones nuevas no hay nada que procesar: distinguirlo evita
            # leer un corte de captura como si fuera el pipeline trabado.
            "sin_captura": atrasado and m["ultima_grabacion"] == m["ultima_clipeada"],
        })

    alertas = []
    atrasados = [m["codigo"] for m in medios if m["atrasado"]]
    if atrasados:
        alertas.append({
            "nivel": "warning",
            "texto": f"{len(atrasados)} medio(s) sin cobertura reciente: {', '.join(atrasados)}",
        })
    if totales["errores"]:
        alertas.append({
            "nivel": "danger",
            "texto": f"{totales['errores']} grabacion(es) en estado error en la ventana",
        })
    for b in batches:
        if b["mas_viejo"] and ahora - b["mas_viejo"] > timedelta(hours=BATCH_LENTO_HORAS):
            horas_batch = (ahora - b["mas_viejo"]).total_seconds() / 3600
            alertas.append({
                "nivel": "warning",
                "texto": f"Cuenta {b['cuenta']}: batch de OpenAI enviado hace {horas_batch:.1f} h sin cerrar",
            })

    return {
        "generado_at": _iso(ahora),
        "ventana_horas": horas,
        "totales": totales,
        "etapas": etapas,
        "por_hora": [{**f, "hora": _iso(f["hora"])} for f in por_hora],
        "por_medio": medios,
        "batches": [{**b, "mas_viejo": _iso(b["mas_viejo"])} for b in batches],
        "alertas": alertas,
    }
