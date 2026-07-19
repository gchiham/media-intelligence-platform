"""Logica de negocio de Clientes (Tenants) -- se implementa en la siguiente
fase. Ver docs/BACKEND_ARCHITECTURE.md."""
from src.modules.clients.repositories import TenantRepository


class ClienteService:
    def __init__(self, repository: TenantRepository):
        self._repository = repository
