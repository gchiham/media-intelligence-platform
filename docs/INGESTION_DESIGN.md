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
| `DiscoveryService` ([discovery.py](../src/modules/recordings/discovery.py)) | backend, script/cron | Escanea `settings.capture_bucket` por nombre de archivo (regex `.mp3`), crea `Grabacion(estado=pendiente)` por archivo nuevo. Idempotente por `s3_key` único. **Bug conocido, no arreglado en el regex mismo:** el capturador migró a subir `.ts`, no `.mp3` — este servicio los ignora en silencio (categoría "no reconocidas"). Ver `CoverageDiscoveryService` abajo, que es la vía que sí los ve. |
| `CoverageDiscoveryService` ([coverage_discovery.py](../src/modules/recordings/coverage_discovery.py)) | backend, script/cron (`discover_grabaciones_coverage.py`) | Lee `recording_coverage` — tabla de una DB externa de solo lectura que mantiene el sistema capturador (Destroyer), vía `DATABASE_URL_COVERAGE` — en vez de escanear S3. Trae el `s3_key` exacto sin adivinar extensión, así que ve `.ts` sin problema. `stream_id` de esa tabla coincide 1:1 con `Medio.codigo` (confirmado, sin necesidad de mapeo). Corre **en paralelo** a `DiscoveryService`, no lo reemplaza todavía — ambos son idempotentes por `s3_key`, así que no se duplican entre sí. |
| `QueueService` ([queue_service.py](../src/modules/recordings/queue_service.py)) | backend, script/cron | Toma `Grabacion` en `pendiente` (con filtros opcionales `--medio`/`--fecha`/`--fecha-desde`/`--fecha-hasta`), publica a `media-intel-transcription-jobs` (con `grabacion_id`), marca `procesando`. SQS estándar no tiene prioridad — si se necesita procesar un rango de fechas antes que el resto del backlog, hay que purgar la cola y reencolar en el orden deseado (visto en la práctica: reencolar "hoy" primero, resto del backlog después). |
| `worker_prefetch.py` (chepita, modificado) | AMI de chepita | Transcribe, sube a S3, y publica a `media-intel-transcription-done` con el contrato completo (ver abajo). Sigue sin DB. |
| `TranscriptionResultConsumer` ([result_consumer.py](../src/modules/recordings/result_consumer.py)) | backend, script/cron | Consume `media-intel-transcription-done` **en loop hasta que un recibo vuelve vacío** (antes: una sola llamada `receive_message`, tope de 10 mensajes por tick de cron sin importar cuántos workers de chepita produjeran resultados más rápido — arreglado 2026-07-22), crea `Transcripcion`, marca `Grabacion` como `procesada`. |
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
- **Ya arreglado, dejado aquí como advertencia:** `worker_prefetch.py` (chepita) nombraba el audio/txt/words locales por *estación*, no por *job* — cuando llegan muchos trabajos seguidos de la misma estación (el caso normal en producción, no el caso de prueba con 10 estaciones distintas que validó el AMI original), el prefetch del job N+1 descargaba sobre el mismo path que el `_cleanup()` del job N borraba justo después, tumbando el archivo recién descargado antes de usarse (~65% de fallos "No such file or directory" la primera vez que se corrió con volumen real de una sola estación). Arreglado incluyendo `job_id` en el nombre de archivo.
- **Otro bug real encontrado el 2026-07-22, no solo el de arriba:** el AMI `v1.0.0` (`ami-098f304c0a5513eba`) tenía horneada una versión de `worker_prefetch.py` **sin el bloque `DONE_QUEUE_URL`/`send_message` hacia `media-intel-transcription-done` en absoluto** (aunque el código ya existía en `scripts/worker_prefetch.py` del repo desde antes). 5 de 6 instancias lanzadas ese día desde v1.0.0 transcribían y subían a S3 correctamente, pero sus resultados nunca llegaban a Postgres — quedaban `procesando` para siempre sin ningún error visible. Ambos bugs (naming por job_id y publish del done-event) ya están resueltos en `scripts/worker_prefetch.py` del repo y horneados juntos en el AMI **`v1.1.0`** (`ami-0b3179b61a0f3c625`, ver `docs/INFRASTRUCTURE.md`) — usar ese AMI al relanzar chepita, no v1.0.0, y de todas formas seguir redeployando `worker_prefetch.py` a mano si se sospecha que el AMI usado quedó desactualizado respecto al repo (no hay CI que los mantenga sincronizados).
- **Backfill manual cuando ya hay grabaciones huérfanas en `procesando` con su resultado ya en S3** (por el bug de arriba, o por cualquier otra razón que un done-event se pierda): `scripts/backfill_transcriptions_from_s3.py` (depende del paquete `src` del repo, pensado para correr vía `docker compose exec backend ...`) reconcilia listando el bucket una sola vez y cruzando contra `grabaciones.estado=procesando` directamente, sin pasar por la cola. Existe también `scripts/backfill_transcriptions_from_s3_standalone.py` — mismo propósito pero sin dependencia del paquete `src` (solo `psycopg`+`boto3`), concurrencia baja por default y commits en batch; **usar esta variante si hay que correrlo desde una instancia que no sea la del backend/Postgres** (ver el incidente de saturación en `docs/INFRASTRUCTURE.md` — la primera corrida se hizo con la versión pesada, 20 hilos + commit por fila, desde el mismo host que Postgres y tumbó la API).
- El cron de producción (`enqueue_transcriptions.py --limit 500`, cada 5 min, sin `--medio`/`--fecha`) encola de a 500 sin distinguir "lo que se pidió procesar" de "todo el backlog histórico" — si se necesita priorizar un rango de fechas específico, hay que purgar la cola SQS y reencolar en el orden deseado a mano (no hay soporte nativo para prioridad en SQS estándar).

## Infraestructura provisionada para esto

- Cola SQS `media-intel-transcription-done` (nueva).
- IAM role `media-intel-backend` + instance profile del mismo nombre, asociado a la instancia del backend MVP — permisos acotados a `s3:GetObject`/`s3:ListBucket` sobre los dos buckets de media, `s3:PutObject`/`GetObject`/`ListBucket` sobre el bucket de clips (`media-intel-clips-050871635829`, nuevo, ver `docs/ORCHESTRATOR_DESIGN.md`), y `sqs:SendMessage`/`ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` sobre las 3 colas de transcripción. La instancia no tenía ningún IAM role antes de esto.

## `CoverageDiscoveryService` — acceso a la DB externa

`DATABASE_URL_COVERAGE` apunta a una Postgres administrada por DigitalOcean (propiedad del equipo del capturador/Destroyer, fuera de este repo) — rol `coverage_reader_discoverysvc`, `SELECT` acotado únicamente a `recording_coverage` (no ve `s3_scan_log` ni ninguna otra tabla de negocio). Esa Postgres usa **Trusted Sources** (allowlist de IP) en vez de reglas de Security Group — el otro equipo agregó ahí la Elastic IP del backend (`32.196.209.233`, asociada a `media-intel-mvp-backend` específicamente por esta razón). Si el backend alguna vez cambia de IP pública (ej. se recrea sin Elastic IP), `CoverageDiscoveryService` deja de poder conectarse hasta que se le avise al otro equipo la IP nueva.

Query sin filtro de `updated_at`/checkpoint todavía — trae todo `recording_coverage` con `media_type='audio' AND status='uploaded'` en cada corrida, confiando en la idempotencia por `s3_key` (igual que `DiscoveryService`). Tabla todavía chica (miles de filas, no millones); revisar si esto escala cuando crezca mucho el histórico del capturador.
