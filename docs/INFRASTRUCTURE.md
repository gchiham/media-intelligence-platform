# Infraestructura AWS — "chepita" (transcripción GPU)

Referencia rápida de la infraestructura real en AWS para el pipeline de transcripción. Ninguno de estos recursos vive en este repo (no hay Terraform/CDK todavía) — todo se administra manualmente vía AWS CLI / consola / SSM. Este documento existe para no tener que redescubrir todo por SSM en cada sesión.

Cuenta AWS: `050871635829` (usuario `media-intelligence-dev`). Región: `us-east-1`.

## Instancias EC2 (tag `Project=media-intel`)

**Las 3 instancias originales fueron TERMINADAS el 2026-07-19** (no solo detenidas): `media-intel-chepita-g6-test` (`i-04ca44c8228a1611a`, g6.xlarge/L4), `media-intel-chepita-bench` (`i-0f54fdb1ea6fa6e0a`, g4dn.xlarge/T4), `media-intel-chepita-builder` (`i-0b07564ecf6a99da4`, c5.xlarge). No existen más — sus Instance IDs ya no son válidos, no intentar `start-instances` con ellos.

**Antes de terminar la g6-test** se horneó un AMI desde esa instancia (ya con `worker.py` desplegado, smoke test corrido y pasando justo antes de la captura — 2761 palabras, 0 errores):

| AMI | AMI ID | Base | Descripción |
|---|---|---|---|
| `CHEPITA-L4-g6xlarge-20260719` | **`ami-098f304c0a5513eba`** | g6.xlarge, GPU L4 | Faster-Whisper Small, int8_float16, batch_size=24, word_timestamps, TranscriptionProvider + DLQ ya desplegados. **Usar este para relanzar chepita**, no el AMI viejo. |
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

**Para relanzar chepita cuando se necesite:**
```bash
aws ec2 run-instances --image-id ami-098f304c0a5513eba --instance-type g6.xlarge \
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
- Python del entorno con las dependencias (torch/cuda/faster-whisper): `/opt/pytorch/bin/python3`.
- La transcripción en sí ya no está inline en `worker.py` — se delega a `FasterWhisperProvider` (implementa `TranscriptionProvider`, ver [TRANSCRIPTION_ARCHITECTURE.md](TRANSCRIPTION_ARCHITECTURE.md)). El árbol `src/modules/transcription/` del repo se despliega **verbatim** (mismo código, misma estructura de paquete) a `/home/ubuntu/app/src/modules/transcription/...`, y `worker.py` hace `sys.path.insert(0, "/home/ubuntu/app")` antes de importar. Desplegado y validado 2026-07-19 (smoke test idéntico al de antes del refactor).
- Scripts de utilidad ya presentes en la instancia: `enqueue_10h.py`, `enqueue_20h.py` (encolan un set fijo de archivos de prueba), `analyze.py` (parsea `gpu.csv`/`sys.csv` de una corrida de benchmark), `run_batch.sh` / `run_batch_prefetch.sh` (arma una corrida completa: purga cola, encola, lanza monitoreo GPU/CPU + 6 workers, espera, resume resultado).

## SQS

- Cola: `media-intel-transcription-jobs`
- URL: `https://sqs.us-east-1.amazonaws.com/050871635829/media-intel-transcription-jobs`
- `VisibilityTimeout`: 1800s (suficiente margen para transcribir + prefetch sin que el mensaje vuelva a quedar visible)
- Formato de mensaje (JSON): `{"station": "...", "s3_input": "s3://mediadev-recordings/<estacion>/<año>/<mes>/<archivo>.mp3", "s3_output_prefix": "s3://media-intel-transcribe-050871635829/<carpeta>/<estacion>"}`

## S3

- Bucket de entrada (audio crudo): `mediadev-recordings`
- Bucket de salida (transcripciones `.txt`): `media-intel-transcribe-050871635829`

## Historial de benchmarking / optimización

Ver [OPTIMIZATION_REPORT.md](OPTIMIZATION_REPORT.md) — Fase de benchmarking cerrada (config base validada) + Fase de optimización (batch tuning, prefetch, propuestas de batch dinámico y autoscaling). Config recomendada y ya implementada en `worker.py`: **g6.xlarge, Whisper Small, Faster-Whisper, int8_float16, batch_size=24, 6 workers persistentes + SQS, prefetch activo**.
