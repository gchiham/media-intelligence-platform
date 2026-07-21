"""El modelo `Tenant` esta definido en src/modules/auth/models.py (decision
previa a esta sesion, ya reflejada en la migracion 0001_initial_schema.py --
no se mueve aqui para no invalidar esa migracion). El modulo `clients`
administra su ciclo de vida igual, importandolo desde ahi."""
from sqlalchemy import select

from src.infrastructure.db.repository import Repository
from src.modules.auth.models import Tenant


class TenantRepository(Repository[Tenant]):
    model = Tenant

    def get_by_nombre(self, nombre: str) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.nombre == nombre)
        return self._session.scalars(stmt).first()
