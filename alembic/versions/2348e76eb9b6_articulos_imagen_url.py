"""articulos: columna imagen_url

Portada del articulo, sacada de media:content/media:thumbnail/enclosure en RSS
o <image:image> en el sitemap de Google News (ver src/modules/prensa/feeds.py,
_imagen_rss). Solo se guarda la URL, nunca se descarga el binario.

Revision ID: 2348e76eb9b6
Revises: c9a1f37e5d84
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2348e76eb9b6"
down_revision: Union[str, None] = "c9a1f37e5d84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("articulos", sa.Column("imagen_url", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("articulos", "imagen_url")
