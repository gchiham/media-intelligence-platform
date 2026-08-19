"""Prensa digital: fuentes_web + articulos, y tipo_medio 'digital'

La pata escrita del monitoreo. A diferencia de la captura de audio (push:
mediaCAP deja archivos en S3), la web es pull -- ningun feed hondureno anuncia
hub WebSub, asi que hay que pollear por cron.

fuentes_web guarda los validadores HTTP (etag / last_modified) para hacer GET
condicional: la mayoria de los medios contesta 304 sin cuerpo cuando no hay
nada nuevo, lo que hace que pollear cada 15 minutos cueste ~300 bytes por
fuente en vez de bajar el feed entero.

El unique (fuente_id, guid) de articulos es lo que hace idempotente al cron:
sin eso, ver los mismos 10 items 96 veces al dia genera 960 filas duplicadas
diarias por fuente.

Revision ID: c9a1f37e5d84
Revises: a7c3e91d4b02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9a1f37e5d84"
down_revision: Union[str, None] = "a7c3e91d4b02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PG 17 permite ADD VALUE dentro de la transaccion de alembic; lo que no
    # permite es usar el valor nuevo en esa misma transaccion. Por eso el alta
    # de los medios digitales va en scripts/seed_fuentes_prensa.py y no aca.
    op.execute("ALTER TYPE tipo_medio ADD VALUE IF NOT EXISTS 'digital'")

    sa.Enum("rss", "sitemap_news", name="tipo_fuente").create(op.get_bind(), checkfirst=True)
    # create_type=False es obligatorio: si se le pasa un sa.Enum a create_table,
    # SQLAlchemy emite su propio CREATE TYPE y choca con el de arriba
    # ("type tipo_fuente already exists"), aunque ese lleve checkfirst.
    tipo_fuente = postgresql.ENUM(
        "rss", "sitemap_news", name="tipo_fuente", create_type=False
    )

    op.create_table(
        "fuentes_web",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("medio_id", sa.UUID(as_uuid=True), sa.ForeignKey("medios.id"), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False, unique=True),
        sa.Column("tipo", tipo_fuente, nullable=False),
        sa.Column("activa", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("fecha_corte", sa.DateTime(timezone=True), nullable=True),
        sa.Column("etag", sa.String(length=255), nullable=True),
        sa.Column("last_modified", sa.String(length=255), nullable=True),
        sa.Column("ultimo_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ultimo_resultado", sa.String(length=255), nullable=True),
        sa.Column("errores_consecutivos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_fuentes_web_medio_id", "fuentes_web", ["medio_id"])
    # El cron arranca siempre con "dame las fuentes activas".
    op.create_index("ix_fuentes_web_activa", "fuentes_web", ["activa"])

    op.create_table(
        "articulos",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "fuente_id", sa.UUID(as_uuid=True), sa.ForeignKey("fuentes_web.id"), nullable=False
        ),
        sa.Column("guid", sa.String(length=512), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("resumen", sa.Text(), nullable=True),
        sa.Column("contenido_html", sa.Text(), nullable=True),
        sa.Column("autor", sa.String(length=255), nullable=True),
        sa.Column("publicado_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("fuente_id", "guid", name="uq_articulos_fuente_guid"),
    )
    op.create_index("ix_articulos_fuente_id", "articulos", ["fuente_id"])
    op.create_index("ix_articulos_publicado_at", "articulos", ["publicado_at"])


def downgrade() -> None:
    op.drop_index("ix_articulos_publicado_at", table_name="articulos")
    op.drop_index("ix_articulos_fuente_id", table_name="articulos")
    op.drop_table("articulos")
    op.drop_index("ix_fuentes_web_activa", table_name="fuentes_web")
    op.drop_index("ix_fuentes_web_medio_id", table_name="fuentes_web")
    op.drop_table("fuentes_web")
    sa.Enum(name="tipo_fuente").drop(op.get_bind(), checkfirst=True)
    # 'digital' se queda en tipo_medio: PostgreSQL no permite quitar un valor de
    # un enum, y dejarlo no rompe nada.
