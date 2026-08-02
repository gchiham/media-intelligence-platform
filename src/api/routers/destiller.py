"""Portal de validacion de Destiller. Capa de transporte pura: la logica de
cola y veredictos vive en ValidacionRepository.

Sirve el HTML y la API que consume. Token-gated con `settings.dashboard_token`,
igual que el dashboard de noticias -- es una herramienta interna, no un
endpoint publico.

**El audio nunca pasa por el backend.** Se entregan URLs prefirmadas de S3 y el
navegador baja directo. No es una optimizacion: el backend de produccion es una
`t3.small` que ademas aloja Postgres, y ya hay un incidente documentado
(2026-07-22, ver docs/INFRASTRUCTURE.md) de esa misma instancia quedandose sin
responder por saturacion. Varias personas validando en paralelo, cada una
descargando horas de audio, la tumbaria de nuevo.
"""
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from src.api.deps import get_db_session
from src.api.schemas.destiller import VeredictoRequest
from src.infrastructure.config import settings
from src.modules.destiller.repositories import ValidacionRepository

router = APIRouter(prefix="/destiller", tags=["destiller"])

_PORTAL = Path(__file__).parent.parent / "templates" / "destiller_portal.html"

# Una sesion de validacion dura horas (hay que escuchar audio), asi que la URL
# tiene que sobrevivir a la tanda completa sin obligar a recargar la pagina.
FIRMA_SEGUNDOS = 8 * 3600


def _autorizar(token: str) -> None:
    if not settings.dashboard_token or token != settings.dashboard_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _repo(session: Session = Depends(get_db_session)) -> ValidacionRepository:
    return ValidacionRepository(session)


def _firmar(claves: set[str]) -> dict[str, str]:
    if not claves:
        return {}
    s3 = boto3.client("s3", region_name=settings.aws_region)
    urls = {}
    for clave in claves:
        try:
            urls[clave] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.clips_bucket, "Key": clave},
                ExpiresIn=FIRMA_SEGUNDOS,
            )
        except ClientError:
            continue  # sin audio se valida por texto, no es motivo de error
    return urls


@router.get("/portal", response_class=HTMLResponse)
def portal(token: str = "") -> HTMLResponse:
    _autorizar(token)
    return HTMLResponse(_PORTAL.read_text(encoding="utf-8"))


@router.get("/modelos")
def modelos(token: str = "", repo: ValidacionRepository = Depends(_repo)) -> dict:
    _autorizar(token)
    return {"modelos": repo.modelos()}


@router.get("/tanda")
def tanda(
    validador: str,
    modelo: str,
    token: str = "",
    limite: int = 40,
    repo: ValidacionRepository = Depends(_repo),
) -> dict:
    """Siguiente tanda de bloques sin validar, reservada para este validador."""
    _autorizar(token)
    validador = validador.strip().lower()
    if not validador:
        raise HTTPException(status_code=400, detail="falta el nombre del validador")

    bloques = repo.siguiente_tanda(validador, modelo, min(max(limite, 1), 200))
    urls = _firmar({b.audio_key for b in bloques if b.audio_key})

    return {
        "progreso": repo.progreso(modelo),
        "bloques": [
            {
                "id": str(b.id),
                "grabacion": b.grabacion_ref,
                "medio": b.medio,
                "fecha": b.fecha,
                "hora_local": b.hora_local,
                "start_word": b.start_word,
                "end_word": b.end_word,
                "inicio": b.inicio_seg,
                "fin": b.fin_seg,
                "tipo": b.tipo_llm.value,
                "confidence": b.confidence,
                "motivo": b.motivo,
                "texto": b.texto,
                "audio": urls.get(b.audio_key or ""),
            }
            for b in bloques
        ],
    }


@router.post("/veredicto")
def veredicto(
    payload: VeredictoRequest,
    token: str = "",
    repo: ValidacionRepository = Depends(_repo),
) -> dict:
    _autorizar(token)
    try:
        bloque_id = uuid.UUID(payload.bloque_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bloque_id invalido")

    if not repo.registrar(
        bloque_id=bloque_id,
        validador=payload.validador.strip().lower(),
        ok=payload.ok,
        tipo_real=payload.tipo_real.value if payload.tipo_real else None,
    ):
        raise HTTPException(status_code=404, detail="bloque no encontrado")
    return {"ok": True}


@router.get("/progreso")
def progreso(modelo: str, token: str = "", repo: ValidacionRepository = Depends(_repo)) -> dict:
    _autorizar(token)
    return repo.progreso(modelo)
