"""``easycat console`` — try EasyCat in your terminal, no API keys required.

The default is deliberately keyless and offline. Ambient credentials or audio
devices never opt a user into provider traffic; live modes require ``--live``:

1. **Keyless text** (default): an interactive
   stdin loop over :func:`easycat.create_text_session` with an echo
   agent. Text sessions are the real keyless path — every turn is
   journaled even though no audio flows.
2. **Scripted voice demo** (``--voice-demo``): one no-key scripted turn
   through the full audio pipeline (transport → VAD → STT → agent →
   TTS), exactly like ``examples/journal_demo.py``.
3. **Live voice** (``--live`` + ``OPENAI_API_KEY`` + working microphone via
   doctor's ``check_microphone()``): a live OpenAI voice session.
4. **Live text** (``--live`` + ``OPENAI_API_KEY``, no microphone): the same
   stdin loop backed by a live OpenAI agent.

Every mode runs with ``debug="light"`` and ``record_to=`` so the run
always ends by printing the exported debug bundle path and a
``easycat replay <bundle>`` hint — the journal is the payoff of the
first run.

``console`` is interactive-only: it has no ``--json`` envelope (see
``easycat explain json-schema``). Automation should replay the exported
bundle instead: ``easycat replay PATH --json``.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer

from easycat.cli._output import stderr_console, stdout_console

if TYPE_CHECKING:
    from easycat.session import Session

ConsoleMode = Literal["keyless-text", "voice-demo", "live-voice", "live-text"]

_DEFAULT_RECORD_DIR = ".easycat/recordings"
_OPENAI_BASE_URL = "https://api.openai.com"
_LIVE_AGENT_MODEL = "gpt-5.6-luna"
_VOICE_DEMO_TIMEOUT_S = 15.0

_MODE_BANNERS: dict[ConsoleMode, str] = {
    "keyless-text": (
        "Starting the keyless, offline echo console. Ambient API keys are not used.\n"
        "Every turn is journaled. Run easycat console --live to explicitly use a\n"
        "live provider, or --voice-demo for the full no-key audio pipeline."
    ),
    "voice-demo": ("Running one scripted turn through the full audio pipeline — no API keys."),
    "live-voice": (
        "Live provider mode enabled; OPENAI_API_KEY and a microphone detected.\n"
        "This can incur provider charges. Speak into your microphone; press Ctrl-C to end."
    ),
    "live-text": (
        "Live provider mode enabled; no working microphone was detected. "
        f"Starting a billable text session with {_LIVE_AGENT_MODEL}."
    ),
}


def _select_mode(*, voice_demo: bool, live: bool = False) -> ConsoleMode:
    """Select an explicitly requested mode without ambient live opt-in."""
    if voice_demo:
        return "voice-demo"
    if not live:
        return "keyless-text"
    if not os.environ.get("OPENAI_API_KEY"):
        raise typer.BadParameter(
            "--live requires OPENAI_API_KEY; set it or omit --live for the keyless console."
        )

    from easycat.cli.diagnose.doctor import check_microphone

    if check_microphone().status == "ok":
        return "live-voice"
    return "live-text"


def _find_exported_bundle(record_dir: Path, *, since: float) -> Path | None:
    """Return the newest debug bundle exported after ``since``.

    Entries that no longer resolve are skipped: a dangling ``*.zip`` symlink in
    the recordings directory would otherwise replace the "Saved debug bundle"
    payoff with a traceback after an otherwise successful run (gh 1107).
    """
    if not record_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for path in record_dir.glob("*.zip"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= since:
            candidates.append((mtime, path))
    if not candidates:
        return None
    return max(candidates)[1]


def _print_journal_summary(session: Session) -> None:
    """Print a one-line journal recap after a clean stop."""
    journal = session.journal
    if journal is None:
        return
    try:
        records = journal.read()
    except Exception:  # noqa: BLE001 — recap only; the bundle is the payoff
        return
    stderr_console.print(f"  Journal captured {len(records)} records.")


def _print_payoff(bundle: Path | None) -> None:
    """End every run with the exported bundle path and a replay hint."""
    if bundle is None:
        stderr_console.print(
            "[yellow]No debug bundle was exported for this run. "
            "Run [cyan]easycat bundles list[/] to inspect earlier sessions.[/]"
        )
        return
    stdout_console.print(
        f"Saved debug bundle: {bundle}", markup=False, highlight=False, soft_wrap=True
    )
    stdout_console.print(
        f"Replay this session: easycat replay {bundle}",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )


async def _chat_loop(session: Session, *, prompt: str = "you: ") -> int:
    """Drive ``session.send_text`` from stdin until EOF/empty line/exit."""
    stderr_console.print("  Type a message and press Enter. Empty line or Ctrl-D ends the run.")
    turns = 0
    while True:
        try:
            line = await asyncio.to_thread(input, prompt)
        except EOFError:
            break
        text = line.strip()
        if not text or text.lower() in {"exit", "quit"}:
            break
        reply = await session.send_text(text)
        stdout_console.print(f"bot: {reply}", markup=False, highlight=False)
        turns += 1
    return turns


async def _run_text_mode(mode: ConsoleMode, record_dir: Path) -> None:
    """Keyless echo loop or live text loop, journaled either way."""
    from easycat import create_text_session

    if mode == "keyless-text":
        from easycat.stubs import NoopAgent

        agent: object = NoopAgent()
    else:
        from easycat.integrations.agents import RemoteResponsesAPIBridge

        agent = RemoteResponsesAPIBridge(
            base_url=_OPENAI_BASE_URL,
            model=_LIVE_AGENT_MODEL,
            api_key=os.environ["OPENAI_API_KEY"],
            reasoning_effort="none",
        )

    session = create_text_session(agent=agent, debug="light", record_to=record_dir)
    async with session:
        await _chat_loop(session)
    _print_journal_summary(session)


async def _run_voice_demo(record_dir: Path) -> None:
    """One scripted no-key turn through the full audio pipeline."""
    from easycat import EasyConfig, TurnManagerConfig, create_session
    from easycat.events import AgentFinal, STTFinal
    from easycat.stubs import scripted_turn_providers

    providers = scripted_turn_providers(
        transcript="Hello, EasyCat!",
        reply=lambda text: f"You said: {text} That was one scripted turn — no API keys.",
    )
    config = EasyConfig.mic(
        transport=providers.transport,
        vad=providers.vad,
        stt=providers.stt,
        agent=providers.agent,
        tts=providers.tts,
        turn_taking=TurnManagerConfig(end_of_turn_silence_ms=1),
        debug="light",
        record_to=record_dir,
    )
    session = create_session(config)
    turn_done = asyncio.Event()

    def _on_transcript(event: STTFinal) -> None:
        stdout_console.print(f"you (scripted): {event.text}", markup=False, highlight=False)

    def _on_reply(event: AgentFinal) -> None:
        stdout_console.print(f"bot: {event.text}", markup=False, highlight=False)
        turn_done.set()

    session.subscribe_event(STTFinal, _on_transcript)
    session.subscribe_event(AgentFinal, _on_reply)
    async with session:
        try:
            await asyncio.wait_for(turn_done.wait(), timeout=_VOICE_DEMO_TIMEOUT_S)
        except TimeoutError:
            stderr_console.print("[yellow]Timed out waiting for the scripted turn.[/]")
        # Let TTS synthesis and playback drain into the journal.
        await asyncio.sleep(0.5)
    _print_journal_summary(session)


async def _run_live_voice(record_dir: Path) -> None:
    """Live OpenAI voice session over the local microphone, until Ctrl-C."""
    from easycat import EasyConfig, create_session
    from easycat._signals import install_shutdown_signal_handlers
    from easycat.helpers import attach_runtime_feedback
    from easycat.integrations.agents import RemoteResponsesAPIBridge

    agent = RemoteResponsesAPIBridge(
        base_url=_OPENAI_BASE_URL,
        model=_LIVE_AGENT_MODEL,
        api_key=os.environ["OPENAI_API_KEY"],
        reasoning_effort="none",
    )
    config = EasyConfig.mic(agent=agent, debug="light", record_to=record_dir)
    session = create_session(config)
    attach_runtime_feedback(session)
    async with session:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        if install_shutdown_signal_handlers(loop, stop_event):
            await stop_event.wait()
        else:
            # No signal-handler support (e.g. Windows ProactorEventLoop):
            # block until KeyboardInterrupt; teardown still runs on exit.
            await asyncio.Event().wait()
    _print_journal_summary(session)


async def _run_mode(mode: ConsoleMode, record_dir: Path) -> None:
    if mode == "voice-demo":
        await _run_voice_demo(record_dir)
    elif mode == "live-voice":
        await _run_live_voice(record_dir)
    else:
        await _run_text_mode(mode, record_dir)


def console(
    voice_demo: bool = typer.Option(
        False,
        "--voice-demo",
        help="Run one scripted no-key turn through the full audio pipeline.",
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help=(
            "Explicitly use the live OpenAI provider (may send data and incur charges); "
            "uses voice when a microphone works, otherwise text."
        ),
    ),
    record_to: Path = typer.Option(
        Path(_DEFAULT_RECORD_DIR),
        "--record-to",
        help="Directory for the exported debug bundle (the first-run payoff).",
    ),
) -> None:
    """Try EasyCat in your terminal — no API keys required.

    Interactive-only: ``console`` has no ``--json`` envelope. Replay the
    exported bundle for machine-readable output: ``easycat replay PATH
    --json``.
    """
    if voice_demo and live:
        raise typer.BadParameter("--voice-demo and --live cannot be used together.")
    mode = _select_mode(voice_demo=voice_demo, live=live)
    stderr_console.print(_MODE_BANNERS[mode])
    started_at = time.time() - 1.0  # 1s margin for filesystem clock granularity
    try:
        asyncio.run(_run_mode(mode, record_to))
    except KeyboardInterrupt:
        # Ctrl-C: session teardown already ran via ``async with``; fall
        # through so the bundle path still prints as the run's payoff.
        stderr_console.print()
    _print_payoff(_find_exported_bundle(record_to, since=started_at))
