"""AIProviderWithFallback: si el primario falla (agotados sus reintentos),
se reintenta con el secundario. Ver src/modules/ai/providers/fallback_provider.py.
Usa dobles de AIAnalysisProvider (no llama a ninguna API real)."""
from unittest.mock import MagicMock

import pytest

from src.modules.ai.exceptions import SegmentationError
from src.modules.ai.providers.fallback_provider import AIProviderWithFallback
from src.modules.ai.schemas import NewsSegment, Word


def _word(i: int) -> Word:
    return Word(index=i, word=f"palabra{i}", start=i * 0.3, end=i * 0.3 + 0.28)


def _segment() -> NewsSegment:
    return NewsSegment(title="t", start_word=0, end_word=1, confidence=0.9)


def test_uses_primary_when_it_succeeds():
    primary = MagicMock()
    primary.segment_news.return_value = [_segment()]
    secondary = MagicMock()

    provider = AIProviderWithFallback(primary=primary, secondary=secondary)
    segments = provider.segment_news([_word(0)])

    assert segments == [_segment()]
    secondary.segment_news.assert_not_called()


def test_falls_back_to_secondary_when_primary_fails():
    primary = MagicMock()
    primary.segment_news.side_effect = SegmentationError("primario agoto reintentos")
    secondary = MagicMock()
    secondary.segment_news.return_value = [_segment()]

    provider = AIProviderWithFallback(primary=primary, secondary=secondary)
    segments = provider.segment_news([_word(0)])

    assert segments == [_segment()]
    primary.segment_news.assert_called_once()
    secondary.segment_news.assert_called_once()


def test_raises_when_both_providers_fail():
    primary = MagicMock()
    primary.segment_news.side_effect = SegmentationError("primario fallo")
    secondary = MagicMock()
    secondary.segment_news.side_effect = SegmentationError("secundario fallo")

    provider = AIProviderWithFallback(primary=primary, secondary=secondary)

    with pytest.raises(SegmentationError) as exc_info:
        provider.segment_news([_word(0)])

    assert "primario fallo" in str(exc_info.value)
    assert "secundario fallo" in str(exc_info.value)
