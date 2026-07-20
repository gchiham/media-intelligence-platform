"""Excepciones del dominio AI/segmentacion."""


class SegmentationError(Exception):
    """Se agotaron los reintentos contra el proveedor de IA para un chunk, o
    el proveedor devolvio un error permanente (no reintentable)."""
