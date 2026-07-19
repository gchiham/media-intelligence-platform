"""Logica de negocio Editorial (News/NewsVersion + cola/aprobacion) -- se
implementa en la siguiente fase. Ver docs/BACKEND_ARCHITECTURE.md."""
from src.modules.editorial.repositories import NoticiaRepository, NoticiaVersionRepository


class NoticiaService:
    def __init__(self, noticias: NoticiaRepository, versiones: NoticiaVersionRepository):
        self._noticias = noticias
        self._versiones = versiones
