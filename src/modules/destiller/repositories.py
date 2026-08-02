"""Cola de validacion de Destiller: reparte bloques entre validadores y guarda
sus veredictos. Ver src/modules/destiller/models.py para el porque del lease.
"""
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from src.modules.destiller.models import BloqueValidacion, TipoBloqueDB, VeredictoValidacion

# Cuanto dura la asignacion de un bloque antes de volver a la cola. Generoso a
# proposito: validar un bloque implica escuchar audio, y una tanda larga puede
# tomar rato. Si fuera corto, dos personas terminarian sobre el mismo bloque.
LEASE = timedelta(minutes=45)


class ValidacionRepository:
    def __init__(self, session: Session):
        self._session = session

    def _vencido(self) -> datetime:
        return datetime.now(timezone.utc) - LEASE

    def siguiente_tanda(
        self, validador: str, modelo: str, limite: int = 40
    ) -> list[BloqueValidacion]:
        """Entrega bloques sin validar y los reserva para este validador.

        `SKIP LOCKED` evita que dos requests concurrentes peleen por las mismas
        filas: el segundo salta las que el primero esta tomando en vez de
        esperarlo. Sin esto, dos validadores entrando al mismo tiempo se
        bloquean mutuamente o -- peor -- reciben la misma tanda.
        """
        ya_opino = select(VeredictoValidacion.bloque_id).where(
            VeredictoValidacion.validador == validador
        )
        libre = (BloqueValidacion.asignado_a.is_(None)) | (
            BloqueValidacion.asignado_en < self._vencido()
        )

        stmt = (
            select(BloqueValidacion)
            .where(BloqueValidacion.modelo == modelo)
            .where(BloqueValidacion.id.notin_(ya_opino))
            .where(libre | (BloqueValidacion.asignado_a == validador))
            .order_by(BloqueValidacion.grabacion_ref, BloqueValidacion.bloque_idx)
            .limit(limite)
            .with_for_update(skip_locked=True)
        )
        bloques = list(self._session.scalars(stmt))

        ahora = datetime.now(timezone.utc)
        for b in bloques:
            b.asignado_a = validador
            b.asignado_en = ahora
        self._session.commit()
        return bloques

    def registrar(
        self, bloque_id: uuid.UUID, validador: str, ok: bool, tipo_real: str | None
    ) -> bool:
        """Guarda el veredicto. Devuelve False si el bloque no existe.

        Es idempotente por (bloque, validador): reenviar el mismo veredicto --
        un doble click, un reintento de red -- actualiza en vez de reventar
        contra la constraint.
        """
        bloque = self._session.get(BloqueValidacion, bloque_id)
        if bloque is None:
            return False

        # Corregir al mismo tipo que dijo el modelo es confirmarlo, no
        # corregirlo. Misma regla que aplica el front; se repite aca porque la
        # API es la que tiene que garantizar que no entre un veredicto
        # contradictorio (tipo_llm == tipo_real con ok=False), venga de donde venga.
        if not ok and tipo_real is not None and tipo_real == bloque.tipo_llm.value:
            ok, tipo_real = True, None

        valores = {
            "bloque_id": bloque_id,
            "validador": validador,
            "ok": ok,
            "tipo_llm": bloque.tipo_llm,
            "tipo_real": TipoBloqueDB(tipo_real) if (not ok and tipo_real) else None,
        }
        stmt = insert(VeredictoValidacion).values(**valores)
        self._session.execute(
            stmt.on_conflict_do_update(
                constraint="uq_destiller_veredicto",
                set_={"ok": stmt.excluded.ok, "tipo_real": stmt.excluded.tipo_real},
            )
        )
        # Se libera para que, si otro validador lo quiere para medir acuerdo,
        # pueda tomarlo -- el filtro de "ya opine" es por validador, no global.
        bloque.asignado_a = None
        bloque.asignado_en = None
        self._session.commit()
        return True

    def progreso(self, modelo: str) -> dict:
        total = self._session.scalar(
            select(func.count(BloqueValidacion.id)).where(BloqueValidacion.modelo == modelo)
        )
        validados = self._session.scalar(
            select(func.count(func.distinct(VeredictoValidacion.bloque_id)))
            .select_from(VeredictoValidacion)
            .join(BloqueValidacion, BloqueValidacion.id == VeredictoValidacion.bloque_id)
            .where(BloqueValidacion.modelo == modelo)
        )
        por_validador = self._session.execute(
            select(VeredictoValidacion.validador, func.count(VeredictoValidacion.id))
            .join(BloqueValidacion, BloqueValidacion.id == VeredictoValidacion.bloque_id)
            .where(BloqueValidacion.modelo == modelo)
            .group_by(VeredictoValidacion.validador)
            .order_by(func.count(VeredictoValidacion.id).desc())
        ).all()
        return {
            "total": total or 0,
            "validados": validados or 0,
            "por_validador": [{"validador": v, "n": n} for v, n in por_validador],
        }

    def modelos(self) -> list[str]:
        return list(
            self._session.scalars(
                select(BloqueValidacion.modelo).distinct().order_by(BloqueValidacion.modelo)
            )
        )
