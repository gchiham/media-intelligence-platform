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
| `CHEPITA-L4-v1.2.0` | **`ami-02c05ccbd6699df41`** | g6.xlarge, GPU L4 | Igual que v1.1.0 + `worker_prefetch.py`/`FasterWhisperProvider` con `large-v3-turbo` por defecto (antes `small`) y soporte de `hotwords` (vocabulario hondureño, ver `docs/EFFICIENCY_REVIEW.md` §5). **Usar este para relanzar chepita**, no un AMI viejo. **Ojo con la config de arranque** — ver más abajo, cambió respecto a v1.1.0. |
| `CHEPITA-L4-v1.1.0` | `ami-0b3179b61a0f3c625` | g6.xlarge, GPU L4 | Fix de `DONE_QUEUE_URL` (ver historial abajo). Sigue en `small`, no en `hotwords` -- superado por v1.2.0. |
| `CHEPITA-L4-g6xlarge-20260719` (v1.0.0, no usar para relanzar) | `ami-098f304c0a5513eba` | g6.xlarge, GPU L4 | Le falta el publish a `media-intel-transcription-done` -- cualquier instancia lanzada desde este AMI transcribe y sube a S3 bien, pero sus resultados nunca llegan a Postgres. Mantener solo como rollback histórico. |
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
| `v1.2.0` | `ami-02c05ccbd6699df41` | 2026-07-24 | `WHISPER_MODEL` por defecto pasa de `small` a `large-v3-turbo` (calidad de nombres propios, ver `docs/EFFICIENCY_REVIEW.md` §5) + soporte de `hotwords` con el vocabulario hondureño (`src/modules/transcription/vocabulary.py`). Horneado con `--no-reboot` desde una instancia ya corriendo el codigo nuevo, sin interrumpir el trabajo en curso. **Requirió bajar la config de arranque de 6 workers/batch_size=24 a 3 workers/batch_size=12** -- turbo pesa mas que small y la config vieja (validada solo para small en `OPTIMIZATION_REPORT.md`) producia `CUDA out of memory` (41 de 66 intentos fallaron en la primera corrida real). | Confirmado en uso real sobre 8 instancias: con 3 workers/batch=12, 0 errores de OOM en >275 archivos procesados. GPU al 100% de utilizacion y ~71-73W de un limite de 72W -- confirma que el techo es la GPU misma, no la config. |

**Para relanzar chepita cuando se necesite:**
```bash
aws ec2 run-instances --image-id ami-02c05ccbd6699df41 --instance-type g6.xlarge \
  --iam-instance-profile Name=media-intel-ec2-transcribe \
  --security-group-ids sg-033ac2dd79d76f56a --subnet-id <subnet-en-una-az-con-capacidad> \
  --count <N> \
  --tag-specifications \
    'ResourceType=instance,Tags=[{Key=Project,Value=media-intel},{Key=Name,Value=media-intel-chepita-g6}]' \
    'ResourceType=volume,Tags=[{Key=Project,Value=media-intel}]' \
    'ResourceType=network-interface,Tags=[{Key=Project,Value=media-intel}]'
```
(La policy de `media-intelligence-dev` exige el tag `Project=media-intel` también en `volume` y `network-interface`, no solo en `instance` -- sin las 3 tag-specifications da `UnauthorizedOperation`, no un error obvio de permisos faltantes. Ver incidente de la primera vez que se topó esto.)

**Capacidad de g6.xlarge es errática por AZ** -- `InsufficientInstanceCapacity` en una zona no significa que no haya en otra, y cuál zona tiene capacidad cambia de una corrida a otra (mismo día, se vio fallar en `us-east-1a` y `us-east-1c` y funcionar en `us-east-1d` en corridas consecutivas). Si falla, reintentar en otra subnet/AZ en vez de asumir que no hay capacidad en la cuenta. Subnets conocidas: `subnet-f67ee591` (1a), `subnet-c099ffee` (1b), `subnet-e98fa4a3` (1c), `subnet-8e9efad2` (1d), `subnet-22d9842d` (1f).

**Los workers no arrancan solos** (no hay systemd unit, ver más abajo) -- hay que lanzarlos a mano por SSM después de que la instancia esté `running` y el agente SSM en `Online`:
```bash
export QUEUE_URL="https://sqs.us-east-1.amazonaws.com/050871635829/media-intel-transcription-jobs"
export DONE_QUEUE_URL="https://sqs.us-east-1.amazonaws.com/050871635829/media-intel-transcription-done"
export WHISPER_MODEL=large-v3-turbo
export WHISPER_COMPUTE_TYPE=int8_float16
export WHISPER_BATCH_SIZE=12
export AWS_DEFAULT_REGION=us-east-1
for i in 0 1 2; do WORKER_ID=w$i setsid nohup /opt/pytorch/bin/python3 /home/ubuntu/worker_prefetch.py < /dev/null > /home/ubuntu/run_logs/worker_w$i.log 2>&1 & done
```

**`WHISPER_BATCH_SIZE=12` y solo 3 workers -- no 24 y 6 como con `small`.** La config de `OPTIMIZATION_REPORT.md` (batch 24, 6 workers) se validó con Whisper Small; con `large-v3-turbo` esa misma config produce `CUDA out of memory` (confirmado: 41 de 66 intentos fallaron en la primera corrida real con turbo antes de bajar a esta config). Con 3 workers/batch=12 la GPU llega al 100% de utilización y ~71-73W de un límite de 72W -- ya no hay margen para subir la concurrencia por instancia, el techo es la GPU misma. Si se necesita más throughput, la palanca es más instancias en paralelo, no más workers por instancia.

**`DONE_QUEUE_URL` sigue siendo obligatoria** (ver v1.1.0 arriba) -- sin ella el worker transcribe y sube a S3 sin error visible, pero el resultado nunca llega a Postgres.

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
- Config actual (defaults en el propio script desde v1.2.0, no requieren env vars salvo que se quiera pisar):
  - `WHISPER_MODEL=large-v3-turbo` ← cambiado 2026-07-22 (antes `small`), ver `docs/EFFICIENCY_REVIEW.md` §5. Ya horneado en v1.2.0 con los pesos precargados; si se relanza desde un AMI más viejo, el primer worker de cada instancia los baja de HuggingFace (~1.6 GB) antes de empezar.
  - `WHISPER_COMPUTE_TYPE=int8_float16`
  - `WHISPER_BATCH_SIZE=12` ← **bajado de 24 a 12 el 2026-07-24**. El 24 venía de Fase 1 de optimización, pero esa medición se hizo con Whisper Small — con `large-v3-turbo` esa misma config (24 + 6 workers) produjo `CUDA out of memory` en un tercio de los intentos. No hay benchmark formal de qué batch es el óptimo para turbo todavía, 12 es el valor que se probó y funcionó, no uno medido con el rigor de `OPTIMIZATION_REPORT.md`.
  - Prefetch activo (un hilo adicional, `Queue(maxsize=1)`, sin descargas duplicadas, limpieza automática de temporales)
  - `word_timestamps=True` ← activado 2026-07-19, probado con smoke test (1 archivo, 2761 palabras, 0 errores). Cada corrida ahora sube dos archivos por estación: `<station>.txt` (legible, igual que antes) y `<station>_words.json` (lista plana `[{"index", "word", "start", "end"}, ...]`) — es el input que espera el siguiente paso del pipeline (segmentación narrativa por LLM, por índice de palabra, no por segundos — ver README de [gchiham/mvp-medios](https://github.com/gchiham/mvp-medios) y `docs/PRD.md`).
- Se lanzan **3 workers persistentes** en paralelo (`WORKER_ID=w0`..`w2`) por instancia, no 6 — bajado junto con el batch por la misma razón de VRAM (ver arriba). Con 3 workers/batch=12 la GPU ya llega al 100% de utilización y ~71-73W de un límite de 72W: no hay margen para subir la concurrencia por instancia, el techo es la GPU misma. Más throughput = más instancias, no más workers por instancia.
- **`DONE_QUEUE_URL` es obligatoria para que el resultado llegue a Postgres** (`https://sqs.us-east-1.amazonaws.com/050871635829/media-intel-transcription-done`) — sin ella el worker sigue transcribiendo y subiendo a S3 sin error visible, pero nadie se entera en la base de datos (ver historial de versiones, v1.1.0). Exportarla junto al resto de env vars antes de lanzar los workers.
- **No hay systemd unit ni arranque automático** — los workers no se levantan solos al bootear una instancia nueva, hay que lanzarlos a mano vía SSM cada vez (`WORKER_ID=w$i setsid nohup ... &` por cada uno, ver el comando completo más arriba). Pendiente si se quiere automatizar.
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

Ver [OPTIMIZATION_REPORT.md](OPTIMIZATION_REPORT.md) — Fase de benchmarking cerrada (config base validada) + Fase de optimización (batch tuning, prefetch, propuestas de batch dinámico y autoscaling). **Esa config (batch_size=24, 6 workers) se midió con Whisper Small y ya no es la vigente** — desde v1.2.0 (2026-07-24) el modelo es `large-v3-turbo` con batch_size=12 y 3 workers (ver sección "Worker de transcripción" arriba). Nadie repitió el benchmark formal de `OPTIMIZATION_REPORT.md` para turbo todavía; sería la siguiente Fase de optimización si se quiere afinar más allá del valor que simplemente funcionó.
