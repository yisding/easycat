"""Tests for safe config and environment defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import patch

from easycat.runtime.records import JournalRecord
from easycat.runtime.safe_defaults import (
    SAFE_CONFIG_FIELDS,
    SAFE_ENV_VARS,
    apply_write_filter,
    safe_config_snapshot,
    safe_env_snapshot,
)


@dataclass
class _FakeConfig:
    """Mimics EasyConfig with a mix of safe and secret fields."""

    debug: str = "full"
    stt: str = "openai"
    tts: str = "openai"
    openai_api_key: str = "sk-secret-12345"
    secret_token: str = "tok-9999"
    timeouts: str = "default"


class TestSafeConfigSnapshot:
    def test_includes_allowlisted_fields(self):
        cfg = _FakeConfig()
        snap = safe_config_snapshot(cfg)
        assert "debug" in snap
        assert "stt" in snap
        assert "tts" in snap
        assert "timeouts" in snap

    def test_excludes_secret_fields(self):
        cfg = _FakeConfig()
        snap = safe_config_snapshot(cfg)
        assert "openai_api_key" not in snap
        assert "secret_token" not in snap

    def test_values_are_repr(self):
        cfg = _FakeConfig()
        snap = safe_config_snapshot(cfg)
        assert snap["debug"] == repr("full")

    def test_excludes_unknown_fields(self):
        """Fields not in the allowlist are excluded even if non-secret."""

        @dataclass
        class _Extended:
            debug: str = "full"
            custom_field: str = "hello"

        snap = safe_config_snapshot(_Extended())
        assert "custom_field" not in snap


class TestSafeEnvSnapshot:
    def test_includes_allowlisted_vars(self):
        with patch.dict(os.environ, {"EASYCAT_DEBUG": "1", "EASYCAT_DATA_DIR": "/tmp/ec"}):
            snap = safe_env_snapshot()
            assert snap["EASYCAT_DEBUG"] == "1"
            assert snap["EASYCAT_DATA_DIR"] == "/tmp/ec"

    def test_excludes_non_allowlisted_vars(self):
        with patch.dict(
            os.environ,
            {"OPENAI_API_KEY": "sk-secret", "AWS_SECRET_ACCESS_KEY": "aws-secret"},
            clear=False,
        ):
            snap = safe_env_snapshot()
            assert "OPENAI_API_KEY" not in snap
            assert "AWS_SECRET_ACCESS_KEY" not in snap

    def test_missing_vars_omitted(self):
        # Ensure vars not in the env are simply absent
        with patch.dict(os.environ, {}, clear=True):
            snap = safe_env_snapshot()
            assert len(snap) == 0


class TestApplyWriteFilter:
    def test_noop_returns_record_unchanged(self):
        rec = JournalRecord(sequence=1, session_id="s1")
        assert apply_write_filter(rec) is rec


class TestAllowlistCompleteness:
    def test_safe_config_fields_is_frozenset(self):
        assert isinstance(SAFE_CONFIG_FIELDS, frozenset)
        assert len(SAFE_CONFIG_FIELDS) > 0

    def test_safe_env_vars_is_frozenset(self):
        assert isinstance(SAFE_ENV_VARS, frozenset)
        assert len(SAFE_ENV_VARS) > 0

    def test_no_secret_fragments_in_safe_config(self):
        secret_fragments = {"key", "secret", "token", "password", "credential", "auth"}
        for field_name in SAFE_CONFIG_FIELDS:
            lower = field_name.lower()
            for frag in secret_fragments:
                assert frag not in lower, (
                    f"SAFE_CONFIG_FIELDS contains '{field_name}' which "
                    f"has secret fragment '{frag}'"
                )
