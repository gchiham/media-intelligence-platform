"""Definicion unica y oficial del contrato de transcripcion (words.json).

Toda la aplicacion depende de estos mismos modelos -- el worker que corre en
chepita (via FasterWhisperProvider) y el modulo de AI que consume su salida
(src/modules/ai/schemas.py re-exporta `Word` desde aqui, no lo redefine).
Ningun componente construye a mano el diccionario de una palabra; todos pasan
por `Word`. Ver docs/TRANSCRIPTION_ARCHITECTURE.md.
"""
from pydantic import BaseModel


class Word(BaseModel):
    index: int
    word: str
    start: float
    end: float


class TranscriptionSegment(BaseModel):
    """Segmento a nivel de frase/oracion -- lo que ya se escribia en el .txt
    legible de cada estacion, antes de que existiera el contrato de palabras."""

    start: float
    end: float
    text: str


class TranscriptionResult(BaseModel):
    language: str
    duration: float
    segments: list[TranscriptionSegment]
    words: list[Word]
