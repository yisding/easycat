"""Tests for safe config and environment defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import patch

from easycat.runtime.records import ErrorInfo, JournalRecord
from easycat.runtime.safe_defaults import (
    SAFE_CONFIG_FIELDS,
    SAFE_ENV_VARS,
    apply_write_filter,
    safe_config_snapshot,
    safe_env_snapshot,
)
from easycat.validation.redaction import REDACTED_PHONE, REDACTED_SECRET


@dataclass
class _FakeConfig:
    """Mimics EasyConfig with a mix of safe and secret fields."""

    debug: str = "full"
    stt: str = "openai"
    tts: str = "openai"
    smart_turn_sensitivity: float = 0.8
    warmup: bool = False
    openai_api_key: str = "sk-secret-12345"
    secret_token: str = "tok-9999"
    timeouts: str = "default"


@dataclass
class _ValueConfig:
    stt: object = None


class TestSafeConfigSnapshot:
    def test_includes_allowlisted_fields(self):
        cfg = _FakeConfig()
        snap = safe_config_snapshot(cfg)
        assert "debug" in snap
        assert "stt" in snap
        assert "tts" in snap
        assert "smart_turn_sensitivity" in snap
        assert "warmup" in snap
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

    def test_arbitrary_processor_repr_is_not_used(self):
        """Custom processor instances must not leak secrets from __repr__."""

        class _SecretNoiseReducer:
            def process(self, frame: object) -> object:
                return frame

            def __repr__(self) -> str:
                return "SecretNoiseReducer(api_key='nr_live_SECRET_12345')"

        class _SecretEchoCanceller:
            def process(self, frame: object) -> object:
                return frame

            def feed_reference(self, frame: object) -> None:
                return None

            def __repr__(self) -> str:
                return "SecretEchoCanceller(token='aec_token_SECRET_67890')"

        @dataclass
        class _Cfg:
            noise_reduction: object = None
            echo_cancellation: object = None

        snap = safe_config_snapshot(
            _Cfg(
                noise_reduction=_SecretNoiseReducer(),
                echo_cancellation=_SecretEchoCanceller(),
            )
        )

        assert "nr_live_SECRET_12345" not in snap["noise_reduction"]
        assert "aec_token_SECRET_67890" not in snap["echo_cancellation"]
        assert "api_key" not in snap["noise_reduction"]
        assert "token" not in snap["echo_cancellation"]
        assert snap["noise_reduction"] == (
            "<tests.runtime.test_safe_defaults."
            "TestSafeConfigSnapshot.test_arbitrary_processor_repr_is_not_used."
            "<locals>._SecretNoiseReducer object>"
        )
        assert snap["echo_cancellation"] == (
            "<tests.runtime.test_safe_defaults."
            "TestSafeConfigSnapshot.test_arbitrary_processor_repr_is_not_used."
            "<locals>._SecretEchoCanceller object>"
        )


class TestNestedSecretRedaction:
    def test_nested_dataclass_secret_is_redacted(self):
        """A secret nested one level deep inside an allowlisted field is redacted."""

        @dataclass
        class _Inner:
            model: str = "whisper"
            api_key: str = "sk-nested-secret"

        @dataclass
        class _Cfg:
            stt: object = None

        snap = safe_config_snapshot(_Cfg(stt=_Inner()))
        assert "sk-nested-secret" not in snap["stt"]
        assert "***" in snap["stt"]
        assert "whisper" in snap["stt"]

    def test_two_level_deep_secret_is_redacted(self):
        """A secret two levels deep must still not leak into the snapshot."""

        @dataclass
        class _Creds:
            token: str = "tok-deep-secret"

        @dataclass
        class _Provider:
            name: str = "deepgram"
            creds: object = None

        @dataclass
        class _Cfg:
            stt: object = None

        snap = safe_config_snapshot(_Cfg(stt=_Provider(creds=_Creds())))
        assert "tok-deep-secret" not in snap["stt"]
        assert "***" in snap["stt"]
        assert "deepgram" in snap["stt"]

    def test_secret_in_nested_dict_is_redacted(self):
        """Secret keys inside a dict value are redacted."""

        @dataclass
        class _Cfg:
            stt: object = None

        snap = safe_config_snapshot(_Cfg(stt={"model": "nova", "api_key": "sk-dict-secret"}))
        assert "sk-dict-secret" not in snap["stt"]
        assert "***" in snap["stt"]
        assert "nova" in snap["stt"]

    def test_secret_in_nested_list_is_redacted(self):
        """Secret-bearing dataclasses inside a list are redacted."""

        @dataclass
        class _Inner:
            password: str = "pw-list-secret"

        @dataclass
        class _Cfg:
            stt: object = None

        snap = safe_config_snapshot(_Cfg(stt=[_Inner()]))
        assert "pw-list-secret" not in snap["stt"]
        assert "***" in snap["stt"]

    def test_opaque_object_repr_is_not_called(self):
        """Custom provider/client reprs may contain credentials and must not be used."""

        class _LeakyProvider:
            def __repr__(self) -> str:
                return "LeakyProvider(api_key='sk-leaked', token='bearer-secret')"

        @dataclass
        class _Cfg:
            stt: object = None

        snap = safe_config_snapshot(_Cfg(stt=_LeakyProvider()))
        assert "sk-leaked" not in snap["stt"]
        assert "bearer-secret" not in snap["stt"]
        assert "LeakyProvider" in snap["stt"]
        assert "api_key" not in snap["stt"]


class TestBoundedSafeRendering:
    def test_self_referential_list_uses_recursion_marker(self):
        value: list[object] = []
        value.append(value)

        snap = safe_config_snapshot(_ValueConfig(stt=value))

        assert snap["stt"] == "[...]"

    def test_self_referential_dataclass_uses_recursion_marker(self):
        @dataclass
        class _Node:
            name: str
            child: object = None

        node = _Node("root")
        node.child = node

        snap = safe_config_snapshot(_ValueConfig(stt=node))

        assert snap["stt"] == "_Node(name='root', child=...)"

    def test_shared_value_is_rendered_at_each_non_recursive_path(self):
        shared = ["nova"]

        snap = safe_config_snapshot(_ValueConfig(stt=[shared, shared]))

        assert snap["stt"] == "[['nova'], ['nova']]"

    def test_large_collection_is_limited(self):
        snap = safe_config_snapshot(_ValueConfig(stt=list(range(100_000))))

        assert snap["stt"].endswith("14, 15, ...]")
        assert len(snap["stt"]) < 100

    def test_deep_collection_is_limited(self):
        value: object = "unreachable"
        for _ in range(100):
            value = [value]

        snap = safe_config_snapshot(_ValueConfig(stt=value))

        assert snap["stt"] == "[[[[[[...]]]]]]"

    def test_oversized_scalar_is_replaced_without_rendering_content(self):
        secret = "sk-testsecret123456"
        value = f"{'a' * 1_000} {secret} {'z' * 1_000}"

        snap = safe_config_snapshot(_ValueConfig(stt=value))

        assert secret not in snap["stt"]
        assert snap["stt"] == f"<str {len(value)} chars>"

    def test_oversized_mapping_key_redacts_value_conservatively(self):
        secret = "not-pattern-shaped-but-sensitive"
        snap = safe_config_snapshot(_ValueConfig(stt={"x" * 10_000: secret}))

        assert secret not in snap["stt"]
        assert "'***'" in snap["stt"]
        assert len(snap["stt"]) < 300

    def test_broad_nested_value_respects_global_output_budget(self):
        leaf_values = [f"{index:02d}-{'x' * 245}" for index in range(16)]
        value = [list(leaf_values) for _ in range(16)]

        snap = safe_config_snapshot(_ValueConfig(stt=value))

        assert len(snap["stt"]) <= 8_192
        assert "..." in snap["stt"]

    def test_scalar_subclass_repr_is_not_called(self):
        class _LeakyString(str):
            def __repr__(self) -> str:
                raise AssertionError("custom repr must not run")

        snap = safe_config_snapshot(_ValueConfig(stt=_LeakyString("sk-never-render")))

        assert "sk-never-render" not in snap["stt"]
        assert "_LeakyString object>" in snap["stt"]

    def test_tuple_subclass_len_is_not_called(self):
        class _HostileTuple(tuple):
            def __len__(self) -> int:
                raise AssertionError("custom __len__ must not run")

        snap = safe_config_snapshot(_ValueConfig(stt=_HostileTuple((42,))))

        assert snap["stt"] == "(42,)"

    def test_secret_string_subclass_key_redacts_value(self):
        class _SecretKey(str):
            def __repr__(self) -> str:
                raise AssertionError("custom repr must not run")

        secret = "sk-mapping-secret123456"
        snap = safe_config_snapshot(_ValueConfig(stt={_SecretKey("api_key"): secret}))

        assert secret not in snap["stt"]
        assert "'***'" in snap["stt"]

    def test_unreadable_dataclass_field_does_not_break_snapshot(self):
        @dataclass
        class _Unreadable:
            model: str = "nova"

            def __getattribute__(self, name: str):
                if name == "model":
                    raise RuntimeError("unavailable")
                return super().__getattribute__(name)

        snap = safe_config_snapshot(_ValueConfig(stt=_Unreadable()))

        assert snap["stt"] == "_Unreadable(model=<unavailable>)"

    def test_unreadable_config_property_does_not_break_snapshot(self):
        class _UnreadableConfig:
            @property
            def debug(self) -> str:
                raise RuntimeError("unavailable")

        snap = safe_config_snapshot(_UnreadableConfig())

        assert snap["debug"] == "<unavailable>"


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

    def test_redacts_secret_like_data_without_dropping_replay_text(self):
        rec = JournalRecord(
            sequence=1,
            session_id="s1",
            data={
                "text": "please call +1 415 555 1212",
                "api_key": "short",
                "headers": {"Authorization": "Bearer short-token"},
                "nested": [{"request_id": "req_abcdef123456"}],
            },
            error=ErrorInfo(
                type="RuntimeError",
                message="Authorization: Bearer sk-testsecret123456",
                traceback="/Users/alice/project failed with tok-secret123456",
                notes="provider id req_abcdef123456",
                children=(
                    ErrorInfo(
                        type="ValueError",
                        message="child leaked sk-childsecret123456",
                        notes="child request req_bcdef1234567",
                    ),
                ),
            ),
        )

        filtered = apply_write_filter(rec)

        assert filtered is not rec
        assert filtered.data["text"] == "please call +1 415 555 1212"
        assert filtered.data["api_key"] == REDACTED_SECRET
        assert filtered.data["headers"]["Authorization"] == REDACTED_SECRET
        assert filtered.data["nested"] == [{"request_id": "req_abcdef123456"}]
        assert filtered.error is not None
        assert filtered.error.message == f"Authorization: {REDACTED_SECRET}"
        assert "/Users/alice" in (filtered.error.traceback or "")
        assert "tok-secret123456" not in (filtered.error.traceback or "")
        assert filtered.error.notes == "provider id req_abcdef123456"
        assert len(filtered.error.children) == 1
        assert filtered.error.children[0].message == f"child leaked {REDACTED_SECRET}"
        assert filtered.error.children[0].notes == "child request req_bcdef1234567"

    def test_pii_policy_irreversibly_redacts_replay_content(self):
        rec = JournalRecord(
            sequence=1,
            session_id="s1",
            data={
                "text": "the date is 2024-01-15; visit https://acme.example/orders",
                "transcript": "order 1234567890",
                "path": "/home/alice/report.pdf",
                "api_key": "short",
            },
        )

        filtered = apply_write_filter(rec, redaction="pii")

        assert filtered.data == {
            "api_key": REDACTED_SECRET,
            "path": "~/report.pdf",
            "text": f"the date is {REDACTED_PHONE}; visit [REDACTED_URL]",
            "transcript": "[REDACTED_TRANSCRIPT]",
        }


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
