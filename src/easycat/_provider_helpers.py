"""Shared helpers for STT/TTS provider implementations."""

from __future__ import annotations

from typing import Any

from easycat.events import WordTimestamp


def get_package_version(pkg: str) -> str:
    try:
        from importlib.metadata import version

        return version(pkg)
    except Exception:
        return "unknown"


def word_timestamps_from_words(words: Any) -> list[WordTimestamp] | None:
    if not isinstance(words, list):
        return None

    timestamps: list[WordTimestamp] = []
    for item in words:
        if not isinstance(item, dict):
            continue
        word = item.get("word")
        if not isinstance(word, str):
            word = item.get("text")
        start = item.get("start")
        end = item.get("end")
        if not isinstance(word, str) or start is None or end is None:
            continue
        timestamps.append(WordTimestamp(word=word, start=float(start), end=float(end)))

    return timestamps or None
