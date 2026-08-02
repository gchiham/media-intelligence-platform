"""Destiller: reparacion de la particion, exclusiones por umbral y composicion
con un AIAnalysisProvider. Ver src/modules/ai/destiller.py.

Todo lo que se prueba aca es determinista y no llama a ninguna API: el valor de
Destiller depende de que la garantia de cobertura total se cumpla incluso
cuando el LLM devuelve basura, porque esa garantia es la que hace medible el
recall en la validacion humana.
"""
from unittest.mock import MagicMock

import pytest

from src.modules.ai.destiller import (
    BloqueDestilado,
    Destiller,
    DestilledAnalysisProvider,
    TipoBloque,
    exclusiones,
    filtrar_words,
    normalizar_particion,
)
from src.modules.ai.exceptions import SegmentationError
from src.modules.ai.schemas import NewsSegment, Word


def _word(i: int) -> Word:
    return Word(index=i, word=f"palabra{i}", start=i * 0.3, end=i * 0.3 + 0.28)


def _bloque(lo: int, hi: int, tipo: TipoBloque = TipoBloque.PUBLICIDAD, conf: float = 0.9):
    return BloqueDestilado(start_word=lo, end_word=hi, tipo=tipo, confidence=conf)


def _segment() -> NewsSegment:
    return NewsSegment(
        title="t", start_word=0, end_word=1, summary="s", keywords=["k"],
        news_type="otro", people=[], organizations=[], locations=[], confidence=0.9,
    )


def _cubre(bloques: list[BloqueDestilado], lo: int, hi: int) -> bool:
    """La particion cubre [lo, hi] sin huecos ni traslapes."""
    if not bloques:
        return lo > hi
    if bloques[0].start_word != lo or bloques[-1].end_word != hi:
        return False
    return all(b.start_word == a.end_word + 1 for a, b in zip(bloques, bloques[1:]))


class TestNormalizarParticion:
    def test_deja_intacta_una_particion_valida(self):
        bloques = [_bloque(0, 9), _bloque(10, 19, TipoBloque.NOTICIA)]
        assert normalizar_particion(bloques, 0, 19) == bloques

    def test_tapa_hueco_con_desconocido(self):
        salida = normalizar_particion([_bloque(0, 4), _bloque(10, 19)], 0, 19)

        assert _cubre(salida, 0, 19)
        hueco = [b for b in salida if b.tipo == TipoBloque.DESCONOCIDO]
        assert [(b.start_word, b.end_word) for b in hueco] == [(5, 9)]

    def test_hueco_al_inicio_y_al_final(self):
        salida = normalizar_particion([_bloque(5, 9)], 0, 19)

        assert _cubre(salida, 0, 19)
        assert [(b.start_word, b.end_word, b.tipo) for b in salida] == [
            (0, 4, TipoBloque.DESCONOCIDO),
            (5, 9, TipoBloque.PUBLICIDAD),
            (10, 19, TipoBloque.DESCONOCIDO),
        ]

    def test_trunca_traslape_contra_el_bloque_previo(self):
        salida = normalizar_particion([_bloque(0, 12), _bloque(10, 19, TipoBloque.NOTICIA)], 0, 19)

        assert _cubre(salida, 0, 19)
        assert [(b.start_word, b.end_word) for b in salida] == [(0, 12), (13, 19)]

    def test_recorta_lo_que_se_sale_del_rango(self):
        salida = normalizar_particion([_bloque(-5, 25)], 0, 19)

        assert [(b.start_word, b.end_word) for b in salida] == [(0, 19)]

    def test_descarta_bloque_completamente_contenido_en_uno_previo(self):
        salida = normalizar_particion([_bloque(0, 19), _bloque(5, 9, TipoBloque.NOTICIA)], 0, 19)

        assert [(b.start_word, b.end_word) for b in salida] == [(0, 19)]

    def test_sin_bloques_devuelve_todo_como_desconocido(self):
        # Ante un fallo total del modelo la cobertura se mantiene, y queda
        # marcada como no clasificada -- nunca como basura.
        salida = normalizar_particion([], 0, 19)

        assert _cubre(salida, 0, 19)
        assert all(b.tipo == TipoBloque.DESCONOCIDO for b in salida)

    def test_desordenados_se_ordenan(self):
        salida = normalizar_particion([_bloque(10, 19), _bloque(0, 9)], 0, 19)

        assert _cubre(salida, 0, 19)


class TestExclusiones:
    def test_solo_basura_sobre_el_umbral(self):
        bloques = [
            _bloque(0, 9, TipoBloque.PUBLICIDAD, 0.95),
            _bloque(10, 19, TipoBloque.PUBLICIDAD, 0.60),
            _bloque(20, 29, TipoBloque.NOTICIA, 0.99),
        ]
        assert [(b.start_word, b.end_word) for b in exclusiones(bloques, 0.85)] == [(0, 9)]

    def test_desconocido_nunca_se_excluye(self):
        # Un hueco del modelo no es evidencia de basura; ante la duda se paga
        # la llamada al LLM de segmentacion.
        bloques = [_bloque(0, 9, TipoBloque.DESCONOCIDO, 1.0)]
        assert exclusiones(bloques, 0.0) == []


class TestFiltrarWords:
    def test_conserva_el_indice_original(self):
        words = [_word(i) for i in range(10)]

        filtradas = filtrar_words(words, [_bloque(2, 4)])

        # El Clipper depende de este indice: renumerar cortaria audio ajeno.
        assert [w.index for w in filtradas] == [0, 1, 5, 6, 7, 8, 9]

    def test_sin_exclusiones_devuelve_todo(self):
        words = [_word(i) for i in range(5)]
        assert filtrar_words(words, []) == words


class TestDestilarEnParalelo:
    def test_conserva_el_orden_de_los_chunks(self):
        # Los chunks salen concurrentes pero los bloques tienen que quedar
        # ordenados por indice: el dashboard de validacion los muestra en orden
        # de emision y `normalizar_particion` ya no vuelve a correr sobre el
        # conjunto completo.
        words = [_word(i) for i in range(2000)]
        destiller = Destiller.__new__(Destiller)
        destiller._chunk_size = 600
        destiller._concurrencia = 8
        destiller._destilar_chunk = lambda chunk: [
            _bloque(chunk[0].index, chunk[-1].index, TipoBloque.NOTICIA)
        ]

        bloques = Destiller.destilar(destiller, words)

        assert [(b.start_word, b.end_word) for b in bloques] == [
            (0, 599), (600, 1199), (1200, 1799), (1800, 1999),
        ]

    def test_sin_palabras_no_llama_al_modelo(self):
        destiller = Destiller.__new__(Destiller)
        destiller._chunk_size = 600
        destiller._concurrencia = 8
        destiller._destilar_chunk = lambda chunk: pytest.fail("no debio llamarse")

        assert Destiller.destilar(destiller, []) == []


class TestDestilledAnalysisProvider:
    def test_segmenta_sobre_la_vista_filtrada(self):
        words = [_word(i) for i in range(10)]
        destiller = MagicMock()
        destiller.destilar.return_value = [
            _bloque(0, 4, TipoBloque.PUBLICIDAD, 0.95),
            _bloque(5, 9, TipoBloque.NOTICIA, 0.95),
        ]
        inner = MagicMock()
        inner.segment_news.return_value = [_segment()]

        provider = DestilledAnalysisProvider(destiller, inner, umbral=0.85)
        assert provider.segment_news(words) == [_segment()]

        enviadas = inner.segment_news.call_args.args[0]
        assert [w.index for w in enviadas] == [5, 6, 7, 8, 9]

    def test_fail_open_si_el_destilado_falla(self):
        # El prompt de segmentacion ya excluye publicidad por su cuenta, asi
        # que degradar al pipeline de hoy es mejor que perder la grabacion.
        words = [_word(i) for i in range(10)]
        destiller = MagicMock()
        destiller.destilar.side_effect = SegmentationError("timeout")
        inner = MagicMock()
        inner.segment_news.return_value = [_segment()]

        provider = DestilledAnalysisProvider(destiller, inner, umbral=0.85)
        assert provider.segment_news(words) == [_segment()]

        assert inner.segment_news.call_args.args[0] == words

    def test_no_llama_al_proveedor_si_todo_era_basura(self):
        words = [_word(i) for i in range(10)]
        destiller = MagicMock()
        destiller.destilar.return_value = [_bloque(0, 9, TipoBloque.PUBLICIDAD, 0.95)]
        inner = MagicMock()

        provider = DestilledAnalysisProvider(destiller, inner, umbral=0.85)

        assert provider.segment_news(words) == []
        inner.segment_news.assert_not_called()
