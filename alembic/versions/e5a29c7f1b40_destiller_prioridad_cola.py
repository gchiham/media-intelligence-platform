"""Prioridad de la cola de validacion de Destiller.

La validacion humana rinde mucho mas si empieza por donde los modelos
discrepan: donde ambos coinciden, o el caso es obvio o los dos comparten el
mismo sesgo -- y para lo segundo esta la muestra de control.

Solo agrega columnas a `destiller_bloques`. No toca `destiller_veredictos`, asi
que recalcular prioridades nunca pone en riesgo trabajo humano ya hecho.

Revision ID: e5a29c7f1b40
Revises: c4d81ba90e17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5a29c7f1b40"
down_revision: Union[str, None] = "c4d81ba90e17"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default en todas: son ADD COLUMN sobre una tabla con datos, y sin
    # default la columna NOT NULL fallaria. Postgres 11+ no reescribe la tabla
    # para un default constante, asi que es instantaneo.
    op.add_column(
        "destiller_bloques",
        sa.Column("prioridad", sa.SmallInteger(), nullable=False, server_default="5"),
    )
    op.add_column(
        "destiller_bloques",
        sa.Column("desacuerdo_binario", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "destiller_bloques",
        sa.Column("porcentaje_acuerdo", sa.Float(), nullable=False, server_default="1"),
    )
    op.add_column("destiller_bloques", sa.Column("modelo_base", sa.String(120), nullable=True))
    op.add_column("destiller_bloques", sa.Column("modelo_comparado", sa.String(120), nullable=True))
    op.add_column(
        "destiller_bloques", sa.Column("version_comparacion", sa.String(40), nullable=True)
    )
    # Indice compuesto en el orden exacto del ORDER BY de la cola. A 815 filas
    # no cambia nada medible; existe para que siga sirviendo cuando el set
    # crezca a decenas de miles de bloques del historico.
    op.create_index(
        "ix_destiller_bloques_cola",
        "destiller_bloques",
        ["modelo", "prioridad", sa.text("desacuerdo_binario DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_destiller_bloques_cola", table_name="destiller_bloques")
    for col in (
        "version_comparacion",
        "modelo_comparado",
        "modelo_base",
        "porcentaje_acuerdo",
        "desacuerdo_binario",
        "prioridad",
    ):
        op.drop_column("destiller_bloques", col)
