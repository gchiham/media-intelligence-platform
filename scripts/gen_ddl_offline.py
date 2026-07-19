"""Generador one-off: compila la metadata SQLAlchemy a DDL real de PostgreSQL,
sin necesitar una conexion viva. Se usa una sola vez para escribir la migracion
inicial de Alembic a mano (no hay Postgres/Docker disponible en esta maquina
todavia). Las migraciones futuras deberian generarse con
`alembic revision --autogenerate` contra una base real.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import AddConstraint, CreateTable

from src.infrastructure.db.registry import Base

dialect = postgresql.dialect()

enums = {}
for table in Base.metadata.sorted_tables:
    for col in table.columns:
        if isinstance(col.type, sa.Enum):
            enums[col.type.name] = col.type

print("### ENUMS ###")
for name, enum_type in enums.items():
    values = ", ".join(f"'{v}'" for v in enum_type.enums)
    print(f"CREATE TYPE {name} AS ENUM ({values});")

print("\n### TABLES ###")
deferred_fks = []
for table in Base.metadata.sorted_tables:
    for fk_constraint in table.foreign_key_constraints:
        if fk_constraint.use_alter:
            deferred_fks.append(fk_constraint)
    ddl = str(CreateTable(table, include_foreign_key_constraints=[
        fk for fk in table.foreign_key_constraints if not fk.use_alter
    ]).compile(dialect=dialect)).strip()
    print(f"-- {table.name}")
    print(ddl + ";\n")

print("### DEFERRED FKS (use_alter) ###")
for fk in deferred_fks:
    ddl = str(AddConstraint(fk).compile(dialect=dialect)).strip()
    print(ddl + ";")

print("\n### DROP ORDER (downgrade) ###")
for table in reversed(Base.metadata.sorted_tables):
    print(f"DROP TABLE IF EXISTS {table.name} CASCADE;")
for name in enums:
    print(f"DROP TYPE IF EXISTS {name};")
