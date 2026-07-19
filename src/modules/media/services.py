"""Logica de negocio de Media Sources / Programas -- se implementa en la
siguiente fase. Ver docs/BACKEND_ARCHITECTURE.md."""
from src.modules.media.repositories import MedioRepository, ProgramaRepository


class MediaService:
    def __init__(self, medios: MedioRepository, programas: ProgramaRepository):
        self._medios = medios
        self._programas = programas
