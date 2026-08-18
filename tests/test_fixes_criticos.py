"""Tests de los fixes de la revision 2026-08-18: perdida silenciosa de
grabaciones con chunks fallidos en batch, mensajes envenenados en el consumer,
y la DLQ que revertia grabaciones ya transcritas.

Todos con fakes, sin Postgres -- validan la logica de decision, no la
persistencia (esa la cubren los tests e2e existentes).
"""
import json
from unittest.mock import MagicMock

from src.modules.ai.batch import BatchSegmentationClient
from src.modules.recordings.models import EstadoGrabacion
from src.modules.recordings.result_consumer import (
    TranscriptionFailureConsumer,
    TranscriptionResultConsumer,
)


class TestGrabacionesConErrorEnBatch:
    def test_chunk_fallido_marca_la_grabacion(self):
        """Un chunk errored deja rastro en grabaciones_con_error: es la senal
        que usa el script de cacheo para NO cachear esa grabacion incompleta
        (cachearla la volvia indistinguible de 'sin noticias' para siempre)."""
        fallido = MagicMock()
        fallido.custom_id = "g1__0"
        fallido.result.type = "errored"

        client = MagicMock()
        client.messages.batches.results.return_value = [fallido]

        resultados = BatchSegmentationClient(client).collect("batch_1", {})

        assert "g1" in resultados.grabaciones_con_error
        # el contrato viejo se mantiene: la key existe con lo parcial
        assert "g1" in resultados.por_grabacion

    def test_grabacion_limpia_no_se_marca(self):
        bloque = MagicMock()
        bloque.type = "tool_use"
        bloque.name = "return_news_segments"
        bloque.input = {
            "news": [{
                "title": "T", "start_word": 0, "end_word": 5, "summary": "s",
                "keywords": [], "news_type": "politica", "people": [],
                "organizations": [], "locations": [], "confidence": 0.9,
            }]
        }
        ok = MagicMock()
        ok.custom_id = "g1__0"
        ok.result.type = "succeeded"
        ok.result.message.content = [bloque]

        client = MagicMock()
        client.messages.batches.results.return_value = [ok]

        resultados = BatchSegmentationClient(client).collect("batch_1", {"g1__0": (0, 599)})

        assert resultados.grabaciones_con_error == set()
        assert len(resultados.por_grabacion["g1"]) == 1

    def test_expirado_marca_la_grabacion(self):
        exp = MagicMock()
        exp.custom_id = "g2__1"
        exp.result.type = "expired"

        client = MagicMock()
        client.messages.batches.results.return_value = [exp]

        resultados = BatchSegmentationClient(client).collect("batch_1", {})

        assert "g2" in resultados.grabaciones_con_error
        assert resultados.expirados == 1


def _msg(body, handle="rh-1"):
    return {"Body": body if isinstance(body, str) else json.dumps(body), "ReceiptHandle": handle}


def _consumer(sqs, mensajes, transcripciones=None, grabaciones=None, s3=None):
    sqs.receive_message.side_effect = [{"Messages": mensajes}, {"Messages": []}]
    return TranscriptionResultConsumer(
        grabaciones=grabaciones or MagicMock(),
        transcripciones=transcripciones or MagicMock(),
        sqs_client=sqs,
        s3_client=s3 or MagicMock(),
        queue_url="q",
    )


class TestMensajeEnvenenado:
    def test_body_no_json_se_descarta_y_no_aborta(self):
        """Antes: json.loads reventaba consume_once entero y el mensaje
        envenenado bloqueaba la cola en cada tick del cron."""
        sqs = MagicMock()
        transcripciones = MagicMock()
        transcripciones.get_by_grabacion_id.return_value = "ya-existe"
        sano = _msg({"grabacion_id": "g-ok"}, "rh-sano")

        consumer = _consumer(sqs, [_msg("esto no es json", "rh-veneno"), sano], transcripciones)
        resultado = consumer.consume_once()

        # el veneno se borro (irrecuperable) y el sano se proceso igual
        handles = [c.kwargs["ReceiptHandle"] for c in sqs.delete_message.call_args_list]
        assert "rh-veneno" in handles
        assert "rh-sano" in handles
        assert resultado.omitidos_ya_existian == 1

    def test_error_transitorio_no_borra_el_mensaje(self):
        """S3 caido: el mensaje debe quedar en cola para reintento, y el
        resto del lote debe procesarse igual."""
        sqs = MagicMock()
        s3 = MagicMock()
        s3.get_object.side_effect = RuntimeError("S3 timeout")
        transcripciones = MagicMock()
        transcripciones.get_by_grabacion_id.return_value = None

        roto = _msg({"grabacion_id": "g1", "words_json_s3_uri": "s3://b/k"}, "rh-roto")
        consumer = _consumer(sqs, [roto], transcripciones, s3=s3)
        consumer.consume_once()

        handles = [c.kwargs["ReceiptHandle"] for c in sqs.delete_message.call_args_list]
        assert "rh-roto" not in handles  # se reintentara

    def test_sin_words_uri_se_descarta_sin_reventar(self):
        sqs = MagicMock()
        transcripciones = MagicMock()
        transcripciones.get_by_grabacion_id.return_value = None

        consumer = _consumer(sqs, [_msg({"grabacion_id": "g1"}, "rh-sin-uri")], transcripciones)
        resultado = consumer.consume_once()

        handles = [c.kwargs["ReceiptHandle"] for c in sqs.delete_message.call_args_list]
        assert "rh-sin-uri" in handles
        assert resultado.sin_grabacion_id == 1


class TestDLQNoRevierteProcesada:
    def test_fallo_duplicado_de_grabacion_ya_transcrita_se_ignora(self):
        """SQS es at-least-once: un duplicado del job puede fallar DESPUES de
        que el original transcribio bien. Antes eso revertia PROCESADA->ERROR."""
        grabacion = MagicMock()
        grabacion.estado = EstadoGrabacion.PROCESADA
        grabaciones = MagicMock()
        grabaciones.get_by_id.return_value = grabacion

        sqs = MagicMock()
        sqs.receive_message.return_value = {
            "Messages": [_msg({"original_job": {"grabacion_id": "g1"}, "error": {"tipo": "x"}})]
        }
        consumer = TranscriptionFailureConsumer(
            grabaciones=grabaciones, sqs_client=sqs, dlq_url="dlq",
        )
        marcadas = consumer.consume_once()

        assert marcadas == 0
        assert grabacion.estado == EstadoGrabacion.PROCESADA  # intacta
        assert sqs.delete_message.called  # el mensaje igual se consume

    def test_fallo_real_si_marca_error(self):
        grabacion = MagicMock()
        grabacion.estado = EstadoGrabacion.PROCESANDO
        grabaciones = MagicMock()
        grabaciones.get_by_id.return_value = grabacion

        sqs = MagicMock()
        sqs.receive_message.return_value = {
            "Messages": [_msg({"original_job": {"grabacion_id": "g1"}, "error": {"tipo": "x"}})]
        }
        consumer = TranscriptionFailureConsumer(
            grabaciones=grabaciones, sqs_client=sqs, dlq_url="dlq",
        )
        marcadas = consumer.consume_once()

        assert marcadas == 1
        assert grabacion.estado == EstadoGrabacion.ERROR

    def test_body_no_json_en_dlq_no_aborta(self):
        grabaciones = MagicMock()
        sqs = MagicMock()
        sqs.receive_message.return_value = {"Messages": [_msg("basura", "rh-x")]}
        consumer = TranscriptionFailureConsumer(
            grabaciones=grabaciones, sqs_client=sqs, dlq_url="dlq",
        )
        assert consumer.consume_once() == 0
        assert sqs.delete_message.called
