"""Pruebas de la ingesta S3 -> Postgres (DiscoveryService, QueueService,
TranscriptionResultConsumer, TranscriptionFailureConsumer, idempotencia de
PipelineRunService) -- ver docs/INGESTION_DESIGN.md.

Postgres real (igual que test_pipeline_run_service_e2e.py), S3/SQS
simulados con MagicMock (igual que test_dlq_handler.py) -- no hace falta AWS
real para validar la logica de estas piezas. Se salta sola si Postgres no
esta accesible. Limpia las filas que crea al terminar.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.infrastructure.db import registry  # noqa: F401 -- registra todos los modelos
from src.infrastructure.db.engine import get_engine
from src.modules.media.models import Medio, Programa, TipoMedio
from src.modules.media.repositories import MedioRepository, ProgramaRepository
from src.modules.recordings.discovery import DiscoveryService
from src.modules.recordings.models import EstadoGrabacion, Grabacion, Transcripcion
from src.modules.recordings.queue_service import QueueService
from src.modules.recordings.repositories import GrabacionRepository, TranscripcionRepository
from src.modules.pipeline.exceptions import RecursosNoDisponibles
from src.modules.pipeline.resolvers import S3RecordingResolver
from src.modules.recordings.result_consumer import TranscriptionFailureConsumer, TranscriptionResultConsumer


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


@pytest.fixture
def medio_programa(session: Session):
    medio = Medio(codigo=f"test_station_{uuid.uuid4().hex[:8]}", nombre="Test Station", tipo=TipoMedio.RADIO)
    session.add(medio)
    session.flush()
    programa = Programa(medio_id=medio.id, nombre="Transmision continua")
    session.add(programa)
    session.commit()
    yield medio, programa
    session.execute(delete(Programa).where(Programa.id == programa.id))
    session.execute(delete(Medio).where(Medio.id == medio.id))
    session.commit()


@pytest.fixture
def cleanup_grabaciones(session: Session):
    created_ids: list[uuid.UUID] = []
    yield created_ids
    for gid in created_ids:
        session.execute(delete(Transcripcion).where(Transcripcion.grabacion_id == gid))
        session.execute(delete(Grabacion).where(Grabacion.id == gid))
    session.commit()


def _s3_client_listing(keys: list[str]) -> MagicMock:
    s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": [{"Key": k} for k in keys]}]
    s3.get_paginator.return_value = paginator
    return s3


class TestDiscoveryService:
    def test_creates_grabacion_for_new_key_and_is_idempotent(self, session, medio_programa, cleanup_grabaciones):
        medio, _ = medio_programa
        key = f"{medio.codigo}/2026/07/2026-07-20T10Z.mp3"
        s3 = _s3_client_listing([key])
        service = DiscoveryService(
            grabaciones=GrabacionRepository(session),
            medios=MedioRepository(session),
            programas=ProgramaRepository(session),
            s3_client=s3,
            bucket="fake-bucket",
        )

        result = service.discover()
        grabacion = GrabacionRepository(session).get_by_s3_key(key)
        cleanup_grabaciones.append(grabacion.id)

        assert result.creadas == 1
        assert grabacion is not None
        assert grabacion.estado == EstadoGrabacion.PENDIENTE
        assert grabacion.fecha_inicio == datetime(2026, 7, 20, 10, tzinfo=timezone.utc)

        # segunda pasada: no duplica
        result2 = service.discover()
        assert result2.creadas == 0
        assert result2.ya_existian == 1

    def test_ignores_files_from_unregistered_station(self, session, cleanup_grabaciones):
        key = "estacion_inexistente_xyz/2026/07/2026-07-20T10Z.mp3"
        s3 = _s3_client_listing([key])
        service = DiscoveryService(
            grabaciones=GrabacionRepository(session),
            medios=MedioRepository(session),
            programas=ProgramaRepository(session),
            s3_client=s3,
            bucket="fake-bucket",
        )

        result = service.discover()

        assert result.creadas == 0
        assert "estacion_inexistente_xyz" in result.estaciones_sin_medio
        assert GrabacionRepository(session).get_by_s3_key(key) is None

    def test_ignores_keys_that_dont_match_expected_pattern(self, session, cleanup_grabaciones):
        s3 = _s3_client_listing(["backups/whatever.txt", "scripts/enqueue.py"])
        service = DiscoveryService(
            grabaciones=GrabacionRepository(session),
            medios=MedioRepository(session),
            programas=ProgramaRepository(session),
            s3_client=s3,
            bucket="fake-bucket",
        )

        result = service.discover()

        assert result.creadas == 0
        assert result.ignoradas_no_reconocidas == 2


class TestQueueService:
    def test_enqueues_pending_and_marks_procesando(self, session, medio_programa, cleanup_grabaciones):
        # OJO: enqueue_pending() opera sobre *toda* la tabla grabaciones (sin
        # filtro por estacion/fixture) -- contra un Postgres compartido que
        # puede tener grabaciones PENDIENTE reales de otras fuentes, hay que
        # revertir cualquier fila ajena que la corrida de la prueba haya
        # tocado, no solo la propia. (Nos mordio una vez: una corrida de esta
        # prueba en el Postgres real de produccion marco 500 grabaciones
        # reales como PROCESANDO por accidente -- ver commit que agrego esta nota.)
        medio, programa = medio_programa
        grabacion = Grabacion(
            programa_id=programa.id,
            s3_key=f"{medio.codigo}/2026/07/2026-07-20T11Z.mp3",
            fecha_inicio=datetime(2026, 7, 20, 11, tzinfo=timezone.utc),
            fecha_fin=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
            estado=EstadoGrabacion.PENDIENTE,
        )
        session.add(grabacion)
        session.commit()
        cleanup_grabaciones.append(grabacion.id)

        sqs = MagicMock()
        service = QueueService(
            grabaciones=GrabacionRepository(session),
            sqs_client=sqs,
            queue_url="https://sqs.example/jobs",
            capture_bucket="capture-bucket",
            output_bucket="output-bucket",
        )

        enqueued_ids: set[str] = set()
        try:
            # limit alto a proposito: si el Postgres tiene backlog real (miles
            # de PENDIENTE con fecha_inicio mas vieja que la de esta prueba),
            # un limit chico podria no llegar a tomar la fila de la prueba --
            # el orden es por fecha_inicio ascendente, no de insercion.
            result = service.enqueue_pending(limit=10000)

            assert result.encoladas >= 1
            enqueued_ids = {
                json.loads(call.kwargs["MessageBody"])["grabacion_id"]
                for call in sqs.send_message.call_args_list
            }
            assert str(grabacion.id) in enqueued_ids

            own_call = next(
                call for call in sqs.send_message.call_args_list
                if json.loads(call.kwargs["MessageBody"])["grabacion_id"] == str(grabacion.id)
            )
            body = json.loads(own_call.kwargs["MessageBody"])
            assert body["station"] == medio.codigo
            assert body["s3_input"] == f"s3://capture-bucket/{grabacion.s3_key}"

            session.refresh(grabacion)
            assert grabacion.estado == EstadoGrabacion.PROCESANDO

            # ya no aparece pendiente en una segunda pasada
            sqs.reset_mock()
            service.enqueue_pending(limit=10000)
            second_pass_ids = {
                json.loads(call.kwargs["MessageBody"])["grabacion_id"]
                for call in sqs.send_message.call_args_list
            }
            enqueued_ids |= second_pass_ids  # tambien hay que revertir estas en el finally
            assert str(grabacion.id) not in second_pass_ids
        finally:
            # revertir cualquier grabacion ajena que esta pasada haya
            # marcado PROCESANDO -- solo la nuestra debe quedar asi,
            # el resto (si el Postgres tenia datos reales) vuelve a PENDIENTE.
            other_ids = [uuid.UUID(gid) for gid in enqueued_ids if gid != str(grabacion.id)]
            if other_ids:
                session.execute(
                    Grabacion.__table__.update()
                    .where(Grabacion.id.in_(other_ids))
                    .values(estado=EstadoGrabacion.PENDIENTE)
                )
                session.commit()


class TestTranscriptionResultConsumer:
    def _grabacion(self, session, programa) -> Grabacion:
        grabacion = Grabacion(
            programa_id=programa.id,
            s3_key="station/2026/07/2026-07-20T12Z.mp3",
            fecha_inicio=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
            fecha_fin=datetime(2026, 7, 20, 13, tzinfo=timezone.utc),
            estado=EstadoGrabacion.PROCESANDO,
        )
        session.add(grabacion)
        session.commit()
        return grabacion

    def test_creates_transcripcion_and_marks_procesada(self, session, medio_programa, cleanup_grabaciones):
        _, programa = medio_programa
        grabacion = self._grabacion(session, programa)
        cleanup_grabaciones.append(grabacion.id)

        words = [{"index": 0, "word": "hola", "start": 0.0, "end": 0.3}]
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": MagicMock(read=lambda: json.dumps(words).encode())}
        sqs = MagicMock()
        receipt = "rh-1"
        message_body = json.dumps({
            "grabacion_id": str(grabacion.id),
            "words_json_s3_uri": "s3://out-bucket/transcripts/station/2026-07-20T12Z_words.json",
        })
        sqs.receive_message.return_value = {"Messages": [{"Body": message_body, "ReceiptHandle": receipt}]}

        consumer = TranscriptionResultConsumer(
            grabaciones=GrabacionRepository(session),
            transcripciones=TranscripcionRepository(session),
            sqs_client=sqs,
            s3_client=s3,
            queue_url="https://sqs.example/done",
        )

        result = consumer.consume_once()

        assert result.procesados == 1
        sqs.delete_message.assert_called_once_with(QueueUrl="https://sqs.example/done", ReceiptHandle=receipt)

        session.refresh(grabacion)
        assert grabacion.estado == EstadoGrabacion.PROCESADA
        transcripcion = TranscripcionRepository(session).get_by_grabacion_id(grabacion.id)
        assert transcripcion is not None
        assert transcripcion.texto_completo == "hola"
        assert transcripcion.segmentos["words"] == words

    def test_duplicate_message_does_not_duplicate_transcripcion(self, session, medio_programa, cleanup_grabaciones):
        _, programa = medio_programa
        grabacion = self._grabacion(session, programa)
        cleanup_grabaciones.append(grabacion.id)

        session.add(Transcripcion(
            grabacion_id=grabacion.id, texto_completo="ya existe", segmentos={"words": []}, proveedor="test",
        ))
        session.commit()

        s3 = MagicMock()
        sqs = MagicMock()
        message_body = json.dumps({
            "grabacion_id": str(grabacion.id),
            "words_json_s3_uri": "s3://out-bucket/x_words.json",
        })
        sqs.receive_message.return_value = {"Messages": [{"Body": message_body, "ReceiptHandle": "rh-2"}]}

        consumer = TranscriptionResultConsumer(
            grabaciones=GrabacionRepository(session),
            transcripciones=TranscripcionRepository(session),
            sqs_client=sqs,
            s3_client=s3,
            queue_url="https://sqs.example/done",
        )

        result = consumer.consume_once()

        assert result.omitidos_ya_existian == 1
        s3.get_object.assert_not_called()
        sqs.delete_message.assert_called_once()


class TestTranscriptionFailureConsumer:
    def test_marks_grabacion_error_from_envelope_message(self, session, medio_programa, cleanup_grabaciones):
        _, programa = medio_programa
        grabacion = Grabacion(
            programa_id=programa.id,
            s3_key="station/2026/07/2026-07-20T13Z.mp3",
            fecha_inicio=datetime(2026, 7, 20, 13, tzinfo=timezone.utc),
            fecha_fin=datetime(2026, 7, 20, 14, tzinfo=timezone.utc),
            estado=EstadoGrabacion.PROCESANDO,
        )
        session.add(grabacion)
        session.commit()
        cleanup_grabaciones.append(grabacion.id)

        sqs = MagicMock()
        envelope = json.dumps({
            "original_job": {"grabacion_id": str(grabacion.id), "station": "station"},
            "error": {"error_type": "PermanentPipelineError", "error_message": "audio corrupto"},
        })
        sqs.receive_message.return_value = {"Messages": [{"Body": envelope, "ReceiptHandle": "rh-3"}]}

        consumer = TranscriptionFailureConsumer(
            grabaciones=GrabacionRepository(session), sqs_client=sqs, dlq_url="https://sqs.example/dlq",
        )

        marcadas = consumer.consume_once()

        assert marcadas == 1
        session.refresh(grabacion)
        assert grabacion.estado == EstadoGrabacion.ERROR
        assert "audio corrupto" in grabacion.error_mensaje

    def test_handles_raw_body_without_envelope(self, session, medio_programa, cleanup_grabaciones):
        _, programa = medio_programa
        grabacion = Grabacion(
            programa_id=programa.id,
            s3_key="station/2026/07/2026-07-20T14Z.mp3",
            fecha_inicio=datetime(2026, 7, 20, 14, tzinfo=timezone.utc),
            fecha_fin=datetime(2026, 7, 20, 15, tzinfo=timezone.utc),
            estado=EstadoGrabacion.PROCESANDO,
        )
        session.add(grabacion)
        session.commit()
        cleanup_grabaciones.append(grabacion.id)

        sqs = MagicMock()
        raw_body = json.dumps({"grabacion_id": str(grabacion.id), "station": "station"})
        sqs.receive_message.return_value = {"Messages": [{"Body": raw_body, "ReceiptHandle": "rh-4"}]}

        consumer = TranscriptionFailureConsumer(
            grabaciones=GrabacionRepository(session), sqs_client=sqs, dlq_url="https://sqs.example/dlq",
        )

        marcadas = consumer.consume_once()

        assert marcadas == 1
        session.refresh(grabacion)
        assert grabacion.estado == EstadoGrabacion.ERROR
        assert "RedrivePolicy" in grabacion.error_mensaje


class TestS3RecordingResolver:
    def test_raises_when_no_transcripcion_yet(self, session, medio_programa, cleanup_grabaciones):
        medio, programa = medio_programa
        grabacion = Grabacion(
            programa_id=programa.id,
            s3_key=f"{medio.codigo}/2026/07/2026-07-20T16Z.mp3",
            fecha_inicio=datetime(2026, 7, 20, 16, tzinfo=timezone.utc),
            fecha_fin=datetime(2026, 7, 20, 17, tzinfo=timezone.utc),
            estado=EstadoGrabacion.PROCESANDO,
        )
        session.add(grabacion)
        session.commit()
        cleanup_grabaciones.append(grabacion.id)

        resolver = S3RecordingResolver(
            grabaciones=GrabacionRepository(session),
            transcripciones=TranscripcionRepository(session),
            s3_client=MagicMock(),
            capture_bucket="capture-bucket",
            work_dir=Path(gettempdir()) / "media-intel-test",
        )

        with pytest.raises(RecursosNoDisponibles):
            resolver.resolve(grabacion.id)

    def test_downloads_audio_and_writes_words_json(self, session, medio_programa, cleanup_grabaciones, tmp_path):
        medio, programa = medio_programa
        grabacion = Grabacion(
            programa_id=programa.id,
            s3_key=f"{medio.codigo}/2026/07/2026-07-20T17Z.mp3",
            fecha_inicio=datetime(2026, 7, 20, 17, tzinfo=timezone.utc),
            fecha_fin=datetime(2026, 7, 20, 18, tzinfo=timezone.utc),
            estado=EstadoGrabacion.PROCESADA,
        )
        session.add(grabacion)
        session.flush()
        words = [{"index": 0, "word": "hola", "start": 0.0, "end": 0.3}]
        session.add(Transcripcion(
            grabacion_id=grabacion.id, texto_completo="hola", segmentos={"words": words}, proveedor="test",
        ))
        session.commit()
        cleanup_grabaciones.append(grabacion.id)

        s3 = MagicMock()
        resolver = S3RecordingResolver(
            grabaciones=GrabacionRepository(session),
            transcripciones=TranscripcionRepository(session),
            s3_client=s3,
            capture_bucket="capture-bucket",
            work_dir=tmp_path,
        )

        resources = resolver.resolve(grabacion.id)

        s3.download_file.assert_called_once()
        assert s3.download_file.call_args.args[0] == "capture-bucket"
        assert s3.download_file.call_args.args[1] == grabacion.s3_key
        assert json.loads(resources.words_json_path.read_text(encoding="utf-8")) == words
        assert resources.output_dir.name == "clips"
