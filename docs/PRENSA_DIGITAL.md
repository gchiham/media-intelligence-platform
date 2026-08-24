# Prensa digital (RSS / sitemaps)

La pata escrita del monitoreo. Complementa la captura de radio y TV, no la
reemplaza.

## Por que hay un cron y no un webhook

La captura de audio es push: mediaCAP deja archivos en S3 y `DiscoveryService`
los levanta. La web es al reves. Se reviso si los feeds hondurenos anuncian hub
WebSub/PubSubHubbub -- que si permitiria suscribirse y recibir aviso-- y
**ninguno lo hace**. RSS es pull puro: el medio actualiza su XML cuando publica,
pero nadie avisa. La unica opcion es preguntar cada tanto.

## Cada cuanto

Lo que limita la frecuencia no es el ritmo de publicacion sino **cuanto
historial cabe en el feed**: si guarda 10 items y publican 15 mientras no
miramos, se perdieron 5 para siempre. Ventana que cubre cada fuente, medida el
2026-08-19:

| Fuente | Items | Ventana |
|---|---|---|
| tunota.com | 3 | 0.1 h |
| hch.tv | 10 | 2.9 h |
| elpais.hn | 10 | 4.2 h |
| oncenoticias.hn | 15 | 6.0 h |
| hondudiario, confidencialhn, radioamerica | 10 | 6-7 h |
| criterio.hn | 5 | 23.6 h |
| proceso.hn | 100 | 24.7 h |
| ellibertador, radioprogreso, elpulso | 10 | ~100 h |
| contracorriente | 15 | 546 h |
| televicentro | 9 | 625 h |
| laprensa, elheraldo (sitemap) | 219-235 | ~47 h |

**Cada 15 minutos para todas.** El peor caso activo (hch.tv, 2.9 h) queda con
casi 12x de margen. No vale la pena escalonar cadencias por fuente: con GET
condicional, pollear de mas no cuesta nada y la logica se complica al pedo.

`tunota.com` es la excepcion: 3 items publicados casi al mismo tiempo. Publica
poco pero en rafaga, asi que si suelta 4 notas juntas se pierde una sin importar
la frecuencia. Si ese medio importa para algun cliente, hay que buscarle otra
via.

```
*/15 * * * * cd /srv/media-intelligence-platform && .venv/bin/python scripts/rss_ingest.py >> logs/rss_ingest.log 2>&1
```

Sale con codigo 1 si alguna fuente fallo.

## Costo de una pasada

De las 10 fuentes medidas, 8 mandan `ETag`/`Last-Modified`, asi que el script
guarda los validadores y manda `If-None-Match` en la vuelta siguiente: el
servidor contesta **304 sin cuerpo** (~300 bytes) cuando no hay novedad, que es
el caso normal entre pasadas.

Las que no mandan validadores bajan el cuerpo entero siempre: proceso.hn (100
items), televicentro.hn y los tres sitemaps de OPSA (~400 KB c/u, con
`Cache-Control: max-age=1`). A 96 pasadas diarias son ~38 MB/dia por sitemap:
irrelevante, pero por eso el dedup tiene que estar en la base y no en memoria.

## Idempotencia

`uq_articulos_fuente_guid` + `ON CONFLICT DO NOTHING`. El cron ve los mismos 10
items ~96 veces al dia; sin esa restriccion serian 960 filas duplicadas diarias
por fuente. El dedup lo resuelve Postgres en una sola query, no un SELECT previo
por item -- que ademas tendria carrera si dos pasadas se solapan.

## Fecha de corte

`FuenteWeb.fecha_corte` evita que el primer poll se trague todo el historial:
Televicentro arrastra 625 h en 9 items y ContraCorriente 546 h en 15. Sin corte,
la base se llena de notas de hace tres semanas como si fueran de hoy. Mismo
cuidado que hubo que tener al dar de alta canal_10 (commit 4c80416).

`scripts/seed_fuentes_prensa.py` pone el corte en el momento del alta. Con
`--sin-corte` se ingiere todo lo que traiga el feed.

## Medios que quedaron afuera

| Medio | Motivo |
|---|---|
| tiempo.hn | Cloudflare devuelve 403 al `/feed/` desde fuera. Falta probar desde la EC2. |
| latribuna.hn | Es WordPress pero el tema intercepta `/feed/` y devuelve la portada en HTML. Su `post-sitemap.xml` no tiene namespace `news:`: sin titulo ni fecha, habria que scrapear cada nota. |
| reportarsinmiedo.org | `/feed/` responde 200 con cuerpo vacio. |
| hrn.hn, estrategiaynegocios.net | No resolvieron DNS. |

Grupo OPSA (La Prensa, El Heraldo, Diez) corre Liferay y no expone RSS por
ninguna ruta. Se usa el sitemap de Google News, que es mejor que un RSS tipico
en historial y trae fecha de publicacion exacta, pero **no trae cuerpo**: solo
titulo y URL.

## Imagen de portada (`Articulo.imagen_url`)

Se guarda la URL de la imagen, nunca el binario -- igual que `clip_s3_uri` en
`Noticia`. Orden de extraccion (`feeds._imagen_rss` / `parsear_sitemap_news`):

1. RSS con namespace Yahoo Media (`<media:content medium="image">` o
   `<media:thumbnail>`) -- lo usan hch.tv y canal_11.
2. `<enclosure type="image/...">` -- RSS 2.0 estandar.
3. Sitemap de Google News: primera `<image:image><image:loc>` del `<url>` --
   confirmado en produccion que La Prensa, El Heraldo y Diez lo traen siempre,
   pese a que el sitemap nunca tiene cuerpo. Es la unica via de imagen para
   esas tres fuentes (las mas grandes en volumen).
4. Fallback: primer `<img src="...">` dentro de `content:encoded`, para feeds
   RSS que no marcan la imagen en un tag aparte.

Las fuentes que no traen `content:encoded` NI corren por sitemap (radio_america,
confidencial_hn, el_pais_hn, el_pulso) se quedan sin imagen: no hay de donde
sacarla sin scrapear la nota.

## Donde se ve

`GET /api/v1/prensa/dashboard?token=...` -- seccion Prensa Digital del portal,
separada del dashboard de radio y TV. Ver docs/API.md.

## Que NO hace

Un `Articulo` no es una `Noticia`. `Noticia` es lo que produce nuestro pipeline
a partir de una grabacion -- tiene `grabacion_id` y offsets de clip
obligatorios-- y lo que el periodista aprueba. Un `Articulo` es material crudo
de un tercero. Mapear articulos a noticias implica hacer `Noticia.grabacion_id`
nullable; queda fuera de alcance.

Tampoco se limpia el HTML: `resumen` y `contenido_html` van crudos, tal como
vinieron. `content:encoded` (cuerpo completo) lo traen 9 de las 17 fuentes RSS.
