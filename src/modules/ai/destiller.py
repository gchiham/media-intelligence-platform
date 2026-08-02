"""Destiller: separa basura (publicidad, religioso, promos, musica, relleno) de
periodismo real, ANTES de segmentar. Ver docs/DESTILLER_PROPOSAL.md.

**Contrato de particion completa (no solo exclusiones).** El LLM no devuelve
"estos son los bloques de basura" sino una particion contigua que cubre TODAS
las palabras del chunk, cada bloque con un tipo. Es mas caro en tokens de
salida, y se hace a proposito: si el modelo solo reportara lo que considera
basura, al validarlo a mano se podria medir precision (de lo que marco, cuanto
era realmente basura) pero nunca recall -- la basura que no marco, y la
noticia que silenciosamente omitio, no aparecerian en ninguna parte para que un
humano las viera. Con particion completa el revisor cubre el 100% del aire y
los dos errores quedan visibles. Ver scripts/destiller_eval.py.

**Indices intactos.** Destiller nunca reescribe la transcripcion: emite rangos
sobre el indice original de `_words.json`. `map_words_to_time` y el Clipper
dependen de que `start_word`/`end_word` referencien ese indice; si se
renumerara, el Clipper cortaria el audio equivocado. Por eso `filtrar_words`
devuelve una sublista de los mismos objetos `Word` (con su `index` original),
no palabras renumeradas: el prompt de segmentacion ve huecos en la numeracion
y sigue devolviendo indices del espacio original sin enterarse de que hubo
exclusiones.
"""
import enum
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from openai import OpenAI
from pydantic import BaseModel, Field

from src.modules.ai.chunking import chunk_words
from src.modules.ai.exceptions import SegmentationError
from src.modules.ai.providers.base import AIAnalysisProvider
from src.modules.ai.providers.prompts import render_chunk
from src.modules.ai.schemas import NewsSegment, Word
from src.shared.errors import PermanentPipelineError, TransientPipelineError, classify_and_wrap

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = [1, 2]  # espera antes del intento 2 y del intento 3


class TipoBloque(str, enum.Enum):
    """Enum cerrado por el mismo motivo que NewsType: texto libre deriva en
    variantes que despues no se pueden contar ni calibrar."""

    NOTICIA = "noticia"
    PUBLICIDAD = "publicidad"
    RELIGIOSO = "religioso"
    PROMO = "promo"
    MUSICA = "musica"
    RELLENO = "relleno"
    # No lo produce el LLM: lo inserta `normalizar_particion` cuando el modelo
    # deja un hueco sin clasificar. Marcarlo explicitamente en vez de rellenar
    # con un tipo real evita que un fallo del modelo se lea como una decision
    # suya durante la validacion humana.
    DESCONOCIDO = "desconocido"


#: Todo lo que no es periodismo real. `DESCONOCIDO` queda fuera a proposito:
#: ante un hueco del modelo, la opcion segura es NO excluir.
#: Esto es CLASIFICACION: lo que el modelo reporta. No es lo que se excluye.
TIPOS_BASURA = frozenset(
    {
        TipoBloque.PUBLICIDAD,
        TipoBloque.RELIGIOSO,
        TipoBloque.PROMO,
        TipoBloque.MUSICA,
        TipoBloque.RELLENO,
    }
)

#: Lo que Destiller efectivamente saca del camino de la segmentacion. Es un
#: subconjunto de TIPOS_BASURA a proposito: clasificar y actuar son cosas
#: distintas. Seguir etiquetando `religioso` y `relleno` cuesta cero y sirve
#: para reportes y para el set de evaluacion; excluirlos es lo que se decidio
#: no hacer.
#:
#: Por que quedaron afuera, medido sobre 20 grabaciones y 3 modelos:
#: - `relleno`: los tres modelos NUNCA coinciden en el (0.0% del aire en
#:   unanimidad) y es la categoria mas disputada que existe -- gpt-4o-mini la
#:   usa para el 12.6% del aire, Qwen para el 1.3%. Excluirla era el mayor
#:   riesgo de perder periodismo y no aportaba nada.
#: - `religioso`: 2.0% del aire en unanimidad. Se cede a proposito: una prédica
#:   no es noticia, pero la postura de una iglesia sobre un tema publico si, y
#:   la etiqueta no distingue una de otra.
TIPOS_EXCLUIBLES = frozenset(
    {TipoBloque.PUBLICIDAD, TipoBloque.MUSICA, TipoBloque.PROMO}
)

#: `promo` necesita mas evidencia que las otras dos. Publicidad y musica son
#: inequivocas (los tres modelos coinciden en ellas mas que en nada); `promo`
#: es donde caen las aperturas de programa -- "buenas tardes amigos,
#: continuamos con el noticiero" -- que discuten si son autopromocion o el
#: arranque de la nota.
#:
#: **No se puede pedir "promo con alta confianza" usando la confianza que
#: declara el modelo.** Medido: en los bloques que Qwen marca `promo`, su
#: confianza es 0.898 cuando los otros dos coinciden y 0.885 cuando discrepan.
#: No separa nada. La unica evidencia que discrimina es el acuerdo entre
#: modelos -- ver `exclusiones_por_consenso`.
TIPOS_EXCLUIBLES_SIN_CONSENSO = frozenset(
    {TipoBloque.PUBLICIDAD, TipoBloque.MUSICA}
)


class BloqueDestilado(BaseModel):
    start_word: int
    end_word: int
    tipo: TipoBloque
    confidence: float = Field(ge=0.0, le=1.0)
    motivo: str = ""

    def es_basura(self) -> bool:
        return self.tipo in TIPOS_BASURA


SYSTEM_PROMPT = """Eres un clasificador de contenido de radio y television en espanol \
de Honduras. Recibes la transcripcion continua de una emision, donde cada palabra viene \
con su indice: "0:Buenas 1:tardes 2:amigos".

Tu tarea es partir ese texto en bloques CONTIGUOS que cubran TODAS las palabras que se \
te dan, y clasificar cada bloque. No estas resumiendo ni extrayendo noticias: estas \
separando lo que es periodismo real de lo que no lo es.

Tipos:
- noticia: periodismo real -- hechos, sucesos, declaraciones, eventos, decisiones de \
gobierno o de instituciones, entrevistas informativas, analisis de un hecho concreto.
- publicidad: anuncios comerciales -- productos, servicios, clinicas, ofertas, numeros \
de contacto o direcciones para comprar, patrocinios leidos al aire.
- religioso: predicas, sermones, invitaciones a cultos o iglesias, oraciones, musica \
cristiana.
- promo: autopromocion de la emisora o del programa, concursos, sorteos, saludos, \
felicitaciones, cumpleanos, horoscopos.
- musica: canciones y cortinas musicales sin contenido hablado relevante.
- relleno: charla sin valor noticioso -- bromas entre locutores, comentarios del clima \
sin dato, muletillas, transiciones vacias, texto ininteligible.

Reglas de particion (son estrictas, la salida se valida contra ellas):
- El primer bloque empieza EXACTAMENTE en el indice de la primera palabra que se te dio.
- El ultimo bloque termina EXACTAMENTE en el indice de la ultima palabra que se te dio.
- Cada bloque empieza en el end_word del bloque anterior mas 1. Sin huecos y sin traslapes.
- start_word y end_word se toman literalmente de los indices que se te dan; nunca los \
inventes ni los aproximes.
- No partas una misma noticia en varios bloques: un bloque de tipo noticia debe cubrir \
la nota completa de principio a fin.
- Bloques consecutivos del mismo tipo que pertenecen al mismo contenido van unidos en \
uno solo. Dos anuncios distintos seguidos si pueden ir separados.
- Ante la duda entre noticia y cualquier otro tipo, elige noticia y baja el confidence. \
Dejar pasar publicidad cuesta unos centavos; descartar una noticia real pierde el \
producto del cliente.
- confidence es tu confianza (0.0 a 1.0) en la clasificacion de ESE bloque.
- motivo: maximo 12 palabras explicando por que ese tipo (ej. "menciona telefono y \
direccion para comprar")."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "bloques": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_word": {"type": "integer"},
                    "end_word": {"type": "integer"},
                    "tipo": {
                        "type": "string",
                        "enum": [t.value for t in TipoBloque if t != TipoBloque.DESCONOCIDO],
                    },
                    "confidence": {"type": "number"},
                    "motivo": {"type": "string"},
                },
                "required": ["start_word", "end_word", "tipo", "confidence", "motivo"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["bloques"],
    "additionalProperties": False,
}


def normalizar_particion(
    bloques: list[BloqueDestilado], lo: int, hi: int
) -> list[BloqueDestilado]:
    """Fuerza que la salida del LLM sea una particion real de [lo, hi].

    El modelo se equivoca de formas predecibles: traslapa dos bloques, deja un
    hueco, o se sale del rango que vio. En vez de descartar la respuesta entera
    (perderiamos la clasificacion util del resto del chunk), se repara de forma
    determinista: recortar al rango, truncar traslapes contra el bloque previo,
    y tapar huecos con DESCONOCIDO. La garantia de cobertura total no puede
    depender de que el LLM se porte bien -- es lo que hace medible el recall.
    """
    if lo > hi:
        return []

    ordenados = sorted(bloques, key=lambda b: (b.start_word, b.end_word))
    salida: list[BloqueDestilado] = []
    cursor = lo

    for bloque in ordenados:
        inicio = max(bloque.start_word, cursor)
        fin = min(bloque.end_word, hi)
        if inicio > fin:
            # Enteramente fuera de rango, o ya cubierto por un bloque previo.
            continue
        if inicio > cursor:
            salida.append(
                BloqueDestilado(
                    start_word=cursor,
                    end_word=inicio - 1,
                    tipo=TipoBloque.DESCONOCIDO,
                    confidence=0.0,
                    motivo="hueco no clasificado por el modelo",
                )
            )
        salida.append(bloque.model_copy(update={"start_word": inicio, "end_word": fin}))
        cursor = fin + 1
        if cursor > hi:
            break

    if cursor <= hi:
        salida.append(
            BloqueDestilado(
                start_word=cursor,
                end_word=hi,
                tipo=TipoBloque.DESCONOCIDO,
                confidence=0.0,
                motivo="hueco no clasificado por el modelo",
            )
        )
    return salida


def exclusiones(
    bloques: list[BloqueDestilado],
    umbral: float,
    tipos: frozenset[TipoBloque] = TIPOS_EXCLUIBLES_SIN_CONSENSO,
) -> list[BloqueDestilado]:
    """Que sacar del camino cuando hay UN solo modelo.

    Por defecto solo publicidad y musica: `promo` exige acuerdo entre modelos
    (ver `exclusiones_por_consenso`) porque ahi caen las aperturas de programa,
    y con un solo modelo no hay como distinguir la autopromocion del arranque
    de una nota.

    El `umbral` se conserva por compatibilidad, pero **la evidencia dice que no
    sirve para decidir**: la confianza de Qwen es 0.94 cuando coincide con los
    otros dos modelos y 0.92 cuando queda solo contra ambos. No discrimina. La
    señal que si funciona es el consenso, no el numero que el modelo se pone a
    si mismo.
    """
    return [b for b in bloques if b.tipo in tipos and b.confidence >= umbral]


def exclusiones_por_consenso(
    por_modelo: list[list[BloqueDestilado]],
    tipos: frozenset[TipoBloque] = TIPOS_EXCLUIBLES,
    minimo: int = 2,
) -> list[tuple[int, int]]:
    """Rangos de palabra que al menos `minimo` modelos coinciden en excluir.

    Devuelve rangos y no bloques a proposito: cada modelo parte la transmision
    en bloques distintos (sobre las mismas 20 grabaciones, Qwen produjo 815 y
    gpt-4o-mini 662), asi que el consenso solo se puede expresar sobre la unidad
    que todos comparten, que es la palabra.

    Esta es la regla que reemplaza al umbral de confianza. Medido: excluye el
    11.8% del aire con unanimidad de 3 y 13.2% con acuerdo de 2, contra 29-36%
    que excluiria un modelo solo -- y esos ~20 puntos de diferencia son
    exactamente la zona en disputa, o sea donde puede haber periodismo.
    """
    votos: dict[int, int] = {}
    for bloques in por_modelo:
        for b in bloques:
            if b.tipo in tipos:
                for i in range(b.start_word, b.end_word + 1):
                    votos[i] = votos.get(i, 0) + 1

    excluidas = sorted(i for i, n in votos.items() if n >= minimo)
    if not excluidas:
        return []

    rangos: list[tuple[int, int]] = []
    inicio = anterior = excluidas[0]
    for i in excluidas[1:]:
        if i == anterior + 1:
            anterior = i
            continue
        rangos.append((inicio, anterior))
        inicio = anterior = i
    rangos.append((inicio, anterior))
    return rangos


def filtrar_words(
    words: list[Word], a_excluir: list[BloqueDestilado] | list[tuple[int, int]]
) -> list[Word]:
    """Vista filtrada del transcript, conservando el `index` original de cada
    palabra (ver nota de indices intactos en el docstring del modulo).

    Acepta bloques o rangos crudos, que es lo que devuelve
    `exclusiones_por_consenso` -- el consenso no tiene un bloque propio porque
    ningun modelo lo delimito.
    """
    if not a_excluir:
        return words
    rangos = [
        b if isinstance(b, tuple) else (b.start_word, b.end_word) for b in a_excluir
    ]
    return [w for w in words if not any(lo <= w.index <= hi for lo, hi in rangos)]


@dataclass
class Uso:
    """Consumo acumulado de una corrida, para poder comparar costo entre
    modelos sobre el mismo material. Se acumula bajo lock porque los chunks
    salen concurrentes."""

    llamadas: int = 0
    tokens_entrada: int = 0
    tokens_salida: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def sumar(self, entrada: int, salida: int) -> None:
        with self._lock:
            self.llamadas += 1
            self.tokens_entrada += entrada
            self.tokens_salida += salida


class Destiller:
    """Adaptador del paso de destilado. Mismo patron de reintentos que
    OpenAIAnalysisProvider (ver ese archivo para el detalle) porque falla por
    los mismos motivos: rate limits y blips de red.

    **Sirve tanto OpenAI como un modelo propio.** Con `base_url` apunta a un
    servidor vLLM (la instancia `destiller`, ver docs/INFRASTRUCTURE.md), que
    expone la misma API de OpenAI -- por eso el cliente es el mismo y lo unico
    que cambia es como se pide la salida estructurada: OpenAI usa
    `response_format: json_schema` con `strict`, vLLM usa `guided_json`
    (decodificacion restringida por gramatica). Las dos garantizan JSON valido
    contra el mismo esquema; el modelo propio no es un modo degradado.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        chunk_size: int = 600,
        base_url: str | None = None,
        concurrencia: int = 8,
    ):
        self._client = OpenAI(api_key=api_key or "sk-no-aplica", base_url=base_url)
        self._model = model
        self._chunk_size = chunk_size
        self._propio = base_url is not None
        self._concurrencia = max(1, concurrencia)
        self.uso = Uso()

    def destilar(self, words: list[Word]) -> list[BloqueDestilado]:
        """Los chunks son independientes entre si -- cada uno se clasifica solo
        con lo que contiene -- asi que se mandan en paralelo.

        No es una micro-optimizacion: medido secuencialmente contra la instancia
        propia daban ~12 tokens/s, muy por debajo de lo que rinde una L4 con un
        7B. La causa es que un chunk a la vez es batch=1, y en decode el costo
        de leer los pesos del modelo (15 GB) se paga por token generado sin
        importar cuantas secuencias haya en vuelo. Mandando varios a la vez, el
        continuous batching de vLLM amortiza esa lectura entre todos.

        `executor.map` conserva el orden de entrada, asi que los bloques salen
        ordenados por indice de palabra sin tener que reordenarlos despues.
        """
        chunks = [c for c in chunk_words(words, self._chunk_size) if c]
        if not chunks:
            return []
        if self._concurrencia == 1:
            return [b for chunk in chunks for b in self._destilar_chunk(chunk)]

        with ThreadPoolExecutor(max_workers=self._concurrencia) as pool:
            por_chunk = list(pool.map(self._destilar_chunk, chunks))
        return [b for bloques in por_chunk for b in bloques]

    def _destilar_chunk(self, chunk: list[Word]) -> list[BloqueDestilado]:
        lo, hi = chunk[0].index, chunk[-1].index
        raw = self._call_with_retry(chunk)
        crudos = [BloqueDestilado.model_validate(item) for item in raw["bloques"]]
        return normalizar_particion(crudos, lo, hi)

    def _formato(self) -> dict:
        formato = {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "destilado", "schema": RESPONSE_SCHEMA, "strict": True},
            }
        }
        if self._propio:
            # `guided_json` NO sirve contra vLLM 0.26: acepta el parametro sin
            # error y lo ignora -- medido contra la instancia, devolvio JSON
            # valido pero con la clave inventada ("bloque" en vez de "bloques"),
            # que revienta despues como KeyError. El `response_format` de OpenAI
            # si lo respeta, asi que ambos proveedores usan exactamente el mismo
            # camino y no hay una variante que se pruebe menos que la otra.
            #
            # `max_tokens` solo hace falta aca porque vLLM no lo infiere: el
            # chunk de 600 palabras se renderiza como "1234:palabra" (~6 tokens
            # por palabra), asi que el prompt ya ronda 4.5k tokens y la salida
            # tiene que caber en la ventana de 16384. Una particion completa son
            # ~25 bloques de ~45 tokens, muy por debajo de este techo.
            formato["max_tokens"] = 3072
        return formato

    def _call_with_retry(self, chunk: list[Word]) -> dict:
        last_error: TransientPipelineError | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": render_chunk(chunk)},
                    ],
                    **self._formato(),
                )
                if response.usage is not None:
                    self.uso.sumar(
                        response.usage.prompt_tokens, response.usage.completion_tokens
                    )
                return json.loads(response.choices[0].message.content)
            except Exception as exc:
                error = classify_and_wrap(exc, module="destiller")
                if isinstance(error, PermanentPipelineError):
                    raise SegmentationError(str(error)) from error
                last_error = error
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(_BACKOFF_SECONDS[attempt - 1])

        raise SegmentationError(
            f"agotados {_MAX_ATTEMPTS} intentos de destilado contra OpenAI: {last_error}"
        ) from last_error


class DestilledAnalysisProvider(AIAnalysisProvider):
    """Compone Destiller + un AIAnalysisProvider: destila primero, segmenta
    despues sobre la vista filtrada. Implementa el mismo puerto, asi que el
    orquestador no distingue un proveedor destilado de uno normal (mismo patron
    que AIProviderWithFallback).

    **Fail-open a proposito:** si el destilado falla, se segmenta el transcript
    completo. El prompt de segmentacion ya excluye publicidad y contenido
    religioso por su cuenta (commit 7f4f5b0), asi que sigue habiendo red de
    seguridad; detener la grabacion por un timeout de un paso opcional seria
    peor que procesarla como se procesa hoy.

    NO esta conectado en src/api/deps.py todavia: `umbral` no tiene valor
    correcto conocido hasta que salga de la validacion humana. Ver
    docs/DESTILLER_PROPOSAL.md.
    """

    def __init__(self, destiller: Destiller, inner: AIAnalysisProvider, umbral: float):
        self._destiller = destiller
        self._inner = inner
        self._umbral = umbral

    def segment_news(self, words: list[Word]) -> list[NewsSegment]:
        try:
            bloques = self._destiller.destilar(words)
        except SegmentationError:
            return self._inner.segment_news(words)

        filtradas = filtrar_words(words, exclusiones(bloques, self._umbral))
        if not filtradas:
            # Toda la grabacion era basura: no hay nada que segmentar y
            # mandar una lista vacia al proveedor no aporta.
            return []
        return self._inner.segment_news(filtradas)
