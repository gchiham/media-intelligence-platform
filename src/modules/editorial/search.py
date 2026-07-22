"""Expansion de query para GET /news/search -- ver src/api/routers/editorial.py.
Solo se llama cuando el ILIKE plano ya devolvio pocos resultados (ver
_EXPAND_THRESHOLD alli), asi que la mayoria de busquedas nunca tocan esto.
La llamada al LLM manda solo la query del usuario (nunca las noticias), asi
que el costo no depende del tamaño del corpus.
"""
import time

from anthropic import Anthropic

_TOOL_NAME = "return_search_terms"

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "terminos": {
            "type": "array",
            "items": {"type": "string"},
            "description": "hasta 8 variantes/alias/errores ortograficos/terminos institucionales relacionados",
        }
    },
    "required": ["terminos"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """Ayudas a expandir busquedas de un dashboard de noticias de Honduras.

Dado un termino de busqueda, devolve hasta 8 variantes cortas que ayuden a \
encontrarlo en texto libre: apodos/alias conocidos, errores ortograficos \
comunes, nombre completo si diste solo un apodo, y (si es una institucion) \
las dependencias/temas mas directamente asociados a ella.

Ejemplos:
- "Juan Orlando" -> ["JOH", "Juan Orlando Hernandez", "Hernandez", "expresidente"]
- "Secretaria de Seguridad" -> ["Policia Nacional", "Ministro de Seguridad", \
"operativos policiales", "capturas", "narcotrafico"]

Cada termino debe ser corto (1-3 palabras), util para un ILIKE '%termino%' \
sobre texto -- no frases largas ni explicaciones."""

_CACHE_TTL_SECONDS = 3600
_cache: dict[str, tuple[float, list[str]]] = {}

_RATE_WINDOW_SECONDS = 3600
_RATE_MAX_PER_WINDOW = 60
_call_timestamps: list[float] = []


class RateLimitExceeded(Exception):
    pass


def expandir_query(query: str, api_key: str, model: str) -> list[str]:
    key = query.strip().lower()
    cached = _cache.get(key)
    now = time.time()
    if cached and cached[0] > now - _CACHE_TTL_SECONDS:
        return cached[1]

    while _call_timestamps and _call_timestamps[0] < now - _RATE_WINDOW_SECONDS:
        _call_timestamps.pop(0)
    if len(_call_timestamps) >= _RATE_MAX_PER_WINDOW:
        raise RateLimitExceeded()
    _call_timestamps.append(now)

    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=256,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": query}],
        tools=[
            {
                "name": _TOOL_NAME,
                "description": "Devuelve los terminos de busqueda expandidos.",
                "input_schema": _RESPONSE_SCHEMA,
                "strict": True,
            }
        ],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
    )

    terminos: list[str] = []
    for block in response.content:
        if block.type == "tool_use" and block.name == _TOOL_NAME:
            terminos = block.input.get("terminos", [])
            break

    _cache[key] = (now, terminos)
    return terminos
