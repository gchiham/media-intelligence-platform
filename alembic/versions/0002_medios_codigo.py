"""agrega medios.codigo (identificador del sistema de captura externo)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-17

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE medios ADD COLUMN codigo VARCHAR(100)")
    op.execute("ALTER TABLE medios ADD CONSTRAINT medios_codigo_key UNIQUE (codigo)")
    op.execute("ALTER TABLE medios ALTER COLUMN codigo SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE medios DROP COLUMN codigo")
