"""Prompt, schema de respuesta y render de chunk compartidos entre adaptadores
de AIAnalysisProvider (OpenAI, Claude...). Un solo lugar de verdad para que
agregar un proveedor nuevo no implique duplicar ni desincronizar las reglas de
segmentacion -- ver AIAnalysisProvider en base.py para el contrato que todos
implementan."""
import json

from src.modules.ai.schemas import MatchedOn, NewsType, PerfilCliente, Word

_NEWS_TYPE_VALUES = [t.value for t in NewsType]

SYSTEM_PROMPT = """Eres un analista editorial que identifica noticias completas dentro \
de una transcripcion continua de radio o television en espanol.

Trabajas EXCLUSIVAMENTE por indice de palabra, nunca por segundos ni minutos.

Reglas:
- Cada noticia es un tema identificable con inicio y fin claros (ej: una nota sobre \
un evento, declaracion, accidente, decision de gobierno, etc.).
- EXCLUYE por completo y NO reportes como noticia, aunque ocupen mucho tiempo al aire: \
publicidad y anuncios comerciales (productos, servicios, clinicas, ofertas, numeros de \
contacto o direcciones para comprar); contenido religioso (predicas, sermones, invitaciones \
a cultos o iglesias, oraciones, musica cristiana); cortinas y musica; autopromocion de la \
emisora o del programa, concursos, sorteos, saludos, felicitaciones y horoscopos; y cualquier \
relleno sin valor noticioso.
- Solo cuenta como noticia el periodismo real: hechos, sucesos, declaraciones, eventos, \
decisiones de gobierno o de instituciones, entrevistas informativas. Ante la duda, si un \
segmento no es claramente una noticia periodistica, NO lo incluyas.
- No inventes informacion que no este en el texto.
- start_word y end_word son los indices (inclusive) de la primera y ultima palabra \
de la noticia, tomados literalmente de los indices que se te dan -- nunca los inventes \
ni los aproximes.
- summary: maximo 2 oraciones, solo lo que dice el texto.
- keywords: entre 5 y 10 palabras o frases cortas que resuman el contenido.
- news_type: elige la categoria que mejor calce -- una de: {news_types}. Usa "otro" \
si ninguna aplica bien.
- people/organizations/locations: solo menciones explicitas en el texto (personas, \
instituciones/empresas/partidos, lugares). Lista vacia si no hay ninguna.
- confidence es tu confianza (0.0 a 1.0) de que el rango detectado es una noticia \
completa y bien delimitada.
- NUNCA incluyas ni infieras programa, periodista, emisora, fecha ni hora -- esos datos \
vienen de la metadata del sistema, no del texto que estas leyendo.
- Si no hay ninguna noticia real en el texto (todo es relleno/publicidad), devuelve una \
lista vacia.""".format(news_types=", ".join(_NEWS_TYPE_VALUES))

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start_word": {"type": "integer"},
                    "end_word": {"type": "integer"},
                    "summary": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "news_type": {"type": "string", "enum": _NEWS_TYPE_VALUES},
                    "people": {"type": "array", "items": {"type": "string"}},
                    "organizations": {"type": "array", "items": {"type": "string"}},
                    "locations": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "title",
                    "start_word",
                    "end_word",
                    "summary",
                    "keywords",
                    "news_type",
                    "people",
                    "organizations",
                    "locations",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["news"],
    "additionalProperties": False,
}

MAX_KEYWORDS = 10


def render_chunk(chunk: list[Word]) -> str:
    return " ".join(f"{w.index}:{w.word}" for w in chunk)


# --------------------------------------------------------------------------
# Variante con deteccion de corte en el limite de chunk (boundary_status).
#
# `chunk_words` parte por indice de palabra sin solape, asi que una noticia
# que cruza el limite queda partida en dos NewsSegment que nadie reconcilia
# (ver src/modules/ai/chunking.py y src/modules/ai/stitching.py). Estas
# constantes son SEPARADAS de SYSTEM_PROMPT/RESPONSE_SCHEMA a proposito: el
# camino sincronico (Claude, primario en produccion) sigue usando las de
# arriba sin cambios. Solo el batch de OpenAI usa estas, que es donde se
# midio que funcionan -- ver docs/EXPERIMENTO_BOUNDARY_STATUS.md.
#
# El experimento tambien mostro que el chunk que EMPIEZA a mitad de una
# noticia no puede detectarlo solo (0/100 sin ayuda): necesita ver un
# adelanto del texto anterior. Al que TERMINA cortado no se le da contexto --
# darselo lo vuelve un sello de goma que responde "continua" casi siempre.

BOUNDARY_STATUS_VALUES = [
    "complete",
    "continues_after_chunk",
    "continues_before_chunk",
    "continues_both",
]

CONTEXTO_PALABRAS = 50

_BOUNDARY_INSTRUCCIONES = """

Ademas de lo anterior, para CADA noticia que reportes indica:
- boundary_status: uno de {boundary_values}.
  - "complete": la noticia empieza y termina dentro de este texto.
  - "continues_after_chunk": el texto se corta mientras la noticia seguia -- \
faltaba mas de esta nota despues del final de lo que estas leyendo.
  - "continues_before_chunk": el texto que estas leyendo empieza a mitad de \
una noticia que ya habia comenzado antes.
  - "continues_both": todo este texto es continuacion de una noticia larga \
que sigue tanto antes como despues.
  Para la gran mayoria de noticias (las que no tocan el principio o el final \
del texto) la respuesta correcta es "complete".
- boundary_confidence: tu confianza (0.0 a 1.0) en que boundary_status es \
correcto.

Vas a recibir dos bloques de texto:
1. CONTEXTO PREVIO: las ultimas palabras de lo que se dijo justo antes de lo \
que tienes que analizar. Es solo para que sepas si tu primer tema es \
continuacion de algo que ya habia empezado -- NUNCA reportes start_word ni \
end_word dentro de este bloque, no es tuyo, no lo repitas como noticia. \
Puede venir vacio si estas leyendo el principio de la transmision.
2. TEXTO A SEGMENTAR: esto es lo unico que debes convertir en noticias.

Si el primer tema de TEXTO A SEGMENTAR es claramente una continuacion de lo \
que ves en CONTEXTO PREVIO (el mismo asunto, sin una introduccion nueva), \
marca boundary_status="continues_before_chunk" en esa noticia.""".format(
    boundary_values=", ".join(BOUNDARY_STATUS_VALUES)
)

BOUNDARY_SYSTEM_PROMPT = SYSTEM_PROMPT + _BOUNDARY_INSTRUCCIONES

# Copia profunda para no mutar el schema de produccion.
BOUNDARY_RESPONSE_SCHEMA = json.loads(json.dumps(RESPONSE_SCHEMA))
_boundary_item = BOUNDARY_RESPONSE_SCHEMA["properties"]["news"]["items"]
_boundary_item["properties"]["boundary_status"] = {
    "type": "string",
    "enum": BOUNDARY_STATUS_VALUES,
}
_boundary_item["properties"]["boundary_confidence"] = {"type": "number"}
_boundary_item["required"] = _boundary_item["required"] + [
    "boundary_status",
    "boundary_confidence",
]


def render_chunk_con_contexto(chunk: list[Word], contexto: list[Word]) -> str:
    return (
        "CONTEXTO PREVIO (no es tuyo, no lo segmentes):\n"
        + (render_chunk(contexto) if contexto else "(ninguno -- este es el inicio)")
        + "\n\nTEXTO A SEGMENTAR:\n"
        + render_chunk(chunk)
    )


# --------------------------------------------------------------------------
# Relevancia por cliente, en la MISMA llamada que la segmentacion.
#
# Las instrucciones y los perfiles van en el mensaje `user`, no en el system:
# `BOUNDARY_SYSTEM_PROMPT` queda byte-identico al que se midio en 51% recall /
# 93% precision de stitching (ver src/modules/ai/stitching.py), asi que dar de
# alta un cliente nuevo no puede degradar la segmentacion -- solo cambia el
# mensaje de usuario. Esa separacion es el punto, no un detalle de estilo.
#
# Por que una sola llamada y no una segunda pasada sobre las noticias ya
# estructuradas: con pocos clientes sale mas barato (~544 vs ~1360 tokens por
# grabacion con 2 clientes; el cruce esta alrededor de 6 clientes), y sobre
# todo evita una SEGUNDA ventana de batch de hasta 24 h. **Revisar esta
# decision al pasar de ~6 clientes.**
#
# Solo se piden las entradas RELEVANTES, no una por cliente por noticia. Los
# tokens de salida cuestan ~4x los de entrada, y la mayoria de las noticias no
# le importan a nadie: emitir "no aplica" con su justificacion costaba ~1000
# tokens de salida por grabacion (2 clientes x ~10 noticias) contra ~150
# pidiendo solo lo relevante. La contra es que ya no se puede distinguir "no
# es relevante" de "el modelo lo paso por alto" -- se acepta a cambio del 85%
# de ahorro, y es lo que mide la corrida de control de 100 casos.

MATCHED_ON_VALUES = [m.value for m in MatchedOn]

_CLIENT_RELEVANCE_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "client_id": {"type": "string"},
            "relevant": {"type": "boolean"},
            "score": {"type": "number"},
            "rationale": {"type": "string"},
            "matched_on": {
                "type": "array",
                "items": {"type": "string", "enum": MATCHED_ON_VALUES},
            },
        },
        "required": ["client_id", "relevant", "score", "rationale", "matched_on"],
        "additionalProperties": False,
    },
}

BOUNDARY_CLIENT_RESPONSE_SCHEMA = json.loads(json.dumps(BOUNDARY_RESPONSE_SCHEMA))
_bc_item = BOUNDARY_CLIENT_RESPONSE_SCHEMA["properties"]["news"]["items"]
_bc_item["properties"]["client_relevance"] = _CLIENT_RELEVANCE_SCHEMA
_bc_item["required"] = _bc_item["required"] + ["client_relevance"]


def render_perfiles(perfiles: list[PerfilCliente]) -> str:
    """Bloque de perfiles + instrucciones de relevancia para el mensaje user.

    El formato es deliberadamente explicito sobre que NO alcanza con que
    aparezca el nombre: el valor de usar un LLM aca (en vez de un grep sobre
    las entidades ya extraidas) es cazar la noticia que le importa al cliente
    aunque no lo mencione."""
    lineas = [
        "PERFILES DE CLIENTE (para clasificar relevancia, no para segmentar):",
    ]
    for p in perfiles:
        lineas.append(f'- client_id: "{p.client_id}"  ({p.nombre})')
        if p.personas_interes:
            lineas.append(f"    personas de interes: {', '.join(p.personas_interes)}")
        if p.instituciones:
            lineas.append(f"    instituciones: {', '.join(p.instituciones)}")
        if p.temas:
            lineas.append(f"    temas: {', '.join(p.temas)}")

    lineas.append(
        """
Para CADA noticia devuelve `client_relevance` con una entrada **solo por los \
clientes a los que esa noticia les importa**. Si no le importa a ninguno, \
devuelve una lista vacia -- que es lo normal en la mayoria de las noticias. \
No incluyas entradas para decir que algo no es relevante.

- client_id: exactamente el de la lista de arriba.
- relevant: true.
- score: 0.0 a 1.0, que tan central es la noticia para ese cliente.
- rationale: maximo 15 palabras, citando lo que dice la noticia.
- matched_on: por que vias resulto relevante, una o varias de: {valores}.

**Lo normal es que una noticia no sea relevante para nadie.** De cada 100 \
noticias que salen al aire, tipicamente menos de 5 tienen relacion real con un \
cliente concreto. Si estas marcando muchas, algo estas haciendo mal.

La prueba para incluir una entrada: **la noticia tiene que ser sobre el \
cliente, o sobre algo que le pasa a el.** No sobre su pais, su gobierno, su \
sector, ni la politica en general.

NO es relevante (aunque el tema suene cercano):
- Que la noticia mencione una institucion del perfil haciendo algo que no \
tiene que ver con el cliente. Que el Congreso legisle sobre salud, que la \
fiscalia investigue otro caso, que un ministerio inaugure una obra: eso es \
la institucion actuando por su cuenta, no el cliente.
- Que hable de un tema general del pais -- economia, seguridad, tercera edad, \
servicios publicos -- aunque al cliente "le podria interesar".
- Que nombre a una persona del perfil hablando de un asunto ajeno al cliente.
- Una mencion de pasada: una lista, un saludo, una comparacion, un dato \
historico de contexto.

SI es relevante aunque no lo nombren:
- Un proceso judicial, investigacion o resolucion en el que el cliente es \
parte, aunque solo nombren al tribunal o a la fiscalia.
- Una decision que lo afecta directamente a el: su situacion legal, su cargo, \
su patrimonio, su partido cuando se habla de el.
- Declaraciones de terceros sobre el cliente.

**Reglas duras:**
- El rationale debe poder nombrar al cliente y decir que le pasa A EL. Si lo \
que escribiste no menciona al cliente, o solo dice "le podria interesar" o \
"afecta al pais", entonces no es relevante: no pongas la entrada.
- No uses una via en matched_on si esa lista viene vacia en el perfil. Si el \
cliente no tiene temas configurados, no existe matched_on "tema".
- score alto (>=0.8) solo cuando el cliente es el sujeto de la noticia. Si \
aparece como contexto secundario, no llega a 0.5 -- y por debajo de 0.5, \
mejor no la incluyas.""".format(valores=", ".join(MATCHED_ON_VALUES))
    )
    return "\n".join(lineas)


def render_chunk_con_contexto_y_perfiles(
    chunk: list[Word], contexto: list[Word], perfiles: list[PerfilCliente]
) -> str:
    return render_perfiles(perfiles) + "\n\n" + render_chunk_con_contexto(chunk, contexto)
