"""noticias: columna clip_s3_uri

Revision ID: a7c3e91f4d5b
Revises: d3f7a1c9e2b4
Create Date: 2026-07-21

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a7c3e91f4d5b"
down_revision: Union[str, None] = "d3f7a1c9e2b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("noticias", sa.Column("clip_s3_uri", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("noticias", "clip_s3_uri")
