"""Portal de validacion de Destiller: bloques y veredictos.

Ver src/modules/destiller/models.py. Tablas de evaluacion, no del pipeline de
produccion -- guardan el set de 20 grabaciones destiladas y el juicio humano
sobre cada bloque, que es de donde sale el umbral de confianza de Destiller.

Revision ID: c4d81ba90e17
Revises: b8e4f21a7c30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM as PGENUM
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "c4d81ba90e17"
down_revision: Union[str, None] = "b8e4f21a7c30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TIPOS = ("noticia", "publicidad", "religioso", "promo", "musica", "relleno", "desconocido")


def upgrade() -> None:
    # El tipo se crea una sola vez, explicitamente. Las columnas lo referencian
    # con `create_type=False`: sin eso, cada `create_table` que lo menciona
    # emite su propio CREATE TYPE y la segunda tabla falla con DuplicateObject
    # -- y como el enum queda commiteado, el reintento del entrypoint vuelve a
    # fallar igual. `checkfirst` cubre justamente ese caso: reejecutar despues
    # de un intento a medias no revienta.
    PGENUM(*_TIPOS, name="tipo_bloque_destiller").create(op.get_bind(), checkfirst=True)
    tipo = PGENUM(*_TIPOS, name="tipo_bloque_destiller", create_type=False)

    op.create_table(
        "destiller_bloques",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("modelo", sa.String(120), nullable=False),
        sa.Column("grabacion_ref", sa.String(200), nullable=False),
        sa.Column("medio", sa.String(80), nullable=False),
        sa.Column("fecha", sa.String(10), nullable=False),
        sa.Column("hora_local", sa.Integer(), nullable=False),
        sa.Column("audio_key", sa.String(1024), nullable=True),
        sa.Column("bloque_idx", sa.Integer(), nullable=False),
        sa.Column("start_word", sa.Integer(), nullable=False),
        sa.Column("end_word", sa.Integer(), nullable=False),
        sa.Column("inicio_seg", sa.Float(), nullable=False),
        sa.Column("fin_seg", sa.Float(), nullable=False),
        sa.Column("tipo_llm", tipo, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("motivo", sa.String(500), nullable=False, server_default=""),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("asignado_a", sa.String(120), nullable=True),
        sa.Column("asignado_en", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("modelo", "grabacion_ref", "bloque_idx", name="uq_destiller_bloque"),
    )
    op.create_index("ix_destiller_bloques_modelo", "destiller_bloques", ["modelo"])
    op.create_index("ix_destiller_bloques_grabacion_ref", "destiller_bloques", ["grabacion_ref"])
    op.create_index("ix_destiller_bloques_medio", "destiller_bloques", ["medio"])
    op.create_index("ix_destiller_bloques_tipo_llm", "destiller_bloques", ["tipo_llm"])
    op.create_index("ix_destiller_bloques_asignado_a", "destiller_bloques", ["asignado_a"])

    op.create_table(
        "destiller_veredictos",
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "bloque_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("destiller_bloques.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("validador", sa.String(120), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("tipo_llm", tipo, nullable=False),
        sa.Column("tipo_real", tipo, nullable=True),
        sa.UniqueConstraint("bloque_id", "validador", name="uq_destiller_veredicto"),
    )
    op.create_index("ix_destiller_veredictos_bloque_id", "destiller_veredictos", ["bloque_id"])
    op.create_index("ix_destiller_veredictos_validador", "destiller_veredictos", ["validador"])


def downgrade() -> None:
    op.drop_table("destiller_veredictos")
    op.drop_table("destiller_bloques")
    PGENUM(name="tipo_bloque_destiller").drop(op.get_bind(), checkfirst=True)
