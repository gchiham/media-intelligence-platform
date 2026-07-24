"""Puerto + adaptador para embeddings de texto, usados en la deduplicacion
semantica de noticias entre emisoras (ver src/modules/editorial/dedup.py).

Mismo patron que `AIAnalysisProvider`/`TranscriptionProvider`: el dominio nunca
sabe que proveedor hay detras.

Por que OpenAI y no Anthropic: Anthropic no expone un endpoint de embeddings,
y `OPENAI_API_KEY` ya esta configurada en el proyecto (hoy se usa solo como
respaldo de segmentacion). `text-embedding-3-small` con 512 dimensiones es
mas que suficiente para agrupar titulares -- la dimension completa (1536)
triplicaria el tamaño del JSONB en `historias.embedding` sin mejorar el
agrupamiento a esta escala.
"""
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, textos: list[str]) -> list[list[float]]:
        """Devuelve un vector por texto, en el mismo orden."""
        raise NotImplementedError


class OpenAIEmbeddingProvider(EmbeddingProvider):
    # 512 en vez de 1536: ver docstring del modulo.
    def __init__(self, api_key: str, model: str = "text-embedding-3-small", dimensions: int = 512):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._dimensions = dimensions

    def embed(self, textos: list[str]) -> list[list[float]]:
        if not textos:
            return []
        respuesta = self._client.embeddings.create(
            model=self._model, input=textos, dimensions=self._dimensions
        )
        # La API garantiza el orden, pero se ordena por indice explicitamente
        # para no depender de esa garantia implicita.
        return [d.embedding for d in sorted(respuesta.data, key=lambda d: d.index)]
