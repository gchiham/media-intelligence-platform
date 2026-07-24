"""Detecta bloques de texto que se repiten identicos al aire (publicidad,
cortinas, promos) para no mandarlos al LLM.

Motivacion medida (docs/EFFICIENCY_REVIEW.md §3): ~21.7 noticias por hora de
84 s promedio son ~30 min de noticia por cada hora de transmision. La otra
mitad del aire se transcribe y se manda al LLM a precio completo para
descubrir que no habia nada que segmentar.

**Por que funciona la deteccion por texto:** un anuncio es un spot grabado que
se emite decenas de veces con el mismo audio, asi que su transcripcion sale
casi identica cada vez. Una noticia repetida entre emisoras NO tiene ese
comportamiento: la lee otro locutor con otras palabras (por eso la
deduplicacion de noticias necesita embeddings y no hashes -- ver
src/modules/editorial/dedup.py). O sea que la repeticion textual *exacta* es
justamente lo que separa publicidad de noticia recurrente.

**Alcance honesto:** esto ahorra la llamada al LLM (lo caro), no el GPU --
para tener el texto ya hubo que transcribir. La huella acustica antes de
transcribir es el paso siguiente y ataca el costo de GPU.

Granularidad: se hashea por ventana deslizante de palabras, no el chunk
completo de 600. Un spot dura ~30 s (~75 palabras), asi que un chunk suele
mezclar publicidad con noticia -- hashear el chunk entero no encontraria
nunca una coincidencia exacta.
"""
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.modules.ai.models import ContenidoRepetido
from src.modules.ai.schemas import Word

VENTANA = 50
PASO = 25
# A partir de cuantas apariciones se considera publicidad. Una noticia puede
# repetirse 2-3 veces en un mismo noticiero (titulares + desarrollo); un spot
# se emite muchas mas. 5 deja margen para no confundirlos.
UMBRAL_APARICIONES = 5
# Fraccion de ventanas del chunk que deben ser conocidas-repetidas para
# saltar el chunk entero. Alto a proposito: ante la duda, se paga la llamada
# al LLM. Perder una noticia real es mucho peor que gastar unos centavos.
UMBRAL_FRACCION_CHUNK = 0.75

_NO_ALFANUM = re.compile(r"[^\w\s]", flags=re.UNICODE)
_ESPACIOS = re.compile(r"\s+")


def _normalizar(texto: str) -> str:
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    return _ESPACIOS.sub(" ", _NO_ALFANUM.sub(" ", sin_acentos.lower())).strip()


def huellas_de_ventanas(
    words: list[Word], ventana: int = VENTANA, paso: int = PASO
) -> list[tuple[str, str]]:
    """Devuelve [(huella, texto_muestra), ...] por ventana deslizante."""
    salida: list[tuple[str, str]] = []
    for i in range(0, max(len(words) - ventana + 1, 0), paso):
        bloque = words[i : i + ventana]
        texto = _normalizar(" ".join(w.word for w in bloque))
        if not texto:
            continue
        huella = hashlib.sha256(texto.encode("utf-8")).hexdigest()
        salida.append((huella, texto))
    return salida


@dataclass
class ResultadoRegistro:
    ventanas: int
    nuevas: int
    conocidas: int


class RepeatedContentIndex:
    """Registra y consulta bloques repetidos.

    Uso tipico en dos fases: primero `registrar()` sobre transcripciones ya
    existentes para que el indice aprenda que se repite, y despues
    `debe_saltarse()` antes de mandar un chunk al LLM.
    """

    def __init__(self, session: Session, umbral: int = UMBRAL_APARICIONES):
        self._session = session
        self._umbral = umbral

    def registrar(self, words: list[Word], medio_codigo: str) -> ResultadoRegistro:
        huellas = huellas_de_ventanas(words)
        if not huellas:
            return ResultadoRegistro(0, 0, 0)

        ahora = datetime.now(timezone.utc)
        existentes = {
            fila.huella: fila
            for fila in self._session.scalars(
                select(ContenidoRepetido).where(
                    ContenidoRepetido.huella.in_([h for h, _ in huellas])
                )
            )
        }

        nuevas = conocidas = 0
        vistas_en_esta_pasada: set[str] = set()
        for huella, texto in huellas:
            # Una misma ventana puede repetirse dentro del mismo audio (un
            # spot que sale dos veces en la hora). No debe contar como dos
            # medios distintos ni inflar el contador de golpe.
            if huella in vistas_en_esta_pasada:
                continue
            vistas_en_esta_pasada.add(huella)

            fila = existentes.get(huella)
            if fila is None:
                self._session.add(
                    ContenidoRepetido(
                        huella=huella,
                        veces_visto=1,
                        medios_distintos=1,
                        primera_vez=ahora,
                        ultima_vez=ahora,
                        muestra_texto=texto[:2000],
                    )
                )
                nuevas += 1
            else:
                fila.veces_visto += 1
                fila.ultima_vez = ahora
                conocidas += 1

        return ResultadoRegistro(len(huellas), nuevas, conocidas)

    def huellas_publicitarias(self, huellas: list[str]) -> set[str]:
        """De las huellas dadas, cuales estan marcadas o contadas como publicidad."""
        if not huellas:
            return set()
        filas = self._session.scalars(
            select(ContenidoRepetido).where(ContenidoRepetido.huella.in_(huellas))
        )
        return {
            f.huella
            for f in filas
            # es_publicidad es el override humano: False fuerza a NO saltar
            # aunque el contador diga que se repite mucho (protege una noticia
            # recurrente mal clasificada); True fuerza a saltar.
            if f.es_publicidad is True
            or (f.es_publicidad is None and f.veces_visto >= self._umbral)
        }

    def debe_saltarse(self, chunk: list[Word]) -> bool:
        """True si el chunk es mayoritariamente contenido repetido conocido."""
        huellas = huellas_de_ventanas(chunk)
        if not huellas:
            return False
        publicitarias = self.huellas_publicitarias([h for h, _ in huellas])
        return (len(publicitarias) / len(huellas)) >= UMBRAL_FRACCION_CHUNK
