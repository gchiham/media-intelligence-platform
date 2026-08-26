"""Dashboard de operacion del pipeline (`GET /dash`).

Sirve una pagina estatica y el JSON que consume, igual que el portal de
Destiller: el HTML no se formatea server-side, el `token` lo lee el JS de la
query string y lo reenvia en cada llamada a `/api/v1/dash/metricas`. Asi el
template puede llevar CSS y JS con llaves sin pelear con `str.format()`.

Las cuatro etapas y de donde sale cada cifra estan documentadas en
`src/modules/pipeline/metricas.py`.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from src.api.deps import get_db_session
from src.infrastructure.config import settings
from src.modules.pipeline.metricas import (
    HORAS_DEFECTO,
    HORAS_MAX,
    MetricasPipelineRepository,
    inicio_ventana,
    resumir,
)

router = APIRouter(prefix="/dash", tags=["dash"])
pagina = APIRouter(tags=["dash"])

_PAGINA = Path(__file__).parent.parent / "templates" / "dash.html"
_MARCA = Path(__file__).parent.parent / "static" / "publiaudit-mark.png"


def _autorizar(token: str) -> None:
    """404 y no 401: la existencia misma de la pagina no se anuncia a quien no
    trae el token (mismo criterio que /news/dashboard y /destiller/portal)."""
    if not settings.dashboard_token or token != settings.dashboard_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@pagina.get(
    "/dash",
    response_class=HTMLResponse,
    summary="Dashboard de avance del pipeline",
    description=(
        "Pagina HTML de solo lectura con el avance de las cuatro etapas del "
        "pipeline (Grabaciones, CHEPITA, Segmentacion LLM y Clipper) hora por "
        "hora y medio por medio. Protegida por `token` en la query string "
        "(mismo `DASHBOARD_TOKEN` que /news/dashboard); responde 404 si no coincide."
    ),
)
def dashboard(token: str = "") -> HTMLResponse:
    _autorizar(token)
    return HTMLResponse(_PAGINA.read_text(encoding="utf-8"))


@pagina.get("/dash/mark.png", include_in_schema=False)
def marca() -> FileResponse:
    """El logo va servido desde aca y no desde un CDN para que la pagina no
    dependa de nada externo. Sin token: es solo la marca."""
    return FileResponse(_MARCA, media_type="image/png")


@router.get(
    "/metricas",
    summary="Avance del pipeline por etapa, hora y medio",
    description=(
        "JSON que consume `GET /dash`. `horas` es el tamaño de la ventana hacia "
        "atras, contada sobre la hora de emision de las grabaciones."
    ),
)
def metricas(
    token: str = "",
    horas: int = Query(HORAS_DEFECTO, ge=1, le=HORAS_MAX),
    session: Session = Depends(get_db_session),
) -> dict:
    _autorizar(token)
    repo = MetricasPipelineRepository(session)
    desde = inicio_ventana(horas)
    return resumir(
        por_hora=repo.por_hora(desde),
        por_medio=repo.por_medio(desde),
        batches=repo.batches_en_vuelo(),
        horas=horas,
    )
