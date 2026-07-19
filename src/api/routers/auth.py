"""Endpoints de autenticacion (login, refresh) -- se implementan en la
siguiente fase. Ver docs/BACKEND_ARCHITECTURE.md."""
from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])
