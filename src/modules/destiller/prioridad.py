"""Prioriza la cola de validacion por desacuerdo entre modelos.

La idea: donde dos modelos discrepan, un humano aporta muchisimo mas que donde
coinciden. Pero "discrepan" no es una propiedad de un bloque -- cada modelo
corta la transmision en bloques distintos (Qwen produjo 815 y gpt-4o-mini 662
sobre las mismas 20 grabaciones), asi que un bloque de uno puede solaparse con
varios del otro y con tipos distintos. Por eso el desacuerdo se mide sobre las
**palabras** que cubre el bloque, no comparando bloque contra bloque: cualquier
regla de correspondencia 1:1 tendria que elegir en silencio con cual de los
solapados comparar.

El numero de prioridad ES el orden de entrega de la cola, no una etiqueta.
"""
import hashlib

#: Todo lo que no es periodismo real. Espeja TIPOS_BASURA de
#: src/modules/ai/destiller.py; `desconocido` queda afuera igual que alla.
BASURA = frozenset({"publicidad", "religioso", "promo", "musica", "relleno"})

P_BINARIO_MAYORIA = 1
P_BINARIO_MINORIA = 2
P_CONTROL = 3
P_SUBTIPO = 4
P_ACUERDO = 5

#: Porcentaje de los acuerdos exactos que igual se validan, para detectar el
#: caso que la priorizacion por desacuerdo es ciega por construccion: que ambos
#: modelos compartan el mismo error. Si solo se revisara donde discrepan, un
#: sesgo comun no apareceria nunca.
PCT_CONTROL = 20


def es_muestra_control(grabacion_ref: str, bloque_idx: int, pct: int = PCT_CONTROL) -> bool:
    """Muestreo deterministico, no aleatorio: reejecutar la carga tiene que dar
    exactamente la misma muestra, o el experimento no es reproducible."""
    h = hashlib.sha256(f"{grabacion_ref}#{bloque_idx}".encode()).hexdigest()
    return int(h[:8], 16) % 100 < pct


def mapa_por_palabra(bloques: list[dict]) -> dict[int, str]:
    """{indice_de_palabra: tipo} a partir de la particion de un modelo."""
    mapa: dict[int, str] = {}
    for b in bloques:
        for i in range(b["start_word"], b["end_word"] + 1):
            mapa[i] = b["tipo"]
    return mapa


def clasificar(
    tipo: str,
    start_word: int,
    end_word: int,
    otro: dict[int, str],
    grabacion_ref: str,
    bloque_idx: int,
) -> tuple[int, float, float]:
    """(prioridad, desacuerdo_binario, porcentaje_acuerdo) de un bloque.

    `desacuerdo_binario` es la fraccion de palabras que el otro modelo pone del
    lado contrario de la unica decision que el pipeline usa: excluir o no.
    """
    otros = [otro[i] for i in range(start_word, end_word + 1) if i in otro]
    if not otros:
        # Sin solape no hay comparacion posible. Va al final y se marca sin
        # modelo comparado, para no hacerlo pasar por un acuerdo que nadie midio.
        return P_ACUERDO, 0.0, 0.0

    es_basura = tipo in BASURA
    flip = sum(1 for o in otros if (o in BASURA) != es_basura) / len(otros)
    acuerdo = sum(1 for o in otros if o == tipo) / len(otros)

    if flip >= 0.5:
        return P_BINARIO_MAYORIA, flip, acuerdo
    if flip > 0:
        return P_BINARIO_MINORIA, flip, acuerdo
    if acuerdo < 1.0:
        return P_SUBTIPO, 0.0, acuerdo
    if es_muestra_control(grabacion_ref, bloque_idx):
        return P_CONTROL, 0.0, 1.0
    return P_ACUERDO, 0.0, 1.0
