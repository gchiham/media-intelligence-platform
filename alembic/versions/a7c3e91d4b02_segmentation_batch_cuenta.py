"""segmentation_batches.cuenta: de que cuenta de OpenAI salio cada batch

Hace falta para mandar batches por las DOS cuentas en paralelo. Un batch
abierto con una cuenta es invisible para la otra (son organizaciones
distintas: `retrieve` falla), asi que al recolectar hay que saber con cual
consultarlo. Hasta ahora el collect lo resolvia por prueba y error --
intentaba con la key configurada y "se omitia" si no lo veia--, lo que
obligaba a correr --collect dos veces con env vars distintas y dejaba batches
sin recolectar si alguien se olvidaba.

Nullable a proposito: las filas viejas (y los batches de Anthropic, que usan
la misma tabla) no tienen cuenta y siguen funcionando con el comportamiento
anterior.

Revision ID: a7c3e91d4b02
Revises: e5a29c7f1b40
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e91d4b02"
down_revision: Union[str, None] = "e5a29c7f1b40"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "segmentation_batches",
        sa.Column("cuenta", sa.String(length=20), nullable=True),
    )
    # Se consulta filtrando por cuenta en cada --collect.
    op.create_index(
        "ix_segmentation_batches_cuenta", "segmentation_batches", ["cuenta"]
    )


def downgrade() -> None:
    op.drop_index("ix_segmentation_batches_cuenta", table_name="segmentation_batches")
    op.drop_column("segmentation_batches", "cuenta")
