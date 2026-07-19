"""Endpoints de Media Sources / Programas -- se implementan en la siguiente
fase. Ver docs/BACKEND_ARCHITECTURE.md."""
from fastapi import APIRouter

router = APIRouter(prefix="/media", tags=["media"])
