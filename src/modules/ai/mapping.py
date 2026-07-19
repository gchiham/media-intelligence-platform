"""Mapeo determinista de indice de palabra a tiempo real de audio -- sin LLM,
sin heuristicas, 100% trazable: start_time = words[start_word].start,
end_time = words[end_word].end. Aplica el padding de contexto y lo recorta a
los limites reales del audio (nunca negativo, nunca mas alla de la duracion)."""
from dataclasses import dataclass

from src.modules.ai.schemas import NewsSegment, Word


@dataclass
class NewsTiming:
    start_time: float
    end_time: float


def map_words_to_time(
    segment: NewsSegment,
    words: list[Word],
    padding: float = 2.0,
    audio_duration: float | None = None,
) -> NewsTiming:
    by_index = {w.index: w for w in words}
    try:
        start_word = by_index[segment.start_word]
        end_word = by_index[segment.end_word]
    except KeyError as exc:
        raise ValueError(
            f"start_word/end_word ({segment.start_word}, {segment.end_word}) "
            f"no encontrados en la lista de palabras"
        ) from exc

    start_time = max(0.0, start_word.start - padding)
    end_time = end_word.end + padding
    if audio_duration is not None:
        end_time = min(end_time, audio_duration)

    return NewsTiming(start_time=start_time, end_time=end_time)
