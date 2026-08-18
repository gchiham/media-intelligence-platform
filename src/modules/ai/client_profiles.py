"""Proyecta los `MonitoringProfile` (una fila por tenant, FR-011) al tipo puro
`PerfilCliente` que consume el prompt.

Existe para que el modulo AI no importe SQLAlchemy ni conozca el modelo
editorial: el prompt solo necesita "a quien le importa que". Dar de alta un
cliente nuevo es insertar una fila en `monitoring_profiles` -- ni este archivo,
ni el prompt, ni el esquema de respuesta cambian.

`client_id` es el `tenant_id` en texto: ya es la clave de aislamiento del resto
del dominio (`ClienteNoticia`, `EtiquetadoPrivado` y la RN-009 cuelgan de ella),
asi que reusarla evita inventar un identificador paralelo que despues haya que
mapear de vuelta al guardar el veredicto.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.modules.ai.schemas import PerfilCliente
from src.modules.auth.models import Tenant
from src.modules.editorial.models import MonitoringProfile


def cargar_perfiles(session: Session) -> list[PerfilCliente]:
    """Todos los perfiles configurados, ordenados de forma estable.

    El orden importa: se le pide al modelo que devuelva `client_relevance` en
    el mismo orden en cada llamada, y un orden que cambie entre corridas
    ensuciaria cualquier comparacion posterior."""
    filas = session.execute(
        select(MonitoringProfile, Tenant)
        .join(Tenant, Tenant.id == MonitoringProfile.tenant_id)
        .order_by(Tenant.nombre)
    ).all()

    return [
        PerfilCliente(
            client_id=str(perfil.tenant_id),
            nombre=tenant.nombre,
            personas_interes=list(perfil.personas_interes or []),
            instituciones=list(perfil.instituciones or []),
            temas=list(perfil.temas or []),
        )
        for perfil, tenant in filas
    ]
