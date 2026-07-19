"""esquema inicial: tenants, usuarios, medios, grabaciones, noticias versionadas, editorial, informes

Revision ID: 0001
Revises:
Create Date: 2026-07-17

Generado a mano a partir de src/infrastructure/db/registry.py (Base.metadata) compilado
contra el dialecto postgresql de SQLAlchemy -- no habia una instancia de Postgres/Docker
disponible en la maquina de desarrollo al momento de crear esta migracion. El DDL de abajo
es exactamente el que SQLAlchemy generaria via autogenerate; las migraciones futuras deberian
crearse con `alembic revision --autogenerate` contra una base real.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ENUM_STATEMENTS = [
    "CREATE TYPE tipo_entidad AS ENUM ('persona', 'institucion', 'empresa', 'lugar')",
    "CREATE TYPE tipo_medio AS ENUM ('radio', 'tv')",
    "CREATE TYPE rol_usuario AS ENUM ('super_admin', 'supervisor_editorial', 'periodista', 'admin_cliente', 'usuario_cliente')",
    "CREATE TYPE estado_grabacion AS ENUM ('pendiente', 'procesando', 'procesada', 'error')",
    "CREATE TYPE estado_informe AS ENUM ('borrador', 'enviado')",
    "CREATE TYPE estado_noticia AS ENUM ('pendiente', 'en_revision', 'aprobada', 'rechazada', 'publicada')",
    "CREATE TYPE sentimiento AS ENUM ('positivo', 'negativo', 'neutro')",
    "CREATE TYPE estado_cliente_noticia AS ENUM ('sugerida', 'descartada', 'confirmada', 'publicada')",
    "CREATE TYPE prioridad AS ENUM ('critica', 'alta', 'media', 'baja')",
]

TABLE_STATEMENTS = [
    """
    CREATE TABLE entidades (
        tipo tipo_entidad NOT NULL,
        nombre VARCHAR(255) NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE medios (
        nombre VARCHAR(255) NOT NULL,
        tipo tipo_medio NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE temas (
        nombre VARCHAR(255) NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (nombre)
    )
    """,
    """
    CREATE TABLE tenants (
        nombre VARCHAR(255) NOT NULL,
        rtn VARCHAR(50),
        contactos JSONB NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE monitoring_profiles (
        personas_interes JSONB NOT NULL,
        instituciones JSONB NOT NULL,
        temas JSONB NOT NULL,
        medios JSONB NOT NULL,
        destinatarios_informe JSONB NOT NULL,
        id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (tenant_id),
        FOREIGN KEY(tenant_id) REFERENCES tenants (id)
    )
    """,
    """
    CREATE TABLE programas (
        medio_id UUID NOT NULL,
        nombre VARCHAR(255) NOT NULL,
        horario VARCHAR(100),
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(medio_id) REFERENCES medios (id)
    )
    """,
    """
    CREATE TABLE subtemas (
        tema_id UUID NOT NULL,
        nombre VARCHAR(255) NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(tema_id) REFERENCES temas (id)
    )
    """,
    """
    CREATE TABLE usuarios (
        tenant_id UUID,
        email VARCHAR(255) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        nombre VARCHAR(255) NOT NULL,
        rol rol_usuario NOT NULL,
        activo BOOLEAN NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(tenant_id) REFERENCES tenants (id),
        UNIQUE (email)
    )
    """,
    """
    CREATE TABLE auditoria (
        tabla VARCHAR(100) NOT NULL,
        registro_id UUID NOT NULL,
        usuario_id UUID,
        accion VARCHAR(100) NOT NULL,
        valor_anterior JSONB,
        valor_nuevo JSONB,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        id UUID NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(usuario_id) REFERENCES usuarios (id)
    )
    """,
    """
    CREATE TABLE grabaciones (
        programa_id UUID NOT NULL,
        s3_key VARCHAR(1024) NOT NULL,
        fecha_inicio TIMESTAMP WITH TIME ZONE NOT NULL,
        fecha_fin TIMESTAMP WITH TIME ZONE NOT NULL,
        estado estado_grabacion NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(programa_id) REFERENCES programas (id),
        UNIQUE (s3_key)
    )
    """,
    """
    CREATE TABLE informes_semanales (
        semana_inicio DATE NOT NULL,
        semana_fin DATE NOT NULL,
        estado estado_informe NOT NULL,
        resumen_texto TEXT NOT NULL,
        enviado_por UUID,
        enviado_at TIMESTAMP WITH TIME ZONE,
        id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(enviado_por) REFERENCES usuarios (id),
        FOREIGN KEY(tenant_id) REFERENCES tenants (id)
    )
    """,
    """
    CREATE TABLE login_events (
        usuario_id UUID NOT NULL,
        ip VARCHAR(64) NOT NULL,
        user_agent VARCHAR(512),
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        id UUID NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(usuario_id) REFERENCES usuarios (id)
    )
    """,
    """
    CREATE TABLE noticias (
        grabacion_id UUID NOT NULL,
        estado estado_noticia NOT NULL,
        version_actual_id UUID,
        clip_inicio_seg FLOAT NOT NULL,
        clip_fin_seg FLOAT NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(grabacion_id) REFERENCES grabaciones (id)
    )
    """,
    """
    CREATE TABLE transcripciones (
        grabacion_id UUID NOT NULL,
        texto_completo VARCHAR NOT NULL,
        segmentos JSONB NOT NULL,
        proveedor VARCHAR(100) NOT NULL,
        id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(grabacion_id) REFERENCES grabaciones (id)
    )
    """,
    """
    CREATE TABLE cliente_noticias (
        noticia_id UUID NOT NULL,
        sentimiento sentimiento,
        estado estado_cliente_noticia NOT NULL,
        fecha_publicacion TIMESTAMP WITH TIME ZONE,
        id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (tenant_id, noticia_id),
        FOREIGN KEY(noticia_id) REFERENCES noticias (id),
        FOREIGN KEY(tenant_id) REFERENCES tenants (id)
    )
    """,
    """
    CREATE TABLE informe_noticias (
        informe_id UUID NOT NULL,
        noticia_id UUID NOT NULL,
        incluida BOOLEAN NOT NULL,
        PRIMARY KEY (informe_id, noticia_id),
        FOREIGN KEY(informe_id) REFERENCES informes_semanales (id),
        FOREIGN KEY(noticia_id) REFERENCES noticias (id)
    )
    """,
    """
    CREATE TABLE noticia_versiones (
        noticia_id UUID NOT NULL,
        numero_version INTEGER NOT NULL,
        titulo VARCHAR(500) NOT NULL,
        resumen TEXT NOT NULL,
        transcripcion_texto TEXT NOT NULL,
        tema_id UUID,
        subtema_id UUID,
        ai_score INTEGER,
        prioridad prioridad,
        confianza JSONB NOT NULL,
        es_generada_por_ia BOOLEAN NOT NULL,
        editado_por UUID,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        id UUID NOT NULL,
        PRIMARY KEY (id),
        UNIQUE (noticia_id, numero_version),
        FOREIGN KEY(noticia_id) REFERENCES noticias (id),
        FOREIGN KEY(tema_id) REFERENCES temas (id),
        FOREIGN KEY(subtema_id) REFERENCES subtemas (id),
        FOREIGN KEY(editado_por) REFERENCES usuarios (id)
    )
    """,
    """
    CREATE TABLE etiquetados_privados (
        cliente_noticia_id UUID NOT NULL,
        subcategoria VARCHAR(255),
        keywords JSONB NOT NULL,
        created_by UUID NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        id UUID NOT NULL,
        tenant_id UUID NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(cliente_noticia_id) REFERENCES cliente_noticias (id),
        FOREIGN KEY(created_by) REFERENCES usuarios (id),
        FOREIGN KEY(tenant_id) REFERENCES tenants (id)
    )
    """,
    """
    CREATE TABLE noticia_version_entidades (
        noticia_version_id UUID NOT NULL,
        entidad_id UUID NOT NULL,
        PRIMARY KEY (noticia_version_id, entidad_id),
        FOREIGN KEY(noticia_version_id) REFERENCES noticia_versiones (id),
        FOREIGN KEY(entidad_id) REFERENCES entidades (id)
    )
    """,
]

# FK circular noticias.version_actual_id -> noticia_versiones.id: se agrega despues
# de crear ambas tablas (mismo patron que use_alter=True en el modelo SQLAlchemy).
DEFERRED_FK_STATEMENTS = [
    "ALTER TABLE noticias ADD FOREIGN KEY(version_actual_id) REFERENCES noticia_versiones (id)",
]

# Orden inverso de dependencias para el downgrade.
DROP_TABLE_ORDER = [
    "noticia_version_entidades",
    "etiquetados_privados",
    "noticia_versiones",
    "informe_noticias",
    "cliente_noticias",
    "transcripciones",
    "noticias",
    "login_events",
    "informes_semanales",
    "grabaciones",
    "auditoria",
    "usuarios",
    "subtemas",
    "programas",
    "monitoring_profiles",
    "tenants",
    "temas",
    "medios",
    "entidades",
]

DROP_ENUM_ORDER = [
    "tipo_entidad",
    "tipo_medio",
    "rol_usuario",
    "estado_grabacion",
    "estado_informe",
    "estado_noticia",
    "sentimiento",
    "estado_cliente_noticia",
    "prioridad",
]


def upgrade() -> None:
    for stmt in ENUM_STATEMENTS:
        op.execute(stmt)
    for stmt in TABLE_STATEMENTS:
        op.execute(stmt)
    for stmt in DEFERRED_FK_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for table in DROP_TABLE_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum_name in DROP_ENUM_ORDER:
        op.execute(f"DROP TYPE IF EXISTS {enum_name}")
