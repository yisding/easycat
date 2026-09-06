from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import runpy
import sys
from pathlib import Path

import pytest

from easycat import EasyConfig
from easycat.cli._app import _docs_entries
from easycat.stt import OpenAIRealtimeSTTConfig
from tests.teaching import _script_runner as script_runner

REPO_ROOT = Path(__file__).resolve().parents[2]
FEATURE_LADDER = REPO_ROOT / "docs" / "using-easycat"
# The ladder table has no Status column: every rung is published, so the column
# only ever read "Available" (gh 1071). The row shape is still pinned so the
# table cannot drift from the chapter folders.
CHAPTER_ROW_RE = re.compile(
    r"^\| (?P<number>\d+) "
    r"\| \[`(?P<name>[^`]+)`\]\(\./(?P<link>[^)]+)/\) "
    r"\| (?P<features>[^|]+) \|$",
    flags=re.MULTILINE,
)
API_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9_]*_API_KEY\b")
UV_EXTRA_RE = re.compile(r"--extra\s+(?P<extra>[A-Za-z0-9_.-]+)")


def _chapter_dirs() -> list[Path]:
    return sorted(FEATURE_LADDER.glob("[0-9][0-9]-*"))


def _prerequisites(readme: str) -> str:
    return readme.split("## Prerequisites", 1)[1].split("\n## ", 1)[0]


def _required_env_vars(tree: ast.AST) -> set[str]:
    required: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "require_env":
            continue
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            required.add(first_arg.value)
    return required


def test_feature_ladder_rows_match_published_chapters() -> None:
    readme = (FEATURE_LADDER / "README.md").read_text(encoding="utf-8")
    rows = [match.groupdict() for match in CHAPTER_ROW_RE.finditer(readme)]
    chapter_dirs = [path.name for path in _chapter_dirs()]

    assert [row["link"] for row in rows] == chapter_dirs
    assert [row["name"] for row in rows] == chapter_dirs
    assert [int(row["number"]) for row in rows] == list(range(len(chapter_dirs)))


def test_feature_chapters_have_self_contained_reader_entrypoints() -> None:
    missing: list[str] = []

    for chapter in _chapter_dirs():
        for filename in ("README.md", "EXERCISES.md", "main.py"):
            if not (chapter / filename).exists():
                missing.append(f"{chapter.name}/{filename}")
        if not (chapter / "README.md").exists():
            continue
        readme = (chapter / "README.md").read_text(encoding="utf-8")
        exercises = (chapter / "EXERCISES.md").read_text(encoding="utf-8")
        chapter_docs = f"{readme}\n{exercises}"
        for script in chapter.glob("*.py"):
            command = f"uv run python docs/using-easycat/{chapter.name}/{script.name}"
            if command not in chapter_docs:
                missing.append(f"{chapter.name}: documented `{command}`")

    assert not missing, "Feature chapters missing reader entrypoints: " + ", ".join(missing)


def test_feature_chapter_prerequisites_cover_script_requirements() -> None:
    stale: list[str] = []

    for chapter in _chapter_dirs():
        readme = (chapter / "README.md").read_text(encoding="utf-8")
        prerequisites = _prerequisites(readme)
        script_keys: set[str] = set()
        script_env_vars: set[str] = set()
        script_extras: set[str] = set()
        for script in chapter.glob("*.py"):
            source = script.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=script.as_posix())
            docstring = ast.get_docstring(tree) or ""
            script_keys.update(API_KEY_RE.findall(docstring))
            script_env_vars.update(_required_env_vars(tree))
            script_extras.update(UV_EXTRA_RE.findall(docstring))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    script_keys.update(API_KEY_RE.findall(node.value))

        missing_keys = sorted(script_keys - set(API_KEY_RE.findall(prerequisites)))
        missing_env_vars = sorted(
            script_env_vars - set(re.findall(r"\b[A-Z][A-Z0-9_]+\b", prerequisites))
        )
        missing_extras = sorted(script_extras - set(UV_EXTRA_RE.findall(prerequisites)))
        if missing_keys or missing_env_vars or missing_extras:
            stale.append(
                f"{chapter.name}: missing keys {missing_keys or '-'}, "
                f"env vars {missing_env_vars or '-'}, "
                f"extras {missing_extras or '-'}"
            )
        if script_env_vars and "uv run easycat doctor --env-file .env" not in prerequisites:
            stale.append(f"{chapter.name}: missing .env doctor preflight")
        if script_env_vars and "add `--env-file .env` after `uv run`" not in prerequisites:
            stale.append(f"{chapter.name}: missing .env runtime guidance")

    assert not stale, "Feature chapter prerequisites drifted:\n" + "\n".join(stale)


def test_first_feature_chapter_uses_only_the_public_easycat_app_surface() -> None:
    script = FEATURE_LADDER / "00-first-voice-app" / "main.py"
    tree = ast.parse(script.read_text(encoding="utf-8"), filename=script.as_posix())
    easycat_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("easycat")
    ]

    assert len(easycat_imports) == 1
    assert easycat_imports[0].module == "easycat"
    assert {alias.name for alias in easycat_imports[0].names} == {"VoiceApp", "require_env"}
    assert 'app.run("local")' in script.read_text(encoding="utf-8")


def test_first_feature_chapter_names_the_registered_realtime_stt() -> None:
    exercises = (FEATURE_LADDER / "00-first-voice-app" / "EXERCISES.md").read_text(
        encoding="utf-8"
    )

    assert 'stt="openai-realtime"' in exercises
    assert 'stt="openai/realtime"' not in exercises
    default_config = EasyConfig(openai_api_key="test-key")
    explicit_config = EasyConfig(openai_api_key="test-key", stt="openai-realtime")
    assert isinstance(default_config.stt, OpenAIRealtimeSTTConfig)
    assert explicit_config.stt == default_config.stt


def test_runtime_modes_chapter_covers_every_voice_app_mode_and_boundary() -> None:
    chapter = FEATURE_LADDER / "01-runtime-modes"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for mode in ("local", "browser", "websocket", "twilio"):
        assert f'"{mode}"' in script
        assert f"main.py {mode}" in readme
    for extra in ("quickstart", "webrtc", "telephony"):
        assert f"--extra {extra}" in _prerequisites(readme)
    for env_var in ("OPENAI_API_KEY", "TWILIO_STREAM_URL", "TWILIO_AUTH_TOKEN"):
        assert env_var in _prerequisites(readme)
    for concept in (
        "fresh session per connection",
        "config_factory",
        "`run()` versus `serve()`",
        "non-loopback browser or WebSocket bind without authentication",
    ):
        assert concept in readme


def test_provider_chapter_teaches_shortcuts_typed_voices_and_discovery() -> None:
    chapter = FEATURE_LADDER / "02-providers-and-voices"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for profile in ("list", "openai", "deepgram-stt", "elevenlabs-voice"):
        assert f'"{profile}"' in script
        assert f"main.py {profile}" in readme
    for public_name in ("available_stt_providers", "available_tts_providers"):
        assert public_name in script
        assert public_name in readme
    for provider_surface in (
        'stt="deepgram/nova-2"',
        "OpenAITTSConfig",
        "ElevenLabsTTSConfig",
        "voice=",
        "voice_id=",
    ):
        assert provider_surface in script
    for concept in (
        "STT and TTS have separate registries",
        "Typed configs take precedence",
        "Specs are reusable; live providers are not",
        "Audio alignment stays automatic",
    ):
        assert concept in readme


def test_provider_chapter_lists_builtin_providers_without_credentials() -> None:
    script = FEATURE_LADDER / "02-providers-and-voices" / "main.py"
    completed = script_runner.run(
        [sys.executable, str(script), "list"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")},
    )

    assert "STT providers:" in completed.stdout
    assert "openai-realtime" in completed.stdout
    assert "deepgram" in completed.stdout
    assert "TTS providers:" in completed.stdout
    assert "elevenlabs" in completed.stdout


def test_conversation_controls_chapter_builds_distinct_policy_profiles() -> None:
    chapter = FEATURE_LADDER / "03-conversation-controls"
    script = chapter / "main.py"
    namespace = runpy.run_path(str(script))
    profile_config = namespace["profile_config"]

    balanced_audio, balanced_turns = profile_config("balanced")
    assert balanced_audio == {}
    assert balanced_turns.end_of_turn_silence_ms == 500

    vad_audio, vad_turns = profile_config("vad-only")
    assert vad_audio["smart_turn"] is False
    assert vad_audio["enable_echo_cancellation"] is True
    assert vad_turns.end_of_turn_silence_ms == 700

    fast_audio, fast_turns = profile_config("fast")
    assert fast_audio["smart_turn"] is True
    assert fast_audio["smart_turn_sensitivity"] == 0.7
    assert fast_turns.end_of_turn_silence_ms == 400

    clean_audio, _ = profile_config("clean")
    assert clean_audio["enable_noise_reduction"] is True
    assert clean_audio["enable_echo_cancellation"] is True

    raw_audio, _ = profile_config("raw")
    assert raw_audio["smart_turn"] is False
    assert raw_audio["enable_noise_reduction"] is False
    assert raw_audio["enable_echo_cancellation"] is False


def test_conversation_controls_teaches_barge_in_and_push_to_talk_boundaries() -> None:
    chapter = FEATURE_LADDER / "03-conversation-controls"
    readme = (chapter / "README.md").read_text(encoding="utf-8")
    main_script = (chapter / "main.py").read_text(encoding="utf-8")
    ptt_script = (chapter / "push_to_talk.py").read_text(encoding="utf-8")

    for profile in ("balanced", "vad-only", "fast", "clean", "raw"):
        assert f'"{profile}"' in main_script
        assert f"main.py {profile}" in readme
    for surface in (
        "smart_turn",
        "enable_noise_reduction",
        "TurnManagerConfig",
        "EasyConfig.mic",
        "VoiceApp(config=config)",
    ):
        assert surface in main_script
    for surface in (
        "TurnMode.PUSH_TO_TALK",
        "create_session",
        "run_stdin_push_to_talk_session",
    ):
        assert surface in ptt_script
    for concept in (
        "VAD answers",
        "Interruption is a state transition",
        "application calls `session.start_turn()` and `session.end_turn()`",
        "Echo cancellation (AEC)",
        "Noise reduction (NR)",
    ):
        assert concept in readme


def test_tools_actions_chapter_keeps_tools_effects_events_and_speech_separate() -> None:
    chapter = FEATURE_LADDER / "04-tools-actions"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for surface in (
        "function_tool",
        "SessionActions",
        "context=actions",
        "session_actions=actions",
        "PhoneticReplacementProcessor",
        "PauseProcessor",
        'style="ellipsis"',
        "session.on(",
        "run_session",
    ):
        assert surface in script
    for concept in (
        "A normal tool returns information",
        "A session action requests a controlled side effect",
        "Tool events observe; they do not act",
        "Output processors change speech, not meaning",
        "does not synthesize filler speech automatically",
    ):
        assert concept in readme


def test_tools_actions_pronunciation_preview_runs_without_credentials() -> None:
    script = FEATURE_LADDER / "04-tools-actions" / "main.py"
    completed = script_runner.run(
        [sys.executable, str(script), "preview"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")},
    )

    assert "Agent text: Siobhan Nguyen's phone number" in completed.stdout
    assert "Spoken text: shi-vawn win's phone number" in completed.stdout
    assert "1 ... 5 ... 5 ... 5" in completed.stdout


def test_agent_bridges_chapter_maps_supported_frameworks_and_custom_boundaries() -> None:
    chapter = FEATURE_LADDER / "05-agent-bridges"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for adapter in (
        "OpenAIAgentsBridge",
        "PydanticAIBridge",
        "LangChainBridge",
        "LangGraphBridge",
        "LlamaAgentsBridge",
        "RemoteResponsesAPIBridge",
        "GenericWorkflowBridge",
        "AgentRunner",
    ):
        assert adapter in script
        assert adapter in readme
    for surface in ("auto_adapt_agent", "EasyConfig.mic", "VoiceApp"):
        assert surface in script
    for concept in (
        "Auto-detection is the default path",
        "Construct a bridge when you need bridge options",
        "Custom workflows have shallow and deep modes",
        "fresh one inside `config_factory` for each connection",
        "subclass public `BridgeTemplate`",
    ):
        assert concept in readme


def test_agent_bridges_matrix_runs_without_framework_sdks_or_credentials() -> None:
    script = FEATURE_LADDER / "05-agent-bridges" / "main.py"
    completed = script_runner.run(
        [sys.executable, str(script), "matrix"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")},
    )

    assert "agents.Agent -> OpenAIAgentsBridge" in completed.stdout
    assert "compiled LangGraph -> LangGraphBridge" in completed.stdout
    assert "SupportWorkflow -> GenericWorkflowBridge (deep_mode=False)" in completed.stdout
    assert "PlainAgent -> unchanged; Session adds AgentRunner" in completed.stdout


def test_session_control_chapter_uses_public_lifecycle_and_event_surfaces() -> None:
    chapter = FEATURE_LADDER / "06-session-control"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for surface in (
        "EasyConfig.mic",
        "create_session",
        "create_text_session",
        "session.on(",
        "session.subscribe_event(",
        "session.send_text(",
        "session.reset_state()",
        "session.wait_closed()",
        "run_session",
    ):
        assert surface in script
    for concept in (
        "Use one public teardown verb",
        "Choose the event subscription surface",
        "Text turns use the real agent path",
        "Reset a conversation without replacing the session",
        "Stopped does not mean uninspectable",
        "`stop(force=True)`",
    ):
        assert concept in readme


def test_session_control_text_lifecycle_runs_without_credentials() -> None:
    script = FEATURE_LADDER / "06-session-control" / "main.py"
    completed = script_runner.run(
        [sys.executable, str(script), "text"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")},
    )

    assert "Reply 1: Workflow turn 1: first message" in completed.stdout
    assert "Reply 2: Workflow turn 2: second message" in completed.stdout
    assert "Reply after reset: Workflow turn 1: after reset" in completed.stdout
    assert "Post-stop guard: Session has been stopped" in completed.stdout


def test_observability_chapter_records_and_explains_postmortem_surfaces() -> None:
    chapter = FEATURE_LADDER / "07-observability"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for surface in (
        'debug="full"',
        "session.journal.read()",
        "session.export_debug_bundle(",
        '"baseline.bundle"',
        '"candidate.bundle"',
    ):
        assert surface in script
    for command in (
        "easycat bundles show",
        "easycat inspect",
        "easycat replay",
        "easycat diff",
        "easycat debugger serve",
        "easycat journal grep",
        "easycat bundles export",
    ):
        assert command in readme
    for concept in (
        "Journal and bundle are different forms",
        "Replay safely",
        "Output redaction is not bundle redaction",
        '`debug="full"` does not open a browser',
        "Tool policy selects `deny`, `stub`, or `allow`",
    ):
        assert concept in readme


def test_observability_pair_supports_show_replay_and_diff_without_credentials(
    tmp_path: Path,
) -> None:
    script = FEATURE_LADDER / "07-observability" / "main.py"
    bundles = tmp_path / "bundles"
    env = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    env["EASYCAT_DATA_DIR"] = str(tmp_path / "data")
    script_runner.run(
        [sys.executable, str(script), "pair", str(bundles)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    baseline = bundles / "baseline.bundle"
    candidate = bundles / "candidate.bundle"
    easycat = Path(sys.executable).with_name("easycat")
    show = script_runner.run(
        [str(easycat), "bundles", "show", str(baseline), "--json"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    replay = script_runner.run(
        [
            str(easycat),
            "replay",
            str(baseline),
            "--fidelity",
            "artifact",
            "--tool-policy",
            "deny",
            "--json",
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    diff = script_runner.run(
        [str(easycat), "diff", str(baseline), str(candidate), "--json"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    show_payload = json.loads(show.stdout)
    replay_payload = json.loads(replay.stdout)
    diff_payload = json.loads(diff.stdout)
    assert show_payload["status"] == "ok"
    assert show_payload["turn_count"] == 2
    assert replay_payload["fidelity_effective"] == "artifact"
    assert replay_payload["side_effecting"] is False
    assert len(diff_payload["turns"]) == 2
    assert all(turn["transcript"]["changed"] for turn in diff_payload["turns"])


def test_testing_evals_chapter_uses_real_offline_turn_and_assertion_surfaces() -> None:
    chapter = FEATURE_LADDER / "08-testing-evals"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for surface in (
        "EvalCase",
        "run_text_turn",
        "assert_exact_match",
        "assert_regex",
        "assert_turn_completed",
        "assert_no_error",
        "assert_llm_judge",
        "assert_latency",
        'percentile="p95"',
    ):
        assert surface in script
    for concept in (
        "The testing ladder",
        "Assertions work on turns and bundles",
        "Eval cases separate input from oracle",
        "Judges are one oracle, not ground truth",
        "Report latency or gate latency deliberately",
        "There is no separate `easycat eval` CLI command",
        "smoke --json` is a low-sample live integration",
        "sweep --require-samples --json` collects",
    ):
        assert concept in readme


def test_testing_evals_suite_passes_and_latency_budget_can_fail_without_credentials() -> None:
    script = FEATURE_LADDER / "08-testing-evals" / "main.py"
    env = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    passing = script_runner.run(
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    failing = script_runner.run(
        [sys.executable, str(script), "--max-ms", "0"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "PASS hours:" in passing.stdout
    assert "PASS refund:" in passing.stdout
    assert "PASS latency: p95 <= 5000.0 ms across 2 turns" in passing.stdout
    assert failing.returncode != 0
    assert "exceeds budget 0.0 ms" in failing.stderr


def test_multi_caller_chapter_uses_public_server_auth_capacity_and_lifecycle_surfaces() -> None:
    chapter = FEATURE_LADDER / "09-multi-caller"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for surface in (
        "BearerTokenAuth",
        "CapacityGate",
        "enforce_bind_guard",
        'authorization_header=f"Bearer {TOKEN}"',
        "LocalSupervisor(max_sessions=1",
        '== ("capacity", None, 1)',
        "gate.start_draining()",
        "gate.drain(",
    ):
        assert surface in script
    for concept in (
        "One connection, one fresh session",
        "Authentication happens before allocation",
        "Non-loopback binds fail closed",
        "Capacity rejects instead of queueing callers",
        "The helper owns connection teardown",
        "Use `VoiceServer` for one production process policy",
        "Graceful shutdown is admission control plus a deadline",
    ):
        assert concept in readme


def test_multi_caller_checkpoint_proves_isolation_and_bounded_rejection() -> None:
    script = FEATURE_LADDER / "09-multi-caller" / "main.py"
    env = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    result = script_runner.run(
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert "PASS auth: missing bearer token rejected before session creation" in result.stdout
    assert "PASS capacity: extra caller rejected instead of queued" in result.stdout
    assert "PASS isolation: released slot created fresh session 2" in result.stdout
    assert "PASS shutdown: draining rejected new work and stopped session 2" in result.stdout
    assert "PASS bind guard: public unauthenticated endpoint failed closed" in result.stdout


@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_multi_caller_start_failure_releases_capacity(failure_type) -> None:
    script = FEATURE_LADDER / "09-multi-caller" / "main.py"
    namespace = runpy.run_path(str(script))
    demo_session = namespace["DemoSession"]

    class FailingSession(demo_session):
        async def start(self) -> None:
            raise failure_type("startup failed")

    supervisor_type = namespace["LocalSupervisor"]
    module_globals = supervisor_type.connect.__globals__
    module_globals["DemoSession"] = FailingSession
    supervisor = supervisor_type(max_sessions=1, events=[])
    authorized = namespace["Request"](authorization_header=f"Bearer {namespace['TOKEN']}")

    with pytest.raises(failure_type, match="startup failed"):
        await supervisor.connect("failed", authorized)

    assert supervisor.sessions == {}
    assert supervisor.gate.active_count == 0
    assert supervisor.gate.reserved_count == 0

    module_globals["DemoSession"] = demo_session
    outcome, replacement = await supervisor.connect("replacement", authorized)
    assert outcome == "accepted"
    assert replacement is not None


@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_multi_caller_stop_failure_releases_capacity(failure_type) -> None:
    script = FEATURE_LADDER / "09-multi-caller" / "main.py"
    namespace = runpy.run_path(str(script))
    demo_session = namespace["DemoSession"]

    class FailingStopSession(demo_session):
        async def stop(self, *, force: bool = False) -> None:
            raise failure_type("shutdown failed")

    supervisor_type = namespace["LocalSupervisor"]
    module_globals = supervisor_type.connect.__globals__
    module_globals["DemoSession"] = FailingStopSession
    supervisor = supervisor_type(max_sessions=1, events=[])
    authorized = namespace["Request"](authorization_header=f"Bearer {namespace['TOKEN']}")

    outcome, session = await supervisor.connect("failed", authorized)
    assert outcome == "accepted"
    assert session is not None
    with pytest.raises(failure_type, match="shutdown failed"):
        await supervisor.disconnect("failed")

    assert supervisor.sessions == {}
    assert supervisor.gate.active_count == 0
    assert supervisor.gate.reserved_count == 0

    module_globals["DemoSession"] = demo_session
    outcome, replacement = await supervisor.connect("replacement", authorized)
    assert outcome == "accepted"
    assert replacement is not None


def test_telephony_chapter_uses_public_twilio_trust_callback_and_action_surfaces() -> None:
    chapter = FEATURE_LADDER / "10-telephony"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for surface in (
        "compute_twilio_webhook_signature",
        "validate_twilio_webhook_signature",
        "TwilioStreamTokenStore",
        "twiml_connect_stream",
        "parse_gather_webhook",
        "parse_call_status_callback",
        "match_screening_platform",
        "classify_ivr_prompt",
        "TwilioSessionActionExecutor",
        "SendDTMFAction",
        "TransferCallAction",
        "EndCallAction",
    ):
        assert surface in script
    for concept in (
        "A Twilio call crosses two planes",
        "Validate the public URL Twilio signed",
        "Preserve call identity across callbacks and media",
        "Inbound media is one session per call",
        "Status callbacks drive call lifecycle",
        "DTMF has input and output paths",
        "Screening, voicemail, and IVR are different states",
        "Call control stays provider-neutral at the session boundary",
        "Outbound calling is a policy boundary",
        "Interruptions must clear provider playback",
    ):
        assert concept in readme


def test_telephony_checkpoint_runs_without_credentials_or_twilio_sdk() -> None:
    script = FEATURE_LADDER / "10-telephony" / "main.py"
    env = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    result = script_runner.run(
        [sys.executable, str(script)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert "PASS handoff: signed webhook minted one-use media authorization" in result.stdout
    assert "PASS callbacks: DTMF, status, screening, and IVR inputs classified" in result.stdout
    assert "PASS actions: DTMF, transfer, and hangup mapped to Twilio updates" in result.stdout


def test_production_ops_chapter_uses_public_health_metrics_and_durability_surfaces() -> None:
    chapter = FEATURE_LADDER / "11-production-ops"
    script = (chapter / "main.py").read_text(encoding="utf-8")
    readme = (chapter / "README.md").read_text(encoding="utf-8")

    for surface in (
        "VoiceServerConfig",
        "VoiceServerHealth",
        "record_request",
        "observe_connections_active",
        "observe_draining",
        "SqliteJournal",
        "ReadonlySqliteJournal",
        "sweep_crashed_journals",
        "JournalRecordKind",
        '"/health/ready?token=secret"',
    ):
        assert surface in script
    for concept in (
        "Treat operations as a contract",
        "Validation is a ladder, not one command",
        "Build once and run the installed artifact",
        "Liveness and readiness answer different questions",
        "Metrics are bounded; journals are forensic",
        "Durability includes crash recovery and retention",
        "Shutdown order is part of correctness",
        "Practice failure before production does it for you",
        "Ladder complete",
    ):
        assert concept in readme


def test_production_ops_checkpoint_runs_and_can_persist_a_clean_journal(tmp_path: Path) -> None:
    script = FEATURE_LADDER / "11-production-ops" / "main.py"
    env = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY")}
    result = script_runner.run(
        [sys.executable, str(script), "--data-dir", str(tmp_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert (
        "PASS policy: public bind has auth, capacity, and bounded drain windows" in result.stdout
    )
    assert (
        "PASS health: draining fails readiness and raw metric paths are rejected" in result.stdout
    )
    assert (
        "PASS durability: clean SQLite journal reopened as a read-only postmortem" in result.stdout
    )
    journal = tmp_path / "journals" / "chapter-11-ops-checkpoint.sqlite"
    assert journal.is_file()


def test_feature_scripts_do_not_import_easycat_internals() -> None:
    internal_imports: list[str] = []

    for script in FEATURE_LADDER.glob("[0-9][0-9]-*/*.py"):
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=script.as_posix())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom | ast.Import) or not node.lineno:
                continue
            modules = (
                [node.module]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
            )
            for module in modules:
                if (
                    module
                    and module.startswith("easycat.")
                    and any(part.startswith("_") for part in module.split("."))
                ):
                    internal_imports.append(
                        f"{script.relative_to(REPO_ROOT).as_posix()}:{node.lineno}: {module}"
                    )

    assert not internal_imports, "Feature lessons imported internals:\n" + "\n".join(
        internal_imports
    )


def test_feature_ladder_is_discoverable_from_public_docs_surfaces() -> None:
    root_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    docs_readme = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    entries = {entry["path"]: entry for entry in _docs_entries()}

    assert "[EasyCat feature ladder](docs/using-easycat/)" in root_readme
    assert "[EasyCat feature ladder](using-easycat/)" in docs_readme
    assert entries["docs/using-easycat/"]["diataxis"] == "tutorial"
    assert entries["docs/using-easycat/"]["audience"] == "learners"
    assert (
        "uv run python docs/using-easycat/00-first-voice-app/main.py"
        in entries["docs/using-easycat/"]["commands"]
    )
    for chapter in _chapter_dirs():
        route = entries[f"docs/using-easycat/{chapter.name}/"]
        assert route["diataxis"] == "tutorial", chapter.name
        assert route["audience"] == "learners", chapter.name
    assert entries["docs/using-easycat/00-first-voice-app/"]["diataxis"] == "tutorial"
    runtime_modes = entries["docs/using-easycat/01-runtime-modes/"]
    assert runtime_modes["diataxis"] == "tutorial"
    assert runtime_modes["audience"] == "learners"
    assert (
        "uv run python docs/using-easycat/01-runtime-modes/main.py browser"
        in runtime_modes["commands"]
    )
    providers = entries["docs/using-easycat/02-providers-and-voices/"]
    assert providers["diataxis"] == "tutorial"
    assert providers["audience"] == "learners"
    assert (
        "uv run python docs/using-easycat/02-providers-and-voices/main.py list"
        in providers["commands"]
    )
    conversation = entries["docs/using-easycat/03-conversation-controls/"]
    assert (
        "uv run python docs/using-easycat/03-conversation-controls/main.py balanced"
        in conversation["commands"]
    )
    tools = entries["docs/using-easycat/04-tools-actions/"]
    assert "uv run python docs/using-easycat/04-tools-actions/main.py preview" in tools["commands"]
    bridges = entries["docs/using-easycat/05-agent-bridges/"]
    assert (
        "uv run python docs/using-easycat/05-agent-bridges/main.py matrix" in bridges["commands"]
    )
    session_control = entries["docs/using-easycat/06-session-control/"]
    assert (
        "uv run python docs/using-easycat/06-session-control/main.py text"
        in session_control["commands"]
    )
    observability = entries["docs/using-easycat/07-observability/"]
    assert (
        "uv run python docs/using-easycat/07-observability/main.py pair .easycat/tutorial/ch07"
        in observability["commands"]
    )
    testing_evals = entries["docs/using-easycat/08-testing-evals/"]
    assert "uv run python docs/using-easycat/08-testing-evals/main.py" in testing_evals["commands"]
    multi_caller = entries["docs/using-easycat/09-multi-caller/"]
    assert "uv run python docs/using-easycat/09-multi-caller/main.py" in multi_caller["commands"]
    telephony = entries["docs/using-easycat/10-telephony/"]
    assert "uv run python docs/using-easycat/10-telephony/main.py" in telephony["commands"]
    production_ops = entries["docs/using-easycat/11-production-ops/"]
    assert (
        "uv run python docs/using-easycat/11-production-ops/main.py" in production_ops["commands"]
    )
