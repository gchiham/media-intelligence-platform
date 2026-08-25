"""Imprime la URL de conexion a RDS con la password vigente del secreto gestionado.

Corre DENTRO del contenedor del backend (`docker compose exec -T backend
python3 - < este archivo`) porque el host no tiene AWS CLI y el contenedor ya
trae boto3 con las credenciales del instance profile (media-intel-backend, que
tiene la politica inline `rds-master-secret`).

La password va CRUDA, sin URL-encodear: SQLAlchemy parsea el campo con
`[^@]*` (asi que `#`, `?` o `>` no le estorban) y despues aplica unquote, asi
que un `%23` se convertiria en `#`... pero ademas alembic/env.py mete la URL en
un ConfigParser, donde cualquier `%` explota con "invalid interpolation syntax".
Si alguna rotacion trae un `%` literal en la password, hay que arreglar
alembic/env.py (duplicar `%` -> `%%`), no encodear aca.
"""
import json
import os
import sys

import boto3

SECRET_ARN = os.environ.get(
    "RDS_MASTER_SECRET_ARN",
    "arn:aws:secretsmanager:us-east-1:050871635829:secret:rds!db-4a4c383d-60e6-4235-b238-d63791e79bfe-Cfqkig",
)
HOST = "media-intel-postgres.ccz7stumgm9m.us-east-1.rds.amazonaws.com"
PUERTO = 5432
BASE = "media_intelligence"


def main() -> None:
    cliente = boto3.client("secretsmanager", region_name="us-east-1")
    datos = json.loads(cliente.get_secret_value(SecretId=SECRET_ARN)["SecretString"])
    usuario = datos["username"]
    clave = datos["password"]
    sys.stdout.write(
        f"postgresql+psycopg://{usuario}:{clave}@{HOST}:{PUERTO}/{BASE}"
    )


if __name__ == "__main__":
    main()
