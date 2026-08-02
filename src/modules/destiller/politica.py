"""Politica de exclusion: que se saca del camino de la segmentacion.

**Esta es la pieza que separa clasificar de actuar.** Los modelos clasifican
todo el aire en siete categorias y eso no cambia; la politica decide, sobre esa
clasificacion ya guardada, que tramos se excluyen. La consecuencia arquitectonica
es la que importa: **cambiar la politica nunca obliga a volver a correr los
modelos.** Se relee lo que ya esta en S3 y se recalcula, en segundos y sin costo
de tokens.

Esa separacion no es estetica. Antes, "ser basura" y "ser excluido" eran la
misma condicion (`b.es_basura()`), asi que ajustar el criterio de exclusion
significaba tocar el enum del dominio, y evaluar una politica alternativa
significaba volver a destilar. Con esto, probar cinco politicas sobre las 20
grabaciones cuesta cero.

**V1 es deliberadamente conservadora.** El objetivo declarado del proyecto ya no
es maximizar aire excluido sino minimizar el riesgo de perder periodismo, asi
que la politica por defecto exige unanimidad y deja fuera las categorias que la
medicion mostro como disputadas.
"""
from dataclasses import dataclass, field

from src.modules.ai.destiller import TipoBloque

#: Categorias que V1 considera candidatas a excluir. Que una categoria este aca
#: NO significa que se excluya: ademas tiene que cumplir el consenso.
#:
#: Por que estas tres y no las cinco de `TIPOS_BASURA`, medido sobre 20
#: grabaciones y 3 modelos (77,006 palabras comparables):
#: - `relleno` FUERA: los tres modelos nunca coinciden en el (0.0% del aire en
#:   unanimidad) y es la categoria mas disputada -- gpt-4o-mini la usa para el
#:   12.6% del aire y Qwen para el 1.3%. Excluirla era el mayor riesgo de
#:   perder periodismo sin ganar nada.
#: - `religioso` FUERA: 2.0% del aire en unanimidad. Se cede a proposito porque
#:   la etiqueta no distingue una prédica de la postura publica de una iglesia,
#:   y lo segundo si es noticia.
EXCLUIBLES_V1 = frozenset(
    {TipoBloque.PUBLICIDAD, TipoBloque.MUSICA, TipoBloque.PROMO}
)

#: Publicidad y musica solas. Es el piso: son las categorias donde los modelos
#: mas coinciden. Excluye 4.0% del aire con unanimidad de 3.
EXCLUIBLES_MINIMAS = frozenset({TipoBloque.PUBLICIDAD, TipoBloque.MUSICA})


@dataclass(frozen=True)
class PoliticaExclusion:
    """Que excluir y con cuanta evidencia.

    `umbral_confianza` existe por compatibilidad y su default es 0.0 a
    proposito. **La confianza que el modelo se declara a si mismo no
    discrimina**: medido, Qwen reporta 0.94 cuando coincide con los otros dos
    modelos y 0.92 cuando queda solo contra ambos; y en los bloques que marca
    `promo`, 0.898 contra 0.885. Filtrar por ese numero da una falsa sensacion
    de control. La evidencia que si separa es `minimo_modelos`.
    """

    nombre: str
    tipos: frozenset[TipoBloque] = EXCLUIBLES_V1
    #: Cuantos modelos tienen que coincidir en que una palabra es excluible.
    #: Con 1 se vuelve al comportamiento de un solo modelo, que excluye 29-36%
    #: del aire -- y los ~20 puntos de diferencia contra la unanimidad son
    #: exactamente la zona en disputa, donde puede haber periodismo.
    minimo_modelos: int = 3
    umbral_confianza: float = 0.0
    motivo: str = ""

    def rangos(self, por_modelo: list[list]) -> list[tuple[int, int]]:
        """Rangos de palabra a excluir, sobre el indice original.

        Trabaja por palabra y no por bloque porque cada modelo parte la
        transmision donde quiere (sobre las mismas 20 grabaciones, Qwen produjo
        815 bloques y gpt-4o-mini 662): la palabra es la unica unidad que todos
        comparten.
        """
        if self.minimo_modelos > len(por_modelo):
            return []  # no hay evidencia suficiente disponible; no se excluye nada

        votos: dict[int, int] = {}
        for bloques in por_modelo:
            for b in bloques:
                tipo = b.tipo if hasattr(b, "tipo") else TipoBloque(b["tipo"])
                conf = b.confidence if hasattr(b, "confidence") else b["confidence"]
                if tipo not in self.tipos or conf < self.umbral_confianza:
                    continue
                lo = b.start_word if hasattr(b, "start_word") else b["start_word"]
                hi = b.end_word if hasattr(b, "end_word") else b["end_word"]
                for i in range(lo, hi + 1):
                    votos[i] = votos.get(i, 0) + 1

        elegidas = sorted(i for i, n in votos.items() if n >= self.minimo_modelos)
        return _a_rangos(elegidas)


def _a_rangos(indices: list[int]) -> list[tuple[int, int]]:
    if not indices:
        return []
    rangos = []
    inicio = anterior = indices[0]
    for i in indices[1:]:
        if i == anterior + 1:
            anterior = i
            continue
        rangos.append((inicio, anterior))
        inicio = anterior = i
    rangos.append((inicio, anterior))
    return rangos


#: La politica vigente de V1. Excluye 11.8% del aire sobre el set de evaluacion.
#: NO esta conectada al pipeline: falta el FPR del estrato de consenso.
CONSERVADORA_V1 = PoliticaExclusion(
    nombre="conservadora-v1",
    tipos=EXCLUIBLES_V1,
    minimo_modelos=3,
    motivo="unanimidad de 3 modelos sobre publicidad, musica y promo",
)

#: Piso absoluto: solo lo inequivoco. Util si el FPR de la V1 sale alto.
MINIMA = PoliticaExclusion(
    nombre="minima",
    tipos=EXCLUIBLES_MINIMAS,
    minimo_modelos=3,
    motivo="unanimidad de 3 sobre publicidad y musica unicamente",
)

#: Solo para comparar en el analisis -- NO usar en produccion. Es la politica
#: que tenia el diseño original (un modelo decide solo) y excluye 29-36% del
#: aire, con el riesgo concentrado en la zona en disputa.
UN_SOLO_MODELO = PoliticaExclusion(
    nombre="un-solo-modelo",
    tipos=EXCLUIBLES_V1,
    minimo_modelos=1,
    motivo="referencia historica, no apta para produccion",
)

CATALOGO = {p.nombre: p for p in (CONSERVADORA_V1, MINIMA, UN_SOLO_MODELO)}
