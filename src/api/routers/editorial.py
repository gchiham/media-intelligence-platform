"""Endpoints de News/NewsVersion + Centro Editorial -- se implementan en la
siguiente fase. Ver docs/BACKEND_ARCHITECTURE.md."""
from fastapi import APIRouter

router = APIRouter(prefix="/news", tags=["news"])
