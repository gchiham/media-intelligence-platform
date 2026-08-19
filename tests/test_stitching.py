"""Pruebas de la fusion de noticias partidas en el limite de un chunk
(src/modules/ai/stitching.py).

Sin Postgres ni LLM: la fusion es deterministica a proposito -- la unica
entrada no deterministica (el `boundary_status` que decide el modelo) llega
ya resuelta en los dicts de entrada.
"""
from src.modules.ai.stitching import fusionar, stitch_por_chunk


def _seg(start, end, titulo="t", **extra):
    base = {
        "title": titulo,
        "start_word": start,
        "end_word": end,
        "summary": "s",
        "keywords": [],
        "news_type": "politica",
        "people": [],
        "organizations": [],
        "locations": [],
        "confidence": 0.9,
        "boundary_status": "complete",
        "boundary_confidence": 0.9,
    }
    base.update(extra)
    return base


def test_fusiona_cuando_el_chunk_siguiente_dice_que_continua():
    por_chunk = {
        0: [_seg(0, 100), _seg(101, 599, titulo="cortada")],
        1: [_seg(600, 700, titulo="continuacion", boundary_status="continues_before_chunk")],
    }
    salida = stitch_por_chunk(por_chunk)

    assert len(salida) == 2
    assert (salida[1]["start_word"], salida[1]["end_word"]) == (101, 700)


def test_no_fusiona_si_el_siguiente_dice_complete():
    por_chunk = {
        0: [_seg(101, 599)],
        1: [_seg(600, 700, boundary_status="complete")],
    }
    salida = stitch_por_chunk(por_chunk)

    assert len(salida) == 2
    assert salida[0]["end_word"] == 599
    assert salida[1]["start_word"] == 600


def test_no_fusiona_a_traves_de_un_chunk_vacio():
    """Si el chunk intermedio no trajo segmentos, unir el 0 con el 2 inventaria
    un rango que cubre 600 palabras que nadie segmento."""
    por_chunk = {
        0: [_seg(101, 599)],
        1: [],
        2: [_seg(1200, 1300, boundary_status="continues_before_chunk")],
    }
    salida = stitch_por_chunk(por_chunk)

    assert len(salida) == 2
    assert (salida[0]["start_word"], salida[0]["end_word"]) == (101, 599)
    assert (salida[1]["start_word"], salida[1]["end_word"]) == (1200, 1300)


def test_solo_el_primer_segmento_del_chunk_puede_fusionar():
    """Un `continues_before_chunk` en un segmento que NO es el primero del
    chunk no puede ser continuacion de nada -- hay otra noticia en medio."""
    por_chunk = {
        0: [_seg(101, 599)],
        1: [
            _seg(600, 650),
            _seg(651, 700, boundary_status="continues_before_chunk"),
        ],
    }
    salida = stitch_por_chunk(por_chunk)

    assert len(salida) == 3


def test_el_contenido_sale_del_fragmento_mas_largo():
    corto = _seg(590, 599, titulo="corto", summary="resumen corto", news_type="deportes")
    largo = _seg(
        600,
        900,
        titulo="largo",
        summary="resumen largo",
        news_type="politica",
        boundary_status="continues_before_chunk",
    )
    fusionado = fusionar(corto, largo)

    assert fusionado["title"] == "largo"
    assert fusionado["summary"] == "resumen largo"
    assert fusionado["news_type"] == "politica"
    assert (fusionado["start_word"], fusionado["end_word"]) == (590, 900)


def test_une_entidades_sin_duplicar_y_baja_la_confianza():
    a = _seg(0, 300, people=["Juan"], organizations=["Congreso"], confidence=0.9)
    b = _seg(301, 400, people=["juan", "Maria"], organizations=["Congreso"], confidence=0.6)
    fusionado = fusionar(a, b)

    assert fusionado["people"] == ["Juan", "Maria"]
    assert fusionado["organizations"] == ["Congreso"]
    # Una noticia reconstruida no puede ser mas confiable que su parte mas debil.
    assert fusionado["confidence"] == 0.6


def test_cadena_de_tres_chunks_se_fusiona_en_una_sola_noticia():
    por_chunk = {
        0: [_seg(500, 599)],
        1: [_seg(600, 1199, boundary_status="continues_before_chunk")],
        2: [_seg(1200, 1250, boundary_status="continues_before_chunk")],
    }
    salida = stitch_por_chunk(por_chunk)

    assert len(salida) == 1
    assert (salida[0]["start_word"], salida[0]["end_word"]) == (500, 1250)
