"""Da de alta los diarios impresos que publica el canal de Telegram.

Van como `digital` y no con un tipo_medio nuevo: La Tribuna y Tiempo son
diarios impresos Y sitios web, y un enum de un solo valor obligaria a elegir
uno. El canal por el que entra el contenido lo dice la tabla donde cae (una
`Edicion` en PDF vs una `FuenteWeb` de RSS), no el tipo del Medio -- mismo
criterio que ya se uso con HCH, que sigue siendo `tv` y le cuelga su feed RSS.

`el_pais_hn` ya existe desde el alta de prensa digital: no se duplica.

Idempotente: se puede correr las veces que sea.

Uso:
    python scripts/seed_medios_impresos.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402
from src.modules.media.models import Medio, TipoMedio  # noqa: E402

# Los codigos coinciden con los que devuelve pdf.identificar() -- si se cambia
# uno hay que cambiar el otro, o el registro no encuentra el Medio.
IMPRESOS = [
    ("la_tribuna", "La Tribuna"),
    ("diario_tiempo", "Diario Tiempo"),
    ("mas_noticias", "Mas Noticias"),
    ("patrulla_grafica", "La Patrulla Grafica"),
]


def main() -> int:
    with Session(get_engine()) as session:
        existentes = {m.codigo for m in session.scalars(select(Medio))}
        creados = 0
        for codigo, nombre in IMPRESOS:
            if codigo in existentes:
                print(f"  ya existe: {codigo}")
                continue
            session.add(Medio(codigo=codigo, nombre=nombre, tipo=TipoMedio.DIGITAL))
            creados += 1
            print(f"  creado:    {codigo} ({nombre})")
        session.commit()
        print(f"\n{creados} medios creados, {len(IMPRESOS) - creados} ya estaban")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
