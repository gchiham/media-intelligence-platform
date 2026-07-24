# TRANSCRIPTION_ARCHITECTURE.md

Refactor de deuda técnica: el módulo de transcripción ahora sigue exactamente el mismo patrón que `AIAnalysisProvider` (ver [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md), sección "Componentes que deberían abstraerse"). **No cambia el comportamiento de chepita** — mismo motor, mismos parámetros, mismo rendimiento validado en [OPTIMIZATION_REPORT.md](OPTIMIZATION_REPORT.md). Solo se desacopló detrás de una interfaz.

## Antes / Después

**Antes:** `scripts/worker_prefetch.py` instanciaba `WhisperModel`/`BatchedInferencePipeline` directamente, llamaba `pipeline.transcribe(...)` en línea, y construía el diccionario de `words.json` a mano dentro del loop principal — la lógica de transcripción y la lógica de orquestación de SQS/S3/prefetch estaban mezcladas en un solo archivo.

**Después:** `worker_prefetch.py` solo coordina SQS/S3/prefetch (sin cambios ahí) y delega toda la transcripción a un `TranscriptionProvider` inyectado — igual que `MediaProcessingOrchestrator` delega la segmentación a un `AIAnalysisProvider` inyectado.

## Componentes

```
src/modules/transcription/
├── models/
│   └── transcription_models.py   -- Word, TranscriptionSegment, TranscriptionResult
└── providers/
    ├── transcription_provider.py       -- interfaz abstracta TranscriptionProvider
    └── faster_whisper_provider.py      -- FasterWhisperProvider (adapter)
```

### `TranscriptionProvider` (puerto)

```python
class TranscriptionProvider(ABC):
    @abstractmethod
    def transcribe(self, audio_path: Path) -> TranscriptionResult: ...
```

Un solo método, igual de angosto que `AIAnalysisProvider.segment_news`. Cualquier motor futuro (Whisper API, WhisperX, otro) se conecta implementando esto — el resto de la aplicación nunca sabe cuál es.

### `FasterWhisperProvider` (adapter)

Los defaults del constructor **son** la configuración de producción validada:

```python
FasterWhisperProvider(
    model_name="large-v3-turbo",  # cambiado desde "small" -- EFFICIENCY_REVIEW.md §5
    compute_type="int8_float16",
    batch_size=24,       # optimo validado en OPTIMIZATION_REPORT.md, Fase 1
    language="es",
    vad_filter=True,
    device="cuda",
    hotwords=None,       # None => vocabulario hondureño de vocabulary.py
)
```

**Sobre `model_name`:** era `small` hasta 2026-07-22. El cambio a `large-v3-turbo` no busca velocidad sino **exactitud en nombres propios** — en monitoreo de medios, un nombre mal transcrito es una mención que el cliente nunca encuentra. turbo retiene >95% de la exactitud de large-v3 con ~1.6 GB de VRAM en int8 (la L4 tiene 23 GB). Ver `docs/EFFICIENCY_REVIEW.md` §5.

**Sobre `hotwords`:** sesga el decoder hacia términos hondureños (ver [vocabulary.py](../src/modules/transcription/vocabulary.py)). La ventana de condicionamiento es de ~224 tokens, así que la lista es corta y curada a propósito — no es un volcado del catálogo `Entidad`. El provider detecta en `__init__` si la versión instalada de faster-whisper acepta el kwarg; si no, transcribe sin sesgo en vez de fallar.

`word_timestamps=True` no es un parámetro configurable — es inherente al contrato (`TranscriptionResult.words` siempre existe), así que quedó fijo dentro de `transcribe()`.

**Import perezoso de `faster_whisper`:** el `import` real está dentro de `__init__`, no a nivel de módulo. Esto permite que cualquier otra parte de la aplicación (o los tests) importe `FasterWhisperProvider`/`TranscriptionProvider` sin tener `torch`/CUDA instalado — solo hace falta donde efectivamente se instancia el provider (chepita). Verificado en esta sesión: el import de la clase funciona en la máquina de desarrollo (sin `faster_whisper` instalado); solo instanciarla fallaría ahí.

### Contrato único de `words.json`

`Word` se define **una sola vez**, en `src/modules/transcription/models/transcription_models.py`. `src/modules/ai/schemas.py` ya no define su propia copia — la re-exporta:

```python
# src/modules/ai/schemas.py
from src.modules.transcription.models.transcription_models import Word
```

Verificado: `Word` (importado desde `ai.schemas`) y `Word` (importado desde `transcription.models`) son literalmente el mismo objeto clase de Python (`is`, no solo `==`). No hay dos definiciones que puedan divergir.

`worker_prefetch.py` ya no construye el JSON a mano — serializa directamente los objetos `Word` que devuelve el provider:

```python
json.dump([w.model_dump() for w in result.words], f, ensure_ascii=False)
```

## Dónde queda el código específico de chepita

El shim de `LD_LIBRARY_PATH` (ruta a `cublas`/`cudnn` de la AMI de chepita) **no** se movió dentro de `FasterWhisperProvider` — se queda en `worker_prefetch.py`, que es el script de despliegue específico de esa instancia. `FasterWhisperProvider` no sabe nada de rutas de `/opt/pytorch`; es portable a cualquier máquina con `faster-whisper` instalado de forma estándar.

## Despliegue

Chepita no tiene un clone del repo — corre scripts sueltos en `/home/ubuntu/` (ver [INFRASTRUCTURE.md](INFRASTRUCTURE.md)). Para que `worker.py` pueda hacer `from src.modules.transcription.providers.faster_whisper_provider import FasterWhisperProvider`, el árbol `src/modules/transcription/` (con sus `__init__.py`) se copia **verbatim** — mismo código, mismos imports — a `/home/ubuntu/app/src/modules/transcription/...`, y `worker_prefetch.py` agrega esa ruta a `sys.path` antes de importar:

```python
sys.path.insert(0, "/home/ubuntu/app")
from src.modules.transcription.providers.faster_whisper_provider import FasterWhisperProvider
```

No es una copia con imports adaptados ni un "flatten" — es literalmente el mismo archivo del repo, en la misma estructura de paquete, solo con la raíz montada en otro lugar. Cero riesgo de que el código desplegado diverja del código fuente por una traducción manual de imports.

Este empaquetado (tar + base64 sobre SSM) sigue siendo manual, igual que el resto del despliegue de chepita — ya documentado como deuda técnica (R5) en `ARCHITECTURE_REVIEW.md` y fuera de alcance de este refactor.

## Validación

Refactor probado en la instancia real (chepita), no solo localmente:

1. Se desplegó el árbol `src/modules/transcription/` completo a `/home/ubuntu/app/`.
2. Se desplegó `worker_prefetch.py` refactorizado como `worker.py`.
3. Smoke test: 1 archivo real (`radio_satelite`, 2761 palabras) procesado con el worker refactorizado.
4. Resultado idéntico al de antes del refactor: mismas 2761 palabras, mismos valores exactos de `index`/`word`/`start`/`end` en los primeros y últimos elementos, `elapsed=15.1s` (vs `15.2s` de la corrida anterior — dentro del ruido normal), 0 errores.
5. Localmente: `Word` importado desde `ai.schemas` y desde `transcription.models` es el mismo objeto de clase; `FasterWhisperProvider` se importa sin `faster_whisper` instalado; los 1 test existentes (`tests/test_orchestrator_e2e.py`) siguen pasando sin modificación.

## Lo que esto habilita (no implementado todavía)

- Agregar un `WhisperAPIProvider` o `WhisperXProvider` sin tocar `worker_prefetch.py` ni ningún otro consumidor — mismo beneficio que ya tiene `AIAnalysisProvider` con OpenAI/Claude.
- Testear el pipeline de transcripción con un provider falso (`FakeTranscriptionProvider`) sin GPU ni SQS, igual que ya se puede testear `MediaProcessingOrchestrator` con un `AIAnalysisProvider` de prueba.
