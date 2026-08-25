"""Genera el JSON de menciones que consume el frontend Kronos Signal.

Corre DENTRO del contenedor del backend (docker exec -i ... python3 - < este
archivo) porque ahi viven DATABASE_URL y las dependencias. Imprime a stdout el
array JSON con el MISMO esquema que el build original embebia (18 campos para
radio/TV, y la variante sin clip -- con url_nota en vez de media_url -- para
prensa digital) -- el frontend no distingue entre sus datos de demo y estos.

El esquema y el catalogo de medios se extrajeron por ingenieria inversa del
bundle (no hay codigo fuente de Kronos en ningun lado: ni GitHub ni el server
ni local -- solo dist/). Ver infra/kronos_refresh/ en el repo.
"""
import html
import json
import os
import re
import sys
from datetime import timedelta, timezone

from sqlalchemy import create_engine, text

TZ = timezone(timedelta(hours=-6))
TOKEN = "b620f9b089419067ed407c251a567cc7f2eee243d5c3e9e5"
BASE = "http://32.196.209.233"
VENTANA_HORAS = 48
LIMITE = 4000
MAX_TRANSCRIPCION = 1500

# codigo en nuestra DB -> (id del catalogo embebido en el bundle, tipo)
MEDIOS = {
    "hch_tv": ("tv-hch", "tv"),
    "canal_5": ("tv-canal-5", "tv"),
    "canal_11": ("tv-canal-11", "tv"),
    "canal_6": ("tv-canal-6", "tv"),
    "teleceiba": ("tv-teleceiba", "tv"),
    "tsi": ("tv-tsi", "tv"),
    "tnh": ("tv-tnh", "tv"),
    "hch_radio": ("radio-hch-radio", "radio"),
    "radio_america": ("radio-america", "radio"),
    "radio_globo": ("radio-globo", "radio"),
    "radio_satelite": ("radio-satelite", "radio"),
    "radio_el_patio": ("radio-el-patio", "radio"),
    "radio_choluteca": ("radio-choluteca", "radio"),
    "radio_valle": ("radio-valle", "radio"),
    "xy_hrn": ("radio-hrn", "radio"),
    "radio_progreso": ("radio-progreso", "radio"),
    # sin equivalente en el catalogo del bundle (la app no los conoce):
    # canal_10, xy_tgu, fm_941, suave_fm, suave_fm_teg, super_100, xy_sps
}

# Prensa digital (tabla articulos, ver src/modules/prensa/). El bundle trae sus
# propios items de prensa pero congelados al 8-ago: parchar_bundle.py se queda
# con los nuestros y hereda del build solo lo que no producimos (social y
# prensa impresa).
MEDIOS_PRENSA = {
    "la_prensa": "prensa-la-prensa",
    "el_heraldo": "prensa-el-heraldo",
    "la_tribuna": "prensa-la-tribuna",
    "proceso_digital": "prensa-proceso-digital",
    "tiempo_digital": "prensa-tiempo-digital",
    "criterio_hn": "prensa-criteriohn",
}

# audiencia segun el catalogo del bundle; alcance = audiencia * 4M (calibrado:
# canal-11 audiencia .019 -> 76,200 que es el valor visto en los datos originales)
AUDIENCIA = {
    "tv-hch": .555, "tv-canal-5": .081, "tv-tsi": .023, "tv-canal-11": .019,
    "tv-canal-6": .018, "tv-teleceiba": .007, "tv-tnh": .005,
}
AUD_DEFAULT = .015

TEMAS = {
    "politica": "Política", "seguridad": "Seguridad", "sociedad": "Sociedad",
    "deportes": "Deportes", "economia": "Economía", "salud": "Salud",
    "internacional": "Internacional", "entretenimiento": "Entretenimiento",
    "tecnologia": "Tecnología", "otro": "Otro",
}

SQL = f"""
SELECT n.id::text, m.codigo, g.fecha_inicio, n.clip_inicio_seg, n.clip_fin_seg,
       v.titulo, v.transcripcion_texto, v.metadatos_ia
FROM noticias n
JOIN noticia_versiones v ON v.id = n.version_actual_id
JOIN grabaciones g ON g.id = n.grabacion_id
JOIN programas p ON p.id = g.programa_id
JOIN medios m ON m.id = p.medio_id
WHERE g.fecha_inicio >= now() - interval '{VENTANA_HORAS} hours'
ORDER BY g.fecha_inicio DESC
LIMIT {LIMITE}
"""

SQL_PRENSA = f"""
SELECT a.id::text, m.codigo, a.publicado_at, a.titulo, a.resumen, a.url
FROM articulos a
JOIN fuentes_web f ON f.id = a.fuente_id
JOIN medios m ON m.id = f.medio_id
WHERE a.publicado_at >= now() - interval '{VENTANA_HORAS} hours'
ORDER BY a.publicado_at DESC
LIMIT {LIMITE}
"""


def limpiar_html(texto: str | None) -> str:
    """El resumen del feed viene con HTML crudo (la ingesta no lo toca a
    proposito, ver src/modules/prensa/models.py)."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", texto or ""))).strip()


def main() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    salida = []
    with engine.connect() as conn:
        for nid, codigo, f0, ini, fin, titulo, texto, meta in conn.execute(text(SQL)):
            mapeo = MEDIOS.get(codigo)
            if not mapeo:
                continue
            medio_id, tipo = mapeo
            meta = meta or {}
            ini = float(ini or 0)
            fin = float(fin or ini)
            cuando = (f0 + timedelta(seconds=ini)).astimezone(TZ)
            texto = (texto or "").strip()
            if len(texto) > MAX_TRANSCRIPCION:
                texto = texto[:MAX_TRANSCRIPCION].rsplit(" ", 1)[0] + "…"
            salida.append({
                "id": nid,
                "medio_id": medio_id,
                "tipo": tipo,
                "fecha_hora": cuando.isoformat(timespec="seconds"),
                "programa": "Señal en vivo",
                "titular": titulo or "(sin titular)",
                "transcripcion": texto,
                "fragmento_inicio_seg": round(ini, 1),
                "fragmento_duracion_seg": round(max(fin - ini, 0), 0),
                "media_url": f"{BASE}/api/v1/news/{nid}/clip?token={TOKEN}",
                "palabras_clave": (meta.get("keywords") or [])[:8],
                "personas": (meta.get("people") or [])[:8],
                "organizaciones": (meta.get("organizations") or [])[:8],
                "tema": TEMAS.get((meta.get("news_type") or "otro").lower(), "Otro"),
                "departamento": "",
                "municipio": "",
                "sentimiento": "neutral",
                "alcance_estimado": round(AUDIENCIA.get(medio_id, AUD_DEFAULT) * 4_000_000 / 100) * 100,
            })

        for aid, codigo, publicado, titulo, resumen, url in conn.execute(text(SQL_PRENSA)):
            medio_id = MEDIOS_PRENSA.get(codigo)
            if not medio_id:
                continue
            texto = limpiar_html(resumen)
            if len(texto) > MAX_TRANSCRIPCION:
                texto = texto[:MAX_TRANSCRIPCION].rsplit(" ", 1)[0] + "…"
            salida.append({
                "id": aid,
                "medio_id": medio_id,
                "tipo": "prensa_rss",
                "fecha_hora": publicado.astimezone(TZ).isoformat(timespec="seconds"),
                "programa": "Web",
                "titular": limpiar_html(titulo) or "(sin titular)",
                "transcripcion": texto,
                "palabras_clave": [],
                "personas": [],
                "organizaciones": [],
                "tema": "Otro",
                "departamento": "",
                "municipio": "",
                "sentimiento": "neutral",
                "alcance_estimado": round(AUD_DEFAULT * 4_000_000 / 100) * 100,
                "url_nota": url,
            })

    json.dump(salida, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    print(f"\n{len(salida)} menciones", file=sys.stderr)


if __name__ == "__main__":
    main()
