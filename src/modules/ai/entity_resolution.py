"""Resuelve menciones crudas del LLM contra el catalogo `Entidad`.

El problema concreto (ya anotado en NewsSegment y en docs/ORCHESTRATOR_DESIGN.md):
el LLM devuelve `people`/`organizations`/`locations` como texto libre, tal como
salio al aire. La misma persona llega como "JOH", "Juan Orlando Hernandez",
"Juan Orlando Hernández" y "el presidente Hernandez" -- cuatro strings, una
sola persona. Sin resolverlos, la pregunta que da valor al producto ("¿cuantas
veces mencionaron a X este mes?") no se puede responder.

Estrategia, de mas confiable a menos:

1. **Coincidencia normalizada exacta** -- sin acentos, minusculas, sin
   puntuacion ni titulos. Resuelve el caso "Hernández" vs "Hernandez".
2. **Alias registrado** -- variantes ya asociadas a la entidad.
3. **Siglas derivadas** -- las iniciales de un nombre de 3+ palabras se
   registran como alias automaticamente al crear la entidad. Esto es lo que
   hace que "JOH" caiga en "Juan Orlando Hernandez" sin intervencion manual.
4. **Difuso conservador** -- `difflib` con umbral alto y solo dentro del mismo
   tipo. Deliberadamente timido: fusionar dos personas distintas es un error
   mucho peor (y silencioso) que dejar dos filas que un humano puede unir
   despues. Por eso NO se usa para siglas ni nombres cortos, donde la
   distancia de edicion es engañosa ("Lopez"/"Perez" comparten demasiado).

Lo que este modulo NO hace: decidir si dos entidades distintas son la misma
persona en el mundo real cuando los nombres no se parecen (ej. "El Papa" y su
nombre propio). Eso es curaduria humana sobre el catalogo, y para eso
`Entidad.alias` es editable.
"""
import re
import unicodedata
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.modules.ai.models import Entidad, TipoEntidad

# Solo se despojan de personas: "Banco Central" o "Ministerio de Salud" no
# deben perder nada, pero "presidente Xiomara Castro" si.
_TITULOS_PERSONA = {
    "presidente", "presidenta", "expresidente", "expresidenta",
    "licenciado", "licenciada", "lic", "doctor", "doctora", "dr", "dra",
    "ingeniero", "ingeniera", "ing", "abogado", "abogada",
    "diputado", "diputada", "alcalde", "alcaldesa", "ministro", "ministra",
    "senor", "senora", "don", "dona", "el", "la",
}

_NO_ALFANUM = re.compile(r"[^\w\s]", flags=re.UNICODE)
_ESPACIOS = re.compile(r"\s+")

# Debajo de esto, la similitud difusa no es confiable: en nombres cortos y
# siglas, dos cadenas distintas comparten demasiados caracteres.
_MIN_LEN_DIFUSO = 10
_UMBRAL_DIFUSO = 0.92


def normalizar(texto: str, tipo: TipoEntidad | None = None) -> str:
    """Forma canonica para comparar: sin acentos, minusculas, sin puntuacion."""
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn"
    )
    limpio = _NO_ALFANUM.sub(" ", sin_acentos.lower())
    limpio = _ESPACIOS.sub(" ", limpio).strip()

    if tipo == TipoEntidad.PERSONA:
        palabras = [p for p in limpio.split() if p not in _TITULOS_PERSONA]
        # Si despues de quitar titulos no queda nada (la mencion era solo
        # "el presidente"), se devuelve el original limpio -- es preferible
        # una entidad basura visible a perder la mencion en silencio.
        if palabras:
            limpio = " ".join(palabras)

    return limpio


def derivar_siglas(nombre_normalizado: str) -> str | None:
    """"juan orlando hernandez" -> "joh". None si no aplica.

    Solo para 3+ palabras: con 2 palabras las siglas son demasiado ambiguas
    ("Maria Perez" -> "mp" chocaria con cualquier otro "M. P.").
    """
    palabras = nombre_normalizado.split()
    if len(palabras) < 3:
        return None
    return "".join(p[0] for p in palabras)


class EntityResolver:
    """Resuelve menciones contra el catalogo, creando entidades nuevas cuando
    no hay coincidencia.

    Mantiene un cache en memoria por tipo para no consultar Postgres una vez
    por mencion -- una grabacion tipica trae decenas de menciones repetidas
    entre sus noticias.
    """

    def __init__(self, session: Session):
        self._session = session
        self._cache: dict[TipoEntidad, dict[str, Entidad]] = {}

    def _indice(self, tipo: TipoEntidad) -> dict[str, Entidad]:
        if tipo not in self._cache:
            filas = self._session.scalars(select(Entidad).where(Entidad.tipo == tipo)).all()
            indice: dict[str, Entidad] = {}
            for entidad in filas:
                indice[entidad.nombre_normalizado] = entidad
                for alias in entidad.alias or []:
                    indice.setdefault(alias, entidad)
            self._cache[tipo] = indice
        return self._cache[tipo]

    def resolve(self, mencion: str, tipo: TipoEntidad) -> Entidad | None:
        clave = normalizar(mencion, tipo)
        if not clave:
            return None

        indice = self._indice(tipo)

        # 1 y 2: exacta o alias registrado.
        entidad = indice.get(clave)
        if entidad is not None:
            entidad.menciones += 1
            return entidad

        # 4: difuso conservador (3 ya esta cubierto porque las siglas se
        # guardaron como alias al crear la entidad).
        entidad = self._match_difuso(clave, indice)
        if entidad is not None:
            # La variante nueva se registra como alias para que la proxima vez
            # entre por el camino exacto, sin recalcular similitud.
            if clave not in (entidad.alias or []):
                entidad.alias = [*(entidad.alias or []), clave]
            entidad.menciones += 1
            indice[clave] = entidad
            return entidad

        return self._crear(mencion, clave, tipo, indice)

    def _match_difuso(self, clave: str, indice: dict[str, Entidad]) -> Entidad | None:
        if len(clave) < _MIN_LEN_DIFUSO:
            return None
        mejor, mejor_ratio = None, 0.0
        for candidato, entidad in indice.items():
            if len(candidato) < _MIN_LEN_DIFUSO:
                continue
            ratio = SequenceMatcher(None, clave, candidato).ratio()
            if ratio > mejor_ratio:
                mejor, mejor_ratio = entidad, ratio
        return mejor if mejor_ratio >= _UMBRAL_DIFUSO else None

    def _crear(
        self, mencion: str, clave: str, tipo: TipoEntidad, indice: dict[str, Entidad]
    ) -> Entidad:
        alias: list[str] = []
        siglas = derivar_siglas(clave)
        # Solo se registra la sigla si esta libre: si ya existe otra entidad
        # con esa sigla, dejarla apuntando a la primera evita fusionar dos
        # personas distintas por una coincidencia de iniciales.
        if siglas and siglas not in indice:
            alias.append(siglas)

        entidad = Entidad(
            tipo=tipo,
            nombre=mencion.strip(),
            nombre_normalizado=clave,
            alias=alias,
            menciones=1,
        )
        self._session.add(entidad)
        indice[clave] = entidad
        for a in alias:
            indice[a] = entidad
        return entidad
