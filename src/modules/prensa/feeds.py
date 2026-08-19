"""Descarga y parseo de feeds. Sin DB y sin efectos: se puede testear con bytes
en memoria (ver tests/test_prensa_feeds.py).

Usa urllib de la stdlib a proposito. httpx esta solo en las deps de dev y
requests no esta declarada; agregar una dependencia de runtime para hacer
GETs condicionales no se justifica.
"""
import gzip
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.entities import html5 as _ENTIDADES_HTML5

from defusedxml.ElementTree import fromstring as parse_xml

# Los feeds son XML de terceros: defusedxml evita billion-laughs y entidades
# externas. ElementTree pelado no protege contra la primera.

# XML solo conoce 5 entidades (amp/lt/gt/apos/quot); WordPress a veces mete
# entidades HTML (&raquo;, &nbsp;, &hellip;...) sueltas fuera de CDATA, lo que
# no es XML valido y tumba el parseo del DOCUMENTO ENTERO, no solo ese campo
# -- visto en produccion con hch.tv (&raquo; en <media:title>). Se reparan
# sobre los bytes crudos: los nombres de entidad son siempre ASCII sea cual
# sea la codificacion declarada, asi que no hace falta decodificar el feed
# entero para arreglar esto.
_ENTIDAD_BYTES = re.compile(rb"&(#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);?")
_ENTIDADES_XML_VALIDAS = {b"amp", b"lt", b"gt", b"apos", b"quot"}


def _reparar_entidades_html(cuerpo: bytes) -> bytes:
    def _reemplazar(m: re.Match[bytes]) -> bytes:
        nombre = m.group(1)
        if nombre.startswith(b"#") or nombre in _ENTIDADES_XML_VALIDAS:
            return m.group(0)  # numerica o XML valida: se deja intacta
        texto = nombre.decode("ascii", "replace")
        caracter = _ENTIDADES_HTML5.get(texto + ";") or _ENTIDADES_HTML5.get(texto)
        return caracter.encode("utf-8") if caracter else m.group(0)

    return _ENTIDAD_BYTES.sub(_reemplazar, cuerpo)

NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
    "news": "http://www.google.com/schemas/sitemap-news/0.9",
}

# Algunos medios (La Tribuna, Tiempo) filtran por user agent. Uno de navegador
# pasa; uno vacio o de script se come un 403.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT_SEG = 25
# Los feeds rondan los 20-80 KB y los sitemaps de OPSA los 430 KB. 8 MB deja
# margen de sobra y corta un cuerpo absurdo antes de que llegue a memoria.
MAX_BYTES = 8 * 1024 * 1024


class ErrorDescarga(Exception):
    """Fallo de red o HTTP != 200/304."""


class FormatoNoSoportado(Exception):
    """El cuerpo no es el XML que esperabamos para el tipo de fuente."""


@dataclass(frozen=True)
class ItemFeed:
    guid: str
    url: str
    titulo: str
    publicado_at: datetime
    resumen: str | None = None
    contenido_html: str | None = None
    autor: str | None = None


@dataclass(frozen=True)
class Descarga:
    modificada: bool  # False = el servidor contesto 304
    cuerpo: bytes | None
    etag: str | None
    last_modified: str | None


def descargar(
    url: str,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: int = TIMEOUT_SEG,
) -> Descarga:
    """GET condicional. Si el servidor contesta 304 devuelve modificada=False y
    cuerpo=None, que es el caso normal: entre pasadas casi nunca hay novedad."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=timeout
        ) as resp:
            crudo = resp.read(MAX_BYTES + 1)
            if len(crudo) > MAX_BYTES:
                raise ErrorDescarga(f"cuerpo mayor a {MAX_BYTES} bytes")
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                crudo = gzip.decompress(crudo)
            return Descarga(
                modificada=True,
                cuerpo=crudo,
                etag=resp.headers.get("ETag"),
                last_modified=resp.headers.get("Last-Modified"),
            )
    except urllib.error.HTTPError as e:
        if e.code == 304:
            # Se conservan los validadores que ya teniamos: el 304 no los repite.
            return Descarga(False, None, etag, last_modified)
        raise ErrorDescarga(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise ErrorDescarga(f"red: {e.reason}") from e
    except OSError as e:  # timeouts de socket, conexiones cortadas
        raise ErrorDescarga(f"red: {e}") from e


def parsear_rss(cuerpo: bytes) -> list[ItemFeed]:
    """RSS 2.0. Las 17 fuentes hondurenas verificadas lo usan; Atom no esta
    soportado a proposito -- si un medio migra, esto falla ruidoso en vez de
    devolver cero items en silencio."""
    raiz = _parsear(cuerpo)
    if raiz.tag != "rss" and raiz.find("channel") is None:
        raise FormatoNoSoportado(f"no es RSS 2.0 (raiz <{raiz.tag}>)")

    items = []
    for nodo in raiz.findall(".//item"):
        link = _texto(nodo, "link")
        guid = _texto(nodo, "guid") or link
        titulo = _texto(nodo, "title")
        publicado = _fecha_rfc2822(_texto(nodo, "pubDate"))
        # Sin guid/url/fecha no hay forma de deduplicar ni de ordenar: se
        # descarta el item y se sigue con el resto del feed.
        if not (guid and link and publicado):
            continue
        items.append(
            ItemFeed(
                guid=guid[:512],
                url=link[:1024],
                titulo=titulo or "(sin titulo)",
                publicado_at=publicado,
                resumen=_texto(nodo, "description"),
                contenido_html=_texto(nodo, "content:encoded"),
                autor=(_texto(nodo, "dc:creator") or _texto(nodo, "author") or None),
            )
        )
    return items


def parsear_sitemap_news(cuerpo: bytes) -> list[ItemFeed]:
    """Sitemap de Google News. Trae titulo y fecha de publicacion exacta, pero
    nunca cuerpo: para el texto hay que ir a la nota."""
    raiz = _parsear(cuerpo)
    urls = raiz.findall("sm:url", NS)
    if not urls:
        raise FormatoNoSoportado(f"no es un sitemap (raiz <{raiz.tag}>)")

    items = []
    for nodo in urls:
        loc = _texto(nodo, "sm:loc")
        news = nodo.find("news:news", NS)
        if loc is None or news is None:
            # Los sitemaps de OPSA mezclan urls de seccion sin bloque <news:news>.
            continue
        publicado = _fecha_iso(_texto(news, "news:publication_date"))
        if not publicado:
            continue
        items.append(
            ItemFeed(
                guid=loc[:512],
                url=loc[:1024],
                titulo=_texto(news, "news:title") or "(sin titulo)",
                publicado_at=publicado,
            )
        )
    return items


PARSERS = {"rss": parsear_rss, "sitemap_news": parsear_sitemap_news}


def _parsear(cuerpo: bytes):
    try:
        return parse_xml(cuerpo)
    except Exception as e:
        # Antes de rendirse, probar con las entidades HTML reparadas -- cubre
        # el caso comun de un feed WordPress con &raquo;/&nbsp;/etc sueltas.
        # Si el cuerpo no tenia ese problema, _reparar_entidades_html no
        # cambia nada y este segundo intento falla igual que el primero.
        reparado = _reparar_entidades_html(cuerpo)
        if reparado != cuerpo:
            try:
                return parse_xml(reparado)
            except Exception:
                pass
        # Cloudflare y los temas de WordPress mal configurados contestan 200 con
        # HTML: sin este mensaje el sintoma es un ParseError pelado en la linea 1.
        muestra = cuerpo[:60].decode("utf-8", "ignore").strip()
        raise FormatoNoSoportado(f"XML invalido ({e}); empieza con: {muestra!r}") from e


def _texto(nodo, ruta: str) -> str | None:
    hijo = nodo.find(ruta, NS) if ":" in ruta else nodo.find(ruta)
    if hijo is None or hijo.text is None:
        return None
    return hijo.text.strip() or None


def _fecha_rfc2822(texto: str | None) -> datetime | None:
    if not texto:
        return None
    try:
        fecha = parsedate_to_datetime(texto)
    except (TypeError, ValueError):
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


def _fecha_iso(texto: str | None) -> datetime | None:
    if not texto:
        return None
    try:
        fecha = datetime.fromisoformat(texto)
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)
