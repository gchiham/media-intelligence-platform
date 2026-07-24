"""Pruebas de las mejoras de docs/EFFICIENCY_REVIEW.md: resolucion de
entidades, deteccion de contenido repetido, agrupamiento en Historia y
armado/recoleccion de batches.

Postgres real donde hace falta persistencia (igual que el resto de la suite);
S3/SQS/LLM/embeddings simulados con MagicMock. Se salta sola si Postgres no
esta accesible.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.infrastructure.db import registry  # noqa: F401 -- registra los modelos
from src.infrastructure.db.engine import get_engine
from src.modules.ai.batch import (
    BatchSegmentationClient,
    build_chunk_requests,
    build_custom_id,
    parse_custom_id,
)
from src.modules.ai.entity_resolution import EntityResolver, derivar_siglas, normalizar
from src.modules.ai.models import ContenidoRepetido, Entidad, TipoEntidad
from src.modules.ai.repeated_content import RepeatedContentIndex, huellas_de_ventanas
from src.modules.ai.schemas import Word
from src.modules.editorial.dedup import HistoriaClusterer, coseno
from src.modules.editorial.models import Historia


def _postgres_reachable() -> bool:
    try:
        with get_engine().connect():
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _postgres_reachable(), reason="requiere PostgreSQL accesible")


@pytest.fixture
def session():
    with Session(get_engine()) as s:
        yield s


def _words(texto: str) -> list[Word]:
    return [
        Word(index=i, word=p, start=float(i), end=float(i) + 0.5)
        for i, p in enumerate(texto.split())
    ]


# --------------------------------------------------------------------------
# Resolucion de entidades
# --------------------------------------------------------------------------


class TestNormalizacion:
    def test_quita_acentos_y_mayusculas(self):
        assert normalizar("Juan Orlando Hernández", TipoEntidad.PERSONA) == "juan orlando hernandez"

    def test_quita_titulos_solo_en_personas(self):
        assert normalizar("el presidente Hernández", TipoEntidad.PERSONA) == "hernandez"
        # Una institucion no debe perder palabras: "Ministerio Publico" no es un titulo.
        assert normalizar("Ministerio Público", TipoEntidad.INSTITUCION) == "ministerio publico"

    def test_no_deja_cadena_vacia_si_todo_era_titulo(self):
        # Preferimos una entidad basura visible a perder la mencion en silencio.
        assert normalizar("el presidente", TipoEntidad.PERSONA) != ""

    def test_siglas_solo_desde_tres_palabras(self):
        assert derivar_siglas("juan orlando hernandez") == "joh"
        assert derivar_siglas("xiomara castro") is None


class TestEntityResolver:
    @pytest.fixture(autouse=True)
    def _limpiar(self, session):
        yield
        session.execute(delete(Entidad).where(Entidad.nombre_normalizado.like("%test%")))
        session.rollback()

    def test_variantes_de_acento_colapsan_a_una_entidad(self, session):
        resolver = EntityResolver(session)
        a = resolver.resolve("Juan Orlando Hernández Testcase", TipoEntidad.PERSONA)
        b = resolver.resolve("Juan Orlando Hernandez Testcase", TipoEntidad.PERSONA)
        assert a is b
        assert a.menciones == 2

    def test_siglas_resuelven_al_nombre_completo(self, session):
        """El caso que motiva todo el modulo: JOH == Juan Orlando Hernandez."""
        resolver = EntityResolver(session)
        completo = resolver.resolve("Juan Orlando Hernandez Testcase", TipoEntidad.PERSONA)
        siglas = resolver.resolve("JOHT", TipoEntidad.PERSONA)
        assert siglas is completo

    def test_no_fusiona_personas_distintas(self, session):
        resolver = EntityResolver(session)
        a = resolver.resolve("Maria Testcase Lopez", TipoEntidad.PERSONA)
        b = resolver.resolve("Carlos Testcase Ramirez", TipoEntidad.PERSONA)
        assert a is not b

    def test_mismo_nombre_distinto_tipo_son_entidades_distintas(self, session):
        resolver = EntityResolver(session)
        persona = resolver.resolve("Testcase Colon", TipoEntidad.PERSONA)
        lugar = resolver.resolve("Testcase Colon", TipoEntidad.LUGAR)
        assert persona is not lugar


# --------------------------------------------------------------------------
# Contenido repetido (publicidad)
# --------------------------------------------------------------------------


class TestContenidoRepetido:
    @pytest.fixture(autouse=True)
    def _limpiar(self, session):
        creadas: list[str] = []
        yield creadas
        if creadas:
            session.execute(delete(ContenidoRepetido).where(ContenidoRepetido.huella.in_(creadas)))
            session.commit()

    def test_texto_identico_produce_la_misma_huella(self):
        a = huellas_de_ventanas(_words(" ".join(["palabra"] * 60)))
        b = huellas_de_ventanas(_words(" ".join(["Palabra!"] * 60)))
        assert a and a[0][0] == b[0][0], "normalizacion debe ignorar mayusculas y puntuacion"

    def test_texto_corto_no_genera_ventanas(self):
        assert huellas_de_ventanas(_words("apenas cinco palabras aca ahora")) == []

    def test_no_se_salta_contenido_visto_pocas_veces(self, session, _limpiar):
        indice = RepeatedContentIndex(session)
        words = _words(" ".join(f"spot{i}" for i in range(60)))
        indice.registrar(words, medio_codigo="radio_test")
        session.commit()
        _limpiar.extend(h for h, _ in huellas_de_ventanas(words))

        assert indice.debe_saltarse(words) is False

    def test_se_salta_cuando_supera_el_umbral(self, session, _limpiar):
        indice = RepeatedContentIndex(session)
        words = _words(" ".join(f"anuncio{i}" for i in range(60)))
        _limpiar.extend(h for h, _ in huellas_de_ventanas(words))

        for _ in range(6):  # umbral por defecto es 5
            indice.registrar(words, medio_codigo="radio_test")
        session.commit()

        assert indice.debe_saltarse(words) is True

    def test_override_humano_gana_al_contador(self, session, _limpiar):
        """es_publicidad=False protege una noticia recurrente mal clasificada."""
        indice = RepeatedContentIndex(session)
        words = _words(" ".join(f"recurrente{i}" for i in range(60)))
        huellas = [h for h, _ in huellas_de_ventanas(words)]
        _limpiar.extend(huellas)

        for _ in range(10):
            indice.registrar(words, medio_codigo="radio_test")
        session.commit()

        session.query(ContenidoRepetido).filter(
            ContenidoRepetido.huella.in_(huellas)
        ).update({"es_publicidad": False}, synchronize_session=False)
        session.commit()

        assert indice.debe_saltarse(words) is False


# --------------------------------------------------------------------------
# Agrupamiento en Historia
# --------------------------------------------------------------------------


class _EmbeddingsFalsos:
    """Vectores deterministas: textos con las mismas palabras clave quedan
    cerca, textos sin solape quedan lejos. Evita depender de la API real."""

    _VOCAB = ["maquinaria", "alcaldes", "rock", "mundial", "dengue", "combustibles"]

    def embed(self, textos: list[str]) -> list[list[float]]:
        salida = []
        for t in textos:
            bajo = t.lower()
            v = [1.0 if termino in bajo else 0.0 for termino in self._VOCAB]
            if not any(v):
                v = [0.01] * len(self._VOCAB)
            salida.append(v)
        return salida


class TestHistoriaClusterer:
    @pytest.fixture(autouse=True)
    def _limpiar(self, session):
        creadas: list[uuid.UUID] = []
        yield creadas
        if creadas:
            session.execute(delete(Historia).where(Historia.id.in_(creadas)))
            session.commit()

    def test_coseno_basico(self):
        assert coseno([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert coseno([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
        assert coseno([], [1.0]) == 0.0

    def test_titulos_distintos_del_mismo_evento_agrupan(self, session, _limpiar):
        """El caso real: '298 alcaldes recibiran kit de maquinaria' y
        'Entrega de maquinaria a alcaldes' son el mismo evento."""
        clusterer = HistoriaClusterer(session, _EmbeddingsFalsos())
        ahora = datetime.now(timezone.utc)

        a = clusterer.asignar(
            "298 alcaldes recibiran kit de maquinaria", "", ahora, uuid.uuid4()
        )
        _limpiar.append(a.historia.id)
        b = clusterer.asignar(
            "Entrega de maquinaria a alcaldes", "", ahora + timedelta(hours=1), uuid.uuid4()
        )

        assert b.es_nueva is False
        assert b.historia.id == a.historia.id
        assert a.historia.total_apariciones == 2

    def test_eventos_distintos_no_agrupan(self, session, _limpiar):
        clusterer = HistoriaClusterer(session, _EmbeddingsFalsos())
        ahora = datetime.now(timezone.utc)

        a = clusterer.asignar("Aumento de casos de dengue", "", ahora, uuid.uuid4())
        _limpiar.append(a.historia.id)
        b = clusterer.asignar("Rebaja en combustibles", "", ahora, uuid.uuid4())
        _limpiar.append(b.historia.id)

        assert b.es_nueva is True
        assert b.historia.id != a.historia.id

    def test_fuera_de_la_ventana_temporal_crea_historia_nueva(self, session, _limpiar):
        """Un tema recurrente no debe engordar una sola historia eterna."""
        clusterer = HistoriaClusterer(session, _EmbeddingsFalsos(), ventana_horas=48)
        ahora = datetime.now(timezone.utc)

        a = clusterer.asignar("Rebaja en combustibles", "", ahora - timedelta(days=10), uuid.uuid4())
        _limpiar.append(a.historia.id)
        session.flush()
        b = clusterer.asignar("Rebaja en combustibles", "", ahora, uuid.uuid4())
        _limpiar.append(b.historia.id)

        assert b.es_nueva is True


# --------------------------------------------------------------------------
# Batch API
# --------------------------------------------------------------------------


class TestBatch:
    def test_custom_id_ida_y_vuelta(self):
        gid = str(uuid.uuid4())
        assert parse_custom_id(build_custom_id(gid, 7)) == (gid, 7)

    def test_custom_id_respeta_el_limite_de_anthropic(self):
        assert len(build_custom_id(str(uuid.uuid4()), 999)) <= 64

    def test_build_chunk_requests_cubre_todas_las_palabras(self):
        words = _words(" ".join(f"p{i}" for i in range(1300)))
        peticiones = build_chunk_requests("g1", words, model="claude-sonnet-5", chunk_size=600)
        assert len(peticiones) == 3
        assert peticiones[0].lo == 0
        assert peticiones[-1].hi == 1299

    def test_collect_descarta_indices_fuera_del_chunk(self):
        """Misma validacion que el camino sincronico: si el modelo inventa un
        rango que no vio, se descarta en vez de generar una noticia falsa."""
        bloque = MagicMock()
        bloque.type = "tool_use"
        bloque.name = "return_news_segments"
        bloque.input = {
            "news": [
                {
                    "title": "Valida",
                    "start_word": 10,
                    "end_word": 20,
                    "summary": "s",
                    "keywords": [],
                    "news_type": "politica",
                    "people": [],
                    "organizations": [],
                    "locations": [],
                    "confidence": 0.9,
                },
                {
                    "title": "Inventada fuera de rango",
                    "start_word": 5000,
                    "end_word": 5100,
                    "summary": "s",
                    "keywords": [],
                    "news_type": "politica",
                    "people": [],
                    "organizations": [],
                    "locations": [],
                    "confidence": 0.9,
                },
            ]
        }
        entrada = MagicMock()
        entrada.custom_id = "g1__0"
        entrada.result.type = "succeeded"
        entrada.result.message.content = [bloque]

        client = MagicMock()
        client.messages.batches.results.return_value = [entrada]

        resultados = BatchSegmentationClient(client).collect("batch_1", {"g1__0": (0, 599)})

        assert len(resultados.por_grabacion["g1"]) == 1
        assert resultados.por_grabacion["g1"][0].title == "Valida"

    def test_un_chunk_fallido_no_pierde_los_demas(self):
        ok = MagicMock()
        ok.custom_id = "g1__0"
        ok.result.type = "errored"

        client = MagicMock()
        client.messages.batches.results.return_value = [ok]

        resultados = BatchSegmentationClient(client).collect("batch_1", {})

        assert resultados.errores
        assert "g1" in resultados.por_grabacion  # la grabacion sigue presente, sin segmentos
