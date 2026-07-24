"""Historia (dedup entre emisoras), Batch API, resolucion de entidades y contenido repetido

Implementa los puntos 1-5 de docs/EFFICIENCY_REVIEW.md §7:

- `historias` + `noticias.historia_id`: agrupa apariciones del mismo evento en
  distintas emisoras (§2 del review -- se medio hasta 8 apariciones del mismo
  evento con titulos distintos).
- `segmentation_batches` / `segmentation_cache`: camino de Batch API al 50% de
  costo para el backlog (§4).
- `entidades.nombre_normalizado` / `alias` / `menciones` + unique: resolucion
  de menciones crudas contra el catalogo (§6).
- `contenido_repetido`: deteccion de publicidad repetida para no mandarla al
  LLM (§3).

Revision ID: b8e4f21a7c30
Revises: a7c3e91f4d5b
Create Date: 2026-07-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b8e4f21a7c30"
down_revision: Union[str, None] = "a7c3e91f4d5b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Historia: agrupacion semantica de apariciones -------------------
    op.create_table(
        "historias",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("titulo_canonico", sa.String(length=500), nullable=False),
        sa.Column("embedding", postgresql.JSONB(), nullable=False),
        sa.Column("primera_aparicion", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ultima_aparicion", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_apariciones", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("medios_distintos", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # La busqueda de candidatas siempre es "historias tocadas en las ultimas N
    # horas", nunca un scan completo -- de ahi el indice por ultima_aparicion.
    op.create_index("ix_historias_ultima_aparicion", "historias", ["ultima_aparicion"])

    op.add_column(
        "noticias",
        sa.Column("historia_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_noticias_historia_id", "noticias", ["historia_id"])
    op.create_foreign_key(
        "fk_noticias_historia_id", "noticias", "historias", ["historia_id"], ["id"]
    )

    # --- Batch API -------------------------------------------------------
    # create_type=False: el tipo se crea explicitamente en la linea de abajo.
    # Sin esto, op.create_table vuelve a emitir CREATE TYPE para la columna y
    # falla con DuplicateObject.
    estado_batch = postgresql.ENUM(
        "enviado", "completado", "error", name="estado_segmentation_batch", create_type=False
    )
    postgresql.ENUM(
        "enviado", "completado", "error", name="estado_segmentation_batch"
    ).create(op.get_bind(), checkfirst=True)

    op.create_table(
        "segmentation_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("anthropic_batch_id", sa.String(length=255), nullable=False, unique=True),
        sa.Column("estado", estado_batch, nullable=False, server_default="enviado"),
        sa.Column("modelo", sa.String(length=100), nullable=False),
        sa.Column("total_requests", sa.Integer(), nullable=False),
        sa.Column("rangos", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("error_mensaje", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_segmentation_batches_estado", "segmentation_batches", ["estado"])

    op.create_table(
        "segmentation_cache",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("grabacion_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("segmentos", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("modelo", sa.String(length=100), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("consumido", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["grabacion_id"], ["grabaciones.id"]),
        sa.ForeignKeyConstraint(["batch_id"], ["segmentation_batches.id"]),
    )
    op.create_index("ix_segmentation_cache_grabacion_id", "segmentation_cache", ["grabacion_id"])
    op.create_index("ix_segmentation_cache_batch_id", "segmentation_cache", ["batch_id"])
    op.create_index("ix_segmentation_cache_consumido", "segmentation_cache", ["consumido"])

    # --- Resolucion de entidades -----------------------------------------
    # nombre_normalizado se agrega nullable, se backfillea desde `nombre`, y
    # recien despues se marca NOT NULL. Hacerlo en un solo paso con
    # server_default='' romperia el unique de abajo si la tabla ya tuviera
    # varias filas del mismo tipo.
    op.add_column("entidades", sa.Column("nombre_normalizado", sa.String(length=255), nullable=True))
    op.add_column(
        "entidades",
        sa.Column("alias", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "entidades",
        sa.Column("menciones", sa.Integer(), nullable=False, server_default="0"),
    )
    # Backfill aproximado (minusculas). La normalizacion real -- quitar
    # acentos y puntuacion -- la hace entity_resolution.normalizar() en
    # Python; aca solo se necesita un valor coherente y no-nulo para las filas
    # preexistentes, que en la practica son cero (nada poblaba `entidades`).
    op.execute("UPDATE entidades SET nombre_normalizado = lower(nombre) WHERE nombre_normalizado IS NULL")
    op.alter_column("entidades", "nombre_normalizado", nullable=False)
    op.create_index("ix_entidades_nombre_normalizado", "entidades", ["nombre_normalizado"])
    op.create_unique_constraint(
        "uq_entidades_tipo_nombre_normalizado", "entidades", ["tipo", "nombre_normalizado"]
    )

    # --- Contenido repetido (publicidad) ---------------------------------
    op.create_table(
        "contenido_repetido",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("huella", sa.String(length=64), nullable=False, unique=True),
        sa.Column("veces_visto", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("medios_distintos", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("primera_vez", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ultima_vez", sa.DateTime(timezone=True), nullable=False),
        sa.Column("muestra_texto", sa.Text(), nullable=False),
        sa.Column("es_publicidad", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_contenido_repetido_huella", "contenido_repetido", ["huella"])


def downgrade() -> None:
    op.drop_table("contenido_repetido")

    op.drop_constraint("uq_entidades_tipo_nombre_normalizado", "entidades", type_="unique")
    op.drop_index("ix_entidades_nombre_normalizado", table_name="entidades")
    op.drop_column("entidades", "menciones")
    op.drop_column("entidades", "alias")
    op.drop_column("entidades", "nombre_normalizado")

    op.drop_table("segmentation_cache")
    op.drop_table("segmentation_batches")
    postgresql.ENUM(name="estado_segmentation_batch").drop(op.get_bind(), checkfirst=True)

    op.drop_constraint("fk_noticias_historia_id", "noticias", type_="foreignkey")
    op.drop_index("ix_noticias_historia_id", table_name="noticias")
    op.drop_column("noticias", "historia_id")

    op.drop_index("ix_historias_ultima_aparicion", table_name="historias")
    op.drop_table("historias")
