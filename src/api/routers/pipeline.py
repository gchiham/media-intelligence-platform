"""Endpoints de Pipeline Runs -- se implementan en la siguiente fase. Ver
docs/BACKEND_ARCHITECTURE.md."""
from fastapi import APIRouter

router = APIRouter(prefix="/pipeline-runs", tags=["pipeline-runs"])
