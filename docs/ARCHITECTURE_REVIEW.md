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
| R1 | **No hay repositorio git.** | `git status` en la raíz del proyecto devuelve "not a git repository". Todo el código de esta sesión (orquestador, providers, modelos) no tiene historial, no se puede diffear, no hay forma de revertir un cambio ni de abrir un PR. Es el riesgo más alto de toda la revisión — no es un problema de arquitectura de código, es la ausencia total de la red de seguridad básica antes de seguir construyendo. |
| ~~R2~~ | ✅ **Resuelto** — **El contrato `words.json` no tenía una fuente de verdad compartida.** | Ver [TRANSCRIPTION_ARCHITECTURE.md](TRANSCRIPTION_ARCHITECTURE.md). `Word` se define una sola vez en `src/modules/transcription/models/transcription_models.py`; `ai/schemas.py` la re-exporta (mismo objeto de clase, verificado). `worker_prefetch.py` serializa `Word.model_dump()` en vez de construir el dict a mano. |
| R3 | **Cero manejo de errores en las llamadas a OpenAI.** | `_segment_chunk` no atrapa `RateLimitError`, `APIConnectionError`, ni el caso de `response.choices[0].message.content` viniendo `None`. Un fallo transitorio de red o un rate limit tumba todo `segment_news` sin reintento — y ya vimos en esta misma sesión que la cuenta de OpenAI puede devolver 429 sin previo aviso. |
| R4 | **El worker de transcripción no tiene manejo de errores ni DLQ verificado.** | `worker.py`/`worker_prefetch.py` en chepita no envuelve `pipeline.transcribe(...)` ni la subida a S3 en un `try/except`. Si una excepción ocurre a mitad de un archivo, el mensaje SQS nunca se borra — vuelve a quedar visible tras el `VisibilityTimeout` (1800s) y se reintenta indefinidamente. `ARCHITECTURE.md` dice "Dead Letter Queues (DLQ) should be enabled", pero no hay evidencia de que la cola real tenga `maxReceiveCount`/DLQ configurado — un archivo corrupto podría reintentar para siempre sin que nadie se entere. |
| R5 | **Despliegue del worker 100% manual y propenso a drift.** | `scripts/worker_prefetch.py` se sincroniza a `/home/ubuntu/worker.py` en chepita a mano, vía `base64` sobre SSM, en cada sesión. Ya documentado en `INFRASTRUCTURE.md`, pero vale repetirlo aquí como riesgo arquitectónico: no hay forma de saber, sin conectarse a la instancia, si el código desplegado coincide con el del repo en un momento dado. |
| R6 | **Granularidad de transacción en `ingestion.py`.** | `sync_grabaciones` acumula inserts para *todos* los medios en una sola `Session` y hace `session.commit()` una sola vez al final del loop completo (`ingestion.py:106`). Un error no relacionado con duplicados (ej. una `ForeignKeyViolation` por un `programa_id` inconsistente a mitad del loop) descarta todo el trabajo acumulado de los medios anteriores, no solo el que falló. |
| R7 | **Ausencia de logging estructurado, pese a estar exigido en `ARCHITECTURE.md`.** | El documento de arquitectura pide "Structured JSON logs" (sección 11) como parte del MVP. Lo que existe hoy es `print()` (`ingestion.py:82`) y una función `log()` casera con formato de texto plano en el worker de chepita — nada emite JSON, no hay niveles (`INFO`/`WARN`/`ERROR`), no hay `job_id` de correlación pese a que `NFR-012` lo pide explícitamente. |
| R8 | **`ruff` está declarado como dependencia pero no configurado.** | `pyproject.toml` lista `ruff>=0.6` en `dev`, pero no hay sección `[tool.ruff]` ni `ruff.toml`. Sin reglas definidas, `ruff check` no aplica ningún estándar real — la herramienta está presente pero inerte. |

---

## Mejoras recomendadas

1. **Inicializar git ya** (`git init`, primer commit con todo lo actual) — no es negociable antes de seguir agregando código. Ver R1.
2. **Compartir el esquema `Word` entre chepita y el backend.** La forma más simple sin acoplar infraestructura: publicar `schemas.py` (o solo `Word`/`NewsSegment`) como un archivito sin dependencias pesadas que ambos lados importen — o, más barato todavía, un test de contrato que valide que un `words.json` real de chepita parsea contra `Word.model_validate` en el CI del repo. Ver R2.
3. **Agregar retries con backoff a `OpenAIAnalysisProvider`.** El SDK de OpenAI ya reintenta algunos errores por defecto, pero no está configurado explícitamente ni cubre `insufficient_quota`/429 sostenido. Envolver `_segment_chunk` con un backoff corto (2-3 intentos) y una excepción de dominio propia (`SegmentationError`) en vez de dejar pasar la excepción cruda del SDK. Ver R3.
4. **Verificar y, si falta, configurar una DLQ real en `media-intel-transcription-jobs`** (`maxReceiveCount` razonable, ej. 3-5) antes de que el worker corra desatendido. Ver R4.
5. **Adoptar el módulo `logging` estándar con formato JSON** (aunque sea el `logging` de la librería estándar con un `Formatter` a JSON, sin traer una dependencia nueva) tanto en `ingestion.py` como en el worker de chepita, incluyendo un `job_id`/`correlation_id` por archivo procesado, tal como pide `NFR-012`. Ver R7.
6. **Configurar `[tool.ruff]`** con al menos las reglas por defecto + `E`, `F`, `I` (imports) y correrlo sobre `src/` una vez para ver la deuda real que revela. Ver R8.
7. **Partir el `commit()` de `ingestion.py` por medio** (o cada N grabaciones) en vez de uno solo al final del loop completo. Ver R6.
8. **Considerar un pequeño overlap de contexto entre chunks** (ej. repetir las últimas 20-30 palabras del chunk anterior en el prompt, marcadas como "contexto, no analices esto de nuevo") para reducir el riesgo de que una noticia real que cae justo en el límite de un chunk quede partida en dos. No es urgente — el diseño actual documenta esta limitación como aceptada — pero es una mejora de precisión relativamente barata si en pruebas con audio real empieza a doler.

---

## Cambios sugeridos antes de producción

Estos no son bloqueantes para seguir prototipando localmente, pero sí antes de que el pipeline procese audio real de forma desatendida:

- Resolver R1 (git) y R4 (DLQ) — son los dos únicos ítems de esta lista con riesgo real de pérdida de trabajo/reintento infinito.
- Definir un timeout explícito en las llamadas a OpenAI (`OpenAI(api_key=..., timeout=...)`) — hoy usa el default del SDK, que puede ser más largo de lo razonable para un pipeline por lotes.
- Decidir qué pasa con un archivo de audio que el worker de chepita falla en transcribir de forma consistente (¿cuántos reintentos antes de moverlo a una cola de "revisión manual"? Hoy la respuesta implícita es "reintenta para siempre").
- Mover las constantes de infraestructura hardcodeadas (`BUCKET = "mediadev-recordings"` en `ingestion.py`, la URL de la cola SQS repetida en varios scripts) a `Settings`, para tener una sola fuente de verdad por ambiente (dev/prod).

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
| Sin git (R1) | Alto — cero trazabilidad, cero forma segura de experimentar |
| `src/modules/transcription/` vacío, sin `TranscriptionProvider` en código | Alto a mediano plazo — bloquea abstraer infraestructura de transcripción sin tocar todo |
| Sin logging estructurado / `job_id` (R7, NFR-012) | Medio — hoy no duele porque todo se opera a mano viendo logs de SSM; va a doler en cuanto haya más de un worker corriendo desatendido |
| Cero tests unitarios (solo 1 test de integración end-to-end) | Medio — `chunking.py`, `mapping.py` (casos límite: padding que cruza 0, `audio_duration` menor al `end_time`), y la lógica de descarte de rangos inválidos en `openai_provider.py` no tienen ni un test unitario, solo quedaron validados manualmente en esta sesión |
| `ruff` sin configurar (R8) | Bajo — por ahora el código es chico y se revisa a mano, pero la deuda crece con cada archivo nuevo |
| Constantes de infraestructura hardcodeadas fuera de `Settings` | Bajo-Medio — hoy solo hay un ambiente (dev), pero duplicar `BUCKET`/queue URLs en varios archivos es exactamente el tipo de cosa que se desincroniza al agregar un ambiente de producción |
| `src/modules/clients/` vacío | Ninguno todavía — es simplemente scope futuro no iniciado, no es una regresión ni un descuido |

---

## Prioridad de cada mejora

| Prioridad | Ítem |
|---|---|
| **Alta** | Inicializar git (R1) |
| **Alta** | Definir `TranscriptionProvider` como interfaz en código (aunque el único adapter siga siendo chepita por ahora) |
| **Alta** | Verificar/configurar DLQ real en la cola SQS de transcripción (R4) |
| **Media** | Compartir el esquema `Word` entre chepita y el backend, o al menos un test de contrato (R2) |
| **Media** | Retries + excepción de dominio propia alrededor de las llamadas a OpenAI (R3) |
| **Media** | Logging estructurado con `job_id` (R7, NFR-012) |
| **Media** | Tests unitarios de `chunking.py`/`mapping.py`/validación de rangos en `openai_provider.py` |
| **Baja** | Configurar `[tool.ruff]` (R8) |
| **Baja** | Partir el commit de `ingestion.py` por medio en vez de uno global (R6) |
| **Baja** | Mover constantes de infraestructura hardcodeadas a `Settings` |
| **Baja** | Overlap de contexto entre chunks para segmentación |

---

**No se aplicó ningún cambio de código en esta revisión** — es solo el diagnóstico, tal como se pidió. Cuando decidas qué atacar primero, lo implementamos uno por uno.
