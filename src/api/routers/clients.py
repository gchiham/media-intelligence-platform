"""Endpoints de Clientes (Tenants) -- se implementan en la siguiente fase.
Ver docs/BACKEND_ARCHITECTURE.md."""
from fastapi import APIRouter

router = APIRouter(prefix="/clients", tags=["clients"])
