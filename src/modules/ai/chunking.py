"""Divide una transcripcion larga en chunks de palabras para enviar al LLM.

Un chunk es un rango de indices de palabras, no de segundos ni de audio (ver
principio de diseno en github.com/gchiham/mvp-medios: "el LLM entiende
narrativa, no tiempo"). Sin solape entre chunks -- una noticia que cae justo en
el limite de un chunk puede quedar partida; es una limitacion conocida y
aceptada del enfoque, no un bug."""
from src.modules.ai.schemas import Word


def chunk_words(words: list[Word], chunk_size: int = 600) -> list[list[Word]]:
    if chunk_size <= 0:
        raise ValueError("chunk_size debe ser positivo")
    return [words[i : i + chunk_size] for i in range(0, len(words), chunk_size)]
