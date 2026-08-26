# Los dos pipelines

Ambos hacen lo mismo en tres pasos —**chepita → segmentación → clipper**— y se
diferencian solo en cómo pagan el paso del medio.

| | Normal | Urgente |
|---|---|---|
| Segmentación | Batch API | Síncrono |
| Costo LLM | mitad | completo |
| Latencia | horas, sin cota por abajo | minutos |
| Disparo | cron | a mano |
| Alcance | todo el backlog | una ventana acotada |

Las dos usan **las dos cuentas de OpenAI en paralelo**: son organizaciones
distintas, cada una con su propia cuota de tokens encolados, así que dos
envíos simultáneos avanzan al doble de velocidad que uno con el doble de
trabajo.

## Cuándo usar cada uno

**Normal** para la operación diaria: drena lo que se acumuló y no importa que
tarde. **Urgente** cuando hay que entregar algo ya — un informe, una consulta
del cliente. La Batch API no acota su latencia por abajo: medido el
2026-08-18, 23 chunks no completaron **ni uno** en varios minutos, mientras el
mismo chunk síncrono tardaba 46 s. Por eso el urgente existe, y por eso exige
una ventana: correrlo sobre todo el backlog pagaría el doble sin necesidad.

## Uso

```bash
# Urgente: lo de hoy desde las 04:00 GMT-6, con clips de audio
python scripts/pipeline.py --urgente --desde-hoy --con-clips

# Urgente sobre una ventana y unos medios concretos
python scripts/pipeline.py --urgente \
  --desde 2026-08-18T10:00:00Z --hasta 2026-08-18T19:00:00Z \
  --medios hch_tv,radio_globo --con-clips

# Normal: recolecta lo de la corrida anterior y manda lo nuevo
python scripts/pipeline.py --normal --con-clips

# Normal en dos tiempos (lo que hace el cron)
python scripts/pipeline.py --normal --submit --sin-clipper   # manda los batches
python scripts/pipeline.py --normal --collect --con-clips    # horas después
```

Flags útiles: `--sin-chepita` / `--sin-clipper` para correr un solo paso,
`--instancias N` para el tamaño de la flota de clippers (`0` = local),
`--esperar-chepita` para bloquear hasta que termine de transcribir.

## Los tres pasos

### 1. Chepita (transcripción)

Encola lo pendiente y se asegura de que haya una instancia GPU viva; si no
hay, invoca el Lambda de lanzamiento. **No espera** salvo que se pida — el
supervisor ya apaga la instancia sola al vaciarse la cola, y esperar
bloquearía el orquestador por horas en el modo normal. El urgente sí espera,
porque el paso siguiente necesita las transcripciones.

Tras vaciarse la cola espera 90 s antes de seguir: el consumer que persiste
las transcripciones corre por cron cada minuto, y sin ese margen la
segmentación no vería las últimas.

### 2. Segmentación

**Batch** (`segment_backlog_batch_openai.py --submit/--collect`) reparte los
chunks entre las dos cuentas, un batch por cuenta, y registra en
`segmentation_batches.cuenta` con cuál salió cada uno. Eso importa: un batch
abierto con una cuenta es **invisible** para la otra (`retrieve` falla), así
que sin ese dato el `--collect` tenía que adivinar — antes había que correrlo
dos veces con env vars distintas, y un batch quedaba sin recolectar si alguien
se olvidaba de la segunda pasada.

El reparto es **por grabación, no por chunk**: los chunks de una misma
grabación tienen que volver juntos para que el stitching pueda fusionar las
noticias partidas en el límite entre chunks contiguos.

**Sync** (`segmentar_ventana_sync.py`) alterna grabaciones entre las dos
cuentas con un pool de hilos y devuelve cuando terminó todo.

### 3. Clipper

Lanza una flota efímera de `c5.xlarge` que se aprovisionan solas desde un
tarball en S3, procesan, y **se apagan solas** (`shutdown -h now` +
`InstanceInitiatedShutdownBehavior=terminate`). Si el script falla, la
instancia queda viva a propósito para poder diagnosticar por SSM.

**Usan reclamo atómico, no sharding.** Cada instancia pide la siguiente
grabación libre con `FOR UPDATE SKIP LOCKED` (`--reclamo`). El sharding
estático (`--shard i --de N`) asumía que todas las instancias ven la *misma*
lista para que `idx % N == i` la cubra exacto; con arranques escalonados —lo
normal, aprovisionar tarda minutos— cada una calculaba sus índices sobre una
lista que ya venía encogiendo, y quedaban huecos: el 2026-08-18 quedaron **97
de 244 grabaciones sin procesar**. El flag `--shard` sigue existiendo como
legado, pero para flota va `--reclamo`.

## Secretos

Los clippers bajan `s3://media-intel-clips-.../deploy/clipper.env` con su rol
IAM. Ese archivo **se genera en el backend**, donde el password de la RDS ya
vive, y se sube desde ahí — nunca se escribe a mano ni viaja dentro del
tarball del repo, que se copia a N máquinas.

Tras el cutover a RDS hubo que repuntarlo: apuntaba al Postgres viejo del
contenedor, así que una flota lanzada ese día habría escrito miles de noticias
a una base que producción ya no lee.

## Ver el avance: `GET /dash`

Dashboard de operación de las cuatro etapas (Grabaciones → CHEPITA →
Segmentación LLM → Clipper), token-gated con el mismo `DASHBOARD_TOKEN` que
`/news/dashboard` y `/prensa/dashboard`:

```
http://<host>/dash?token=$DASHBOARD_TOKEN
```

Contesta de un vistazo la pregunta de siempre — *hasta qué hora tenemos* — con
un embudo por etapa, una matriz de cobertura hora por hora y una tabla con la
última hora cubierta por cada medio. El JSON que consume es
`GET /api/v1/dash/metricas?horas=N&token=…`.

**El eje es `grabaciones.fecha_inicio`, no `created_at`:** lo que se mide es
hasta qué hora *del aire* llegó cada etapa, no cuándo corrió el cron. Por eso a
las 09:50 GMT-6 lo normal es ver la última hora cerrada en 08:00 y no en 09:00.

Un medio se marca atrasado contra el medio más adelantado (tolerancia de 2 h),
no contra el reloj. Cuando sus cuatro etapas llegan igual de lejos se etiqueta
**"sin grabaciones nuevas"**: no hay nada trabado en el pipeline, lo que falta
es la captura aguas arriba — la distinción que evita salir a buscar un bug de
segmentación cuando lo que pasó fue que mediaCAP dejó de publicar.

La consulta va contra las cuatro tablas del pipeline en la ventana pedida
(24 h por defecto, 14 días de tope) — ver `src/modules/pipeline/metricas.py`.
