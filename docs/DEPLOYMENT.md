# DEPLOYMENT.md

Despliegue del MVP en una única instancia EC2 (Ubuntu 24.04 LTS) con Docker Compose: `backend` (FastAPI/Uvicorn) + `postgres` + `nginx`. **Sin ECS/EKS/Kubernetes/Terraform/CloudFormation/RDS/S3/ElastiCache/ALB/Auto Scaling/CI-CD** — deliberadamente, es la arquitectura ya decidida en `docs/ARCHITECTURE.md` (sección 15: `Deployment: Docker Compose on EC2`) para el MVP, no un cambio de rumbo.

Esta fase **no modifica el dominio ni la arquitectura de la aplicación** — solo agrega la infraestructura de despliegue alrededor de lo que ya existe y está probado (`docs/BACKEND_ARCHITECTURE.md`, `docs/API.md`).

## Requisitos del servidor

- EC2 Ubuntu 24.04 LTS. Tamaño mínimo razonable para el MVP: `t3.small` (2 vCPU, 2 GB RAM) — el backend no hace transcripción ni GPU (eso es chepita, instancia separada, ver `docs/INFRASTRUCTURE.md`); sí ejecuta `ffmpeg` para el clipping de `POST /pipeline/process`, que es liviano en CPU.
- Security Group: puerto `80` (o el que definas en `HTTP_PORT`) abierto al público; puerto `22` (SSH) restringido a tu IP; **no** exponer el puerto de Postgres (`POSTGRES_HOST_PORT`) a `0.0.0.0/0` — solo para administración desde una IP conocida, o mejor, ni siquiera abrirlo en el Security Group y administrar Postgres por SSH tunneling.
- Al menos 20 GB de disco (imagen Docker + `postgres_data` + `data/` + `logs/`, que crecen con el uso).

## Instalación de Docker y Docker Compose

Ubuntu 24.04 ya no necesita el paquete `docker-compose` viejo (v1) — Docker Compose v2 viene como plugin de `docker` (`docker compose`, sin guion):

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Correr docker sin sudo (opcional, requiere volver a iniciar sesion despues):
sudo usermod -aG docker $USER
```

Verificar: `docker --version` y `docker compose version`.

## Configuración del `.env`

```bash
git clone <url-del-repo> media-intelligence-platform
cd media-intelligence-platform
cp .env.example .env
nano .env   # o el editor que prefieras
```

Variables obligatorias a completar (`docker-compose.yml` falla explícitamente al arrancar si faltan — `POSTGRES_PASSWORD` y `OPENAI_API_KEY` usan la sintaxis `${VAR:?mensaje}`):

| Variable | Qué es |
|---|---|
| `POSTGRES_PASSWORD` | Password real de Postgres. Generar uno fuerte: `openssl rand -base64 24`. **Nunca "postgres" en producción.** |
| `OPENAI_API_KEY` | Key real de OpenAI (la usa `AIAnalysisProvider` al ejecutar `POST /pipeline/process`). |

Variables con default razonable (revisar, no obligatorio cambiar): `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_HOST_PORT`, `OPENAI_MODEL`, `LOCAL_MEDIA_DIR`, `HTTP_PORT`. Ver `.env.example` para la explicación de cada una y la distinción desarrollo/producción.

## Despliegue inicial

Con `.env` ya completo:

```bash
docker compose up -d
```

Esto, en orden (automático, sin pasos manuales adicionales):
1. Levanta `postgres`, espera a que su healthcheck (`pg_isready`) pase.
2. Levanta `backend`: espera a que Postgres acepte conexiones (chequeo propio en `docker/entrypoint.sh`, redundante con el healthcheck de compose a propósito — doble seguro), corre `alembic upgrade head`, y recién ahí arranca Uvicorn.
3. Levanta `nginx`, que espera a que `backend` esté `healthy` (`GET /health` → `200`).

Verificar que todo quedó arriba:

```bash
docker compose ps          # los 3 servicios deben decir "healthy"
curl http://localhost/health   # {"status":"ok"}  (via nginx, puerto 80 o el HTTP_PORT que configuraste)
```

Alternativa con más verificación automática: `./scripts/deploy.sh` (hace lo mismo, más un chequeo de salud con reintentos y reporta si algo no levantó bien).

## Actualización de versiones

```bash
git pull
./scripts/deploy.sh
```

`deploy.sh` reconstruye la imagen del `backend` (recoge el código nuevo) y hace `docker compose up -d` — Compose reemplaza solo el contenedor `backend` si `postgres`/`nginx` no cambiaron. Las migraciones nuevas (si las hay) corren solas al arrancar el backend actualizado — no hace falta ejecutarlas a mano.

## Ejecución de migraciones

Ya son automáticas en cada arranque del backend (`docker/entrypoint.sh` corre `alembic upgrade head` antes de Uvicorn). Para correrlas manualmente (ej. depurar sin reiniciar el contenedor):

```bash
docker compose exec backend alembic upgrade head
docker compose exec backend alembic current   # ver la revision aplicada
docker compose exec backend alembic history    # ver el historial completo
```

## Backups

```bash
./scripts/backup_database.sh
```

Corre `pg_dump` dentro del contenedor `postgres` (no necesita `psql`/`pg_dump` instalado en el host) y guarda un `.sql.gz` comprimido en `postgres_backups/` (gitignored, no versionado). **No sube a S3 todavía** — deliberadamente fuera de alcance de esta fase. Recomendado: agregar un cron del sistema operativo que lo corra diario:

```bash
crontab -e
# agregar:
0 3 * * * cd /ruta/al/repo && ./scripts/backup_database.sh >> logs/backup.log 2>&1
```

## Restauración

```bash
./scripts/restore_database.sh postgres_backups/media_intelligence_20260101_030000.sql.gz
```

**Destructivo** — pide confirmación explícita (escribir `si`) antes de reemplazar los datos actuales.

## Estructura de directorios

```
media-intelligence-platform/
├── docker-compose.yml
├── Dockerfile
├── .env                      -- NO se commitea (gitignored)
├── docker/
│   ├── entrypoint.sh          -- migra + arranca Uvicorn
│   └── nginx/nginx.conf
├── data/                      -- volumen persistente, montado en el backend
│   ├── audio/                 -- reservado (ver nota abajo)
│   ├── words/                 -- reservado (ver nota abajo)
│   ├── clips/                 -- reservado
│   └── recordings/            -- el que REALMENTE usa LocalFileRecordingResolver hoy
├── logs/                      -- persistente, host-visible
│   ├── nginx/                 -- access.log, error.log
│   ├── postgres/              -- postgresql.log
│   └── backend/                -- reservado (ver seccion Logging)
├── postgres_backups/          -- generado por backup_database.sh (gitignored)
└── scripts/
    ├── deploy.sh, start.sh, stop.sh
    └── backup_database.sh, restore_database.sh
```

### Nota: `audio`/`words` vs `LocalFileRecordingResolver`

Se crearon los directorios `data/audio/`, `data/words/`, `data/clips/` tal como se pidió, pero **`LocalFileRecordingResolver`** (`src/modules/pipeline/resolvers.py`, ya construido y probado en la fase de API) espera el audio y el `words.json` de una `Grabacion` **juntos en un mismo directorio** — no separados. Por eso `LOCAL_MEDIA_DIR` apunta a `data/recordings/`, no a `data/audio`+`data/words`. No se cambió esa lógica en esta fase (fuera de alcance — "no modificar el dominio"). Si más adelante se quiere la separación real, es un cambio en `LocalFileRecordingResolver`, no en el despliegue.

## Logging — dónde queda cada uno

| Servicio | Dónde | Cómo verlo |
|---|---|---|
| **nginx** | `logs/nginx/access.log`, `logs/nginx/error.log` (bind mount, host-visible directo) | `tail -f logs/nginx/access.log` |
| **postgres** | `logs/postgres/postgresql.log` (bind mount; `logging_collector=on` configurado en `docker-compose.yml`, sin tocar la imagen) | `tail -f logs/postgres/postgresql.log` |
| **backend** | stdout del contenedor, JSON estructurado (`src/shared/logging_utils.py`, ya existía) — capturado por el driver `json-file` de Docker, con rotación (`max-size: 10m`, `max-file: 5`, configurado en `docker-compose.yml`) | `docker compose logs -f backend` |

El backend no escribe a `logs/backend/` en esta fase — se decidió no tocar `src/shared/logging_utils.py` (agregar un `FileHandler` ahí sería modificar código de aplicación, fuera de alcance de "solo infraestructura de despliegue"). El directorio queda reservado por si se decide hacerlo después.

## Solución de problemas

**`docker compose up -d` falla inmediatamente con un mensaje sobre una variable faltante** — falta `POSTGRES_PASSWORD` o `OPENAI_API_KEY` en `.env`. Revisa `.env.example`.

**El backend nunca queda `healthy`** — revisa `docker compose logs backend`. Causas típicas:
- Postgres no llegó a aceptar conexiones a tiempo (poco probable, `entrypoint.sh` reintenta 30 veces con 2s de espera) — si igual pasa, revisa `docker compose logs postgres`.
- `alembic upgrade head` falló (esquema con conflictos, migración rota) — el log del backend muestra el traceback completo de Alembic.
- `OPENAI_API_KEY` inválida — no bloquea el arranque (`/health` no depende de OpenAI), pero `POST /pipeline/process` fallará en runtime.

**`nginx` no queda `healthy`** — depende de que `backend` esté `healthy` primero (`depends_on: condition: service_healthy`); si el backend nunca sana, nginx tampoco arranca su chequeo. Revisa el backend primero.

**Cambié algo en `docker/nginx/nginx.conf` y no se aplica** — el archivo está montado read-only (`:ro`), se lee al arrancar el contenedor. `docker compose restart nginx` (no hace falta reconstruir, no es parte de la imagen).

**Necesito ver si las migraciones ya corrieron** — `docker compose exec backend alembic current`.

**Puerto 80 ya está en uso en el servidor** — cambia `HTTP_PORT` en `.env` y `docker compose up -d` de nuevo.

**Quiero conectarme a Postgres desde mi máquina para inspeccionar datos** — con `POSTGRES_HOST_PORT` expuesto (default `5433`) y un túnel SSH si el Security Group no lo permite directo: `psql -h localhost -p 5433 -U <POSTGRES_USER> -d <POSTGRES_DB>`.

## Validado

Todo lo de este documento se probó de verdad en esta sesión, no solo se escribió: `docker compose build backend` construyó la imagen sin errores; `docker compose up -d` levantó los 3 servicios y los 3 quedaron `healthy`; el log del backend confirmó las 4 migraciones (`0001` → `0002` → `f142223dde8b` → `1c9fad29b98d`) corriendo automáticamente contra una base nueva y vacía; `GET /health` respondió `200` a través de nginx con los headers de seguridad presentes; `logs/nginx/access.log` y `logs/postgres/postgresql.log` se poblaron en el host; y la suite completa de **44 tests** pasó contra el Postgres dockerizado, confirmando que el redespliegue no rompió nada del dominio ni de la API.
