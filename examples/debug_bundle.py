"""Record a session with debug capture, auto-export a bundle, and inspect it.

End-to-end debug-capture workflow:
  1. Run a local mic/speaker session with ``debug="light"`` so every
     pipeline stage records to the journal.
  2. After ``Ctrl+C`` stops the session, ``record_to=`` writes a
     timestamped ``RunBundle`` zip.
  3. Load the bundle back in the same process and print the records,
     provider versions, and replayable TTS audio summary.

Setup:
  export OPENAI_API_KEY="..."
  uv sync --extra quickstart --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run python examples/debug_bundle.py
  uv run --env-file .env python examples/debug_bundle.py  # if keys live in .env
"""

from __future__ import annotations

from pathlib import Path

from easycat import EasyConfig, run
from easycat.debug.bundle import RunBundle

BUNDLE_DIR = Path("runs")


def _summarize(bundle: RunBundle, path: Path) -> None:
    """Print a compact postmortem summary from an exported bundle."""
    records = list(bundle.records())
    chunks = bundle.replay_audio()
    total_bytes = sum(len(c.data) for c in chunks)
    total_ms = sum(c.duration_ms for c in chunks)

    print(f"\nBundle: {path}")
    print(f"  provider_versions: {bundle.manifest.provider_versions}")
    print(f"  records:           {len(records)}")
    print(f"  tts replay:        {len(chunks)} chunks, {total_bytes} bytes, {total_ms:.0f} ms")

    for record in records:
        name = record.get("name")
        data = record.get("data") or {}
        if not isinstance(data, dict):
            continue
        text = data.get("text") or ""
        if name == "stt_final" and text:
            print(f"  user:             {text}")
        elif name == "agent_final" and text:
            print(f"  agent:            {text}")


def _new_bundle_after(before: set[Path]) -> Path:
    created = [path for path in BUNDLE_DIR.glob("*.zip") if path not in before]
    if not created:
        raise RuntimeError(f"No bundle was exported under {BUNDLE_DIR}/")
    return max(created, key=lambda path: path.stat().st_mtime)


def main() -> None:
    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "openai-agents is required. For an app, run: "
            "uv add 'easycat[quickstart]'. In this repo, run: "
            "uv sync --extra quickstart --group dev"
        ) from exc

    before = set(BUNDLE_DIR.glob("*.zip"))
    print(f"Recording session. Press Ctrl+C to stop and export a bundle under {BUNDLE_DIR}/.\n")
    run(
        EasyConfig.mic(
            agent=Agent(name="assistant", instructions="You are a helpful voice assistant."),
            record_to=BUNDLE_DIR,
            debug="light",
        )
    )
    bundle_path = _new_bundle_after(before)
    print(f"\nExported bundle to {bundle_path}")

    _summarize(RunBundle.load(bundle_path), bundle_path)


if __name__ == "__main__":
    main()
