"""iSearch: busqueda inteligente sobre noticias (GET /news/search). Dos
etapas -- ver NoticiaRepository.buscar_candidatos para la primera (prefiltro
barato en Postgres). Este modulo es la segunda: el LLM razona solo sobre los
candidatos que ya pasaron el filtro (nunca sobre el universo completo), y
explica por que cada uno es relevante -- tolera variantes, alias y errores
ortograficos que el prefiltro por si solo no resuelve.
"""
import json

from anthropic import Anthropic

_TOOL_NAME = "return_search_results"

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "id de la noticia, tal como se recibio"},
                    "score": {
                        "type": "integer",
                        "description": "0-100, que tan relevante es esta noticia para la busqueda",
                    },
                    "why": {
                        "type": "string",
                        "description": "explicacion breve (menos de 20 palabras) en español, basada solo en el texto dado",
                    },
                },
                "required": ["id", "score", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """Sos el motor de busqueda de un dashboard de monitoreo de noticias de Honduras.

Te dan una consulta de un usuario y una lista de noticias candidatas (ya \
prefiltradas). Tu trabajo es decidir cuales son realmente relevantes para \
esa consulta, incluso si el texto exacto de la consulta no aparece -- \
reconoce personas por variantes/alias/apodos/errores ortograficos (ej. \
"Juan Orlando" debe matchear "JOH", "Juan Orlando Hernandez", "el \
expresidente"), e instituciones por sus dependencias/temas relacionados \
(ej. "Secretaria de Seguridad" debe matchear noticias de Policia Nacional, \
capturas, operativos, narcotrafico, el ministro de turno, etc., aunque \
nunca diga "Secretaria de Seguridad" textualmente).

Para cada noticia relevante (score >= 40) devolve un score 0-100 y una \
explicacion breve. Basa la explicacion UNICAMENTE en el titulo/resumen que \
se te dio -- nunca inventes datos, acusaciones o nombres que no esten ahi. \
Si ninguna noticia es relevante, devolve una lista vacia."""


def rerank_candidatos(
    query: str,
    candidatos: list[dict],
    api_key: str,
    model: str,
    min_score: int = 40,
) -> list[dict]:
    """candidatos: dicts con al menos id/titulo/resumen/medio_nombre/created_at.
    Devuelve una lista de {id, score, why}, score>=min_score, orden desc."""
    if not candidatos:
        return []

    items = [
        {
            "id": str(c["id"]),
            "titulo": c["titulo"] or "",
            "resumen": (c["resumen"] or "")[:400],
            "medio": c.get("medio_nombre") or "",
            "keywords": (c.get("metadatos_ia") or {}).get("keywords", [])[:8]
            if isinstance(c.get("metadatos_ia"), dict)
            else [],
        }
        for c in candidatos
    ]

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f'Consulta: "{query}"\n\nNoticias candidatas:\n{json.dumps(items, ensure_ascii=False)}',
            }
        ],
        tools=[
            {
                "name": _TOOL_NAME,
                "description": "Devuelve las noticias relevantes con su score y explicacion.",
                "input_schema": _RESPONSE_SCHEMA,
                "strict": True,
            }
        ],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
    )

    results = []
    for block in response.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            results = block.input.get("results", [])
            break

    candidato_ids = {str(c["id"]) for c in candidatos}
    filtrados = [
        r for r in results if r.get("score", 0) >= min_score and r.get("id") in candidato_ids
    ]
    filtrados.sort(key=lambda r: r["score"], reverse=True)
    return filtrados
