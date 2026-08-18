"""Une los dos pedazos de una noticia que quedo partida en el limite de dos
chunks consecutivos.

Por que hace falta: `chunk_words` parte la transcripcion por indice de palabra
**sin solape** (ver src/modules/ai/chunking.py), asi que una noticia que cruza
el limite se reporta como dos `NewsSegment` distintos y nadie los reconcilia --
el cliente termina viendo la misma historia como dos Noticias, ambas
incompletas. El LLM marca esos casos con `boundary_status` (ver
prompts.BOUNDARY_SYSTEM_PROMPT) y esto los une.

**La decision de fusionar la toma UN SOLO lado: el chunk que empieza.** Medido
sobre 100 fronteras reales (docs/EXPERIMENTO_BOUNDARY_STATUS.md): el lado que
termina cortado casi nunca lo detecta por su cuenta (8%), y cuando se le da
contexto para ayudarlo se vuelve un sello de goma que responde
"continues_after_chunk" en 94% de los casos aunque solo 79% sean cortes reales
-- deja de discriminar. Exigir que ambos lados coincidan bajaba el recall de
51% a 42% vetando fusiones correctas. Por eso el veto de ese lado se quito a
proposito; su `boundary_status` se conserva solo como dato de diagnostico.

Rendimiento medido con `gpt-4.1-mini`: **51% de recall, 93% de precision** --
arregla la mitad de las noticias partidas, y ~1 de cada 14 fusiones une dos
noticias que en realidad eran distintas. Ese trade se acepto explicitamente
(ver el doc). No esta validado con Claude: el camino sincronico no usa esto.
"""
from src.modules.ai.providers.prompts import MAX_KEYWORDS

# Solo estos dos valores disparan una fusion -- son los que reporta el chunk
# que EMPIEZA a mitad de una noticia.
_CONTINUA_ANTES = ("continues_before_chunk", "continues_both")

_CAMPOS_LISTA = ("keywords", "people", "organizations", "locations")


def _union_ordenada(a: list, b: list) -> list:
    """Concatena sin duplicados conservando el orden de aparicion."""
    vistos = set()
    salida = []
    for valor in list(a) + list(b):
        clave = valor.strip().lower() if isinstance(valor, str) else valor
        if clave in vistos:
            continue
        vistos.add(clave)
        salida.append(valor)
    return salida


def fusionar(primero: dict, segundo: dict) -> dict:
    """Une dos fragmentos contiguos en una sola noticia.

    El rango se estira de punta a punta. El contenido descriptivo (title,
    summary, news_type) sale del fragmento **mas largo** en palabras, que es
    el que mejor representa la nota; las listas de entidades y keywords se
    unen; y la confianza baja a la del fragmento menos seguro -- una noticia
    reconstruida no puede ser mas confiable que su parte mas debil.
    """
    largo_primero = primero["end_word"] - primero["start_word"]
    largo_segundo = segundo["end_word"] - segundo["start_word"]
    dominante = primero if largo_primero >= largo_segundo else segundo

    fusionado = {
        **dominante,
        "start_word": primero["start_word"],
        "end_word": segundo["end_word"],
        "confidence": min(primero.get("confidence", 0.0), segundo.get("confidence", 0.0)),
    }
    for campo in _CAMPOS_LISTA:
        fusionado[campo] = _union_ordenada(primero.get(campo, []), segundo.get(campo, []))
    fusionado["keywords"] = fusionado["keywords"][:MAX_KEYWORDS]
    return fusionado


def stitch_por_chunk(por_chunk: dict[int, list[dict]]) -> list[dict]:
    """Aplana los segmentos de todos los chunks de una grabacion, fusionando
    los que quedaron partidos en un limite.

    `por_chunk` mapea indice de chunk -> segmentos de ese chunk (dicts, con
    `boundary_status` incluido). Devuelve la lista plana ya reconciliada, en
    orden de aparicion.
    """
    salida: list[tuple[int, dict]] = []  # (indice del chunk del ultimo fragmento, segmento)

    for idx in sorted(por_chunk):
        segmentos = sorted(por_chunk[idx], key=lambda s: s["start_word"])
        if not segmentos:
            continue

        primero = segmentos[0]
        # Solo se fusiona con el chunk inmediatamente anterior: si el anterior
        # no trajo ningun segmento, `salida[-1]` seria de un chunk mas viejo y
        # unirlos inventaria un rango que cubre un hueco que nadie segmento.
        viene_del_anterior = bool(salida) and salida[-1][0] == idx - 1
        if viene_del_anterior and primero.get("boundary_status") in _CONTINUA_ANTES:
            anterior_idx, anterior_seg = salida[-1]
            salida[-1] = (idx, fusionar(anterior_seg, primero))
            segmentos = segmentos[1:]

        salida.extend((idx, s) for s in segmentos)

    return [segmento for _, segmento in salida]
