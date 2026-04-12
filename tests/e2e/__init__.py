"""End-to-end test suite for the debug-first refactor.

Every test in this package drives real audio bytes through the full
EasyCat pipeline (transport -> VAD -> STT -> agent -> TTS -> transport)
and verifies the debug-first contract (journal, bundles, bridges,
interruption) end to end.

Tests are gated with two markers:

- ``integration_socket`` — requires localhost bind/connect permissions.
- ``integration_live``   — requires ``OPENAI_API_KEY`` (or other live
  provider credentials). These skip cleanly when creds are absent.

Shared infrastructure lives in ``conftest.py``, ``_audio.py``,
``_clients.py``, and ``_assertions.py``.
"""
