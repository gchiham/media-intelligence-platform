"""grabaciones: index en estado + columna error_mensaje

Revision ID: bab6842982b3
Revises: 1c9fad29b98d
Create Date: 2026-07-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "bab6842982b3"
down_revision: Union[str, None] = "1c9fad29b98d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("grabaciones", sa.Column("error_mensaje", sa.Text(), nullable=True))
    op.create_index(op.f("ix_grabaciones_estado"), "grabaciones", ["estado"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_grabaciones_estado"), table_name="grabaciones")
    op.drop_column("grabaciones", "error_mensaje")
