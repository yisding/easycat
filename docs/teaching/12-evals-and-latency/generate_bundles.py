"""Build five eval bundles for chapter 12.

These fixtures mirror the event shape the chapter-6 streaming agent
writes (``stt.final``, ``agent.first_token``, ``stage.tts.execute``,
``turn.gap``, and, where relevant, ``interruption.start``). The
numbers are invented but representative.

Run once; the checked-in bundles and ground_truth.csv are the
artifacts the reader actually uses.

    uv run python docs/teaching/12-evals-and-latency/generate_bundles.py
"""

from __future__ import annotations

import csv
import types
from pathlib import Path

from easycat.debug.export import export_debug_bundle
from easycat.runtime import InMemoryRingBuffer, JournalRecordKind

HERE = Path(__file__).parent
BUNDLES = HERE / "bundles"
GROUND_TRUTH = HERE / "ground_truth.csv"


def _emit(j, name, sid, data):
    j.append(kind=JournalRecordKind.EVENT, name=name, session_id=sid, data=data)


def _save(j, sid: str, filename: str) -> None:
    BUNDLES.mkdir(exist_ok=True)
    path = BUNDLES / filename
    export_debug_bundle(types.SimpleNamespace(journal=j), path, overwrite=True)
    print(f"  wrote {path.relative_to(Path.cwd())}")


def _turn(
    j,
    sid: str,
    t_start: float,
    stt_text: str,
    agent_first_token_delay_ms: float,
    tts_spans_ms: list[float],
    total_gap_ms: float,
    interruption_t_ms: float | None = None,
    tool_calls: list[dict] | None = None,
) -> None:
    """Emit a canonical turn's worth of records.

    ``tool_calls`` is an optional list of ``{"name", "args", "result",
    "elapsed_ms"}`` dicts; each becomes a paired
    ``tool.call.started`` / ``tool.call.result`` following the same
    shape chapter 7 emits.
    """
    _emit(j, "turn.started", sid, {"stage": "turn", "t_ms": t_start})
    _emit(
        j,
        "stt.final",
        sid,
        {"stage": "stt", "text": stt_text, "t_ms": t_start + 1000},
    )
    _emit(
        j,
        "agent.first_token",
        sid,
        {"stage": "agent", "t_ms": t_start + 1000 + agent_first_token_delay_ms},
    )
    for tc in tool_calls or ():
        _emit(
            j,
            "tool.call.started",
            sid,
            {"stage": "tool", "name": tc["name"], "args": tc.get("args", {})},
        )
        _emit(
            j,
            "tool.call.result",
            sid,
            {
                "stage": "tool",
                "name": tc["name"],
                "elapsed_ms": tc["elapsed_ms"],
                "result": tc["result"],
            },
        )
    for i, ms in enumerate(tts_spans_ms):
        _emit(
            j,
            "stage.tts.execute",
            sid,
            {"stage": "tts", "text": f"sentence {i + 1}", "elapsed_ms": ms},
        )
    if interruption_t_ms is not None:
        _emit(j, "interruption.start", sid, {"stage": "vad", "t_ms": interruption_t_ms})
    _emit(
        j,
        "turn.gap",
        sid,
        {"stage": "turn", "total_gap_ms": total_gap_ms, "text": stt_text},
    )


def build_all() -> list[dict[str, str]]:
    specs: list[tuple[str, dict]] = [
        # Fast clean turn.
        (
            "turn_01_fast.bundle",
            dict(
                t_start=1_000_000.0,
                stt_text="what time is it",
                agent_first_token_delay_ms=350,
                tts_spans_ms=[300, 280, 310],
                total_gap_ms=1150,
            ),
        ),
        # Slow agent turn (P95 spike).
        (
            "turn_02_slow_agent.bundle",
            dict(
                t_start=1_100_000.0,
                stt_text="tell me a joke",
                agent_first_token_delay_ms=2100,
                tts_spans_ms=[320, 290],
                total_gap_ms=2900,
            ),
        ),
        # Normal turn with a ghost interruption (like ch11 bug 3).
        (
            "turn_03_ghost_interrupt.bundle",
            dict(
                t_start=1_200_000.0,
                stt_text="whats the weather",
                agent_first_token_delay_ms=400,
                tts_spans_ms=[310],
                total_gap_ms=900,
                interruption_t_ms=1_200_001_450.0,
            ),
        ),
        # Normal turn, correctly interrupted by a real user.
        (
            "turn_04_real_interrupt.bundle",
            dict(
                t_start=1_300_000.0,
                stt_text="stop",
                agent_first_token_delay_ms=380,
                tts_spans_ms=[290, 310],
                total_gap_ms=1250,
                interruption_t_ms=1_300_001_400.0,
            ),
        ),
        # Medium-clean turn.
        (
            "turn_05_medium.bundle",
            dict(
                t_start=1_400_000.0,
                stt_text="remind me to buy milk",
                agent_first_token_delay_ms=600,
                tts_spans_ms=[310, 280, 300, 320],
                total_gap_ms=1700,
            ),
        ),
        # Turn with two tool calls — exercises the chapter-7 tool
        # shape so exercise 2's "run on tool-bearing bundles" has
        # something to consume.
        (
            "tools_01_weather.bundle",
            dict(
                t_start=1_500_000.0,
                stt_text="whats the weather in paris and set a timer for five minutes",
                agent_first_token_delay_ms=820,
                tts_spans_ms=[340, 310, 290],
                total_gap_ms=2200,
                tool_calls=[
                    {
                        "name": "get_weather",
                        "args": {"city": "paris"},
                        "result": "18C, light rain",
                        "elapsed_ms": 1480,
                    },
                    {
                        "name": "set_timer",
                        "args": {"seconds": 300},
                        "result": "ok",
                        "elapsed_ms": 42,
                    },
                ],
            ),
        ),
    ]

    rows = []
    for filename, kwargs in specs:
        sid = filename.removesuffix(".bundle")
        j = InMemoryRingBuffer(capacity=1_000)
        _turn(j, sid, **kwargs)
        _save(j, sid, filename)
        rows.append(
            {
                "bundle": filename,
                "reference_transcript": kwargs["stt_text"],
                "had_real_barge_in": "1" if filename == "turn_04_real_interrupt.bundle" else "0",
                "had_tool_call": "1" if kwargs.get("tool_calls") else "0",
            }
        )

    with GROUND_TRUTH.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "bundle",
                "reference_transcript",
                "had_real_barge_in",
                "had_tool_call",
            ],
        )
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {GROUND_TRUTH.relative_to(Path.cwd())}")


def main() -> None:
    print("Building evaluation bundles...")
    build_all()


if __name__ == "__main__":
    main()
