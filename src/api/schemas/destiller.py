"""Schemas de transporte del portal de validacion de Destiller."""
from pydantic import BaseModel, Field

from src.modules.destiller.models import TipoBloqueDB


class VeredictoRequest(BaseModel):
    bloque_id: str
    validador: str = Field(min_length=1, max_length=120)
    ok: bool
    # Solo se lee cuando ok=False. Se tipa con el enum para que un tipo
    # inventado se rechace en el borde y no llegue a la calibracion.
    tipo_real: TipoBloqueDB | None = None
