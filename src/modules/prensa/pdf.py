"""Prensa impresa: leer un periodico en PDF y prepararlo para el modelo.

Todo lo determinista del pipeline vive aca -- identificar de que diario y fecha
es, deduplicar, sacar el texto y renderizar las paginas. Nada de esto necesita
LLM, y hacerlo antes de llamarlo abarata y estabiliza la extraccion: el medio y
la fecha salen del nombre del archivo (67 de 68 aciertos medidos sobre agosto
2026), no de pedirselos al modelo.

Sin efectos de red ni DB: se puede probar con un PDF suelto.
"""
import hashlib
import io
import re
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pdfplumber

# Una pagina con menos texto que esto no vale la pena mandarla como texto: o es
# una plana de anuncios o es una pagina que solo tiene imagenes (deportes).
MIN_CHARS_PAGINA = 250
# Un titular es texto notablemente mas grande que el cuerpo. El corte se calcula
# por pagina (cada diario maqueta distinto): en Diario Tiempo el cuerpo va en
# 9 pt y el titular en 35 pt.
FACTOR_TITULAR = 1.6
MAX_PALABRAS_TITULAR = 25
# 150 dpi deja la pagina en ~1688x2063 px y ~700 KB en JPEG: legible para leer
# una nota de periodico sin que el front tenga que bajar el PDF entero (los de
# La Tribuna pesan hasta 86 MB).
DPI_PAGINA = 150
CALIDAD_JPEG = 80

_MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
    "diciembre": 12,
}

# Patron en el nombre del archivo -> codigo de Medio. El canal republica cada
# diario con el mismo patron, asi que reconocerlo alcanza.
MEDIOS_POR_PATRON = (
    (re.compile(r"la[-_]?tribuna", re.I), "la_tribuna"),
    (re.compile(r"diario\s*tiempo", re.I), "diario_tiempo"),
    (re.compile(r"ma_?s[-_ ]?noticias", re.I), "mas_noticias"),
    (re.compile(r"patrulla[-_ ]?grafica", re.I), "patrulla_grafica"),
    (re.compile(r"^EP[-_]", re.I), "el_pais_hn"),
)


@dataclass(frozen=True)
class Identidad:
    """De que diario y de que fecha es el archivo."""

    medio: str | None
    fecha: date | None


def identificar(nombre: str) -> Identidad:
    """Deduce medio y fecha de edicion del nombre del archivo.

    La fecha del NOMBRE es la de la edicion, que es la que importa: la carpeta
    donde cayo el archivo es la fecha en que Telegram lo publico, y difieren (un
    diario del 16-ago puede subirse el 17). Devuelve None en lo que no pueda
    deducir, en vez de adivinar -- quien llame decide si preguntarle al modelo o
    dejarlo para revision manual.
    """
    limpio = re.sub(r"^\d+_", "", nombre)  # el id del mensaje de Telegram
    medio = next((c for patron, c in MEDIOS_POR_PATRON if patron.search(limpio)), None)
    return Identidad(medio=medio, fecha=_fecha(limpio))


def _fecha(nombre: str) -> date | None:
    s = nombre.lower()
    # OJO: el nombre del mes se captura con [a-z]+ y no con \w+ -- en Python
    # \w incluye el guion bajo, y estos archivos usan "_" como separador, asi
    # que \w+ capturaba "agosto_" y no matcheaba contra _MESES.
    # "13 de agosto de 2026" y tambien "15 de agosto 2026" (sin el segundo de).
    m = re.search(r"(\d{1,2})[_\- ]?de[_\- ]?([a-z]+)[_\- ]?(?:de[_\- ]?)?(20\d{2})", s)
    if m and m.group(2) in _MESES:
        return _valida(int(m.group(3)), _MESES[m.group(2)], int(m.group(1)))
    m = re.search(r"(\d{1,2})[-_]([a-z]+)[-_](20\d{2})", s)
    if m and m.group(2) in _MESES:
        return _valida(int(m.group(3)), _MESES[m.group(2)], int(m.group(1)))
    m = re.search(r"(\d{2})(\d{2})(20\d{2})", s)  # DDMMYYYY
    if m:
        return _valida(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"(\d{2})-(\d{2})-(\d{2})(?:[_.\-]|$)", s)  # DD-MM-YY
    if m:
        return _valida(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"_(\d{2})(\d{2})(\d{2})_", s)  # _DDMMYY_
    if m:
        return _valida(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def _valida(anio: int, mes: int, dia: int) -> date | None:
    try:
        return date(anio, mes, dia)
    except ValueError:
        return None


def sha256(ruta: Path) -> str:
    """Hash del contenido, que es la clave de dedup real.

    El canal republica el mismo PDF con otro nombre: de 68 archivos de agosto
    2026, 2 pares eran byte-identicos y uno cruzaba de dia. Deduplicar por
    nombre o por (medio, fecha) no los agarra.
    """
    h = hashlib.sha256()
    with ruta.open("rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloque)
    return h.hexdigest()


def _tam_cuerpo(palabras) -> float:
    tams = [round(w["size"], 1) for w in palabras if w.get("size")]
    if not tams:
        return 0.0
    try:
        return statistics.mode(tams)
    except statistics.StatisticsError:
        return statistics.median(tams)


def _lineas(palabras, tolerancia: float = 3.0):
    lineas, actual, y = [], [], None
    for w in palabras:
        if y is None or abs(w["top"] - y) <= tolerancia:
            actual.append(w)
            y = w["top"] if y is None else y
        else:
            lineas.append(actual)
            actual, y = [w], w["top"]
    if actual:
        lineas.append(actual)
    return lineas


def pagina_a_markdown(page, numero: int) -> str | None:
    """Markdown de una pagina, con los titulares marcados como `##`.

    Devuelve None si la pagina no tiene texto util (anuncio o plana de fotos).
    Marcar los titulares por tamano de fuente le entrega al modelo la estructura
    ya resuelta, sin gastar un token en deducirla.
    """
    palabras = page.extract_words(extra_attrs=["size"])
    if not palabras:
        return None
    cuerpo = _tam_cuerpo(palabras)
    corte = cuerpo * FACTOR_TITULAR if cuerpo else float("inf")

    partes: list[str] = []
    buffer: list[str] = []
    titular: list[str] = []
    tam_titular: float | None = None

    def cerrar_titular() -> None:
        nonlocal titular, tam_titular
        if titular:
            texto = " ".join(titular)
            # Un titular ocupa varias lineas ("Calle de Campuca, / cada vez mas
            # / deteriorada" es UNO, no tres); si junto queda larguisimo,
            # entonces no era titular sino texto en fuente grande.
            corto = len(texto.split()) <= MAX_PALABRAS_TITULAR
            partes.append(f"\n## {texto}\n" if corto else texto)
            titular, tam_titular = [], None

    for linea in _lineas(palabras):
        texto = " ".join(w["text"] for w in linea).strip()
        if not texto:
            continue
        tam = max((w.get("size") or 0) for w in linea)
        if tam >= corte:
            if re.fullmatch(r"\d{1,3}", texto):
                continue  # folio de la pagina, no titular
            if tam_titular is not None and abs(tam - tam_titular) > 1.0:
                cerrar_titular()
            if buffer:
                partes.append(" ".join(buffer))
                buffer = []
            titular.append(texto)
            tam_titular = tam
        else:
            cerrar_titular()
            buffer.append(texto)
    cerrar_titular()
    if buffer:
        partes.append(" ".join(buffer))

    md = "\n".join(partes).strip()
    if len(md) < MIN_CHARS_PAGINA:
        return None
    # Los PDF cortan palabras a fin de linea; recomponerlas evita que el modelo
    # copie "admi -nistrativas" al articulo extraido.
    md = re.sub(r"(\w)\s*-\s+(\w)", r"\1\2", md)
    return f"<!-- pagina {numero} -->\n{md}"


@dataclass(frozen=True)
class Lectura:
    markdown: str
    paginas_total: int
    paginas_con_texto: list[int]
    paginas_sin_texto: list[int]


def leer(ruta: Path) -> Lectura:
    """Convierte el PDF a markdown y reporta que paginas quedaron fuera.

    `paginas_sin_texto` no es descarte: son las que hay que mandarle al modelo
    como imagen si se quiere cobertura completa. Medido en Diario Tiempo: 14 de
    28 paginas no tienen una sola palabra extraible, y ahi vive TODA la seccion
    de deportes.
    """
    trozos: list[str] = []
    con: list[int] = []
    sin: list[int] = []
    with pdfplumber.open(ruta) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            md = pagina_a_markdown(page, i)
            if md:
                trozos.append(md)
                con.append(i)
            else:
                sin.append(i)
            # pdfplumber cachea los objetos parseados de CADA pagina y no los
            # suelta al avanzar: sin esto el proceso crecio a 1.28 GB y el
            # kernel lo mato al noveno periodico (el backend corre en un
            # t3.small de 1.9 GB). Medido el 2026-08-29.
            page.flush_cache()
            page.get_textmap.cache_clear()
    return Lectura("\n\n".join(trozos), total, con, sin)


def render_pagina(ruta: Path, numero: int, dpi: int = DPI_PAGINA) -> bytes:
    """Una pagina como JPEG, para que el front pueda mostrar donde salio la nota.

    No se puede servir el PDF completo: los de La Tribuna llegan a 86 MB.
    """
    with pdfplumber.open(ruta) as pdf:
        page = pdf.pages[numero - 1]
        imagen = page.to_image(resolution=dpi)
        buf = io.BytesIO()
        imagen.original.convert("RGB").save(
            buf, "JPEG", quality=CALIDAD_JPEG, optimize=True
        )
        # Mismo motivo que en leer(): renderizar 40 paginas seguidas sin soltar
        # la cache de pdfplumber revienta la memoria del t3.small.
        page.flush_cache()
        return buf.getvalue()
