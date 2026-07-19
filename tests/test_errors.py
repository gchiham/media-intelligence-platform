import json
import subprocess

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import BaseModel, ValidationError

from src.shared.errors import PermanentPipelineError, TransientPipelineError, classify_and_wrap


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "boom"}}, "GetObject")


@pytest.mark.parametrize("code", ["SlowDown", "InternalError", "ServiceUnavailable", "Throttling"])
def test_botocore_transient_codes(code):
    err = classify_and_wrap(_client_error(code), module="download")
    assert isinstance(err, TransientPipelineError)


@pytest.mark.parametrize("code", ["NoSuchKey", "NoSuchBucket", "AccessDenied"])
def test_botocore_permanent_codes(code):
    err = classify_and_wrap(_client_error(code), module="download")
    assert isinstance(err, PermanentPipelineError)


def test_botocore_404_from_download_file_head_object_is_permanent():
    # boto3 download_file() hace HeadObject antes de GetObject -- una key
    # inexistente responde code="404", no "NoSuchKey". Regresion real
    # encontrada en pruebas contra S3 real (ver docs/ERROR_HANDLING.md).
    err = classify_and_wrap(_client_error("404"), module="download")
    assert isinstance(err, PermanentPipelineError)


def test_botocore_unknown_code_defaults_transient():
    err = classify_and_wrap(_client_error("SomethingWeNeverSaw"), module="download")
    assert isinstance(err, TransientPipelineError)


def test_botocore_connection_error_is_transient():
    err = classify_and_wrap(
        EndpointConnectionError(endpoint_url="https://s3.amazonaws.com"), module="download"
    )
    assert isinstance(err, TransientPipelineError)


def test_json_decode_error_is_permanent():
    try:
        json.loads("{not valid json")
    except json.JSONDecodeError as exc:
        err = classify_and_wrap(exc, module="segmentation")
    assert isinstance(err, PermanentPipelineError)


def test_pydantic_validation_error_is_permanent():
    class Model(BaseModel):
        index: int

    try:
        Model.model_validate({"index": "not-an-int-and-not-parseable"})
    except ValidationError as exc:
        err = classify_and_wrap(exc, module="segmentation")
    assert isinstance(err, PermanentPipelineError)


def test_ffmpeg_generic_failure_is_permanent():
    exc = subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"Invalid data found when processing input")
    err = classify_and_wrap(exc, module="clipping")
    assert isinstance(err, PermanentPipelineError)


def test_ffmpeg_disk_full_is_transient():
    exc = subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"No space left on device")
    err = classify_and_wrap(exc, module="clipping")
    assert isinstance(err, TransientPipelineError)


def test_timeout_error_is_transient():
    err = classify_and_wrap(TimeoutError("took too long"), module="transcription")
    assert isinstance(err, TransientPipelineError)


def test_already_wrapped_error_passes_through():
    original = PermanentPipelineError("ya clasificado")
    assert classify_and_wrap(original, module="x") is original


def test_unknown_exception_uses_default():
    class WeirdError(Exception):
        pass

    err = classify_and_wrap(WeirdError("???"), module="x", default=PermanentPipelineError)
    assert isinstance(err, PermanentPipelineError)
