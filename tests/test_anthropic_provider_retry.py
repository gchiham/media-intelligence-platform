"""Reintento/backoff en AnthropicAnalysisProvider -- mismo enfoque que
test_openai_provider_retry.py (cliente Anthropic simulado via
unittest.mock, sin llamar a la API real)."""
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest

from src.modules.ai.exceptions import SegmentationError
from src.modules.ai.providers.anthropic_provider import AnthropicAnalysisProvider
from src.modules.ai.schemas import Word


def _word(i: int) -> Word:
    return Word(index=i, word=f"palabra{i}", start=i * 0.3, end=i * 0.3 + 0.28)


def _fake_response(news: list[dict]):
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = "return_news_segments"
    tool_block.input = {"news": news}
    response = MagicMock()
    response.content = [tool_block]
    return response


def _connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def _rate_limit_error() -> anthropic.RateLimitError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=429, request=request)
    return anthropic.RateLimitError(message="rate limited", response=response, body=None)


def _auth_error() -> anthropic.AuthenticationError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=401, request=request)
    return anthropic.AuthenticationError(message="invalid api key", response=response, body=None)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr("src.modules.ai.providers.anthropic_provider.time.sleep", lambda *_: None)


@pytest.fixture
def provider() -> AnthropicAnalysisProvider:
    return AnthropicAnalysisProvider(api_key="sk-ant-test", chunk_size=600)


def test_succeeds_on_first_try_no_retry(provider):
    provider._client.messages.create = MagicMock(
        return_value=_fake_response([{"title": "t", "start_word": 0, "end_word": 1, "confidence": 0.9}])
    )
    segments = provider.segment_news([_word(0), _word(1)])
    assert len(segments) == 1
    assert provider._client.messages.create.call_count == 1


def test_retries_transient_error_then_succeeds(provider):
    call_count = {"n": 0}

    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise _connection_error()
        return _fake_response([])

    provider._client.messages.create = MagicMock(side_effect=flaky)
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

    provider._client.messages.create = MagicMock(side_effect=flaky)
    segments = provider.segment_news([_word(0)])
    assert segments == []
    assert call_count["n"] == 3


def test_gives_up_after_max_attempts(provider):
    # side_effect debe ser la excepcion ya instanciada -- ver la nota en
    # test_openai_provider_retry.py sobre el pitfall de pasar la funcion
    # constructora en su lugar.
    provider._client.messages.create = MagicMock(side_effect=_connection_error())
    with pytest.raises(SegmentationError):
        provider.segment_news([_word(0)])
    assert provider._client.messages.create.call_count == 3


def test_permanent_error_does_not_retry(provider):
    provider._client.messages.create = MagicMock(side_effect=_auth_error())
    with pytest.raises(SegmentationError):
        provider.segment_news([_word(0)])
    assert provider._client.messages.create.call_count == 1
