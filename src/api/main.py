"""Punto de entrada de la API (uvicorn src.api.main:app).

Mapea excepciones tipadas del dominio a codigos HTTP (ver docs/API.md) --
nunca se devuelve un traceback al cliente; cualquier excepcion no prevista
se loguea completa server-side (structured JSON, src/shared/logging_utils.py)
y responde 500 generico.
"""
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from src.api.routers import auth, clients, editorial, media, pipeline
from src.infrastructure.db import registry  # noqa: F401 -- registra todos los modelos (FKs cruzadas entre modulos)
from src.modules.editorial.exceptions import (
    ColaVacia,
    NoticiaNoBloqueadaPorEditor,
    NoticiaNoEncontrada,
)
from src.modules.pipeline.exceptions import GrabacionNoEncontrada, RecursosNoDisponibles
from src.shared.logging_utils import get_logger

app = FastAPI(title="Media Intelligence Platform API")
logger = get_logger("api")


@app.get("/health", tags=["health"], summary="Chequeo de salud")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(ColaVacia)
def handle_cola_vacia(request: Request, exc: ColaVacia) -> Response:
    return Response(status_code=204)


@app.exception_handler(NoticiaNoEncontrada)
@app.exception_handler(GrabacionNoEncontrada)
def handle_not_found(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(NoticiaNoBloqueadaPorEditor)
@app.exception_handler(RecursosNoDisponibles)
def handle_conflict(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(Exception)
def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        f"unhandled exception in {request.url.path}",
        extra={
            "extra_fields": {
                "path": str(request.url.path),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "stack_trace": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            }
        },
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(auth.router, prefix="/api/v1")
app.include_router(clients.router, prefix="/api/v1")
app.include_router(editorial.router, prefix="/api/v1")
app.include_router(media.router, prefix="/api/v1")
app.include_router(pipeline.router, prefix="/api/v1")
