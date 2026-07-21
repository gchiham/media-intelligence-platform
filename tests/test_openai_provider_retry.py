"""Reintento/backoff en OpenAIAnalysisProvider (R3 de docs/ARCHITECTURE_REVIEW.md).
Usa un cliente OpenAI simulado (unittest.mock) -- no llama a la API real, para
que estos tests sean rapidos, deterministicos y no gasten cuota."""
import json
from unittest.mock import MagicMock

import httpx
import pytest
from openai import APIConnectionError, AuthenticationError, RateLimitError

from src.modules.ai.exceptions import SegmentationError
from src.modules.ai.providers.openai_provider import OpenAIAnalysisProvider
from src.modules.ai.schemas import Word


def _word(i: int) -> Word:
    return Word(index=i, word=f"palabra{i}", start=i * 0.3, end=i * 0.3 + 0.28)


def _news_item() -> dict:
    return {
        "title": "t",
        "start_word": 0,
        "end_word": 1,
        "summary": "s",
        "keywords": ["k"],
        "news_type": "otro",
        "people": [],
        "organizations": [],
        "locations": [],
        "confidence": 0.9,
    }


def _fake_response(news: list[dict]):
    message = MagicMock()
    message.content = json.dumps({"news": news})
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


def _connection_error() -> APIConnectionError:
    return APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))


def _rate_limit_error() -> RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code=429, request=request)
    return RateLimitError(message="rate limited", response=response, body=None)


def _auth_error() -> AuthenticationError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code=401, request=request)
    return AuthenticationError(message="invalid api key", response=response, body=None)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    # los tests de reintento no deben tardar de verdad los 1s/2s de backoff.
    monkeypatch.setattr("src.modules.ai.providers.openai_provider.time.sleep", lambda *_: None)


@pytest.fixture
def provider() -> OpenAIAnalysisProvider:
    return OpenAIAnalysisProvider(api_key="sk-test", chunk_size=600)


def test_succeeds_on_first_try_no_retry(provider):
    provider._client.chat.completions.create = MagicMock(
        return_value=_fake_response([_news_item()])
    )
    segments = provider.segment_news([_word(0), _word(1)])
    assert len(segments) == 1
    assert provider._client.chat.completions.create.call_count == 1


def test_retries_transient_error_then_succeeds(provider):
    call_count = {"n": 0}

    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise _connection_error()
        return _fake_response([])

    provider._client.chat.completions.create = MagicMock(side_effect=flaky)
    segments = provider.segment_news([_word(0)])
    assert segments == []
    assert call_count["n"] == 2


def test_retries_rate_limit_then_succeeds(provider):
    call_count = {"n": 0}

    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise _rate_limit_error()
        return _fake_response([])

    provider._client.chat.completions.create = MagicMock(side_effect=flaky)
    segments = provider.segment_news([_word(0)])
    assert segments == []
    assert call_count["n"] == 3


def test_gives_up_after_max_attempts(provider):
    # side_effect debe ser la EXCEPCION YA INSTANCIADA -- si fuera la funcion
    # constructora, MagicMock la llamaria y usaria el valor devuelto como
    # respuesta en vez de relanzarla.
    provider._client.chat.completions.create = MagicMock(side_effect=_connection_error())
    with pytest.raises(SegmentationError):
        provider.segment_news([_word(0)])
    assert provider._client.chat.completions.create.call_count == 3


def test_permanent_error_does_not_retry(provider):
    provider._client.chat.completions.create = MagicMock(side_effect=_auth_error())
    with pytest.raises(SegmentationError):
        provider.segment_news([_word(0)])
    assert provider._client.chat.completions.create.call_count == 1
