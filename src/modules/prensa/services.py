"""Ingesta de prensa digital. Una pasada por invocacion, sin loop infinito --
igual que consume_transcription_results.py: el que decide el ritmo es el cron.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.modules.media.models import Medio
from src.modules.prensa import feeds
from src.modules.prensa.models import Articulo, FuenteWeb


@dataclass
class ResultadoFuente:
    medio: str
    url: str
    estado: str  # ok | sin_cambios | error
    nuevos: int = 0
    repetidos: int = 0
    previos_al_corte: int = 0
    detalle: str | None = None

    def resumen(self) -> str:
        if self.estado == "error":
            return f"error: {self.detalle}"
        if self.estado == "sin_cambios":
            return "sin_cambios"
        return f"ok: {self.nuevos} nuevos"


class IngestaPrensaService:
    """Cada fuente es su propia unidad transaccional: una que falle no puede
    tumbar la pasada de las otras 20 ni dejar a medias lo ya insertado."""

    def __init__(self, session: Session, descargar=feeds.descargar):
        self._session = session
        self._descargar = descargar

    def fuentes_activas(self, codigos_medio: list[str] | None = None) -> list[FuenteWeb]:
        stmt = select(FuenteWeb).join(Medio).where(FuenteWeb.activa.is_(True))
        if codigos_medio:
            stmt = stmt.where(Medio.codigo.in_(codigos_medio))
        return list(self._session.scalars(stmt.order_by(Medio.codigo)))

    def procesar_todas(self, codigos_medio: list[str] | None = None) -> list[ResultadoFuente]:
        return [self.procesar(f) for f in self.fuentes_activas(codigos_medio)]

    def procesar(self, fuente: FuenteWeb) -> ResultadoFuente:
        medio = self._session.get(Medio, fuente.medio_id)
        resultado = ResultadoFuente(medio=medio.codigo, url=fuente.url, estado="ok")
        try:
            descarga = self._descargar(fuente.url, fuente.etag, fuente.last_modified)
            if not descarga.modificada:
                resultado.estado = "sin_cambios"
            else:
                items = feeds.PARSERS[fuente.tipo.value](descarga.cuerpo)
                self._guardar(fuente, items, resultado)
                # Recien se persisten los validadores despues de guardar: si el
                # insert falla, la proxima pasada tiene que volver a bajar el
                # cuerpo entero, no comerse un 304 con los articulos perdidos.
                fuente.etag = descarga.etag
                fuente.last_modified = descarga.last_modified
            fuente.errores_consecutivos = 0
        except (feeds.ErrorDescarga, feeds.FormatoNoSoportado) as e:
            self._session.rollback()
            # Recargar: el rollback dejo la instancia expirada.
            fuente = self._session.get(FuenteWeb, fuente.id)
            resultado.estado = "error"
            resultado.detalle = str(e)
            fuente.errores_consecutivos += 1

        fuente.ultimo_poll_at = datetime.now(timezone.utc)
        fuente.ultimo_resultado = resultado.resumen()[:255]
        self._session.commit()
        return resultado

    def _guardar(self, fuente: FuenteWeb, items, resultado: ResultadoFuente) -> None:
        filas = []
        vistos = set()
        for item in items:
            # Un feed puede repetir un guid dentro de la misma respuesta; hay que
            # colapsarlo antes del INSERT para que el conteo de nuevos no mienta.
            if item.guid in vistos:
                resultado.repetidos += 1
                continue
            vistos.add(item.guid)
            if fuente.fecha_corte and item.publicado_at < fuente.fecha_corte:
                resultado.previos_al_corte += 1
                continue
            filas.append(
                {
                    "fuente_id": fuente.id,
                    "guid": item.guid,
                    "url": item.url,
                    "titulo": item.titulo,
                    "resumen": item.resumen,
                    "contenido_html": item.contenido_html,
                    "autor": item.autor[:255] if item.autor else None,
                    "publicado_at": item.publicado_at,
                }
            )
        if not filas:
            return

        # ON CONFLICT DO NOTHING contra uq_articulos_fuente_guid: el dedup lo
        # resuelve la base en una sola query, no un SELECT previo por item (que
        # ademas tendria carrera si dos pasadas se solapan).
        stmt = (
            pg_insert(Articulo)
            .values(filas)
            .on_conflict_do_nothing(constraint="uq_articulos_fuente_guid")
            .returning(Articulo.id)
        )
        insertados = len(self._session.execute(stmt).fetchall())
        resultado.nuevos = insertados
        resultado.repetidos += len(filas) - insertados
