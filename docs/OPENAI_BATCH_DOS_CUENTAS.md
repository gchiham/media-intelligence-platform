# Batch API de OpenAI con dos cuentas en paralelo

Referencia para correr trabajos de Batch API de OpenAI repartidos entre dos
cuentas al mismo tiempo (dos colas independientes de hasta 24h corriendo
juntas terminan antes que una sola cola con el doble de peticiones). Surgió
de filtrar `transcripciones_3al11.txt` para el informe de JOH
(`scripts/filtro_joh_batch_openai.py`), pero el patrón es reusable para
cualquier tarea de Batch API sobre texto.

## Credenciales

**No están en este documento ni deben duplicarse en ningún otro archivo.**
Viven solo en `.env`, en la raíz del repo:

| Variable | Cuenta | Notas |
|---|---|---|
| `OPENAI_API_KEY` | Cuenta 1 (la principal del proyecto) | Parte de `Settings` (`src/infrastructure/config.py`) — se lee con `settings.openai_api_key.get_secret_value()`. |
| `OPENAI_API_KEY_2` | Cuenta 2 (segunda cuenta, para correr en paralelo) | **No** es parte de `Settings` — pydantic-settings ignora env vars que no están declaradas como campo (`extra="ignore"`). Hay que leerla directo del archivo `.env`, no vía `settings`. |

Patrón para leer la segunda key sin agregarla a `Settings` (no vale la pena
un campo nuevo para algo que solo usan scripts ad-hoc):

```python
def _leer_env_var(nombre: str) -> str | None:
    env_path = Path(__file__).parent.parent / ".env"
    for linea in env_path.read_text(encoding="utf-8").splitlines():
        if linea.strip().startswith(f"{nombre}="):
            return linea.split("=", 1)[1].strip()
    return None
```

Si `OPENAI_API_KEY_2` no está en `.env` todavía, pedirle a alguien con acceso
que la agregue ahí directamente (nunca pegarla en el chat/conversación).

## Modelo usado

`gpt-5-mini` — buen balance costo/calidad para tareas de clasificación con
juicio (más capaz que `gpt-4o-mini`, que es el default del proyecto en
`OPENAI_MODEL` para segmentación). Para otra tarea, evaluar si conviene ese
mismo modelo o el default del proyecto.

## Cómo repartir el trabajo entre las dos cuentas

1. Partir el input en chunks por presupuesto de caracteres (no por cantidad
   de items) para no pasarse del contexto del modelo — ver
   `armar_chunks()` en `scripts/filtro_joh_batch_openai.py`.
2. Mandar la primera mitad de los chunks a la cuenta 1 (`_openai()`), la
   segunda mitad a la cuenta 2 (`_openai2()`), cada una en su propio batch
   (`client.batches.create(...)`).
3. Cada request lleva un `custom_id` único (ej. `chunk_0000`) que identifica
   el chunk sin importar a qué cuenta fue — al recolectar, se busca por
   `custom_id` en ambos batches y se hace merge.
4. Poll de estado en paralelo con `client.batches.retrieve(batch_id).status`
   hasta `completed`/`failed`/`expired`/`cancelled` en las dos cuentas.
5. Collect: bajar `output_file_id` de cada batch (`client.files.content(...)`),
   parsear el JSONL, unir por `custom_id`.

`scripts/filtro_joh_batch_openai.py` implementa exactamente este flujo con
tres comandos:

```bash
python scripts/filtro_joh_batch_openai.py --submit                       # manda la 1ra mitad a la cuenta 1
python scripts/filtro_joh_batch_openai.py --reenviar-restantes-openai2   # manda la 2da mitad a la cuenta 2
python scripts/filtro_joh_batch_openai.py --seguir                       # poll de las dos cuentas hasta que terminen
python scripts/filtro_joh_batch_openai.py --collect                      # baja y une los resultados
```

Estado persistido en `data/filtro_joh_semana/estado.json` (fuera de git,
`data/` está en `.gitignore`) — permite retomar `--seguir`/`--collect` en
otra sesión sin perder el progreso, siempre que no se edite el script a
mitad de camino (si se edita, hay que reconstruir el estado a mano o volver
a correr `--submit` desde cero).

## Para una tarea nueva (no el filtro de JOH)

Copiar `scripts/filtro_joh_batch_openai.py` como punto de partida y
cambiar:

- `ENTRADA` — el archivo de origen.
- `SYSTEM_PROMPT` — las instrucciones de la tarea.
- `leer_bloques()` / `armar_chunks()` — cómo se particiona el input (acá
  parte por un delimitador de líneas `====`; para otro formato puede ser
  por líneas, por párrafos, por registros de una tabla, etc.).
- La lógica de `collect()` que arma el archivo final — acá reensambla
  bloques `NOTICIA N`; para otra tarea el formato de salida va a ser otro.

## Gotchas ya resueltos (no repetir la investigación)

- **`Settings` ignora env vars no declaradas** — por eso `OPENAI_API_KEY_2`
  se lee del archivo directo, no vía `settings.openai_api_key_2` (no existe
  ese campo).
- **El endpoint de batch es `/v1/chat/completions`**, igual que las llamadas
  síncronas — no hay un endpoint especial para batch, el archivo JSONL
  subido con `purpose="batch"` es lo que activa el modo async.
- **Cuidado con procesos de `--seguir` viejos corriendo en background** al
  editar el script — un proceso viejo con el estado anterior en memoria
  puede pisar `estado.json` con datos obsoletos en cada vuelta del loop de
  polling. Si se edita el script mientras un `--seguir` está corriendo,
  matar ese proceso antes de tocar `estado.json` a mano.
- **`gpt-5-mini` vía batch no necesita `temperature` ni `max_tokens`
  explícitos** para este tipo de tarea — se dejó el default del modelo.
