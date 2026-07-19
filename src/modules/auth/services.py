"""Logica de negocio de Usuarios (autenticacion, RBAC) -- se implementa en la
siguiente fase. Ver docs/BACKEND_ARCHITECTURE.md."""
from src.modules.auth.repositories import UsuarioRepository


class UsuarioService:
    def __init__(self, repository: UsuarioRepository):
        self._repository = repository
