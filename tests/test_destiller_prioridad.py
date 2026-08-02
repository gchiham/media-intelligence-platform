"""Priorizacion de la cola de validacion. Ver src/modules/destiller/prioridad.py.

Lo que importa probar aca no es la aritmetica sino que el desacuerdo se mida
por palabra: los dos modelos cortan en lugares distintos, y una comparacion
bloque-contra-bloque daria resultados que dependen de con cual de los bloques
solapados se comparo.
"""
from src.modules.destiller.prioridad import (
    P_ACUERDO,
    P_BINARIO_MAYORIA,
    P_BINARIO_MINORIA,
    P_CONTROL,
    P_SUBTIPO,
    clasificar,
    es_muestra_control,
    mapa_por_palabra,
)


def _otro(tipos_por_palabra: dict[int, str]) -> dict[int, str]:
    return tipos_por_palabra


class TestClasificar:
    def test_desacuerdo_binario_total_es_prioridad_1(self):
        otro = _otro({i: "publicidad" for i in range(10)})
        p, flip, acuerdo = clasificar("noticia", 0, 9, otro, "g", 0)

        assert p == P_BINARIO_MAYORIA
        assert flip == 1.0
        assert acuerdo == 0.0

    def test_desacuerdo_de_frontera_es_prioridad_2(self):
        # El otro modelo corta la noticia dos palabras antes: coinciden en el
        # fondo, discrepan en donde termina. Importa porque el Clipper corta
        # el audio justo con esos limites.
        otro = _otro({i: ("noticia" if i < 8 else "publicidad") for i in range(10)})
        p, flip, _ = clasificar("noticia", 0, 9, otro, "g", 0)

        assert p == P_BINARIO_MINORIA
        assert flip == 0.2

    def test_mitad_exacta_cuenta_como_mayoria(self):
        otro = _otro({i: ("noticia" if i < 5 else "promo") for i in range(10)})
        p, flip, _ = clasificar("noticia", 0, 9, otro, "g", 0)

        assert p == P_BINARIO_MAYORIA
        assert flip == 0.5

    def test_acuerdo_binario_con_subtipo_distinto_es_prioridad_4(self):
        # Los dos dicen "es basura", discrepan en cual. Al pipeline le da
        # igual: se excluye igual. Por eso va despues de la muestra de control.
        otro = _otro({i: "publicidad" for i in range(10)})
        p, flip, acuerdo = clasificar("promo", 0, 9, otro, "g", 0)

        assert p == P_SUBTIPO
        assert flip == 0.0
        assert acuerdo == 0.0

    def test_acuerdo_exacto_cae_en_control_o_en_resto(self):
        otro = _otro({i: "noticia" for i in range(10)})
        p, flip, acuerdo = clasificar("noticia", 0, 9, otro, "g", 0)

        assert p in (P_CONTROL, P_ACUERDO)
        assert (flip, acuerdo) == (0.0, 1.0)

    def test_sin_solape_no_inventa_acuerdo(self):
        # Si el otro modelo no cubrio esas palabras, no hay comparacion. No
        # debe contarse como acuerdo perfecto: seria un acuerdo que nadie midio.
        p, flip, acuerdo = clasificar("noticia", 0, 9, {}, "g", 0)

        assert p == P_ACUERDO
        assert acuerdo == 0.0

    def test_solape_parcial_solo_mira_las_palabras_comparables(self):
        otro = _otro({0: "publicidad", 1: "publicidad"})
        p, flip, _ = clasificar("noticia", 0, 9, otro, "g", 0)

        assert p == P_BINARIO_MAYORIA
        assert flip == 1.0  # 2 de 2 comparables, no 2 de 10

    def test_desconocido_no_es_basura(self):
        # Un hueco del modelo no es evidencia de basura, misma regla que en
        # src/modules/ai/destiller.py.
        otro = _otro({i: "desconocido" for i in range(10)})
        p, flip, _ = clasificar("noticia", 0, 9, otro, "g", 0)

        assert flip == 0.0
        assert p == P_SUBTIPO


class TestMuestraControl:
    def test_es_deterministica(self):
        # Reejecutar la carga tiene que dar la misma muestra o el experimento
        # deja de ser reproducible.
        assert es_muestra_control("radio_globo__x", 7) == es_muestra_control("radio_globo__x", 7)

    def test_toma_aproximadamente_el_porcentaje_pedido(self):
        n = sum(1 for i in range(4000) if es_muestra_control("g", i, pct=20))
        assert 0.17 < n / 4000 < 0.23


class TestMapaPorPalabra:
    def test_expande_rangos_a_palabras(self):
        bloques = [
            {"start_word": 0, "end_word": 2, "tipo": "noticia"},
            {"start_word": 3, "end_word": 4, "tipo": "promo"},
        ]
        assert mapa_por_palabra(bloques) == {
            0: "noticia", 1: "noticia", 2: "noticia", 3: "promo", 4: "promo",
        }
