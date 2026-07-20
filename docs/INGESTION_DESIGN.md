# INGESTION_DESIGN.md

Cierra el círculo entre chepita (transcripción GPU, S3+SQS puro) y el backend MVP (Postgres, flujo editorial ya funcionando): quién descubre grabaciones nuevas, quién las encola, quién registra el resultado, y cómo `POST /pipeline/process` sabe qué ya está listo. No modifica el flujo editorial existente ni `MediaProcessingOrchestrator` — los extiende.

## Principios

- **PostgreSQL es la fuente de verdad del estado.** S3 solo almacena archivos. Nunca se infiere el estado de una `Grabacion` leyendo S3 — siempre se consulta Postgres.
- **Chepita no conoce PostgreSQL.** Las instancias GPU siguen sin credenciales de DB — solo SQS + S3, igual que hoy.
- **Sin bus de eventos dedicado.** Las transiciones de `Grabacion.estado`/`PipelineRun.estado` en Postgres, más los mensajes SQS, son los "eventos" de esta fase. Nada de EventBridge/Kafka/Redis Streams todavía.
- **Mensajes SQS autosuficientes.** Cada mensaje trae todo lo necesario para procesarlo — ningún consumidor adivina datos ni deriva keys de otras.
- **Todo consumidor es idempotente.** Un mensaje puede llegar dos veces (SQS es at-least-once); el resultado final debe ser el mismo que si hubiera llegado una vez.
- **Procesos separados en código, mismo deploy en infraestructura.** Discovery/Queue/Consumers son módulos + scripts distintos, pero corren en el mismo backend (cron/script), no como servicios nuevos — se separan en infraestructura solo cuando el crecimiento lo justifique.

## Componentes

| Componente | Vive en | Responsabilidad |
|---|---|---|
| `DiscoveryService` ([discovery.py](../src/modules/recordings/discovery.py)) | backend, script/cron | Escanea `settings.capture_bucket`, crea `Grabacion(estado=pendiente)` por archivo nuevo. Idempotente por `s3_key` único. |
| `QueueService` ([queue_service.py](../src/modules/recordings/queue_service.py)) | backend, script/cron | Toma `Grabacion` en `pendiente`, publica a `media-intel-transcription-jobs` (con `grabacion_id`), marca `procesando`. |
| `worker_prefetch.py` (chepita, modificado) | AMI de chepita | Transcribe, sube a S3, y publica a `media-intel-transcription-done` con el contrato completo (ver abajo). Sigue sin DB. |
| `TranscriptionResultConsumer` ([result_consumer.py](../src/modules/recordings/result_consumer.py)) | backend, script/cron | Consume `media-intel-transcription-done`, crea `Transcripcion`, marca `Grabacion` como `procesada`. |
| `TranscriptionFailureConsumer` ([result_consumer.py](../src/modules/recordings/result_consumer.py)) | backend, script/cron | Consume la DLQ existente, marca `Grabacion` como `error` con el motivo. |
| `S3RecordingResolver` ([resolvers.py](../src/modules/pipeline/resolvers.py)) | backend, vía `RECORDING_RESOLVER=s3` | Implementa `RecordingResolver`: descarga el audio de S3, escribe el words.json a partir de `Transcripcion.segmentos` (ya en Postgres — nunca se vuelve a leer de S3). |

## Flujo completo

```
S3 captura (mediadev-recordings)
  │ DiscoveryService
  ▼
Grabacion(pendiente) ── Postgres ──
  │ QueueService
  ▼
SQS media-intel-transcription-jobs { grabacion_id, station, s3_input, s3_output_prefix }
  │
  ▼
Grabacion.estado = procesando
  │
  ▼
chepita worker_prefetch.py — transcribe, sube .txt + _words.json a S3
  │
  ├─ éxito → SQS media-intel-transcription-done → TranscriptionResultConsumer
  │             → Transcripcion creada, Grabacion.estado = procesada
  │
  └─ falla permanente → DLQ → TranscriptionFailureConsumer → Grabacion.estado = error

POST /api/v1/pipeline/process { recording_id }
  │ S3RecordingResolver (audio de S3 + words de Postgres)
  │ PipelineRunService: si ya hay PipelineRun COMPLETADO para esta grabacion, lo devuelve tal cual (idempotencia)
  ▼
PipelineRun(completado) + Noticia/NoticiaVersion(pendiente)
  │
  ▼
flujo editorial ya construido (GET /news/pending → start-review → draft → approve/reject)
```

## Contratos SQS

**`media-intel-transcription-jobs`** (existente, con un campo nuevo):
```json
{
  "grabacion_id": "uuid",
  "station": "string",
  "s3_input": "s3://mediadev-recordings/...",
  "s3_output_prefix": "s3://media-intel-transcribe-.../transcripts/<station>/<fecha_inicio>"
}
```

**`media-intel-transcription-done`** (nueva):
```json
{
  "grabacion_id": "uuid",
  "station": "string",
  "audio_s3_uri": "s3://...",
  "transcription_txt_s3_uri": "s3://...",
  "words_json_s3_uri": "s3://...",
  "duration_seconds": 3600.0,
  "language": "es",
  "status": "completed",
  "worker_id": "string",
  "started_at": "iso8601",
  "finished_at": "iso8601"
}
```

**DLQ** (`media-intel-transcription-jobs-dlq`, existente, sin cambio de contrato — ver [ERROR_HANDLING.md](ERROR_HANDLING.md)): envelope `{"original_job", "error"}` en el camino de fallo permanente inmediato, o solo el body original crudo cuando `RedrivePolicy` mueve el mensaje sin pasar por nuestro código.

## Idempotencia

| Paso | Mecanismo |
|---|---|
| Discovery no duplica `Grabacion` | `s3_key` único; se chequea `get_by_s3_key` antes de insertar |
| Queue no re-encola | Solo lee `estado=pendiente`; pasa a `procesando` en la misma pasada |
| Result consumer no duplica `Transcripcion` | `grabacion_id` único en `transcripciones`; si ya existe, no reinserta (solo borra el mensaje) |
| Pipeline no genera `Noticia` duplicada | `PipelineRunService.run()` devuelve el `PipelineRun` `completado` existente si ya hay uno para esa `grabacion_id`, sin volver a correr el orquestador |

## Manejo de errores

- Transcripción falla permanentemente → DLQ (ya existente, sin cambios) → `TranscriptionFailureConsumer` refleja el fallo en `Grabacion.estado=error` + `error_mensaje` (columna nueva).
- Result/Failure consumer fallan a mitad de camino (ej. Postgres caído) → el mensaje SQS no se borra hasta que el commit en Postgres se confirma, así que se reintenta solo en la siguiente pasada.
- `pipeline/process` sigue usando la clasificación de errores ya existente (`PermanentPipelineError`/`TransientPipelineError`, retry con backoff) — sin cambios.

## Cambios en PostgreSQL

- `grabaciones.estado`: índice nuevo (se filtra por estado constantemente).
- `grabaciones.error_mensaje` (Text, nullable): columna nueva, mismo patrón que `pipeline_runs.error_mensaje`.
- Sin cambios de schema en `transcripciones`/`pipeline_runs`/`noticias` — ya tenían todo lo necesario.
- Seed de datos (no es migración): `medios`/`programas` para las estaciones reales (`scripts/seed_medios.py`, ya existía desde el commit inicial -- fuente de verdad `config/stations.json` de mediaCAP; se le agregaron 4 estaciones que ya tienen archivos reales en S3 pero no estaban sembradas: `suave_fm_teg`, `super_100`, `tnh`, `tsi`) — bloqueante para que Discovery pueda resolver `programa_id`.

## Riesgos conocidos

- La lista de estaciones/tipo de medio en el seeder es una inferencia por nombre de carpeta S3, no una fuente oficial (no hay `config/stations.json` de mediaCAP en este repo) — revisar antes de confiar en reportes que separen por tipo de medio.
- Si `TranscriptionResultConsumer` deja de correr (cron caído) sin que nadie lo note, `Grabacion` queda en `procesando` indefinidamente aunque S3 ya tenga el resultado — vale la pena una alarma simple ("grabaciones en `procesando` hace más de N horas").
- Backlog grande de una sola pasada (miles de archivos): Discovery/Queue no tienen límite de paginación real (`enqueue_pending` sí tiene `limit`, default 500) — correr en lotes si el volumen es muy grande, no de una sola vez sin verificar.

## Infraestructura provisionada para esto

- Cola SQS `media-intel-transcription-done` (nueva).
- IAM role `media-intel-backend` + instance profile del mismo nombre, asociado a la instancia del backend MVP — permisos acotados a `s3:GetObject`/`s3:ListBucket` sobre los dos buckets de media, y `sqs:SendMessage`/`ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` sobre las 3 colas de transcripción. La instancia no tenía ningún IAM role antes de esto.
