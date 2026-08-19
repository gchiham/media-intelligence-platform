"""Parseo de feeds: sin red y sin DB, con cuerpos recortados de las respuestas
reales de los medios (2026-08-19)."""
import pytest

from src.modules.prensa.feeds import (
    FormatoNoSoportado,
    parsear_rss,
    parsear_sitemap_news,
)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>Criterio.hn</title>
  <item>
    <title>Titulo con acentos: eleccion</title>
    <link>https://criterio.hn/nota-uno/</link>
    <guid isPermaLink="false">https://criterio.hn/?p=123</guid>
    <pubDate>Wed, 19 Aug 2026 01:07:55 +0000</pubDate>
    <dc:creator>Redaccion</dc:creator>
    <description>Resumen corto</description>
    <content:encoded>&lt;p&gt;Cuerpo completo&lt;/p&gt;</content:encoded>
  </item>
  <item>
    <title>Sin fecha, se descarta</title>
    <link>https://criterio.hn/nota-dos/</link>
    <guid>https://criterio.hn/?p=124</guid>
  </item>
</channel>
</rss>"""

SITEMAP = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
  <url>
    <loc>https://www.laprensa.hn/sucesos/nota-HG31735075</loc>
    <news:news>
      <news:publication><news:name>La Prensa</news:name></news:publication>
      <news:publication_date>2026-08-16T22:05:32-06:00</news:publication_date>
      <news:title>Titulo de la nota</news:title>
    </news:news>
  </url>
  <url><loc>https://www.laprensa.hn/sucesos</loc></url>
</urlset>"""


def test_rss_extrae_campos_y_contenido_completo():
    items = parsear_rss(RSS)
    assert len(items) == 1  # el segundo item no tiene pubDate
    item = items[0]
    assert item.guid == "https://criterio.hn/?p=123"
    assert item.url == "https://criterio.hn/nota-uno/"
    assert item.titulo == "Titulo con acentos: eleccion"
    assert item.autor == "Redaccion"
    assert item.contenido_html == "<p>Cuerpo completo</p>"
    assert item.publicado_at.isoformat() == "2026-08-19T01:07:55+00:00"


def test_rss_sin_guid_cae_al_link():
    cuerpo = RSS.replace(b'<guid isPermaLink="false">https://criterio.hn/?p=123</guid>', b"")
    assert parsear_rss(cuerpo)[0].guid == "https://criterio.hn/nota-uno/"


def test_sitemap_ignora_urls_sin_bloque_news():
    items = parsear_sitemap_news(SITEMAP)
    assert len(items) == 1
    assert items[0].titulo == "Titulo de la nota"
    # -06:00 es la hora local de Honduras; se conserva el offset del origen.
    assert items[0].publicado_at.isoformat() == "2026-08-16T22:05:32-06:00"
    assert items[0].contenido_html is None  # el sitemap nunca trae cuerpo


def test_html_en_vez_de_xml_falla_con_mensaje_util():
    """Cloudflare y los temas de WordPress mal configurados contestan 200 con
    HTML -- es el modo de falla real de latribuna.hn y tiempo.hn."""
    with pytest.raises(FormatoNoSoportado) as e:
        parsear_rss(b"<!doctype html><html lang='es'><head><title>La Tribuna")
    assert "doctype html" in str(e.value)


def test_atom_falla_ruidoso_en_vez_de_devolver_cero():
    atom = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    with pytest.raises(FormatoNoSoportado):
        parsear_rss(atom)
