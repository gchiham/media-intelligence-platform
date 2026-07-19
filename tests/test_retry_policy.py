import pytest

from src.modules.transcription.queue.retry_policy import compute_backoff_seconds


def test_backoff_increases_by_attempt():
    assert compute_backoff_seconds(1) == 30
    assert compute_backoff_seconds(2) == 90
    assert compute_backoff_seconds(3) == 240


def test_backoff_caps_beyond_schedule():
    assert compute_backoff_seconds(10) == 300


def test_backoff_rejects_non_positive_attempt():
    with pytest.raises(ValueError):
        compute_backoff_seconds(0)
