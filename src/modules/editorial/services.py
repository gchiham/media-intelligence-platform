"""Dominio Editorial: cola FIFO (FR-050), bloqueo exclusivo por editor
(FR-051), edicion libre versionada (FR-054/FR-071/RN-003), aprobar/rechazar
en una sola accion (FR-056). La IA nunca aprueba ni publica (RN-001/RN-002) --
`approve()`/`reject()` solo los puede ejecutar el periodista que tiene la
noticia bloqueada a su nombre.

Cada metodo publico es una unidad transaccional completa: valida, muta,
commitea una sola vez -- o revierte todo y relanza la excepcion si algo falla
despues de empezar a escribir. Ver docs/EDITORIAL_DOMAIN.md.

No incluye (fuera de alcance de esta fase, ver docs/BACKEND_ARCHITECTURE.md):
endpoints, autenticacion/RBAC (aqui `editor_id` es solo un UUID que el
llamador provee), ni publicacion al cliente (ClienteNoticia).
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.modules.editorial.exceptions import (
    ColaVacia,
    NoticiaNoBloqueadaPorEditor,
    NoticiaNoEncontrada,
)
from src.modules.editorial.models import EstadoNoticia, Noticia, NoticiaVersion
from src.modules.editorial.repositories import NoticiaRepository, NoticiaVersionRepository


class NoticiaService:
    def __init__(
        self,
        session: Session,
        noticias: NoticiaRepository,
        versiones: NoticiaVersionRepository,
    ):
        self._session = session
        self._noticias = noticias
        self._versiones = versiones

    def start_review(self, editor_id: uuid.UUID) -> Noticia:
        """Toma la noticia PENDIENTE mas antigua de la cola, la asigna a
        `editor_id` y la pasa a EN_REVISION. Dequeue + asignacion son
        atomicos entre si (mismo commit) para que el bloqueo de FR-051 nunca
        quede a medio aplicar."""
        noticia = self._noticias.siguiente_pendiente_con_lock()
        if noticia is None:
            raise ColaVacia()

        noticia.estado = EstadoNoticia.EN_REVISION
        noticia.asignado_a = editor_id
        noticia.asignado_at = datetime.now(timezone.utc)
        self._session.commit()
        return noticia

    def save_draft(
        self,
        noticia_id: uuid.UUID,
        editor_id: uuid.UUID,
        *,
        titulo: str | None = None,
        resumen: str | None = None,
        transcripcion_texto: str | None = None,
        tema_id: uuid.UUID | None = None,
        subtema_id: uuid.UUID | None = None,
    ) -> NoticiaVersion:
        """Crea una NoticiaVersion nueva -- nunca sobrescribe la anterior
        (RN-003). Los campos no provistos heredan el valor de la version
        actual (edicion parcial: se puede mandar solo `titulo`, por ejemplo,
        sin repetir el resto)."""
        noticia = self._get_locked_by(noticia_id, editor_id)
        actual = self._versiones.get_by_id(noticia.version_actual_id)
        if actual is None:
            raise NoticiaNoEncontrada(noticia_id)

        try:
            nueva = NoticiaVersion(
                noticia_id=noticia.id,
                numero_version=actual.numero_version + 1,
                titulo=titulo if titulo is not None else actual.titulo,
                resumen=resumen if resumen is not None else actual.resumen,
                transcripcion_texto=(
                    transcripcion_texto if transcripcion_texto is not None else actual.transcripcion_texto
                ),
                tema_id=tema_id if tema_id is not None else actual.tema_id,
                subtema_id=subtema_id if subtema_id is not None else actual.subtema_id,
                ai_score=actual.ai_score,
                prioridad=actual.prioridad,
                confianza=actual.confianza,
                es_generada_por_ia=False,
                editado_por=editor_id,
            )
            self._versiones.add(nueva)
            self._session.flush()

            noticia.version_actual_id = nueva.id
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        return nueva

    def approve(self, noticia_id: uuid.UUID, editor_id: uuid.UUID) -> Noticia:
        """FR-056: una sola accion aprueba la noticia completa (no hay
        aprobacion por cliente separada). No publica al cliente todavia --
        eso es una fase posterior, fuera de alcance aqui."""
        return self._resolver(noticia_id, editor_id, EstadoNoticia.APROBADA)

    def reject(self, noticia_id: uuid.UUID, editor_id: uuid.UUID, motivo: str | None = None) -> Noticia:
        return self._resolver(noticia_id, editor_id, EstadoNoticia.RECHAZADA, motivo=motivo)

    def _resolver(
        self, noticia_id: uuid.UUID, editor_id: uuid.UUID, estado_final: EstadoNoticia, motivo: str | None = None,
    ) -> Noticia:
        noticia = self._get_locked_by(noticia_id, editor_id)
        try:
            noticia.estado = estado_final
            if estado_final == EstadoNoticia.RECHAZADA:
                noticia.motivo_rechazo = motivo
            # Se libera el bloqueo -- la revision de este editor ya termino.
            noticia.asignado_a = None
            noticia.asignado_at = None
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return noticia

    def _get_locked_by(self, noticia_id: uuid.UUID, editor_id: uuid.UUID) -> Noticia:
        noticia = self._noticias.get_by_id(noticia_id)
        if noticia is None:
            raise NoticiaNoEncontrada(noticia_id)
        if noticia.estado != EstadoNoticia.EN_REVISION or noticia.asignado_a != editor_id:
            raise NoticiaNoBloqueadaPorEditor(noticia_id, editor_id)
        return noticia
