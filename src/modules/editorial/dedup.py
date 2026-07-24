"""Agrupa apariciones de la misma noticia en distintas emisoras bajo una
`Historia` (ver el modelo para el hallazgo que lo motiva).

**Por que semantico y no por titulo exacto.** Medido en produccion sobre 145
grabaciones: el mismo evento aparecia como "298 alcaldes recibiran kit de
maquinaria para mejorar carreteras" (5 veces) y "Entrega de maquinaria a
alcaldes para mejorar carreteras" (3 veces) -- 8 apariciones, dos titulos. Un
`GROUP BY titulo` las cuenta como dos noticias distintas y el periodista las
cura por separado. Lo mismo con "Dia Internacional del Rock" / "Dia Mundial
del Rock" y con las dos redacciones del Francia vs España.

**Ventana temporal, no historico completo.** Solo se compara contra historias
tocadas en las ultimas `ventana_horas`. Dos razones: (a) el costo de comparar
crece con el historico y no aporta -- una nota de hoy no agrupa con una de
hace tres meses aunque se parezca; (b) un tema recurrente ("rebaja de
combustibles", que pasa cada semana) debe generar una historia nueva cada vez,
no engordar una sola historia eterna.

**El umbral hay que calibrarlo.** El default es un punto de partida razonable,
no un valor validado con estas 15 emisoras. Muy alto => la misma historia
queda dividida; muy bajo => se fusionan noticias distintas del mismo tema, que
es el error peor porque es silencioso. Ver `calibrar()` mas abajo.
"""
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.modules.ai.embeddings import EmbeddingProvider
from src.modules.editorial.models import Historia

# Punto de partida, NO validado con datos de estas emisoras -- ver docstring.
UMBRAL_SIMILITUD = 0.83
VENTANA_HORAS = 48


def coseno(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    producto = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return producto / (na * nb)


def texto_para_embedding(titulo: str, resumen: str) -> str:
    """El titulo pesa mas que el resumen para discriminar historias distintas,
    asi que va primero y sin diluir. Se recorta el resumen para que una nota
    larga no ahogue la señal del titular."""
    return f"{titulo.strip()}. {resumen.strip()[:400]}"


@dataclass
class Asignacion:
    historia: Historia
    es_nueva: bool
    similitud: float


class HistoriaClusterer:
    def __init__(
        self,
        session: Session,
        embeddings: EmbeddingProvider,
        umbral: float = UMBRAL_SIMILITUD,
        ventana_horas: int = VENTANA_HORAS,
    ):
        self._session = session
        self._embeddings = embeddings
        self._umbral = umbral
        self._ventana_horas = ventana_horas

    def _candidatas(self, momento: datetime) -> list[Historia]:
        desde = momento - timedelta(hours=self._ventana_horas)
        return list(
            self._session.scalars(
                select(Historia).where(Historia.ultima_aparicion >= desde)
            )
        )

    def asignar(
        self,
        titulo: str,
        resumen: str,
        momento: datetime,
        medio_id: uuid.UUID,
        embedding: list[float] | None = None,
    ) -> Asignacion:
        """Ubica esta aparicion en una Historia existente o crea una nueva.

        `embedding` se puede pasar precalculado para poder embeber en lote
        (una sola llamada a la API por N noticias) en vez de una por una.
        """
        if embedding is None:
            embedding = self._embeddings.embed([texto_para_embedding(titulo, resumen)])[0]

        mejor: Historia | None = None
        mejor_sim = 0.0
        for candidata in self._candidatas(momento):
            sim = coseno(embedding, candidata.embedding)
            if sim > mejor_sim:
                mejor, mejor_sim = candidata, sim

        if mejor is not None and mejor_sim >= self._umbral:
            self._actualizar(mejor, embedding, momento, medio_id)
            return Asignacion(historia=mejor, es_nueva=False, similitud=mejor_sim)

        historia = Historia(
            titulo_canonico=titulo.strip()[:500],
            embedding=embedding,
            primera_aparicion=momento,
            ultima_aparicion=momento,
            total_apariciones=1,
            medios_distintos=1,
        )
        self._session.add(historia)
        # El id se necesita para el FK de Noticia antes del commit final.
        self._session.flush()
        return Asignacion(historia=historia, es_nueva=True, similitud=mejor_sim)

    def _actualizar(
        self, historia: Historia, embedding: list[float], momento: datetime, medio_id: uuid.UUID
    ) -> None:
        n = historia.total_apariciones
        # Centroide incremental: el embedding de la historia es el promedio de
        # sus apariciones, no el de la primera. Asi una historia que arranco
        # con un titular pobre se va corrigiendo con las emisiones siguientes.
        historia.embedding = [
            (viejo * n + nuevo) / (n + 1) for viejo, nuevo in zip(historia.embedding, embedding)
        ]
        historia.total_apariciones = n + 1
        if momento > historia.ultima_aparicion:
            historia.ultima_aparicion = momento
        if momento < historia.primera_aparicion:
            historia.primera_aparicion = momento

    def calibrar(self, pares: list[tuple[str, str, bool]]) -> dict[float, dict]:
        """Ayuda para elegir el umbral con datos reales.

        `pares` son (texto_a, texto_b, son_la_misma_historia) etiquetados a
        mano. Devuelve, por umbral candidato, cuantos aciertos y errores da --
        para poder elegir con evidencia en vez de con el default de arriba.
        """
        vectores = self._embeddings.embed([t for par in pares for t in (par[0], par[1])])
        resultados: dict[float, dict] = {}
        for umbral in [0.75, 0.78, 0.80, 0.83, 0.85, 0.88, 0.90, 0.93]:
            vp = fp = vn = fn = 0
            for i, (_, _, misma) in enumerate(pares):
                sim = coseno(vectores[2 * i], vectores[2 * i + 1])
                predicho = sim >= umbral
                if predicho and misma:
                    vp += 1
                elif predicho and not misma:
                    fp += 1
                elif not predicho and misma:
                    fn += 1
                else:
                    vn += 1
            resultados[umbral] = {
                "verdaderos_positivos": vp,
                "falsos_positivos": fp,
                "verdaderos_negativos": vn,
                "falsos_negativos": fn,
            }
        return resultados
