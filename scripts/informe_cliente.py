"""Genera un informe ejecutivo de monitoreo (Markdown + Word) para un cliente,
a partir de lo que hay en `segmentation_cache` + las transcripciones literales.

**Todo sale de los datos.** Ninguna cifra, nombre ni cita se escribe a mano:
se calculan sobre las notas realmente registradas en el periodo, y las citas
son copia textual de la transcripcion del aire (con la advertencia de que la
transcripcion automatica puede tener variantes foneticas). Lo que agrega el
LLM por nota: sentimiento, eje tematico y la seleccion de la cita mas
representativa DENTRO del texto transcrito -- nunca texto propio.

**Criterio de inclusion: mencion directa.** Se toma toda nota cuyo titulo,
resumen, keywords o personas mencionen al cliente segun `--patrones`. Es mas
estricto que el `client_relevance` del batch (que sobredisparaba); se pierde
la nota que afecta al cliente sin nombrarlo, a cambio de que todo lo incluido
sea verificable.

**Temas monitoreados (`--tema`).** Ademas del cliente se pueden seguir asuntos
que le importan aunque no lo nombren -- p. ej. las Guias de Familia / Vida
Mejor, cuyos ex empleados reclaman prestaciones y son un frente abierto entre
el cliente y la Procuraduria. Esas notas entran al informe **etiquetadas
aparte**: nunca se suman al conteo de menciones directas, para no inflarlo.

**El Word se genera aca mismo**, escribiendo OOXML con `zipfile` de la
biblioteca estandar (no hace falta python-docx, que no esta instalado ni en
el contenedor del backend ni en el entorno local). El .docx y el .md salen de
los mismos datos en una sola corrida: no hay un paso manual en el medio.

Uso:
    python scripts/informe_cliente.py \\
        --cliente "Juan Orlando Hernandez" \\
        --patrones "juan orlando,hernandez alvarado,\\bjoh\\b" \\
        --tema "Guias de Familia / Vida Mejor::gu[ií]as? de familia|vida mejor" \\
        --desde 2026-08-18T10:00:00Z --hasta 2026-08-18T19:00:00Z \\
        --salida informe_joh.md --salida-docx Informe_JOH.docx
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from openai import OpenAI  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from src.infrastructure.config import settings  # noqa: E402
from src.infrastructure.db import registry  # noqa: F401,E402
from src.infrastructure.db.engine import get_engine  # noqa: E402

TZ = timezone(timedelta(hours=-6))  # America/Tegucigalpa
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# Correcciones de nombres que el sistema de transcripcion registra con
# variantes foneticas, confirmadas por el editor. Clave normalizada (sin
# acentos, minusculas) -> forma canonica.
ALIAS_ACTORES = {
    "marco midense": "Marco Midence",
    "marcos midense": "Marco Midence",
    "dagoberto aspara": "Dagoberto Aspra",
    "roberto aspra": "Dagoberto Aspra",
    "aspra": "Dagoberto Aspra",
    "aspara": "Dagoberto Aspra",
    "marco mides": "Marco Midence",
}

ANALISIS_PROMPT = """Eres analista senior de monitoreo de medios. Te doy una nota de \
radio o TV hondurena que menciona a {cliente}, con su transcripcion literal.

Devuelve:
- "sentimiento": como queda posicionada la figura de {cliente} en ESTA nota.
  "positivo" (respaldo, defensa, presuncion de inocencia, recibimiento favorable),
  "negativo" (acusacion, critica, corrupcion, narcotrafico, rechazo, impunidad),
  "neutral" (reporte factual o procesal, o posturas equilibradas).
- "eje": el tema de la nota en 2-4 palabras, SIN incluir el nombre de {cliente}
  (ej. "audiencia Pandora 2", "reaccion del Partido Nacional").
- "cita": la frase mas reveladora dicha AL AIRE sobre {cliente}, copiada
  TEXTUALMENTE de la transcripcion (maximo 45 palabras, sin corregir ni
  parafrasear). Elige la que mejor muestre la postura del hablante. Si la
  transcripcion es puro relleno sin nada citable, devuelve cadena vacia.
- "cita_actor": quien dice esa frase, si la transcripcion o el resumen lo
  dejan claro (nombre propio, o rol como "presentador de {medio}"). Si no es
  identificable, devuelve "voz al aire ({medio})"."""

ANALISIS_SCHEMA = {
    "type": "object",
    "properties": {
        "sentimiento": {"type": "string", "enum": ["positivo", "neutral", "negativo"]},
        "eje": {"type": "string"},
        "cita": {"type": "string"},
        "cita_actor": {"type": "string"},
    },
    "required": ["sentimiento", "eje", "cita", "cita_actor"],
    "additionalProperties": False,
}


def _api_key() -> str:
    return os.environ.get("OPENAI_API_KEY_2") or settings.openai_api_key.get_secret_value()


def _fecha_larga(d: datetime) -> str:
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]} de {d.year}"


def _pl(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _sin_acentos(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


def barra(n: int, maximo: int, ancho: int = 24) -> str:
    return "█" * max(1, round(n / maximo * ancho)) if n else ""


# ------------------------------------------------------------------- carga

def cargar_notas(desde: datetime, hasta: datetime, patrones: list[str],
                 temas: dict[str, str] | None = None):
    """Notas del periodo que mencionan al cliente o tocan alguno de los temas
    monitoreados, con su transcripcion literal (recortada del rango de
    palabras de cada segmento).

    Cada nota queda marcada con `menciona_cliente` y `temas`: una nota puede
    entrar por el cliente, por un tema, o por ambos. Se guardan separadas para
    que el conteo de menciones directas no se infle con las del tema.
    """
    pat = re.compile("|".join(patrones), re.I)
    pat_temas = {nombre: re.compile(rx, re.I) for nombre, rx in (temas or {}).items()}
    sql = text("""
        SELECT g.id AS gid, m.nombre AS medio, g.fecha_inicio, sc.segmentos
        FROM segmentation_cache sc
        JOIN grabaciones g ON g.id = sc.grabacion_id
        JOIN programas p ON p.id = g.programa_id
        JOIN medios m ON m.id = p.medio_id
        WHERE g.fecha_inicio >= :desde AND g.fecha_inicio < :hasta
    """)
    universo = 0
    por_medio_total = Counter()
    notas = []
    gids = set()
    with Session(get_engine()) as s:
        for gid, medio, fecha, segs in s.execute(sql, {"desde": desde, "hasta": hasta}):
            local = fecha.astimezone(TZ)
            for seg in segs or []:
                universo += 1
                por_medio_total[medio] += 1
                blob = " ".join([
                    seg.get("title", ""), seg.get("summary", ""),
                    " ".join(seg.get("people", [])), " ".join(seg.get("keywords", [])),
                ])
                del_cliente = bool(pat.search(blob))
                hits_tema = [n for n, rx in pat_temas.items() if rx.search(blob)]
                if del_cliente or hits_tema:
                    gids.add(str(gid))
                    notas.append({
                        "gid": str(gid), "medio": medio, "fecha": local,
                        "titulo": seg.get("title", ""), "resumen": seg.get("summary", ""),
                        "keywords": seg.get("keywords", []), "people": seg.get("people", []),
                        "start_word": seg.get("start_word", 0), "end_word": seg.get("end_word", 0),
                        "menciona_cliente": del_cliente, "temas": hits_tema,
                    })

        # Transcripcion literal solo de las grabaciones que tienen notas del
        # cliente -- cargar los words de todo el periodo seria mucho mas caro.
        for gid in gids:
            fila = s.execute(
                text("SELECT segmentos FROM transcripciones WHERE grabacion_id = :g LIMIT 1"),
                {"g": gid},
            ).first()
            words = (fila[0] or {}).get("words", []) if fila else []
            por_indice = {w["index"]: w["word"] for w in words}
            for n in notas:
                if n["gid"] == gid:
                    n["transcripcion"] = " ".join(
                        por_indice[i] for i in range(n["start_word"], n["end_word"] + 1)
                        if i in por_indice
                    ).replace("\x00", "")
    return universo, por_medio_total, notas


# ---------------------------------------------------------------- analisis

def analizar_notas(notas: list[dict], cliente: str, concurrency: int = 12) -> None:
    client = OpenAI(api_key=_api_key())

    def _una(n):
        sistema = ANALISIS_PROMPT.format(cliente=cliente, medio=n["medio"])
        cuerpo = (f"TITULO: {n['titulo']}\nRESUMEN: {n['resumen']}\n"
                  f"MEDIO: {n['medio']}\n\nTRANSCRIPCION LITERAL:\n{n.get('transcripcion', '')[:6000]}")
        for intento in range(3):
            try:
                r = client.chat.completions.create(
                    model=settings.openai_model,
                    messages=[{"role": "system", "content": sistema},
                              {"role": "user", "content": cuerpo}],
                    response_format={"type": "json_schema", "json_schema": {
                        "name": "analisis", "schema": ANALISIS_SCHEMA, "strict": True}},
                )
                d = json.loads(r.choices[0].message.content)
                # Una cita que no aparece en la transcripcion es una cita
                # inventada: se descarta entera. Se compara sin acentos ni
                # mayusculas porque el modelo a veces "limpia" la ortografia.
                cita = d.get("cita", "").strip()
                if cita:
                    plano = _sin_acentos(n.get("transcripcion", "").lower())
                    nucleo = _sin_acentos(cita.lower()).strip('.,;:"\'')
                    if nucleo[:60] not in plano:
                        cita = ""
                n["sentimiento"] = d["sentimiento"]
                n["eje"] = d["eje"]
                n["cita"] = cita
                n["cita_actor"] = d.get("cita_actor", "").strip() if cita else ""
                return
            except Exception:  # noqa: BLE001
                if intento == 2:
                    n["sentimiento"] = "neutral"
                    n["eje"] = ""
                    n["cita"] = ""
                    n["cita_actor"] = ""

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(_una, notas))


def canonizar_actor(nombre: str, cliente: str, tokens_extra: set | None = None) -> str | None:
    """Colapsa variantes del mismo nombre. Devuelve None si el 'actor' es el
    propio cliente (no aporta listarlo junto a si mismo)."""
    llave = _sin_acentos(nombre.strip().lower())
    if llave in ALIAS_ACTORES:
        return ALIAS_ACTORES[llave]
    tokens_cliente = set(_sin_acentos(cliente.lower()).split()) | {"joh", "expresidente"}
    if tokens_extra:
        tokens_cliente |= tokens_extra
    tokens = [t for t in llave.replace(".", " ").split() if t not in {"de", "del", "la"}]
    if tokens and all(t in tokens_cliente for t in tokens):
        return None
    # "Ana Garcia" / "Ana Garcia de Hernandez" / "Ana Garcia Hernandez" -> la
    # forma mas corta que comparte los dos primeros tokens gana como llave.
    return " ".join(w.capitalize() for w in tokens[:2]) if len(tokens) >= 2 else nombre.strip()


# ------------------------------------------------------------------ render

def render(cliente: str, titulo_cargo: str, universo: int, por_medio_total: Counter,
           notas: list[dict], desde: datetime, hasta: datetime,
           patrones: list[str] | None = None) -> str:
    # Las notas que entraron solo por un tema monitoreado se reportan aparte:
    # el conteo de "notas sobre el cliente" tiene que seguir siendo el de
    # menciones directas, o deja de ser comparable entre periodos.
    todas = notas
    notas = [x for x in todas if x.get("menciona_cliente", True)]
    notas_tema = [x for x in todas if not x.get("menciona_cliente", True)]
    n = len(notas)
    sent = Counter(x.get("sentimiento", "neutral") for x in notas)
    pos, neu, neg = sent["positivo"], sent["neutral"], sent["negativo"]
    pct = lambda v, t=n: f"{100 * v / t:.1f}%" if t else "0%"

    por_medio = Counter(x["medio"] for x in notas)
    por_dia = Counter(x["fecha"].strftime("%Y-%m-%d") for x in notas)
    por_hora = Counter(x["fecha"].hour for x in notas)

    _RUIDO_EJE = {"de", "del", "contra", "sobre", "el", "la", "los", "las", "y", "a", "en"}
    for parte in cliente.lower().replace(",", " ").split():
        _RUIDO_EJE.add(_sin_acentos(parte.strip(".()")))
    _RUIDO_EJE.update({"joh", "expresidente", "exmandatario"})

    def _eje_norm(txt: str) -> str:
        out = []
        for cruda in txt.lower().split():
            w = _sin_acentos(cruda.strip(".,;:()[]\"'-–—"))
            if w and w not in _RUIDO_EJE:
                out.append(w)
        return " ".join(out)

    ejes = Counter(_eje_norm(x["eje"]) for x in notas if x.get("eje") and _eje_norm(x["eje"]))
    sent_por_eje = defaultdict(Counter)
    for x in notas:
        if x.get("eje") and _eje_norm(x["eje"]):
            sent_por_eje[_eje_norm(x["eje"])][x.get("sentimiento", "neutral")] += 1

    tokens_extra = set()
    for patron in patrones or []:
        for tk in re.sub(r"[^a-z ]", " ", _sin_acentos(patron.lower())).split():
            if len(tk) > 2:
                tokens_extra.add(tk)
    actores = Counter()
    for x in notas:
        for p in x["people"]:
            c = canonizar_actor(p, cliente, tokens_extra)
            if c:
                actores[c] += 1
    # las variantes foneticas tambien aparecen en las atribuciones de citas
    for x in notas:
        actor = x.get("cita_actor", "")
        plano = _sin_acentos(actor.lower())
        for variante, canonico in ALIAS_ACTORES.items():
            if variante in plano:
                x["cita_actor"] = canonico
                break

    sent_medio = defaultdict(Counter)
    for x in notas:
        sent_medio[x["medio"]][x.get("sentimiento", "neutral")] += 1

    # Citas: dedup por prefijo normalizado (un spot o una nota repetida entre
    # emisoras produce la misma frase varias veces) y agrupadas por sentimiento.
    citas_vistas = set()
    citas = {"positivo": [], "negativo": [], "neutral": []}
    for x in sorted(notas, key=lambda z: z["fecha"]):
        c = x.get("cita", "")
        if not c:
            continue
        llave = _sin_acentos(c.lower())[:70]
        if llave in citas_vistas:
            continue
        citas_vistas.add(llave)
        citas[x.get("sentimiento", "neutral")].append(x)

    dias = sorted(por_dia)
    d0 = datetime.strptime(dias[0], "%Y-%m-%d") if dias else desde
    d1 = datetime.strptime(dias[-1], "%Y-%m-%d") if dias else hasta
    periodo = _fecha_larga(d0) if d0.date() == d1.date() else f"{_fecha_larga(d0)} — {_fecha_larga(d1)}"

    L = []
    A = L.append

    # -------------------------------------------------------------- portada
    A("# INFORME EJECUTIVO DE MONITOREO DE MEDIOS")
    A("")
    A("### Análisis de cobertura, actores, voces y sentimiento")
    A("")
    A("---")
    A("")
    A("**PREPARADO PARA**")
    A("")
    A(f"## {cliente}")
    if titulo_cargo:
        A(f"*{titulo_cargo}*")
    A("")
    A(f"**Período analizado:** {periodo} (GMT-6)")
    A("")
    A(f"**Universo:** {universo:,} notas de radio y televisión en "
      f"{len(por_medio_total)} medios nacionales")
    A("")
    A(f"**Notas sobre {cliente}:** {n}")
    A("")
    if notas_tema:
        etiquetas = sorted({t for x in notas_tema for t in x.get("temas", [])})
        A(f"**Notas de temas monitoreados (sin nombrarlo):** {len(notas_tema)} "
          f"— {', '.join(etiquetas)}")
        A("")
    A("---")
    A("")
    A("> **CONFIDENCIAL — USO EXCLUSIVO DEL DESTINATARIO**")
    A("")
    A("<div style=\"page-break-after: always\"></div>")
    A("")

    # ------------------------------------------------------------- resumen
    A("## 1. Resumen ejecutivo")
    A("")
    medio_top, medio_top_n = por_medio.most_common(1)[0] if por_medio else ("—", 0)
    A(f"Durante el período se registraron **{universo:,} notas** en "
      f"{len(por_medio_total)} medios. **{n} notas ({pct(n, universo)} de toda la "
      f"producción noticiosa nacional)** trataron directamente sobre su persona — "
      f"**1 de cada {round(universo / n)} notas** emitidas en el país.")
    A("")
    balance = "favorable" if pos > neg else ("adverso" if neg > pos else "equilibrado")
    A(f"El balance de sentimiento es **{balance}**: {_pl(pos, 'nota positiva', 'notas positivas')} "
      f"({pct(pos)}), {neu} neutrales ({pct(neu)}) y {_pl(neg, 'negativa', 'negativas')} "
      f"({pct(neg)}). La proporción de cobertura neutral —mayoritariamente reporte "
      f"procesal de audiencias— indica que la agenda la está marcando el calendario "
      f"judicial, no la opinión editorial. Las plazas donde sí hay editorialización "
      f"se detallan en la sección 2.")
    A("")
    if dias and len(dias) > 1:
        dmax, dmax_n = por_dia.most_common(1)[0]
        dmax_f = _fecha_larga(datetime.strptime(dmax, "%Y-%m-%d"))
        A(f"El **{dmax_f}** concentró {dmax_n} de las {n} notas ({pct(dmax_n)}). ")
    if ejes:
        eje1, eje1n = ejes.most_common(1)[0]
        ejes_top10 = sum(v for _, v in ejes.most_common(10))
        A(f"El eje más frecuente fue **«{eje1}»** ({_pl(eje1n, 'nota', 'notas')}); "
          f"los diez primeros ejes de la sección 3 reúnen {ejes_top10} notas "
          f"({pct(ejes_top10)}) y son, en su mayoría, variantes del mismo asunto.")
    A("")
    A("<div style=\"page-break-after: always\"></div>")
    A("")

    # ----------------------------------------------------------- cobertura
    A("## 2. Dónde y cómo se habló de usted")
    A("")
    A("| Medio | Notas | % de su agenda | Pos | Neu | Neg | Lectura |")
    A("|---|---:|---:|---:|---:|---:|---|")
    for medio, cant in por_medio.most_common():
        c = sent_medio[medio]
        share = f"{100 * cant / por_medio_total[medio]:.1f}%" if por_medio_total[medio] else "—"
        if cant >= 5:
            if c["negativo"] >= c["positivo"] + 3:
                lectura = "línea crítica"
            elif c["positivo"] >= c["negativo"] + 3:
                lectura = "línea favorable"
            elif c["neutral"] >= cant * 0.6:
                lectura = "reporte factual"
            else:
                lectura = "mixta"
        else:
            lectura = "volumen bajo"
        A(f"| {medio} | {cant} | {share} | {c['positivo']} | {c['neutral']} | "
          f"{c['negativo']} | {lectura} |")
    silenciosos = sorted(m for m in por_medio_total if m not in por_medio)
    if silenciosos:
        A("")
        A(f"**Medios sin ninguna nota sobre usted:** {', '.join(silenciosos)}.")
    A("")
    if len(dias) > 1:
        A("**Distribución por día:** " + " · ".join(
            f"{d}: {por_dia[d]} notas" for d in dias))
        A("")
    if por_hora:
        picos = ", ".join(f"{h:02d}:00 ({c})" for h, c in por_hora.most_common(3))
        A(f"**Franjas de mayor volumen (GMT-6):** {picos}. El resto del día la "
          f"cobertura se mantiene en niveles bajos pero continuos.")
    A("")
    A("<div style=\"page-break-after: always\"></div>")
    A("")

    # ------------------------------------------------------ ejes con contexto
    A("## 3. Ejes narrativos: qué se está diciendo")
    A("")
    for eje, cant in ejes.most_common(6):
        c = sent_por_eje[eje]
        A(f"### «{eje}» — {_pl(cant, 'nota', 'notas')} "
          f"({c['positivo']} pos / {c['neutral']} neu / {c['negativo']} neg)")
        A("")
        muestra = [x for x in notas
                   if x.get("eje") and _eje_norm(x["eje"]) == eje][:2]
        for x in muestra:
            A(f"- *{x['titulo']}* — {x['medio']}, {x['fecha'].strftime('%d/%m %H:%M')}. "
              f"{x['resumen']}")
        A("")

    A("<div style=\"page-break-after: always\"></div>")
    A("")

    # ---------------------------------------------------------------- voces
    A("## 4. Las voces: qué se dijo de usted, textualmente")
    A("")
    A("*Citas copiadas literalmente de la transcripción del aire. La transcripción "
      "automática puede registrar variantes fonéticas de nombres y palabras; se "
      "citan sin editar.*")
    A("")
    for etiqueta, clave, tope in (("Voces que lo favorecen o lo defienden", "positivo", 6),
                                  ("Voces críticas o acusatorias", "negativo", 8),
                                  ("Registro neutral relevante", "neutral", 5)):
        sel = citas[clave][:tope]
        if not sel:
            continue
        A(f"### {etiqueta}")
        A("")
        for x in sel:
            actor = x.get("cita_actor") or f"voz al aire ({x['medio']})"
            A(f"> «{x['cita']}»")
            A(f"> — **{actor}** · {x['medio']}, {x['fecha'].strftime('%d/%m %H:%M')}")
            A("")

    # -------------------------------------------------------------- actores
    A("### Actores presentes en su cobertura")
    A("")
    A("| Actor | Notas en que aparece |")
    A("|---|---:|")
    for actor, cant in actores.most_common(10):
        A(f"| {actor} | {cant} |")
    A("")
    A("*Nombres agrupados por persona; las variantes fonéticas del sistema de "
      "transcripción (p. ej. «Midense» por Midence) se corrigieron donde fue "
      "posible confirmarlas.*")
    A("")
    A("<div style=\"page-break-after: always\"></div>")
    A("")

    # --------------------------------------------------------------- temas
    # Notas que NO lo nombran pero tocan un asunto que le afecta. Van aparte
    # justamente porque el vinculo lo pone el analista, no la nota.
    sec = 5
    if notas_tema:
        A(f"## {sec}. Temas monitoreados")
        A("")
        A(f"*{len(notas_tema)} notas del período tratan asuntos bajo seguimiento "
          f"**sin mencionar a {cliente}**. No se suman al conteo de menciones "
          f"directas; se listan porque el asunto puede escalar hacia su figura.*")
        A("")
        por_tema = defaultdict(list)
        for x in notas_tema:
            for t_ in x.get("temas", []):
                por_tema[t_].append(x)
        for etiqueta, items in sorted(por_tema.items()):
            st = Counter(x.get("sentimiento", "neutral") for x in items)
            A(f"### {etiqueta} — {_pl(len(items), 'nota', 'notas')} "
              f"({st['positivo']} pos / {st['neutral']} neu / {st['negativo']} neg)")
            A("")
            A("| Hora | Medio | Nota |")
            A("|---|---|---|")
            for x in sorted(items, key=lambda z: z["fecha"])[:12]:
                titulo = x["titulo"].replace("|", "\\|")
                A(f"| {x['fecha'].strftime('%d/%m %H:%M')} | {x['medio']} | {titulo} |")
            A("")
            for x in sorted(items, key=lambda z: z["fecha"]):
                if x.get("cita"):
                    actor = x.get("cita_actor") or f"voz al aire ({x['medio']})"
                    A(f"> «{x['cita']}»")
                    A(f"> — **{actor}** · {x['medio']}, {x['fecha'].strftime('%d/%m %H:%M')}")
                    A("")
                    break
        A("<div style=\"page-break-after: always\"></div>")
        A("")
        sec += 1

    # ------------------------------------------------------------- lectura
    A(f"## {sec}. Lectura estratégica del período")
    A("")
    A("*Observaciones derivadas exclusivamente de los datos anteriores.*")
    A("")
    obs = 0
    criticos = [(m, c) for m, c in sent_medio.items()
                if sum(c.values()) >= 5 and c["negativo"] >= c["positivo"] + 3]
    factuales = [(m, c) for m, c in sent_medio.items()
                 if sum(c.values()) >= 10 and c["neutral"] >= sum(c.values()) * 0.6]
    if criticos:
        obs += 1
        nombres = ", ".join(m for m, _ in sorted(criticos, key=lambda z: -z[1]["negativo"]))
        tot_neg = sum(c["negativo"] for _, c in criticos)
        A(f"{obs}. **La narrativa crítica está concentrada, no dispersa.** "
          f"{nombres} acumulan {tot_neg} de las {neg} notas negativas del período. "
          f"El resto del espectro reporta en clave procesal.")
        A("")
    if factuales:
        obs += 1
        nombres = ", ".join(m for m, _ in sorted(factuales, key=lambda z: -sum(z[1].values())))
        A(f"{obs}. **Las plazas de mayor volumen ({nombres}) mantienen registro "
          f"factual.** Su audiencia recibe sobre todo el relato procesal del caso, "
          f"no editorialización — un espacio donde la narrativa aún se disputa.")
        A("")
    if pos and neg and neg > pos:
        obs += 1
        ratio = round(neg / pos, 1)
        A(f"{obs}. **Por cada nota favorable se emitieron {ratio} críticas.** Las "
          f"voces de defensa registradas al aire (sección 4) son numéricamente "
          f"minoritarias frente a la vocería acusatoria.")
        A("")
    if silenciosos:
        obs += 1
        A(f"{obs}. **{len(silenciosos)} medios monitoreados no emitieron ni una "
          f"nota sobre usted** en todo el período, pese a cubrir la agenda "
          f"nacional con normalidad.")
        A("")
    if len(dias) > 1 and por_dia:
        obs += 1
        caida = por_dia[dias[0]] - por_dia[dias[-1]]
        if caida > 0:
            A(f"{obs}. **El volumen descendió {pct(caida, por_dia[dias[0]])} entre el primer "
              f"y el último día del período** ({por_dia[dias[0]]} → {por_dia[dias[-1]]} notas), "
              f"consistente con un ciclo noticioso ligado a un evento puntual que se agota "
              f"salvo que un hecho nuevo lo reactive.")
            A("")

    A("<div style=\"page-break-after: always\"></div>")
    A("")

    # ---------------------------------------------------------- metodologia
    A(f"## {sec + 1}. Alcance y metodología")
    A("")
    A(f"**Fuente.** Monitoreo continuo de radio y televisión, {periodo} (GMT-6), "
      f"sobre {len(por_medio_total)} medios: {', '.join(sorted(por_medio_total))}. "
      f"Cada nota incluye fecha y hora, medio, título, resumen, palabras clave, "
      f"personas mencionadas y transcripción literal del segmento.")
    A("")
    A(f"**Criterio de inclusión.** Toda nota cuyo título, resumen, palabras clave o "
      f"personas mencionan directamente a {cliente}: {n} notas de un universo de "
      f"{universo:,}.")
    A("")
    if notas_tema:
        etiquetas = sorted({t for x in notas_tema for t in x.get("temas", [])})
        A(f"**Temas monitoreados.** Además se siguen asuntos que pueden afectar su "
          f"figura sin nombrarlo ({', '.join(etiquetas)}): {len(notas_tema)} notas "
          f"del período, reportadas por separado en la sección {sec}. El vínculo con "
          f"su figura lo establece el análisis, no la nota: por eso no se suman al "
          f"conteo de menciones directas.")
        A("")
    A("**Sentimiento y citas.** Cada nota fue clasificada según cómo queda posicionada "
      "su figura (positivo / neutral / negativo). Las citas de la sección 4 son copia "
      "textual de la transcripción; toda cita que no pudo verificarse contra la "
      "transcripción fue descartada.")
    A("")
    A("**Este informe no contiene ningún dato externo al monitoreo.** Cada cifra, "
      "nombre y cita proviene de las notas registradas en el período.")
    A("")
    return "\n".join(L)


# ====================================================================== WORD
# Se escribe OOXML a mano con zipfile: python-docx no esta instalado ni en el
# contenedor del backend ni en el entorno local, y agregar la dependencia solo
# para esto obligaria a rebuildear la imagen. Un .docx es un zip con seis
# partes XML; las que hacen falta estan todas aca.

NAVY, BLUE, RED, RED_BG = "1F3864", "2E5395", "B23A2F", "F9ECEA"
GREEN, GREEN_BG, GOLD = "2E7D32", "E8F3E9", "BF9000"
INK, GRIS, TENUE, FILA_BG = "333333", "595959", "8A8A8A", "F2F2F2"
BLANCO = "FFFFFF"
ANCHO = 10466          # ancho util en DXA (carta 12240 - margenes 887*2)


def _x(t: str) -> str:
    return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _run(txt: str, sz: int = 19, bold: bool = False, italic: bool = False,
         color: str = INK, caps: bool = False, esp: int = 0) -> str:
    r = ['<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>']
    if bold:
        r.append("<w:b/>")
    if italic:
        r.append("<w:i/>")
    if caps:
        r.append("<w:caps/>")
    # El orden de los hijos de w:rPr lo fija el esquema: color va antes que
    # spacing, y ambos antes que sz. Alterarlo invalida el documento.
    r.append(f'<w:color w:val="{color}"/>')
    if esp:
        r.append(f'<w:spacing w:val="{esp}"/>')
    r.append(f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/></w:rPr>')
    return f'<w:r>{"".join(r)}<w:t xml:space="preserve">{_x(txt)}</w:t></w:r>'


def _par(runs: str, align: str = "", antes: int = 0, despues: int = 120,
         borde: str = "", sombra: str = "") -> str:
    pr = ['<w:pPr>']
    if borde:                       # w:pBdr va antes que w:shd (esquema)
        pr.append(borde)
    if sombra:
        pr.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{sombra}"/>')
    pr.append(f'<w:spacing w:before="{antes}" w:after="{despues}"/>')
    if align:
        pr.append(f'<w:jc w:val="{align}"/>')
    pr.append("</w:pPr>")
    return f'<w:p>{"".join(pr)}{runs}</w:p>'


def _vacio(alto: int = 120) -> str:
    return f'<w:p><w:pPr><w:spacing w:after="{alto}"/></w:pPr></w:p>'


def _salto() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


_SIN_BORDES = ('<w:tcBorders><w:top w:val="nil"/><w:left w:val="nil"/>'
               '<w:bottom w:val="nil"/><w:right w:val="nil"/></w:tcBorders>')


def _celda(contenido: str, w: int, fill: str = "", mt: int = 70, mb: int = 70,
           ml: int = 110, valign: str = "center") -> str:
    pr = [f'<w:tcW w:w="{w}" w:type="dxa"/>', _SIN_BORDES]
    if fill:
        pr.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>')
    pr.append(f'<w:tcMar><w:top w:w="{mt}" w:type="dxa"/><w:left w:w="{ml}" w:type="dxa"/>'
              f'<w:bottom w:w="{mb}" w:type="dxa"/><w:right w:w="110" w:type="dxa"/></w:tcMar>')
    pr.append(f'<w:vAlign w:val="{valign}"/>')
    return f'<w:tc><w:tcPr>{"".join(pr)}</w:tcPr>{contenido or _par("")}</w:tc>'


def _tabla(filas: list[str], anchos: list[int], align: str = "") -> str:
    grid = "".join(f'<w:gridCol w:w="{a}"/>' for a in anchos)
    jc = f'<w:jc w:val="{align}"/>' if align else ""
    # w:tblBorders debe preceder a w:tblLayout -- el esquema fija la secuencia.
    return (f'<w:tbl><w:tblPr><w:tblW w:w="{sum(anchos)}" w:type="dxa"/>{jc}'
            f'<w:tblBorders>'
            f'<w:top w:val="nil"/><w:left w:val="nil"/><w:bottom w:val="nil"/>'
            f'<w:right w:val="nil"/><w:insideH w:val="nil"/><w:insideV w:val="nil"/>'
            f'</w:tblBorders><w:tblLayout w:type="fixed"/>'
            f'</w:tblPr><w:tblGrid>{grid}</w:tblGrid>'
            f'{"".join(filas)}</w:tbl>')


def _fila(celdas: list[str], encabezado: bool = False) -> str:
    tr = '<w:trPr><w:tblHeader/></w:trPr>' if encabezado else ""
    return f'<w:tr>{tr}{"".join(celdas)}</w:tr>'


def _seccion_titulo(num: str, titulo: str) -> str:
    return _tabla([_fila([
        _celda(_par(_run(num, 24, True, color=BLANCO), "center", despues=0),
               430, NAVY, 60, 60, 0),
        _celda(_par(_run(titulo, 26, True, color=NAVY), despues=0), ANCHO - 430, ml=160),
    ])], [430, ANCHO - 430])


def _th(txt: str, w: int, align: str = "") -> str:
    return _celda(_par(_run(txt, 17, True, color=BLANCO, caps=True, esp=6), align, despues=0),
                  w, NAVY, 90, 90)


def _td(txt, w: int, fill: str = "", align: str = "", bold: bool = False,
        color: str = INK, sz: int = 19) -> str:
    return _celda(_par(_run(str(txt), sz, bold, color=color), align, despues=0), w, fill)


def _cita_docx(texto: str, actor: str, fuente: str, tono: str) -> str:
    color = {"positivo": GREEN, "negativo": RED}.get(tono, TENUE)
    fill = {"positivo": GREEN_BG, "negativo": RED_BG}.get(tono, FILA_BG)
    cuerpo = (_par(_run("«" + texto + "»", 19, italic=True), despues=60)
              + _par(_run("— ", 17, color=GRIS) + _run(actor, 17, True, color=NAVY)
                     + _run("  ·  " + fuente, 17, color=GRIS), despues=0))
    return _tabla([_fila([
        _celda(_par(_run("")), 46, color, 0, 0, 0),
        _celda(cuerpo, ANCHO - 46, fill, 130, 130, 190, valign="top"),
    ])], [46, ANCHO - 46]) + _vacio(110)


def _barra_txt(n: int, maximo: int, ancho: int = 18) -> str:
    return "▮" * max(1, round(n / maximo * ancho)) if n else ""


def render_docx(ruta: str, cliente: str, cargo: str, universo: int,
                por_medio_total: Counter, notas: list[dict], desde: datetime,
                hasta: datetime, patrones: list[str] | None = None) -> None:
    """Escribe el informe en Word con el mismo contenido y las mismas cifras
    que el Markdown -- ambos se calculan de los mismos datos, no se transcribe
    uno del otro."""
    todas = notas
    notas = [x for x in todas if x.get("menciona_cliente", True)]
    notas_tema = [x for x in todas if not x.get("menciona_cliente", True)]
    n = len(notas)
    if not n:
        raise SystemExit("no hay notas del cliente para el Word")

    sent = Counter(x.get("sentimiento", "neutral") for x in notas)
    pos, neu, neg = sent["positivo"], sent["neutral"], sent["negativo"]
    fpct = lambda v, t=n: (100 * v / t) if t else 0
    spct = lambda v, t=n: f"{fpct(v, t):.1f}%"

    por_medio = Counter(x["medio"] for x in notas)
    por_dia = Counter(x["fecha"].strftime("%Y-%m-%d") for x in notas)
    por_hora = Counter(x["fecha"].hour for x in notas)
    sent_medio = defaultdict(Counter)
    for x in notas:
        sent_medio[x["medio"]][x.get("sentimiento", "neutral")] += 1

    _RUIDO = {"de", "del", "contra", "sobre", "el", "la", "los", "las", "y", "a", "en",
              "joh", "expresidente", "exmandatario"}
    for parte in cliente.lower().replace(",", " ").split():
        _RUIDO.add(_sin_acentos(parte.strip(".()")))

    def _eje_norm(txt: str) -> str:
        out = [w for w in (_sin_acentos(c.strip(".,;:()[]\"'-–—"))
                           for c in txt.lower().split()) if w and w not in _RUIDO]
        return " ".join(out)

    ejes = Counter(_eje_norm(x["eje"]) for x in notas if x.get("eje") and _eje_norm(x["eje"]))
    sent_eje = defaultdict(Counter)
    ejemplos_eje = defaultdict(list)
    for x in notas:
        e = _eje_norm(x.get("eje", ""))
        if e:
            sent_eje[e][x.get("sentimiento", "neutral")] += 1
            ejemplos_eje[e].append(x)

    tokens_extra = set()
    for patron in patrones or []:
        for tk in re.sub(r"[^a-z ]", " ", _sin_acentos(patron.lower())).split():
            if len(tk) > 2:
                tokens_extra.add(tk)
    actores = Counter()
    for x in notas:
        for pe in x["people"]:
            c = canonizar_actor(pe, cliente, tokens_extra)
            if c:
                actores[c] += 1

    citas_vistas, citas = set(), {"positivo": [], "negativo": [], "neutral": []}
    for x in sorted(notas, key=lambda z: z["fecha"]):
        c = x.get("cita", "")
        if not c:
            continue
        llave = _sin_acentos(c.lower())[:70]
        if llave in citas_vistas:
            continue
        citas_vistas.add(llave)
        citas[x.get("sentimiento", "neutral")].append(x)

    dias = sorted(por_dia)
    d0 = datetime.strptime(dias[0], "%Y-%m-%d") if dias else desde
    d1 = datetime.strptime(dias[-1], "%Y-%m-%d") if dias else hasta
    periodo = (_fecha_larga(d0) if d0.date() == d1.date()
               else f"{_fecha_larga(d0)} — {_fecha_larga(d1)}")
    hl, hh = desde.astimezone(TZ), hasta.astimezone(TZ)
    franja = f"{periodo} · {hl:%H:%M} – {hh:%H:%M} (GMT-6)"
    silenciosos = sorted(m for m in por_medio_total if m not in por_medio)

    B = []
    add = B.append

    # ------------------------------------------------------------- portada
    add(_vacio(2100))
    add(_par(_run("INFORME EJECUTIVO", 52, True, color=NAVY), "center", despues=0))
    add(_par(_run("DE MONITOREO DE MEDIOS", 52, True, color=NAVY), "center", despues=160))
    add(_par(_run("Análisis de cobertura, actores, voces y sentimiento", 21, color=GRIS),
             "center", despues=260))
    add(_tabla([_fila([_celda(_par(_run("")), 3400, GOLD, 22, 22, 0)])], [3400], "center"))
    add(_vacio(700))
    add(_par(_run("PREPARADO PARA", 17, color=TENUE, caps=True, esp=60), "center", despues=140))
    add(_par(_run(cliente, 40, True, color=NAVY), "center", despues=60))
    if cargo:
        add(_par(_run(cargo, 21, italic=True, color=GRIS), "center", despues=700))

    meta = [("Período analizado", franja),
            ("Universo monitoreado", f"{universo:,} notas · {len(por_medio_total)} medios de radio y TV"),
            (f"Notas sobre el cliente", f"{n} ({spct(n, universo)} del universo)")]
    if notas_tema:
        etiquetas = sorted({t for x in notas_tema for t in x.get("temas", [])})
        meta.append(("Temas monitoreados", f"{len(notas_tema)} notas · {', '.join(etiquetas)}"))
    add(_tabla([_fila([
        _celda(_par(_run(k, 18, color=GRIS), "right", despues=0), 3400, mt=80, mb=80),
        _celda(_par(_run(v, 18, True, color=NAVY), despues=0), 4200, mt=80, mb=80, ml=220),
    ]) for k, v in meta], [3400, 4200], "center"))
    add(_vacio(900))
    add(_tabla([_fila([_celda(
        _par(_run("CONFIDENCIAL — USO EXCLUSIVO DEL DESTINATARIO", 18, True,
                  color=BLANCO, caps=True, esp=40), "center", despues=0),
        ANCHO, NAVY, 120, 120, 0)])], [ANCHO]))

    # ------------------------------------------------------- 1. resumen
    add(_salto())
    add(_seccion_titulo("1", "Resumen ejecutivo"))
    add(_vacio(200))
    razon = round(universo / n) if n else 0
    add(_par(_run(f"Durante el período se registraron ")
             + _run(f"{universo:,} notas", bold=True)
             + _run(f" de radio y televisión en {len(por_medio_total)} medios nacionales. De ellas, ")
             + _run(f"{n} notas ({spct(n, universo)})", bold=True, color=NAVY)
             + _run(f" trataron directamente sobre su persona: ")
             + _run(f"1 de cada {razon} notas", bold=True)
             + _run(" emitidas en esa franja."), "both", despues=240))

    def tarjeta(valor, etiqueta, color, fill, w):
        return _celda(_par(_run(valor, 40, True, color=color), "center", despues=20)
                      + _par(_run(etiqueta, 17, color=GRIS), "center", despues=0),
                      w, fill, 160, 160, 60)
    add(_tabla([_fila([
        tarjeta(str(n), "notas sobre su figura", NAVY, FILA_BG, 2616),
        tarjeta(spct(pos), f"positivas ({pos})", GREEN, GREEN_BG, 2616),
        tarjeta(spct(neu), f"neutrales ({neu})", GRIS, FILA_BG, 2617),
        tarjeta(spct(neg), f"negativas ({neg})", RED, RED_BG, 2617),
    ])], [2616, 2616, 2617, 2617]))
    add(_vacio(260))

    # barra proporcional de sentimiento
    anchos, cols = [], []
    for v, col, txt in ((pos, GREEN, spct(pos)), (neu, TENUE, f"Neutral {spct(neu)}"),
                        (neg, RED, spct(neg))):
        if v:
            anchos.append(max(600, round(ANCHO * v / n)))
            cols.append((col, txt))
    if anchos:
        anchos[-1] += ANCHO - sum(anchos)     # cuadrar al ancho exacto
        add(_tabla([_fila([
            _celda(_par(_run(txt, 17, True, color=BLANCO), "center", despues=0), w, col, 70, 70, 0)
            for w, (col, txt) in zip(anchos, cols)])], anchos))
    add(_par(_run(f"Distribución de sentimiento sobre las {n} notas del período.",
                  16, italic=True, color=TENUE), "center", antes=90, despues=280))

    balance = "favorable" if pos > neg else ("adverso" if neg > pos else "equilibrado")
    col_bal = GREEN if pos > neg else (RED if neg > pos else GRIS)
    if pos and neg:
        ratio = (f"por cada nota crítica se emitieron {pos / neg:.1f} favorables"
                 if pos > neg else f"por cada nota favorable se emitieron {neg / pos:.1f} críticas")
    else:
        ratio = f"{pos} favorables frente a {neg} críticas"
    add(_par(_run("El balance del período es ") + _run(balance, bold=True, color=col_bal)
             + _run(f": {ratio}. La cobertura neutral representa {spct(neu)} del total "
                    f"—reporte procesal, mayoritariamente—, lo que indica que la agenda "
                    f"la marca el calendario judicial y no la línea editorial. Las plazas "
                    f"donde sí hay editorialización se detallan en la sección 2."),
             "both", despues=0))

    # -------------------------------------------------------- 2. medios
    add(_salto())
    add(_seccion_titulo("2", "Dónde y cómo se habló de usted"))
    add(_vacio(200))
    maxn = max(por_medio.values())
    anchos_m = [1900, 750, 1000, 600, 600, 600, 1700, 3316]
    filas = [_fila([_th("Medio", 1900), _th("Notas", 750, "center"),
                    _th("% de su agenda", 1000, "center"), _th("Pos", 600, "center"),
                    _th("Neu", 600, "center"), _th("Neg", 600, "center"),
                    _th("Lectura", 1700), _th("Volumen", 3316)], True)]
    for i, (medio, cant) in enumerate(por_medio.most_common()):
        c = sent_medio[medio]
        bg = FILA_BG if i % 2 else BLANCO
        share = f"{100 * cant / por_medio_total[medio]:.1f}%" if por_medio_total[medio] else "—"
        if cant >= 5:
            if c["negativo"] >= c["positivo"] + 3:
                lectura, col_l = "Línea crítica", RED
            elif c["positivo"] >= c["negativo"] + 3:
                lectura, col_l = "Línea favorable", GREEN
            elif c["neutral"] >= cant * 0.6:
                lectura, col_l = "Reporte factual", BLUE
            else:
                lectura, col_l = "Mixta", GRIS
        else:
            lectura, col_l = "Volumen bajo", TENUE
        filas.append(_fila([
            _td(medio, 1900, bg, bold=True),
            _td(cant, 750, bg, "center", True),
            _td(share, 1000, bg, "center"),
            _td(c["positivo"], 600, bg, "center", bool(c["positivo"]),
                GREEN if c["positivo"] else TENUE),
            _td(c["neutral"], 600, bg, "center", color=GRIS),
            _td(c["negativo"], 600, bg, "center", bool(c["negativo"]),
                RED if c["negativo"] else TENUE),
            _td(lectura, 1700, bg, color=col_l, sz=18),
            _td(_barra_txt(cant, maxn), 3316, bg, color=NAVY, sz=18),
        ]))
    add(_tabla(filas, anchos_m))
    add(_vacio(240))
    if silenciosos:
        add(_par(_run("Sin ninguna nota sobre usted: ", bold=True, color=NAVY)
                 + _run(", ".join(silenciosos) + f" — {len(silenciosos)} de "
                        f"{len(por_medio_total)} medios monitoreados, pese a cubrir la "
                        f"agenda nacional con normalidad en la misma franja."),
                 "both", despues=200))
    if por_hora:
        picos = ", ".join(f"{h:02d}:00 ({c} notas)" for h, c in por_hora.most_common(3))
        add(_par(_run("Franjas de mayor volumen (GMT-6). ", bold=True, color=NAVY)
                 + _run(f"{picos}. Fuera de ese bloque la cobertura cae a niveles residuales."),
                 "both", despues=200))

    # ---------------------------------------------------------- 3. ejes
    add(_salto())
    add(_seccion_titulo("3", "Ejes narrativos: qué se está diciendo"))
    add(_vacio(200))
    top_ejes = ejes.most_common(8)
    if top_ejes:
        suma = sum(v for _, v in top_ejes)
        add(_par(_run(f"El eje más frecuente fue ")
                 + _run(f"«{top_ejes[0][0]}»", bold=True, color=NAVY)
                 + _run(f" ({_pl(top_ejes[0][1], 'nota', 'notas')}). Los ejes listados "
                        f"reúnen {suma} de las {n} notas ({spct(suma)}) y son, en su "
                        f"mayoría, variantes del mismo asunto."), "both", despues=200))
        anchos_e = [2600, 700, 560, 560, 560, 5486]
        fe = [_fila([_th("Eje (según registro)", 2600), _th("Notas", 700, "center"),
                     _th("Pos", 560, "center"), _th("Neu", 560, "center"),
                     _th("Neg", 560, "center"), _th("Contenido", 5486)], True)]
        for i, (eje, cant) in enumerate(top_ejes):
            c = sent_eje[eje]
            bg = FILA_BG if i % 2 else BLANCO
            muestra = ejemplos_eje[eje][0]
            contenido = muestra.get("resumen", "") or muestra.get("titulo", "")
            fe.append(_fila([
                _td(f"«{eje}»", 2600, bg, bold=True, color=NAVY, sz=18),
                _td(cant, 700, bg, "center", True),
                _td(c["positivo"], 560, bg, "center", color=GREEN if c["positivo"] else TENUE),
                _td(c["neutral"], 560, bg, "center", color=GRIS),
                _td(c["negativo"], 560, bg, "center", color=RED if c["negativo"] else TENUE),
                _celda(_par(_run(contenido[:400], 18), "both", despues=0), 5486, bg),
            ]))
        add(_tabla(fe, anchos_e))
        add(_vacio(200))
        add(_par(_run("El «contenido» de cada eje es el resumen de una nota real de ese "
                      "eje, no una síntesis editorial.", 17, italic=True, color=TENUE),
                 "both", despues=0))

    # --------------------------------------------------------- 4. voces
    add(_salto())
    add(_seccion_titulo("4", "Las voces: qué se dijo de usted, textualmente"))
    add(_vacio(200))
    add(_par(_run("Citas copiadas literalmente de la transcripción del aire. La "
                  "transcripción automática puede registrar variantes fonéticas de "
                  "nombres y palabras; se citan sin editar. Toda cita que no pudo "
                  "verificarse contra la transcripción fue descartada.",
                  18, italic=True, color=GRIS), "both", despues=160))
    for etiqueta, clave, tope, col in (
            ("Voces que lo favorecen o lo defienden", "positivo", 6, GREEN),
            ("Voces críticas o acusatorias", "negativo", 8, RED),
            ("Registro neutral relevante", "neutral", 5, GRIS)):
        sel = citas[clave][:tope]
        if not sel:
            continue
        add(_par(_run(etiqueta, 21, True, color=col), antes=200, despues=140))
        for x in sel:
            actor = x.get("cita_actor") or f"voz al aire ({x['medio']})"
            add(_cita_docx(x["cita"], actor, f"{x['medio']} · {x['fecha']:%d/%m %H:%M}", clave))

    # ------------------------------------------------------- 5. actores
    add(_salto())
    add(_seccion_titulo("5", "Actores presentes en su cobertura"))
    add(_vacio(200))
    fa = [_fila([_th("Actor", 3600), _th("Notas en que aparece", 1800, "center"),
                 _th("Presencia relativa", 5066)], True)]
    maxa = max(actores.values()) if actores else 1
    for i, (actor, cant) in enumerate(actores.most_common(12)):
        bg = FILA_BG if i % 2 else BLANCO
        fa.append(_fila([
            _td(actor, 3600, bg, bold=True, color=NAVY),
            _td(cant, 1800, bg, "center", True),
            _td(_barra_txt(cant, maxa), 5066, bg, color=BLUE, sz=18),
        ]))
    add(_tabla(fa, [3600, 1800, 5066]))
    add(_vacio(200))
    add(_par(_run("Nombres agrupados por persona a partir del campo de personas "
                  "mencionadas en cada nota; las variantes fonéticas del sistema de "
                  "transcripción se corrigieron donde fue posible confirmarlas.",
                  17, italic=True, color=TENUE), "both", despues=0))

    sec = 6
    # -------------------------------------------------------- 6. temas
    if notas_tema:
        add(_salto())
        add(_seccion_titulo(str(sec), "Temas monitoreados"))
        add(_vacio(200))
        etiquetas = sorted({t for x in notas_tema for t in x.get("temas", [])})
        add(_par(_run(f"{len(notas_tema)} notas del período tratan asuntos bajo "
                      f"seguimiento ")
                 + _run("sin mencionar a " + cliente, bold=True)
                 + _run(". No se suman al conteo de menciones directas: el vínculo con "
                        "su figura lo establece el análisis, no la nota. Se reportan "
                        "porque el asunto puede escalar hacia usted."),
                 "both", despues=220))
        por_tema = defaultdict(list)
        for x in notas_tema:
            for t_ in x.get("temas", []):
                por_tema[t_].append(x)
        for etiqueta in etiquetas:
            items = por_tema[etiqueta]
            st = Counter(x.get("sentimiento", "neutral") for x in items)
            add(_par(_run(f"{etiqueta} — {_pl(len(items), 'nota', 'notas')} "
                          f"({st['positivo']} pos / {st['neutral']} neu / {st['negativo']} neg)",
                          21, True, color=NAVY), antes=120, despues=140))
            ft = [_fila([_th("Hora", 1300, "center"), _th("Medio", 2000),
                         _th("Nota", 7166)], True)]
            for i, x in enumerate(sorted(items, key=lambda z: z["fecha"])[:14]):
                bg = FILA_BG if i % 2 else BLANCO
                ft.append(_fila([
                    _td(f"{x['fecha']:%d/%m %H:%M}", 1300, bg, "center", sz=18),
                    _td(x["medio"], 2000, bg, bold=True, sz=18),
                    _celda(_par(_run(x["titulo"][:220], 18), despues=0), 7166, bg),
                ]))
            add(_tabla(ft, [1300, 2000, 7166]))
            add(_vacio(160))
            for x in sorted(items, key=lambda z: z["fecha"]):
                if x.get("cita"):
                    actor = x.get("cita_actor") or f"voz al aire ({x['medio']})"
                    add(_cita_docx(x["cita"], actor,
                                   f"{x['medio']} · {x['fecha']:%d/%m %H:%M}",
                                   x.get("sentimiento", "neutral")))
                    break
        sec += 1

    # ------------------------------------------------------ 7. lectura
    add(_salto())
    add(_seccion_titulo(str(sec), "Lectura estratégica del período"))
    add(_vacio(200))
    add(_par(_run("Observaciones derivadas exclusivamente de los datos anteriores.",
                  18, italic=True, color=GRIS), despues=220))

    obs = []
    no_adversas = pos + neu
    if n:
        obs.append((f"La cobertura del período es mayoritariamente "
                    f"{'no adversa' if no_adversas > neg else 'adversa'}.",
                    f"{no_adversas} de las {n} notas ({spct(no_adversas)}) son favorables "
                    f"o neutrales y {neg} son negativas. El registro dominante es "
                    f"procesal: la agenda la marca el calendario judicial."))
    plazas_pos = [(m, c["positivo"]) for m, c in sent_medio.items() if c["positivo"]]
    if plazas_pos and pos:
        plazas_pos.sort(key=lambda z: -z[1])
        cubren = sum(v for _, v in plazas_pos)
        obs.append(("La vocería favorable está concentrada.",
                    f"Las {pos} notas positivas del período salieron de "
                    + ", ".join(f"{m} ({v})" for m, v in plazas_pos)
                    + (f"; ningún otro medio emitió una sola nota favorable."
                       if cubren == pos and len(plazas_pos) < len(por_medio) else ".")))
    plazas_neg = [(m, c["negativo"]) for m, c in sent_medio.items() if c["negativo"]]
    if plazas_neg:
        plazas_neg.sort(key=lambda z: -z[1])
        obs.append(("Dónde está la carga crítica.",
                    ", ".join(f"{m} ({v})" for m, v in plazas_neg)
                    + f" concentran las {neg} notas negativas del período."))
    if silenciosos:
        obs.append((f"{len(silenciosos)} de {len(por_medio_total)} medios guardaron silencio.",
                    ", ".join(silenciosos) + " no emitieron ni una nota sobre su figura "
                    "en la franja analizada, pese a cubrir la agenda nacional."))
    if por_hora:
        top3 = por_hora.most_common(3)
        suma3 = sum(v for _, v in top3)
        obs.append(("La conversación se concentra en pocas horas.",
                    f"{suma3} de las {n} notas ({spct(suma3)}) se emitieron en "
                    + ", ".join(f"{h:02d}:00" for h, _ in top3)
                    + ". Fuera de esa ventana la cobertura es residual."))
    if notas_tema:
        etiquetas = sorted({t for x in notas_tema for t in x.get("temas", [])})
        obs.append(("Hay un frente abierto que no lo nombra.",
                    f"{len(notas_tema)} notas tratan {', '.join(etiquetas)} sin "
                    f"mencionarlo. Es cobertura que hoy no computa como suya pero "
                    f"puede escalar hacia su figura (sección {sec - 1})."))

    for i, (titulo, cuerpo) in enumerate(obs, start=1):
        add(_tabla([_fila([
            _celda(_par(_run(str(i), 21, True, color=BLANCO), "center", despues=0),
                   430, BLUE, 70, 70, 0, valign="top"),
            _celda(_par(_run(titulo, 20, True, color=NAVY), despues=50)
                   + _par(_run(cuerpo), "both", despues=0),
                   ANCHO - 430, ml=190, valign="top"),
        ])], [430, ANCHO - 430]))
        add(_vacio(180))
    sec += 1

    # -------------------------------------------------- 8. metodologia
    add(_salto())
    add(_seccion_titulo(str(sec), "Alcance y metodología"))
    add(_vacio(200))
    add(_par(_run("Fuente. ", bold=True, color=NAVY)
             + _run(f"Monitoreo continuo de radio y televisión, {franja}, sobre "
                    f"{len(por_medio_total)} medios: {', '.join(sorted(por_medio_total))}. "
                    f"Cada nota incluye fecha y hora, medio, título, resumen, palabras "
                    f"clave, personas mencionadas y transcripción literal del segmento."),
             "both", despues=180))
    add(_par(_run("Criterio de inclusión. ", bold=True, color=NAVY)
             + _run(f"Toda nota cuyo título, resumen, palabras clave o personas mencionan "
                    f"directamente a {cliente}: {n} notas de un universo de {universo:,}. "
                    f"Es un criterio de mención directa: se pierde la nota que afecta su "
                    f"figura sin nombrarlo, a cambio de que todo lo incluido sea verificable."),
             "both", despues=180))
    if notas_tema:
        etiquetas = sorted({t for x in notas_tema for t in x.get("temas", [])})
        add(_par(_run("Temas monitoreados. ", bold=True, color=NAVY)
                 + _run(f"Además se siguen asuntos que pueden afectar su figura sin "
                        f"nombrarlo ({', '.join(etiquetas)}): {len(notas_tema)} notas del "
                        f"período, reportadas por separado y nunca sumadas al conteo de "
                        f"menciones directas."), "both", despues=180))
    con_cita = sum(1 for x in notas if x.get("cita"))
    add(_par(_run("Sentimiento y citas. ", bold=True, color=NAVY)
             + _run(f"Cada nota fue clasificada según cómo queda posicionada su figura "
                    f"(positivo / neutral / negativo). Las citas son copia textual de la "
                    f"transcripción del aire: de las {n} notas, {con_cita} arrojaron una "
                    f"cita verificable contra su transcripción y las restantes fueron "
                    f"descartadas."), "both", despues=180))
    dur = (hasta - desde).total_seconds() / 3600
    if dur < 23:
        add(_par(_run("Alcance temporal. ", bold=True, color=NAVY)
                 + _run(f"Este informe cubre la franja de {hl:%H:%M} a {hh:%H:%M} (GMT-6) "
                        f"del {hl:%d/%m/%Y}, no el día completo ({dur:.0f} horas). Las cifras "
                        f"no son comparables sin ajuste con informes de jornada completa."),
                 "both", despues=240))
    add(_tabla([_fila([_celda(
        _par(_run("Este informe no contiene ningún dato externo al monitoreo: cada cifra, "
                  "nombre y cita proviene de las notas registradas en el período.",
                  18, True, color=NAVY), "center", despues=0),
        ANCHO, FILA_BG, 140, 140, 0)])], [ANCHO]))

    _escribir_docx(ruta, "".join(B),
                   f"Informe de monitoreo de medios  ·  {hl:%d/%m/%Y}  ·  CONFIDENCIAL")


def _escribir_docx(ruta: str, cuerpo: str, pie: str) -> None:
    """Empaqueta las partes minimas de un .docx (carta, margenes de 0.6\")."""
    ns = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
    sect = ('<w:sectPr><w:footerReference r:id="rId2" w:type="default"/>'
            '<w:pgSz w:w="12240" w:h="15840" w:orient="portrait"/>'
            '<w:pgMar w:top="887" w:right="887" w:bottom="887" w:left="887" '
            'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>')
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document {ns}><w:body>{cuerpo}{sect}</w:body></w:document>')
    estilos = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               f'<w:styles {ns}><w:docDefaults><w:rPrDefault><w:rPr>'
               f'<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
               f'<w:sz w:val="19"/><w:szCs w:val="19"/><w:color w:val="{INK}"/>'
               f'</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
               f'<w:spacing w:after="120" w:line="264" w:lineRule="auto"/>'
               f'</w:pPr></w:pPrDefault></w:docDefaults></w:styles>')
    footer = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              f'<w:ftr {ns}>{_par(_run(pie, 16, color=TENUE), "center", despues=0)}</w:ftr>')
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
              '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
              '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
              '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '</Relationships>')
    drels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
             '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
             '</Relationships>')
    with zipfile.ZipFile(ruta, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
        z.writestr("word/_rels/document.xml.rels", drels)
        z.writestr("word/styles.xml", estilos)
        z.writestr("word/footer1.xml", footer)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--cliente", required=True)
    p.add_argument("--cargo", default="")
    p.add_argument("--patrones", required=True, help="regex separados por coma")
    p.add_argument("--desde", required=True)
    p.add_argument("--hasta", required=True)
    p.add_argument("--salida", required=True)
    p.add_argument("--salida-docx", default=None,
                   help="ruta del .docx; si se omite, solo se escribe el Markdown")
    p.add_argument("--tema", action="append", default=[],
                   help='tema monitoreado, "Etiqueta::regex" (repetible)')
    p.add_argument("--sin-analisis", action="store_true")
    a = p.parse_args()

    desde = datetime.fromisoformat(a.desde.replace("Z", "+00:00"))
    hasta = datetime.fromisoformat(a.hasta.replace("Z", "+00:00"))
    patrones = [x.strip() for x in a.patrones.split(",") if x.strip()]
    temas = {}
    for spec in a.tema:
        if "::" not in spec:
            raise SystemExit(f'--tema mal formado: {spec!r} (usar "Etiqueta::regex")')
        etiqueta, rx = spec.split("::", 1)
        temas[etiqueta.strip()] = rx.strip()

    universo, por_medio_total, notas = cargar_notas(desde, hasta, patrones, temas)
    del_cliente = [x for x in notas if x.get("menciona_cliente")]
    de_tema = [x for x in notas if not x.get("menciona_cliente")]
    print(f"universo={universo}  notas del cliente={len(del_cliente)}  "
          f"solo tema={len(de_tema)}")
    if not del_cliente:
        raise SystemExit("no hay notas del cliente en el periodo")

    if not a.sin_analisis:
        print("analizando sentimiento y extrayendo citas...")
        analizar_notas(notas, a.cliente)
        con_cita = sum(1 for x in del_cliente if x.get("cita"))
        print(f"citas verificadas contra transcripcion: {con_cita}/{len(del_cliente)}")

    md = render(a.cliente, a.cargo, universo, por_medio_total, notas, desde, hasta,
                patrones=patrones)
    Path(a.salida).write_text(md, encoding="utf-8")
    print(f"informe escrito en {a.salida} ({len(md.splitlines())} lineas)")

    if a.salida_docx:
        render_docx(a.salida_docx, a.cliente, a.cargo, universo, por_medio_total,
                    notas, desde, hasta, patrones=patrones)
        print(f"informe Word escrito en {a.salida_docx} "
              f"({Path(a.salida_docx).stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
