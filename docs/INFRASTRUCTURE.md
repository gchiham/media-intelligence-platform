# Infraestructura AWS — "chepita" (transcripción GPU)

Referencia rápida de la infraestructura real en AWS para el pipeline de transcripción. Ninguno de estos recursos vive en este repo (no hay Terraform/CDK todavía) — todo se administra manualmente vía AWS CLI / consola / SSM. Este documento existe para no tener que redescubrir todo por SSM en cada sesión.

Cuenta AWS: `050871635829` (usuario `media-intelligence-dev`). Región: `us-east-1`.

## Instancias EC2 (tag `Project=media-intel`)

| Nombre | Instance ID | Tipo | AZ | Estado (2026-07-19) | Propósito |
|---|---|---|---|---|---|
| `media-intel-chepita-g6-test` | `i-04ca44c8228a1611a` | g6.xlarge (GPU NVIDIA L4, 23034 MiB VRAM) | us-east-1a | **stopped** | Instancia validada para producción de transcripción. Corre `worker.py` (persistente + SQS). Ver [OPTIMIZATION_REPORT.md](OPTIMIZATION_REPORT.md). |
| `media-intel-chepita-builder` | `i-0b07564ecf6a99da4` | c5.xlarge | us-east-1a | **stopped** | Instancia de build/CPU, no GPU. |
| `media-intel-chepita-bench` | `i-0f54fdb1ea6fa6e0a` | g4dn.xlarge (GPU NVIDIA T4) | us-east-1f | **stopped** | Instancia GPU alternativa usada en benchmarking previo (T4 vs L4). |

Las 3 quedaron detenidas (stop, no terminadas) el 2026-07-19 tras cerrar la fase de optimización. Para reanudar trabajo en la g6-test:

```
aws ec2 start-instances --instance-ids i-04ca44c8228a1611a
```

**Acceso:** solo vía **AWS Systems Manager (SSM) Run Command** — no hay SSH abierto (el puerto 22 está bloqueado por Security Group; existe `~/.ssh/destroyer-worker.pem` pero no sirve para conexión directa por IP pública). El rol de instancia es `media-intel-ec2-transcribe` (tiene permisos de S3/SQS pero **no** `sqs:ListQueues`, hay que usar la URL directa).

Ejemplo de comando remoto:
```bash
aws ssm send-command --instance-ids i-04ca44c8228a1611a \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":["<comando>"],"executionTimeout":["900"]}'
# luego:
aws ssm get-command-invocation --command-id <id> --instance-id i-04ca44c8228a1611a
```

## Worker de transcripción (en `i-04ca44c8228a1611a`)

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
