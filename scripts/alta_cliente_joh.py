"""Alta del tenant Juan Orlando Hernandez: Tenant + Usuario admin_cliente +
MonitoringProfile, con los datos recolectados en scratchpad/formulario_alta_cliente.txt.

"Todos los medios" del formulario se resuelve contra la tabla Medio real (debe
estar poblada por scripts/seed_medios.py antes de correr esto).

Idempotente: usa nombre de Tenant / email de Usuario como llave para no duplicar
si se corre dos veces. La contrasena del admin se genera al azar y se imprime
una sola vez -- no se guarda en ningun lado, hay que copiarla de la salida.

Uso: python scripts/alta_cliente_joh.py
"""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.auth.models import RolUsuario, Tenant, Usuario  # noqa: E402
from src.modules.auth.repositories import UsuarioRepository  # noqa: E402
from src.modules.clients.repositories import TenantRepository  # noqa: E402
from src.modules.editorial.models import MonitoringProfile  # noqa: E402
from src.modules.editorial.repositories import MonitoringProfileRepository  # noqa: E402
from src.modules.media.repositories import MedioRepository  # noqa: E402
from src.shared.security import password_hasher  # noqa: E402

TENANT_NOMBRE = "Juan Orlando Hernandez"
CONTACTOS = {"nombre": "Ana Garcia de Hernandez", "rol": "contacto_principal"}

ADMIN_NOMBRE = "Gustavo Chi Ham"
ADMIN_EMAIL = "gchiham@gmail.com"

PERSONAS_INTERES = [
    "Juan Orlando Hernandez",
    "Ana Garcia de Hernandez",
    "Expresidente Hernandez",
    "JOH",
]
INSTITUCIONES = ["Partido Nacional de Honduras"]
TEMAS = [
    "Juan Orlando",
    "Juan Orlando Hernandez",
    "JOH",
    "expresidente",
    "narcotrafico",
    "extradicion",
    "Corte de Nueva York",
    "Partido Nacional",
]

def main() -> None:
    with Session(get_engine()) as session:
        tenants = TenantRepository(session)
        usuarios = UsuarioRepository(session)
        monitoring_profiles = MonitoringProfileRepository(session)
        medios = MedioRepository(session)

        tenant = tenants.get_by_nombre(TENANT_NOMBRE)
        if tenant is None:
            tenant = tenants.add(Tenant(nombre=TENANT_NOMBRE, contactos=CONTACTOS))
            session.flush()
            print(f"Tenant creado: {tenant.id}")
        else:
            print(f"Tenant ya existia: {tenant.id}")

        admin = usuarios.get_by_email(ADMIN_EMAIL)
        if admin is None:
            temp_password = secrets.token_urlsafe(12)
            admin = usuarios.add(
                Usuario(
                    tenant_id=tenant.id,
                    email=ADMIN_EMAIL,
                    password_hash=password_hasher.hash(temp_password),
                    nombre=ADMIN_NOMBRE,
                    rol=RolUsuario.ADMIN_CLIENTE,
                )
            )
            session.flush()
            print(f"Usuario admin creado: {admin.id} -- contrasena temporal: {temp_password}")
        else:
            print(f"Usuario admin ya existia: {admin.id}")

        profile = monitoring_profiles.get_by_tenant_id(tenant.id)
        todos_los_medios = [str(m.id) for m in medios.list(limit=1000)]
        if profile is None:
            profile = monitoring_profiles.add(
                MonitoringProfile(
                    tenant_id=tenant.id,
                    personas_interes=PERSONAS_INTERES,
                    instituciones=INSTITUCIONES,
                    temas=TEMAS,
                    medios=todos_los_medios,
                )
            )
            print(f"MonitoringProfile creado con {len(todos_los_medios)} medios")
        else:
            profile.personas_interes = PERSONAS_INTERES
            profile.instituciones = INSTITUCIONES
            profile.temas = TEMAS
            profile.medios = todos_los_medios
            print(f"MonitoringProfile actualizado con {len(todos_los_medios)} medios")

        session.commit()


if __name__ == "__main__":
    main()
