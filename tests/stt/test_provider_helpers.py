"""Tests for shared provider helper functions."""

from __future__ import annotations

import pytest

from easycat._provider_helpers import get_package_version, word_timestamps_from_words


def test_get_package_version_returns_unknown_for_missing_package():
    assert get_package_version("easycat-definitely-not-installed") == "unknown"


def test_word_timestamps_accept_word_or_text_keys():
    timestamps = word_timestamps_from_words(
        [
            {"word": "hello", "start": 0, "end": 0.3},
            {"text": "world", "start": "0.4", "end": "0.7"},
        ]
    )

    assert timestamps is not None
    assert [timestamp.word for timestamp in timestamps] == ["hello", "world"]
    assert timestamps[0].start == 0.0
    assert timestamps[1].end == 0.7


def test_word_timestamps_skip_missing_values():
    assert (
        word_timestamps_from_words(
            [
                {"word": "missing-start", "end": 0.2},
                {"word": "missing-end", "start": 0.3},
                {"start": 0.4, "end": 0.5},
            ]
        )
        is None
    )


def test_word_timestamps_preserve_non_numeric_timestamp_error():
    with pytest.raises(ValueError):
        word_timestamps_from_words([{"word": "bad", "start": "nope", "end": 0.3}])
