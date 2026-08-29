"""Prensa impresa: ediciones, notas_impresas y nota_traducciones

Los periodicos en PDF que publica el canal de Telegram. NO se reusa la tabla
`articulos` (RSS/sitemap): un articulo web y una nota de periodico son cosas
distintas, y meterlas juntas obligaba a dejar en nullable la mitad de las
columnas y a un "exactamente uno de fuente_id o edicion_id". Los reportes que
quieran ver ambas juntas hacen la union en la consulta.

Tampoco se agrega 'impreso' a tipo_medio: La Tribuna es diario impreso Y sitio
web, y un enum de un valor obligaria a elegir. Las ediciones cuelgan del Medio
que ya existe, igual que los feeds RSS de HCH cuelgan de su Medio de tv.

Revision ID: 19f7475e155d
Revises: 2348e76eb9b6
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "19f7475e155d"
down_revision: Union[str, None] = "2348e76eb9b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ediciones",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("medio_id", sa.UUID(as_uuid=True), sa.ForeignKey("medios.id"), nullable=False),
        sa.Column("fecha_edicion", sa.Date(), nullable=False),
        # Dedup por contenido: 2 de 68 PDFs de agosto 2026 eran byte-identicos
        # a otro (uno cruzaba de dia). Sin esto se paga la extraccion dos veces.
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("s3_pdf_key", sa.String(length=1024), nullable=False),
        sa.Column("paginas_total", sa.Integer(), nullable=False),
        sa.Column("paginas_sin_texto", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("origen_url", sa.String(length=1024), nullable=True),
        sa.Column("modelo", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("extraido_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tokens_entrada", sa.Integer(), nullable=True),
        sa.Column("tokens_salida", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("sha256", name="uq_ediciones_sha256"),
    )
    op.create_index("ix_ediciones_medio_id", "ediciones", ["medio_id"])
    # El portal lista por dia; el reproceso busca por rango de fechas.
    op.create_index("ix_ediciones_fecha_edicion", "ediciones", ["fecha_edicion"])

    op.create_table(
        "notas_impresas",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "edicion_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ediciones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("indice", sa.Integer(), nullable=False),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("sumario", sa.Text(), nullable=True),
        sa.Column("cuerpo", sa.Text(), nullable=False),
        sa.Column("seccion", sa.String(length=120), nullable=True),
        # Array y no entero: una nota que dice "pasa a la pag. 12" vive en dos
        # paginas y el front tiene que poder abrir las dos.
        sa.Column("paginas", postgresql.ARRAY(sa.Integer()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("edicion_id", "indice", name="uq_notas_impresas_edicion_indice"),
    )
    op.create_index("ix_notas_impresas_edicion_id", "notas_impresas", ["edicion_id"])

    op.create_table(
        "nota_traducciones",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "nota_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("notas_impresas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idioma", sa.String(length=8), nullable=False),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("sumario", sa.Text(), nullable=True),
        sa.Column("cuerpo", sa.Text(), nullable=True),
        sa.Column("modelo", sa.String(length=64), nullable=True),
        sa.Column("traducido_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("nota_id", "idioma", name="uq_nota_traduccion_idioma"),
    )
    op.create_index("ix_nota_traducciones_nota_id", "nota_traducciones", ["nota_id"])


def downgrade() -> None:
    op.drop_index("ix_nota_traducciones_nota_id", table_name="nota_traducciones")
    op.drop_table("nota_traducciones")
    op.drop_index("ix_notas_impresas_edicion_id", table_name="notas_impresas")
    op.drop_table("notas_impresas")
    op.drop_index("ix_ediciones_fecha_edicion", table_name="ediciones")
    op.drop_index("ix_ediciones_medio_id", table_name="ediciones")
    op.drop_table("ediciones")
