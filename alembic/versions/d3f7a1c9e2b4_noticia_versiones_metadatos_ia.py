"""noticia_versiones: columna metadatos_ia

Revision ID: d3f7a1c9e2b4
Revises: bab6842982b3
Create Date: 2026-07-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d3f7a1c9e2b4"
down_revision: Union[str, None] = "bab6842982b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "noticia_versiones",
        sa.Column("metadatos_ia", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("noticia_versiones", "metadatos_ia")
