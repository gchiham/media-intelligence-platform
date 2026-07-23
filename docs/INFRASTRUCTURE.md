# Infraestructura AWS — "chepita" (transcripción GPU)

Referencia rápida de la infraestructura real en AWS para el pipeline de transcripción. Ninguno de estos recursos vive en este repo (no hay Terraform/CDK todavía) — todo se administra manualmente vía AWS CLI / consola / SSM. Este documento existe para no tener que redescubrir todo por SSM en cada sesión.

Cuenta AWS: `050871635829` (usuario `media-intelligence-dev`). Región: `us-east-1`.

## Instancias EC2 (tag `Project=media-intel`)

**Las 3 instancias originales fueron TERMINADAS el 2026-07-19** (no solo detenidas): `media-intel-chepita-g6-test` (`i-04ca44c8228a1611a`, g6.xlarge/L4), `media-intel-chepita-bench` (`i-0f54fdb1ea6fa6e0a`, g4dn.xlarge/T4), `media-intel-chepita-builder` (`i-0b07564ecf6a99da4`, c5.xlarge). No existen más — sus Instance IDs ya no son válidos, no intentar `start-instances` con ellos.

**Patrón que se volvió a repetir el 2026-07-20/21:** lanzar 1-4 instancias `g6.xlarge` de chepita ad-hoc para drenar un backlog de transcripción, y **terminarlas** (no solo pararlas) cuando ya no hacen falta — no quedan IDs fijos de "la instancia de chepita", cada vez que se necesita, se relanza desde el AMI y se le redeploya `worker_prefetch.py` a mano (ver nota en `docs/INGESTION_DESIGN.md` sobre por qué el AMI no basta solo). Antes de terminar instancias con trabajo pendiente en la cola, confirmar con el usuario — la transcripción que falta se queda en `PROCESANDO`/en SQS indefinidamente hasta relanzar.

**Instancia temporal para *pipelines* de IA (Sonnet + ffmpeg), no de transcripción — patrón "Clipper":** el backend de producción (`media-intel-mvp-backend`, `t3.small`) no tiene CPU para correr varios `POST /pipeline/process` en paralelo (cada uno dispara ~13 llamadas a Claude + varios `ffmpeg` de clipping) — un batch de 208 grabaciones con 6 en paralelo dejó el `load average` en 13.85 sobre 2 vCPUs. La solución fue lanzar una instancia separada, más grande (`c5.xlarge`, 4 vCPU), nombrada `Clipper`, temporal:
  - Misma AMI base que el backend (Ubuntu 24.04 plano, `ami-052355af2a014bd2c`), **no** la AMI de chepita (no necesita GPU).
  - Mismo IAM instance profile que el backend (`media-intel-backend`) — ya tiene los permisos S3 correctos, no hace falta un rol nuevo.
  - Security group propio (`media-intel-clipper-sg`), con una regla adicional en el SG del backend permitiendo el puerto 5433 (Postgres) **desde ese SG específico** — para que Clipper hable con la misma Postgres de producción sin exponer el puerto a todo internet.
  - No corre `docker compose` completo (no necesita su propio Postgres/nginx) — se hace `docker build` de la imagen del backend y `docker run` directo, pasando `DATABASE_URL` apuntando a la IP **privada** del backend (`172.31.5.81:5433`, no la pública — evita el viaje de ida y vuelta por el IGW).
  - **Terminar cuando termine el trabajo** — es explícitamente temporal, no un servicio permanente. Si se necesita de nuevo, se relanza igual (unos 5 minutos: instalar Docker, clonar, buildear, correr).
  - **Terminada el 2026-07-22** (`i-0364e1c067c9427f6`) — ya no existe, `describe-instances` no la devuelve. El patrón (instancia separada + regla de SG puntual hacia `media-intel-backend` para hablar con Postgres) se reusó ese mismo día para correr un backfill de reconciliación desde una instancia de chepita en vez de Clipper — ver el incidente y la regla de SG pendiente de cerrar más abajo.

**Incidente 2026-07-22 — backfill tumbó la API de producción:** un script de reconciliación S3→Postgres (`scripts/backfill_transcriptions_from_s3.py`, ver `docs/INGESTION_DESIGN.md`) se corrió con 20 hilos concurrentes y un `commit()` por fila **desde la misma `t3.small` que aloja Postgres** (`media-intel-mvp-backend`) — la saturó lo suficiente (CPU y fsync de WAL) para dejar `/health` sin responder y el agente SSM sin ejecutar comandos nuevos. Se resolvió con `aws ec2 reboot-instances` (los contenedores tienen `restart: unless-stopped`, volvieron solos). Lección: **cualquier backfill/reconciliación de volumen alto debe correr desde una instancia separada** (chepita u otra), nunca desde el host de Postgres — para eso existe `scripts/backfill_transcriptions_from_s3_standalone.py` (sin dependencia del paquete `src`, concurrencia baja por default, commits en batch).

**Pendiente de limpieza:** la regla de ingreso `sgr-0c597009d2198a293` en `sg-0b0ba7ad930b6632b` (puerto 5433 desde `sg-033ac2dd79d76f56a`, el SG de chepita) sigue abierta — se agregó para correr el backfill de reconciliación desde chepita el 2026-07-22 y ya no hace falta. El usuario IAM `media-intelligence-dev` no tiene `ec2:RevokeSecurityGroupIngress` (solo `Authorize`, asimetría real en su policy) — alguien con más acceso tiene que quitarla a mano o agregar ese permiso.

**Antes de terminar la g6-test** se horneó un AMI desde esa instancia (ya con `worker.py` desplegado, smoke test corrido y pasando justo antes de la captura — 2761 palabras, 0 errores):

| AMI | AMI ID | Base | Descripción |
|---|---|---|---|
| `CHEPITA-L4-v1.1.0` | **`ami-0b3179b61a0f3c625`** | g6.xlarge, GPU L4 | Igual que v1.0.0 + `worker_prefetch.py` con publish a `media-intel-transcription-done`/`DONE_QUEUE_URL` (faltaba en v1.0.0 pese a estar en el repo -- ver historial de versiones abajo). **Usar este para relanzar chepita**, no el AMI viejo. |
| `CHEPITA-L4-g6xlarge-20260719` (v1.0.0, no usar para relanzar) | `ami-098f304c0a5513eba` | g6.xlarge, GPU L4 | Le falta el publish a `media-intel-transcription-done` -- cualquier instancia lanzada desde este AMI transcribe y sube a S3 bien, pero sus resultados nunca llegan a Postgres. Mantener solo como rollback hasta validar v1.1.0 en uso real. |
| `CHEPITA` (anterior, no usar para relanzar producción) | `ami-02b763fe7507a3a11` | — | Horneado desde la instancia de *bench* (T4), no de producción (L4) — el nombre es engañoso, quedó ahí de antes de esta sesión. |

### Versionado de AMIs

Semver (`vMAJOR.MINOR.PATCH`) en el **Name** del AMI — es el único campo confiable para versionar: AWS no permite renombrar un AMI después de creado, solo se puede fijar bien desde el `create-image` que lo genera. (No se pudo además usar un tag `Version` porque el usuario IAM `media-intelligence-dev` no tiene permiso `ec2:CreateTags` sobre AMIs — la tabla de abajo en este documento es la fuente de verdad del versionado, no un tag de AWS.)

**Qué bump corresponde a qué cambio:**

| Bump | Cuándo | Ejemplo |
|---|---|---|
| **MAJOR** (`vX.0.0`) | Cambio incompatible en la base horneada: SO, versión de CUDA/driver, arquitectura de GPU objetivo, versión de Python. Obliga a re-validar todo el pipeline desde cero (benchmarks incluidos, no solo el smoke test). | Migrar de L4 a otra GPU; salto de CUDA 12 a 13. |
| **MINOR** (`vx.Y.0`) | Cambio compatible hacia atrás: nueva versión de `faster-whisper`/`torch`, código de worker nuevo desplegado (ej. el refactor de `TranscriptionProvider`, el manejo de errores con DLQ), nueva dependencia. El worker sigue comportándose igual para lo que ya funcionaba, solo se agregan capacidades. | Agregar `word_timestamps`, agregar el DLQ handler. |
| **PATCH** (`vx.y.Z`) | Parche menor sin cambio de comportamiento: actualización de seguridad del SO, ajuste de config, fix de un bug puntual. | Actualizar paquetes del SO por seguridad. |

**Proceso para hornear una versión nueva:**
1. Confirmar que la instancia fuente pasa el smoke test (0 errores) — igual que se hizo para v1.0.0.
2. `aws ec2 create-image --instance-id <id> --name "CHEPITA-L4-vX.Y.Z" --description "<qué cambio respecto a la version anterior, en <=255 caracteres>"`.
3. Esperar a que quede `available`, registrar la fila nueva en la tabla de abajo (AMI ID, fecha, qué cambió, resultado de la validación).
4. **No borrar la versión anterior de inmediato** — dejarla como rollback hasta confirmar que la nueva funciona en uso real, no solo en el smoke test.
5. Cuando una versión vieja ya no haga falta (varias versiones más nuevas ya validadas en uso real), recién ahí `aws ec2 deregister-image` + borrar el snapshot asociado (`aws ec2 delete-snapshot`) para no acumular costo de storage indefinidamente.

**Historial de versiones:**

| Versión | AMI ID | Fecha | Qué cambió | Validación |
|---|---|---|---|---|
| `v1.0.0` | `ami-098f304c0a5513eba` | 2026-07-19 | Primera versión versionada formalmente (horneada antes de este esquema, el `Name` real en AWS quedó como `CHEPITA-L4-g6xlarge-20260719` — de aquí en adelante los `Name` sí siguen el esquema `vX.Y.Z`). Incluye: Faster-Whisper Small, int8_float16, batch_size=24, word_timestamps, `TranscriptionProvider`, manejo de errores con DLQ. | Smoke test 1 archivo, 2761 palabras, 0 errores, justo antes de capturar el AMI. |
| `v1.1.0` | `ami-0b3179b61a0f3c625` | 2026-07-22 | Fix critico: el `worker_prefetch.py` horneado en v1.0.0 no publicaba a `media-intel-transcription-done` (le faltaba el bloque `DONE_QUEUE_URL`/`send_message`, aunque el codigo ya existia en el repo desde antes) -- 5 de 6 instancias lanzadas desde v1.0.0 transcribian y subian a S3 correctamente pero sus resultados nunca llegaban a Postgres (quedaban `procesando` para siempre). Horneado con `--no-reboot` desde una instancia ya corriendo el `worker_prefetch.py` correcto, sin interrumpir el trabajo en curso. | Confirmado en uso real: throughput de ingesta subio de ~1-2 grabaciones/min a ~30+/min tras relanzar los workers de las 5 instancias con el script corregido. No se corrio un smoke test aislado nuevo -- la validacion fue la corrida de produccion misma. |

**Para relanzar chepita cuando se necesite:**
```bash
aws ec2 run-instances --image-id ami-0b3179b61a0f3c625 --instance-type g6.xlarge \
  --iam-instance-profile Name=<perfil-con-rol-media-intel-ec2-transcribe> \
  --security-group-ids <sg-de-media-intel-transcribe-sg> --subnet-id <subnet-us-east-1a> \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Project,Value=media-intel},{Key=Name,Value=media-intel-chepita-g6}]'
```
(Rellenar `--iam-instance-profile`/`--security-group-ids`/`--subnet-id` con los valores reales al momento de relanzar — no quedaron fijados aquí porque cambian según la VPC vigente.)

**Acceso:** solo vía **AWS Systems Manager (SSM) Run Command** — no hay SSH abierto (el puerto 22 está bloqueado por Security Group; existe `~/.ssh/destroyer-worker.pem` pero no sirve para conexión directa por IP pública). El rol de instancia es `media-intel-ec2-transcribe` (tiene permisos de S3/SQS pero **no** `sqs:ListQueues`, hay que usar la URL directa).

Ejemplo de comando remoto:
```bash
aws ssm send-command --instance-ids <nuevo-instance-id> \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":["<comando>"],"executionTimeout":["900"]}'
# luego:
aws ssm get-command-invocation --command-id <id> --instance-id <nuevo-instance-id>
```

## Worker de transcripción (tal como quedó desplegado y horneado en el AMI)

- `/home/ubuntu/worker.py` — **worker canónico de producción** (con prefetch integrado, ver [scripts/worker_prefetch.py](../scripts/worker_prefetch.py) en este repo para la versión versionada). Carga el modelo Whisper una sola vez, consume la cola SQS en loop, descarga el archivo N+1 en un hilo mientras transcribe N.
- Config actual (defaults en el propio script, no requieren env vars):
  - `WHISPER_MODEL=small`
  - `WHISPER_COMPUTE_TYPE=int8_float16`
  - `WHISPER_BATCH_SIZE=24` ← recomendado tras Fase 1 de optimización
  - Prefetch activo (un hilo adicional, `Queue(maxsize=1)`, sin descargas duplicadas, limpieza automática de temporales)
  - `word_timestamps=True` ← activado 2026-07-19, probado con smoke test (1 archivo, 2761 palabras, 0 errores). Cada corrida ahora sube dos archivos por estación: `<station>.txt` (legible, igual que antes) y `<station>_words.json` (lista plana `[{"index", "word", "start", "end"}, ...]`) — es el input que espera el siguiente paso del pipeline (segmentación narrativa por LLM, por índice de palabra, no por segundos — ver README de [gchiham/mvp-medios](https://github.com/gchiham/mvp-medios) y `docs/PRD.md`).
- Se lanzan **6 workers persistentes** en paralelo (`WORKER_ID=w0`..`w5`), cada uno consumiendo la misma cola SQS — SQS reparte los mensajes automáticamente, sin coordinación extra.
- **`DONE_QUEUE_URL` es obligatoria para que el resultado llegue a Postgres** (`https://sqs.us-east-1.amazonaws.com/050871635829/media-intel-transcription-done`) — sin ella el worker sigue transcribiendo y subiendo a S3 sin error visible, pero nadie se entera en la base de datos (ver historial de versiones, v1.1.0). Exportarla junto al resto de env vars antes de lanzar los 6 workers.
- **No hay systemd unit ni arranque automático** — los workers no se levantan solos al bootear una instancia nueva, hay que lanzarlos a mano vía SSM cada vez (`WORKER_ID=w$i setsid nohup ... &` por cada uno). Pendiente si se quiere automatizar.
- Python del entorno con las dependencias (torch/cuda/faster-whisper): `/opt/pytorch/bin/python3`.
- La transcripción en sí ya no está inline en `worker.py` — se delega a `FasterWhisperProvider` (implementa `TranscriptionProvider`, ver [TRANSCRIPTION_ARCHITECTURE.md](TRANSCRIPTION_ARCHITECTURE.md)). El árbol `src/modules/transcription/` del repo se despliega **verbatim** (mismo código, misma estructura de paquete) a `/home/ubuntu/app/src/modules/transcription/...`, y `worker.py` hace `sys.path.insert(0, "/home/ubuntu/app")` antes de importar. Desplegado y validado 2026-07-19 (smoke test idéntico al de antes del refactor).
- Scripts de utilidad ya presentes en la instancia: `enqueue_10h.py`, `enqueue_20h.py` (encolan un set fijo de archivos de prueba), `analyze.py` (parsea `gpu.csv`/`sys.csv` de una corrida de benchmark), `run_batch.sh` / `run_batch_prefetch.sh` (arma una corrida completa: purga cola, encola, lanza monitoreo GPU/CPU + 6 workers, espera, resume resultado).

## SQS

- Cola: `media-intel-transcription-jobs`
- URL: `https://sqs.us-east-1.amazonaws.com/050871635829/media-intel-transcription-jobs`
- `VisibilityTimeout`: 1800s (suficiente margen para transcribir + prefetch sin que el mensaje vuelva a quedar visible)
- Formato de mensaje (JSON): `{"station": "...", "s3_input": "s3://mediadev-recordings/<estacion>/<año>/<mes>/<archivo>.mp3", "s3_output_prefix": "s3://media-intel-transcribe-050871635829/<carpeta>/<estacion>"}`

## S3

- Bucket de entrada (audio crudo): `mediadev-recordings` — **el capturador sube `.ts`, no `.mp3`** (cambio de formato no reflejado en el regex de `DiscoveryService`, ver `docs/INGESTION_DESIGN.md`). `worker_prefetch.py` no le importa la extensión real (ffmpeg/faster-whisper detectan el formato por contenido, no por nombre de archivo) — se puede seguir descargando y guardando localmente con extensión `.mp3` sin problema.
- Bucket de salida (transcripciones `.txt`/`_words.json`): `media-intel-transcribe-050871635829`
- Bucket de clips de noticia (nuevo, 2026-07-21): `media-intel-clips-050871635829` — privado, SSE-encriptado, un objeto por noticia detectada (`<grabacion_id>/news_XXX.mp3`). Ver `docs/ORCHESTRATOR_DESIGN.md` / `docs/BACKEND_ARCHITECTURE.md` (`ClipStorage`).

## Historial de benchmarking / optimización

Ver [OPTIMIZATION_REPORT.md](OPTIMIZATION_REPORT.md) — Fase de benchmarking cerrada (config base validada) + Fase de optimización (batch tuning, prefetch, propuestas de batch dinámico y autoscaling). Config recomendada y ya implementada en `worker.py`: **g6.xlarge, Whisper Small, Faster-Whisper, int8_float16, batch_size=24, 6 workers persistentes + SQS, prefetch activo**.
