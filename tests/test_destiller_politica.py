"""Politica de exclusion. Ver src/modules/destiller/politica.py.

Lo que se prueba aca es la propiedad arquitectonica que justifica el modulo:
la politica opera sobre clasificaciones ya producidas, asi que cambiarla nunca
obliga a volver a correr los modelos.
"""
from src.modules.ai.destiller import BloqueDestilado, TipoBloque
from src.modules.destiller.politica import (
    CATALOGO,
    CONSERVADORA_V1,
    MINIMA,
    UN_SOLO_MODELO,
    PoliticaExclusion,
)


def _b(lo: int, hi: int, tipo: TipoBloque, conf: float = 0.9) -> BloqueDestilado:
    return BloqueDestilado(start_word=lo, end_word=hi, tipo=tipo, confidence=conf)


class TestConsenso:
    def test_unanimidad_excluye_solo_lo_que_los_tres_marcan(self):
        a = [_b(0, 9, TipoBloque.PUBLICIDAD), _b(10, 19, TipoBloque.PROMO)]
        b = [_b(0, 9, TipoBloque.PUBLICIDAD), _b(10, 19, TipoBloque.NOTICIA)]
        c = [_b(0, 9, TipoBloque.MUSICA), _b(10, 19, TipoBloque.PROMO)]

        assert CONSERVADORA_V1.rangos([a, b, c]) == [(0, 9)]

    def test_sin_modelos_suficientes_no_excluye_nada(self):
        # Si solo hay un modelo disponible, una politica que exige tres no
        # degrada a "uso lo que tengo": no excluye. Fallar hacia no-excluir es
        # la unica direccion segura.
        a = [_b(0, 9, TipoBloque.PUBLICIDAD)]

        assert CONSERVADORA_V1.rangos([a]) == []

    def test_une_rangos_contiguos_aunque_los_modelos_corten_distinto(self):
        a = [_b(0, 4, TipoBloque.PUBLICIDAD), _b(5, 9, TipoBloque.MUSICA)]
        b = [_b(0, 9, TipoBloque.PUBLICIDAD)]

        politica = PoliticaExclusion(nombre="par", minimo_modelos=2)
        assert politica.rangos([a, b]) == [(0, 9)]


class TestAlcance:
    def test_relleno_y_religioso_nunca_se_excluyen_en_v1(self):
        a = [_b(0, 9, TipoBloque.RELLENO), _b(10, 19, TipoBloque.RELIGIOSO)]

        assert CONSERVADORA_V1.rangos([a, a, a]) == []

    def test_la_politica_minima_deja_promo_afuera(self):
        a = [_b(0, 9, TipoBloque.PROMO)]

        assert MINIMA.rangos([a, a, a]) == []
        assert CONSERVADORA_V1.rangos([a, a, a]) == [(0, 9)]

    def test_desconocido_nunca_se_excluye(self):
        a = [_b(0, 9, TipoBloque.DESCONOCIDO, 1.0)]

        assert UN_SOLO_MODELO.rangos([a]) == []


class TestDesacople:
    def test_cambiar_politica_no_requiere_recorrer_modelos(self):
        # La misma clasificacion, ya producida una sola vez, alimenta politicas
        # distintas. Esta es la propiedad que justifica el modulo.
        clasificacion = [[_b(0, 9, TipoBloque.PROMO), _b(10, 19, TipoBloque.PUBLICIDAD)]] * 3

        assert CONSERVADORA_V1.rangos(clasificacion) == [(0, 19)]
        assert MINIMA.rangos(clasificacion) == [(10, 19)]

    def test_acepta_bloques_como_diccionario(self):
        # Lo que vuelve de S3 son dicts, no objetos: la politica tiene que
        # poder evaluarse sobre el JSON guardado sin rehidratar nada.
        crudo = [[{"start_word": 0, "end_word": 9, "tipo": "publicidad", "confidence": 0.9}]] * 3

        assert CONSERVADORA_V1.rangos(crudo) == [(0, 9)]

    def test_catalogo_expone_las_politicas_por_nombre(self):
        assert CATALOGO["conservadora-v1"] is CONSERVADORA_V1
        assert set(CATALOGO) == {"conservadora-v1", "minima", "un-solo-modelo"}
