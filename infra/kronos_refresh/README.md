# Kronos Signal — refresco de datos

El frontend de `/kronos/` (nginx sirve `/home/ubuntu/kronos-signal/dist`) es un
build de Vite **sin código fuente en ningún lado**: ni GitHub, ni el servidor,
ni local — solo `dist/`. Sus datos van embebidos en el bundle como
`x=JSON.parse(`...`)`, congelados a la fecha del build (2026-08-09), y la UI
filtra "últimas 24 h" contra el reloj real: por eso el tablero amanecía vacío.

Estos scripts lo mantienen vivo sin el fuente:

| Archivo | Dónde corre | Qué hace |
|---|---|---|
| `generar_menciones.py` | dentro del contenedor backend (`docker exec -i ... python3 -`) | consulta la DB (últimas 48 h, medios mapeados al catálogo del bundle) y emite el JSON con el esquema de 18 campos que la app espera |
| `parchar_bundle.py` | host | localiza el template literal del `JSON.parse` en el bundle, lo reemplaza, y bumpea `?v=` en `index.html` para reventar caché |
| — | — | el bundle se resuelve leyendo `index.html`, **no** por nombre fijo: Carlos redespliega con hash nuevo (`index-BUiZHVla.js` → `index-D1FeIfGh.js` el 24-ago) y el parche con el hash viejo murió 16 h en silencio |
| `refresh.sh` | host, cron de `ubuntu` cada 30 min | orquesta los dos; si la consulta falla o trae <10 menciones, el bundle no se toca |

Del build se heredan tal cual los items que no producimos (`social`,
`prensa_impresa`); `tv`, `radio` y `prensa_rss` salen de nuestra DB. El parche
siempre parte del `.orig`, así que correrlo N veces da el mismo resultado.

El esquema de items, el catálogo de medios (`tv-hch`, `radio-globo`, ...) y la
calibración de `alcance_estimado` (audiencia × 4M) se extrajeron por ingeniería
inversa del bundle. El original quedó en
`/home/ubuntu/kronos-signal/index-BUiZHVla.js.orig`.

**Si aparece el código fuente de Kronos**, lo correcto es hacer que la app
consuma un endpoint del backend y jubilar este parche.

Gotcha de caché: el asset conserva su nombre (`index-BUiZHVla.js`), así que sin
el `?v=` del `index.html` los navegadores siguen mostrando datos viejos.
