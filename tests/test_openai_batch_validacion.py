"""El submit no puede reportar exito para un batch que OpenAI rechazo.

Regresion del 2026-08-28: `batches.create()` devuelve un batch en `validating`
y el codigo logueaba "batch enviado" ahi mismo. La validacion fallaba segundos
despues y la segmentacion quedaba detenida sin una sola linea de error -- 4
batches seguidos "enviados", los 4 en failed.
"""
from types import SimpleNamespace

import pytest

from src.modules.ai import openai_batch
from src.modules.ai.openai_batch import BatchRechazado, OpenAIBatchSegmentationClient


class _Batches:
    """Devuelve los estados dados, en orden, en sucesivos retrieve()."""

    def __init__(self, estados, errores=None):
        self._estados = list(estados)
        self._errores = errores
        self.retrieves = 0

    def create(self, **_):
        return SimpleNamespace(id="batch_x", status="validating")

    def retrieve(self, _batch_id):
        self.retrieves += 1
        estado = self._estados.pop(0) if self._estados else "in_progress"
        return SimpleNamespace(id="batch_x", status=estado, errors=self._errores)


class _Files:
    def create(self, **_):
        return SimpleNamespace(id="file_x")


def _cliente(batches):
    return OpenAIBatchSegmentationClient(SimpleNamespace(files=_Files(), batches=batches))


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    monkeypatch.setattr(openai_batch.time, "sleep", lambda _s: None)


def _peticion():
    return SimpleNamespace(grabacion_id="g1", chunk_index=0, params={"model": "m"})


def test_batch_rechazado_lanza_en_vez_de_reportar_exito():
    errores = SimpleNamespace(data=[SimpleNamespace(code="invalid_request", message="Cannot find file")])
    cliente = _cliente(_Batches(["failed"], errores))

    with pytest.raises(BatchRechazado) as e:
        cliente.submit([_peticion()])

    assert "Cannot find file" in str(e.value)


def test_espera_mientras_valida_y_acepta_cuando_avanza():
    """Dos retrieve en `validating` y al tercero pasa: es un envio bueno."""
    batches = _Batches(["validating", "validating", "in_progress"])

    assert _cliente(batches).submit([_peticion()]) == "batch_x"
    assert batches.retrieves == 3


def test_no_bloquea_para_siempre_si_se_queda_validando(monkeypatch):
    """Si nunca sale de `validating` se da por enviado -- collect lo revisara.
    Lo que no puede hacer es colgar el cron."""
    monkeypatch.setattr(openai_batch, "ESPERA_VALIDACION_SEG", 0.01)
    batches = _Batches(["validating"] * 50)

    assert _cliente(batches).submit([_peticion()]) == "batch_x"


def test_expired_y_cancelled_tambien_se_tratan_como_rechazo():
    for estado in ("expired", "cancelled"):
        with pytest.raises(BatchRechazado):
            _cliente(_Batches([estado])).submit([_peticion()])
