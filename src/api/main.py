"""Punto de entrada de la API (uvicorn src.api.main:app). Estructura
preparada, sin endpoints de negocio todavia -- solo /health (requerido desde
el MVP, ver docs/ARCHITECTURE.md seccion 11) y los routers por modulo
incluidos vacios, listos para que la siguiente fase les agregue rutas. Ver
docs/BACKEND_ARCHITECTURE.md."""
from fastapi import FastAPI

from src.api.routers import auth, clients, editorial, media, pipeline

app = FastAPI(title="Media Intelligence Platform API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(clients.router)
app.include_router(editorial.router)
app.include_router(media.router)
app.include_router(pipeline.router)
