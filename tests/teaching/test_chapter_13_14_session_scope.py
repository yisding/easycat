"""Keep the production-session ownership boundary executable."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
CHAPTER_13 = ROOT / "docs" / "teaching" / "13-swap-providers-and-transports"
CHAPTER_14 = ROOT / "docs" / "teaching" / "14-bring-your-own-agent"


def load_script(path: Path):
    name = f"teaching_scope_{path.parent.name}_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_session_scope_probe_distinguishes_graceful_and_cancelled_stop() -> None:
    completed = subprocess.run(
        [sys.executable, str(CHAPTER_13 / "session_scope_probe.py")],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "graceful": {
            "events": [
                "client.open",
                "session.start",
                "session.work",
                "shutdown.signal",
                "session.stop(force=False)",
                "session.stop(force=True) -> no-op",
                "session.export_postmortem",
                "client.close",
            ],
            "session_closed": True,
        },
        "cancelled": {
            "events": [
                "client.open",
                "session.start",
                "session.work",
                "outer.cancel",
                "session.stop(force=True)",
                "session.export_postmortem",
                "client.close",
            ],
            "session_closed": True,
        },
    }


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.asyncio
async def test_chapter_14_scopes_session_and_custom_client(
    monkeypatch, tmp_path: Path, cancelled: bool
) -> None:
    chapter = load_script(CHAPTER_14 / "main.py")
    events: list[object] = []

    class FakeClient:
        async def __aenter__(self):
            events.append("client.open")
            return self

        async def __aexit__(self, _exc_type, _exc, _tb) -> None:
            events.append("client.close")

    class FakeSession:
        async def __aenter__(self):
            events.append("session.start")
            return self

        async def __aexit__(self, _exc_type, _exc, _tb) -> None:
            events.append(("session.stop", True))

    client = FakeClient()
    session = FakeSession()

    async def fake_wait(_session) -> None:
        events.append("wait")
        if cancelled:
            raise asyncio.CancelledError

    def fake_export(exported_session, path, *, overwrite: bool) -> None:
        events.append(("export", exported_session, path, overwrite))

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(AsyncOpenAI=lambda: client))
    monkeypatch.setattr(chapter, "EasyConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(chapter, "LocalTransportConfig", lambda: object())
    monkeypatch.setattr(chapter, "create_session", lambda _config: session)
    monkeypatch.setattr(chapter, "attach_runtime_feedback", lambda _session: None)
    monkeypatch.setattr(chapter, "wait_for_shutdown_signal", fake_wait)
    monkeypatch.setattr(chapter, "export_debug_bundle", fake_export)
    monkeypatch.setattr(chapter, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(chapter.time, "time", lambda: 123)

    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await chapter.main()
    else:
        await chapter.main()

    bundle = tmp_path / "ch14-bridge-123.bundle"
    assert events == [
        "client.open",
        "session.start",
        "wait",
        ("session.stop", True),
        ("export", session, bundle, True),
        "client.close",
    ]


@pytest.mark.asyncio
async def test_chapter_13_exports_after_cancelled_session(monkeypatch, tmp_path: Path) -> None:
    chapter = load_script(CHAPTER_13 / "main.py")
    events: list[object] = []

    class FakeSession:
        async def __aenter__(self):
            events.append("session.start")
            return self

        async def __aexit__(self, _exc_type, _exc, _tb) -> None:
            events.append(("session.stop", True))

    session = FakeSession()

    async def fake_wait(_session) -> None:
        events.append("wait")
        raise asyncio.CancelledError

    def fake_export(exported_session, path, *, overwrite: bool) -> None:
        events.append(("export", exported_session, path, overwrite))

    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(sys, "argv", ["main.py"])
    monkeypatch.setattr(chapter, "build_agent", lambda: object())
    monkeypatch.setattr(chapter, "transport_config", lambda _name: object())
    monkeypatch.setattr(chapter, "EasyConfig", lambda **kwargs: kwargs)
    monkeypatch.setattr(chapter, "create_session", lambda _config: session)
    monkeypatch.setattr(chapter, "attach_runtime_feedback", lambda _session: None)
    monkeypatch.setattr(chapter, "wait_for_shutdown_signal", fake_wait)
    monkeypatch.setattr(chapter, "export_debug_bundle", fake_export)
    monkeypatch.setattr(chapter, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(chapter.time, "time", lambda: 456)

    with pytest.raises(asyncio.CancelledError):
        await chapter.main()

    bundle = tmp_path / "ch13-openai-local-456.bundle"
    assert events == [
        "session.start",
        "wait",
        ("session.stop", True),
        ("export", session, bundle, True),
    ]


def test_chapters_teach_scope_and_custom_dependency_ownership() -> None:
    chapter_13 = (CHAPTER_13 / "README.md").read_text(encoding="utf-8")
    chapter_14 = (CHAPTER_14 / "README.md").read_text(encoding="utf-8")
    exercises_13 = (CHAPTER_13 / "EXERCISES.md").read_text(encoding="utf-8")
    exercises_14 = (CHAPTER_14 / "EXERCISES.md").read_text(encoding="utf-8")
    normalized_13 = " ".join(chapter_13.split())
    normalized_14 = " ".join(chapter_14.split())

    assert "The production session boundary" in chapter_13
    assert "session_scope_probe.py" in chapter_13
    assert "read-only postmortem" in normalized_13
    assert "stop(force=False)" in chapter_13
    assert "idempotent no-op" in chapter_13
    assert "cancelled trace has only an effective `stop(force=True)`" in exercises_13
    assert "session_scope_probe.py" in exercises_13
    assert "Caller-owned workflow dependencies" in chapter_14
    assert "`GenericWorkflowBridge` does not infer" in normalized_14
    assert "caller-owned `AsyncOpenAI`" in exercises_14
