"""Extraccion de notas de un periodico en PDF, por la Batch API de OpenAI.

Dos pasadas separadas (extraer, despues traducir) y no una sola que haga las
dos cosas, aunque una sola ahorraria ~15K tokens de entrada por edicion:

1. El riesgo real es el truncamiento. Extraer 24 notas ya consumio 11.234
   tokens de salida en la medicion; pedir tambien el ingles lo duplica y
   cuando trunca se pierden las ultimas notas Y la extraccion, no solo la
   traduccion.
2. Traducir es tarea facil y se puede hacer con un modelo mas barato. Meterla
   en la llamada de extraccion obliga a pagar el modelo caro para las dos.
3. Reintentar la traduccion no debe volver a pagar la extraccion.

El batch_id y a que edicion corresponde cada request se guardan en S3 y no en
una tabla nueva: es un dato de proceso, efimero, que solo vive entre el envio y
la recoleccion.
"""
import json
from dataclasses import dataclass

# Version del prompt. Se guarda en `ediciones.prompt_version` para poder
# re-extraer selectivamente solo lo que salio con una version vieja.
PROMPT_VERSION = "v1"

MODELO_EXTRACCION = "gpt-5-mini"
# Medido sobre los mismos 2 periodicos: gpt-4.1-mini encontro 15 notas donde
# gpt-5-mini encontro 24, y las 9 que ignoro eran reales (internacionales,
# empleo, columnas). No es que las resumiera: no las vio.
MODELO_TRADUCCION = "gpt-5.6-terra"
# Terra, Luna y Sol dieron calidad indistinguible traduciendo; Terra gasto 30%
# menos tokens de salida y fue el mas rapido.

MAX_SALIDA = 16000

PROMPT = """Eres un analista de medios hondureno. Recibis una edicion completa de
un periodico, en markdown, con los titulares marcados como `##` y el numero de
pagina en comentarios `<!-- pagina N -->`.

Extrae TODAS las notas periodisticas.

Reglas:
- Una nota = un titular con su cuerpo. Si una nota continua en otra pagina,
  unila en un solo articulo y pone las dos paginas en `paginas`.
- Ignora publicidad, clasificados, horoscopos, sudokus, carteleras y avisos.
- `cuerpo`: el texto de la nota, limpio, sin pies de foto sueltos.
- `sumario`: 1-2 oraciones tuyas resumiendo la nota.
- `paginas`: los numeros de pagina donde aparece, en orden.
- `seccion`: la seccion del diario si se puede determinar (Nacionales,
  Sucesos, Deportes, Internacionales...), null si no.
"""

ESQUEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "extraccion_periodico",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["notas"],
            "properties": {
                "notas": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["titulo", "sumario", "cuerpo", "seccion", "paginas"],
                        "properties": {
                            "titulo": {"type": "string"},
                            "sumario": {"type": "string"},
                            "cuerpo": {"type": "string"},
                            "seccion": {"type": ["string", "null"]},
                            "paginas": {"type": "array", "items": {"type": "integer"}},
                        },
                    },
                }
            },
        },
    },
}

PROMPT_TRADUCCION = """Traduce al ingles esta nota de un periodico hondureno.
Manten los nombres propios, cargos e instituciones con la forma usual en ingles
periodistico. No resumas ni omitas: es una traduccion completa."""

ESQUEMA_TRADUCCION = {
    "type": "json_schema",
    "json_schema": {
        "name": "traduccion",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["title", "summary", "body"],
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "body": {"type": "string"},
            },
        },
    },
}


@dataclass(frozen=True)
class Peticion:
    custom_id: str
    cuerpo: dict


def _cuerpo(modelo: str, mensajes: list, esquema: dict) -> dict:
    cuerpo = {
        "model": modelo,
        "messages": mensajes,
        "max_completion_tokens": MAX_SALIDA,
        "response_format": esquema,
    }
    # Los gpt-5* no aceptan temperature != 1 y rechazan la request entera; en
    # los 4.1 si conviene bajarla, que esto es extraccion y no redaccion.
    if not modelo.startswith("gpt-5"):
        cuerpo["temperature"] = 0
    return cuerpo


def peticion_extraccion(edicion_id: str, markdown: str) -> Peticion:
    return Peticion(
        custom_id=f"ext|{edicion_id}",
        cuerpo=_cuerpo(
            MODELO_EXTRACCION,
            [{"role": "user", "content": PROMPT + "\n\n---\n\n" + markdown}],
            ESQUEMA,
        ),
    )


def peticion_traduccion(nota_id: str, titulo: str, cuerpo: str) -> Peticion:
    return Peticion(
        custom_id=f"trad|{nota_id}",
        cuerpo=_cuerpo(
            MODELO_TRADUCCION,
            [{"role": "user", "content":
              f"{PROMPT_TRADUCCION}\n\nTITULAR: {titulo}\n\nCUERPO:\n{cuerpo}"}],
            ESQUEMA_TRADUCCION,
        ),
    )


def a_jsonl(peticiones: list[Peticion]) -> bytes:
    return ("\n".join(
        json.dumps({
            "custom_id": p.custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": p.cuerpo,
        }, ensure_ascii=False)
        for p in peticiones
    ) + "\n").encode("utf-8")
