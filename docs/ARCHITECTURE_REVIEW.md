# ARCHITECTURE_REVIEW.md

Auditoría técnica de la base actual del proyecto (pipeline de segmentación + clipping validado end-to-end, sin S3/PostgreSQL/SQS/API todavía). Solo revisión — ningún cambio aplicado.

**Alcance revisado:** `src/` completo (application, modules/ai, modules/auth, modules/clients, modules/editorial, modules/media, modules/recordings, modules/reports, modules/transcription, infrastructure, shared, api), `pyproject.toml`, `alembic/`, `tests/`, `docs/PRD.md`, `docs/ARCHITECTURE.md`, y `scripts/worker_prefetch.py` (el worker que corre en chepita, porque es parte real del pipeline aunque viva fuera de `src/`).

---

## Lo que está bien

1. **Separación Ports & Adapters real, no solo declarada.** `AIAnalysisProvider` (`src/modules/ai/providers/base.py`) es una interfaz de verdad con un solo método, y `OpenAIAnalysisProvider` la implementa sin filtrar detalles de OpenAI hacia el orquestador. FR-041 (proveedor de LLM intercambiable) está resuelto en código, no solo en el PRD.
2. **El orquestador no sobre-abstrae lo que no necesita ser abstracto.** Solo el paso que realmente va a cambiar de proveedor (el LLM) está detrás de una interfaz. `map_words_to_time` y `clip_audio` se llaman directo — no hay una interfaz `ClippingProvider` ni `TimeMapperProvider` especulativa para algo que siempre va a ser "ffmpeg local". Buen juicio de YAGNI.
3. **`map_words_to_time` busca por `word.index` (dict), no por posición de lista.** Evita una clase entera de bugs si algún día `words` llega desordenado o filtrado — un detalle fácil de pasar por alto que ya está bien resuelto.
4. **Validación defensiva en el adaptador OpenAI.** `_segment_chunk` descarta cualquier `start_word`/`end_word` que el modelo devuelva fuera del rango del chunk que realmente vio (`lo <= start_word <= end_word <= hi`), y usa `response_format` con JSON Schema estricto en vez de confiar en que el LLM devuelva JSON válido por las buenas.
5. **Modelo de datos ya preparado para los requisitos no funcionales del PRD**, no solo para el CRUD básico: `NoticiaVersion` inmutable con `UniqueConstraint(noticia_id, numero_version)` (RN-003/FR-071), `TenantRequiredMixin` aplicado consistentemente en todo lo que es por-cliente, UUID v7 (ordenable por tiempo, pensado para paginación/particionamiento futuro), `Auditoria` genérica separada de `NoticiaVersion` con una nota clara de por qué son dos mecanismos distintos.
6. **Tipado consistente y útil, no decorativo.** `Mapped[...]` en todos los modelos SQLAlchemy, dataclasses tipadas en el orquestador, `Field(ge=0, le=1)` en `NewsSegment.confidence` en vez de un `float` suelto. `Settings.openai_api_key` usa `SecretStr` — se enmascara solo en cualquier `repr`/log accidental.
7. **`ProcessAudioJob` ya deja el seam correcto para S3.** El propio diseño documentado en `ORCHESTRATOR_DESIGN.md` anticipa que integrar S3 después solo debería tocar cómo se construye el `Job`, no el orquestador ni los módulos que coordina. Es una buena señal de que la capa de aplicación está bien cortada.
8. **Chunking sin solape es una decisión explícita y documentada**, no un descuido — está anotado en el docstring de `chunking.py` como limitación conocida y aceptada, con la razón.

---

## Riesgos encontrados

| # | Riesgo | Detalle |
|---|---|---|
| ~~R1~~ | ✅ **Resuelto** — **No había repositorio git.** | `git init` + primer commit se hizo poco después de esta revisión; el repo ya tiene historial completo y está en [GitHub](https://github.com/gchiham/media-intelligence-platform). |
| ~~R2~~ | ✅ **Resuelto** — **El contrato `words.json` no tenía una fuente de verdad compartida.** | Ver [TRANSCRIPTION_ARCHITECTURE.md](TRANSCRIPTION_ARCHITECTURE.md). `Word` se define una sola vez en `src/modules/transcription/models/transcription_models.py`; `ai/schemas.py` la re-exporta (mismo objeto de clase, verificado). `worker_prefetch.py` serializa `Word.model_dump()` en vez de construir el dict a mano. |
| ~~R3~~ | ✅ **Resuelto** — **Cero manejo de errores en las llamadas a OpenAI.** | `_segment_chunk`/`_call_with_retry` (`src/modules/ai/providers/openai_provider.py`) ahora reutiliza `classify_and_wrap` (el mismo mecanismo del DLQ, R4) con hasta 3 intentos y backoff corto (1s, 2s — sincrónico dentro de un request HTTP, no los minutos del worker SQS). Error permanente (ej. API key inválida) no reintenta, error transitorio agotado levanta `SegmentationError` propia. 5 tests nuevos con cliente OpenAI simulado (`tests/test_openai_provider_retry.py`) + confirmado que el camino feliz sigue funcionando contra OpenAI real (los tests end-to-end existentes siguen pasando sin cambios). |
| ~~R4~~ | ✅ **Resuelto** — **El worker de transcripción no tenía manejo de errores ni DLQ verificado.** | Ver [ERROR_HANDLING.md](ERROR_HANDLING.md). Excepciones tipadas (`TransientPipelineError`/`PermanentPipelineError`), backoff vía `ChangeMessageVisibility`, DLQ real verificada (`maxReceiveCount=3`, ya estaba configurada en AWS) con reenvío inmediato para errores permanentes. Validado contra AWS real, incluyendo un bug real encontrado y corregido en la clasificación de errores de S3. |
| R5 | **Despliegue del worker 100% manual y propenso a drift.** | Sigue así — `scripts/worker_prefetch.py` se sincroniza a mano vía `base64` sobre SSM. Parcialmente mitigado desde entonces: existe un AMI horneado (`docs/INFRASTRUCTURE.md`, sección "Versionado de AMIs") que congela una versión conocida del entorno + código desplegado, pero seguir desplegando cambios de código a una instancia viva sigue siendo manual. |
| R6 | **Granularidad de transacción en `ingestion.py`.** | Sigue abierto — `sync_grabaciones` sigue haciendo un solo `session.commit()` al final del loop completo (`ingestion.py:106`). No se tocó. |
| ~~R7~~ | ✅ **Resuelto** — **Ausencia de logging estructurado, pese a estar exigido en `ARCHITECTURE.md`.** | `src/shared/logging_utils.py`: `logging` estándar + `JsonFormatter`, usado por el worker de transcripción (`ERROR_HANDLING.md`) y por el manejo de excepciones de la API (`docs/API.md`). `job_id`/`correlation_id` incluido en cada log de error. `ingestion.py` sigue usando `print()` para su único `[WARN]` — no se tocó, es de bajo impacto. |
| R8 | **`ruff` está declarado como dependencia pero no configurado.** | Sigue abierto — `pyproject.toml` lista `ruff>=0.6` en `dev`, pero sigue sin `[tool.ruff]` ni `ruff.toml`. |

---

## Mejoras recomendadas

1. **Inicializar git ya** (`git init`, primer commit con todo lo actual) — no es negociable antes de seguir agregando código. Ver R1.
2. **Compartir el esquema `Word` entre chepita y el backend.** La forma más simple sin acoplar infraestructura: publicar `schemas.py` (o solo `Word`/`NewsSegment`) como un archivito sin dependencias pesadas que ambos lados importen — o, más barato todavía, un test de contrato que valide que un `words.json` real de chepita parsea contra `Word.model_validate` en el CI del repo. Ver R2.
3. ~~Agregar retries con backoff a `OpenAIAnalysisProvider`~~ — hecho. Ver R3.
4. **Verificar y, si falta, configurar una DLQ real en `media-intel-transcription-jobs`** (`maxReceiveCount` razonable, ej. 3-5) antes de que el worker corra desatendido. Ver R4.
5. **Adoptar el módulo `logging` estándar con formato JSON** (aunque sea el `logging` de la librería estándar con un `Formatter` a JSON, sin traer una dependencia nueva) tanto en `ingestion.py` como en el worker de chepita, incluyendo un `job_id`/`correlation_id` por archivo procesado, tal como pide `NFR-012`. Ver R7.
6. **Configurar `[tool.ruff]`** con al menos las reglas por defecto + `E`, `F`, `I` (imports) y correrlo sobre `src/` una vez para ver la deuda real que revela. Ver R8.
7. **Partir el `commit()` de `ingestion.py` por medio** (o cada N grabaciones) en vez de uno solo al final del loop completo. Ver R6.
8. **Considerar un pequeño overlap de contexto entre chunks** (ej. repetir las últimas 20-30 palabras del chunk anterior en el prompt, marcadas como "contexto, no analices esto de nuevo") para reducir el riesgo de que una noticia real que cae justo en el límite de un chunk quede partida en dos. No es urgente — el diseño actual documenta esta limitación como aceptada — pero es una mejora de precisión relativamente barata si en pruebas con audio real empieza a doler.

---

## Cambios sugeridos antes de producción

Estos no son bloqueantes para seguir prototipando localmente, pero sí antes de que el pipeline procese audio real de forma desatendida:

- ~~Resolver R1 (git) y R4 (DLQ)~~ — hechos, ver arriba.
- Definir un timeout explícito en las llamadas a OpenAI (`OpenAI(api_key=..., timeout=...)`) — hoy usa el default del SDK, que puede ser más largo de lo razonable para un pipeline por lotes. Sigue sin hacerse.
- ~~Decidir qué pasa con un archivo de audio que el worker de chepita falla en transcribir de forma consistente~~ — resuelto por el DLQ (R4): tras `maxReceiveCount=3` va a la DLQ automáticamente, o de inmediato si se clasifica como error permanente.
- Mover las constantes de infraestructura hardcodeadas (`BUCKET = "mediadev-recordings"` en `ingestion.py`, la URL de la cola SQS repetida en varios scripts) a `Settings`, para tener una sola fuente de verdad por ambiente (dev/prod). Sigue sin hacerse.

---

## Componentes que deberían abstraerse

| Componente | ¿Por qué? | Prioridad |
|---|---|---|
| ~~**`TranscriptionProvider`**~~ | ✅ **Resuelto** — ver [TRANSCRIPTION_ARCHITECTURE.md](TRANSCRIPTION_ARCHITECTURE.md). Existe `TranscriptionProvider` (interfaz) + `FasterWhisperProvider` (adapter), mismo patrón que `AIAnalysisProvider`. `worker_prefetch.py` refactorizado para usarlo, validado en chepita sin cambios de comportamiento. | ~~Alta~~ |
| **Fuente del `words.json`** (hoy: ruta local fija) | El propio `ORCHESTRATOR_DESIGN.md` ya lo anticipa: cuando se integre S3, `ProcessAudioJob` va a necesitar poder venir de "ruta local" o "descargar de S3" sin que el orquestador lo sepa. No hace falta abstraerlo *ahora* (sería especular), pero es el próximo candidato obvio. | Media (cuando se integre S3) |

No se identificaron más candidatos urgentes — el resto de los componentes (`clip_audio`, `map_words_to_time`, ingestión de S3) son correctamente concretos porque no hay una necesidad real de intercambiarlos, solo de reusarlos.

---

## Componentes preparados para escalar

- **`AIAnalysisProvider`**: cambiar de OpenAI a Claude/otro proveedor no debería tocar `MediaProcessingOrchestrator` en absoluto — el constructor ya recibe el provider inyectado.
- **Modelo de datos multi-tenant**: `TenantRequiredMixin` + UUID v7 + schema compartido ya están aplicados desde el día uno en los módulos que lo necesitan (`ClienteNoticia`, `MonitoringProfile`, `EtiquetadoPrivado`, `InformeSemanal`) — agregar tenants nuevos no requiere cambios de esquema.
- **`NoticiaVersion` inmutable**: soporta versionado infinito sin rediseño (RN-003), y `Noticia.version_actual_id` como puntero permite lecturas rápidas sin tener que calcular "la última versión" con una subquery cada vez.
- **`MediaProcessingOrchestrator` vía inyección de dependencias**: testeable con providers falsos sin tocar el pipeline real (ya se probó con un provider real en `tests/test_orchestrator_e2e.py`, pero un mock sería igual de simple).

---

## Deuda técnica

| Ítem | Impacto si no se paga |
|---|---|
| ~~Sin git (R1)~~ | **Resuelto** |
| ~~`src/modules/transcription/` vacío, sin `TranscriptionProvider` en código~~ | **Resuelto** — ver `TRANSCRIPTION_ARCHITECTURE.md` |
| ~~Sin logging estructurado / `job_id` (R7, NFR-012)~~ | **Resuelto** — ver `ERROR_HANDLING.md`/`API.md` |
| ~~Cero tests unitarios (solo 1 test de integración end-to-end)~~ | **Resuelto** — 44 tests (unitarios + integración contra Postgres/OpenAI/ffmpeg reales) al momento de esta actualización |
| `ruff` sin configurar (R8) | Bajo — sigue sin resolverse. Por ahora el código es chico y se revisa a mano, pero la deuda crece con cada archivo nuevo |
| Constantes de infraestructura hardcodeadas fuera de `Settings` | Bajo-Medio — sigue sin resolverse (`BUCKET` en `ingestion.py`, URLs de SQS repetidas en scripts) |
| Despliegue manual del worker de transcripción (R5) | Medio — parcialmente mitigado por el AMI versionado (`INFRASTRUCTURE.md`), pero desplegar cambios de código a una instancia viva sigue siendo manual |
| `src/modules/clients/` vacío | Ninguno todavía — es simplemente scope futuro no iniciado, no es una regresión ni un descuido |

---

## Prioridad de cada mejora

| Prioridad | Ítem |
|---|---|
| ~~Alta~~ | ~~Inicializar git (R1)~~ — hecho |
| ~~Alta~~ | ~~Definir `TranscriptionProvider` como interfaz en código~~ — hecho |
| ~~Alta~~ | ~~Verificar/configurar DLQ real en la cola SQS de transcripción (R4)~~ — hecho |
| ~~Media~~ | ~~Compartir el esquema `Word` entre chepita y el backend~~ — hecho (R2) |
| ~~Media~~ | ~~Retries + excepción de dominio propia alrededor de las llamadas a OpenAI en `openai_provider.py` (R3)~~ — hecho. No queda nada abierto de prioridad Media/Alta; solo quedan los dos ítems de prioridad Baja. |
| ~~Media~~ | ~~Logging estructurado con `job_id` (R7, NFR-012)~~ — hecho |
| ~~Media~~ | ~~Tests unitarios de `chunking.py`/`mapping.py`/validación de rangos~~ — hecho |
| **Baja** | Configurar `[tool.ruff]` (R8) |
| **Baja** | Partir el commit de `ingestion.py` por medio en vez de uno global (R6) |
| **Baja** | Mover constantes de infraestructura hardcodeadas a `Settings` |
| **Baja** | Overlap de contexto entre chunks para segmentación |

---

**No se aplicó ningún cambio de código en esta revisión** — es solo el diagnóstico, tal como se pidió. Cuando decidas qué atacar primero, lo implementamos uno por uno.
