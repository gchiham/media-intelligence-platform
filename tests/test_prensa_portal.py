"""Render de la seccion Prensa Digital del portal. Sin DB: se le pasan filas
armadas a mano, que es lo que devuelve ArticuloRepository.listar_recientes()."""
import uuid
from datetime import datetime, timezone

from src.api.routers.prensa import render_pagina

FILAS = [
    {
        "id": uuid.uuid4(),
        "titulo": "Garifunas rechazan reunion",
        "url": "https://criterio.hn/nota-uno/",
        "resumen": "Resumen corto",
        "autor": "Redaccion",
        "publicado_at": datetime(2026, 8, 19, 1, 7, 55, tzinfo=timezone.utc),
        "tiene_cuerpo": True,
        "medio_nombre": "Criterio.hn",
    },
    {
        "id": uuid.uuid4(),
        "titulo": "Nota de La Prensa",
        "url": "https://www.laprensa.hn/nota-dos",
        "resumen": None,
        "autor": None,
        "publicado_at": datetime(2026, 8, 19, 3, 0, tzinfo=timezone.utc),
        "tiene_cuerpo": False,  # los sitemaps de OPSA no traen cuerpo
        "medio_nombre": "La Prensa",
    },
]


def test_render_muestra_medios_y_enlaza_a_la_nota_original():
    pagina = render_pagina(FILAS, token="secreto")
    assert "Criterio.hn (1)" in pagina and "La Prensa (1)" in pagina
    assert "Todos (2)" in pagina
    assert 'href="https://criterio.hn/nota-uno/"' in pagina
    assert 'target="_blank" rel="noopener noreferrer"' in pagina
    # Sin cuerpo se avisa: en esos medios solo hay titular.
    assert "solo titular" in pagina


def test_fechas_en_horario_de_honduras():
    """01:07 UTC del 19 es todavia el 18 en Honduras (GMT-6)."""
    assert "2026-08-18 19:07" in render_pagina(FILAS, token="x")


def test_titulo_hostil_se_escapa():
    fila = dict(FILAS[0], titulo="<script>alert(1)</script>", resumen="<img src=x onerror=y>")
    pagina = render_pagina([fila], token="x")
    assert "<script>alert(1)</script>" not in pagina
    assert "&lt;script&gt;" in pagina
    assert "<img src=x" not in pagina


def test_token_se_escapa_en_el_enlace_al_otro_dashboard():
    assert 'token=a&amp;b' in render_pagina(FILAS, token="a&b")
