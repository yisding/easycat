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
GOLDEN = BUNDLES / "golden"
GROUND_TRUTH = HERE / "ground_truth.csv"
GOLDEN_GROUND_TRUTH = GOLDEN / "ground_truth.csv"


def _emit(j, name, sid, data):
    j.append(kind=JournalRecordKind.EVENT, name=name, session_id=sid, data=data)


def _save(j, sid: str, filename: str, into: Path = BUNDLES) -> None:
    into.mkdir(parents=True, exist_ok=True)
    path = into / filename
    export_debug_bundle(types.SimpleNamespace(journal=j), path, overwrite=True)
    print(f"  wrote {path.relative_to(Path.cwd())}")


def _turn(
    j,
    sid: str,
    t_start: float,
    stt_text: str,
    agent_first_token_delay_ms: float = 350,
    tts_spans_ms: list[float] | None = None,
    total_gap_ms: float = 1000.0,
    interruption_t_ms: float | None = None,
    tool_calls: list[dict] | None = None,
) -> None:
    """Emit a canonical turn's worth of records.

    ``tool_calls`` is an optional list of ``{"name", "args", "result",
    "elapsed_ms"}`` dicts; each becomes a paired
    ``tool.call.started`` / ``tool.call.result`` following the same
    shape chapter 7 emits.
    """
    if tts_spans_ms is None:
        tts_spans_ms = [300]
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


def build_golden() -> None:
    """Build the golden WER fixtures.

    Each bundle has a hand-tuned mismatch between the STT hypothesis
    (the text in ``stt.final``) and the reference transcript (in
    ``ground_truth.csv``), producing a known, reproducible WER value.
    Use these to verify the WER pipeline before pointing it at real
    recordings — if the numbers below don't match, your pipeline has
    drifted.

    Three fixtures:

    | bundle                       | ref words | edits | expected WER |
    |------------------------------|-----------|-------|--------------|
    | golden_01_wer_5pct.bundle    | 20        | 1 sub | 5.0%         |
    | golden_02_wer_10pct.bundle   | 10        | 1 sub | 10.0%        |
    | golden_03_wer_25pct.bundle   |  8        | 2     | 25.0%        |

    All edits are at the word level (the same shape ``_wer_words``
    in ``evals.py`` measures). Punctuation matters — neither side
    includes any.
    """
    cases = [
        {
            "filename": "golden_01_wer_5pct.bundle",
            "reference": (
                "today seems like a really good day to go for a walk "
                "in the park with my dog and friends"
            ),
            "hypothesis": (
                # 1 substitution: "great" instead of "good" (word 6 of 20)
                "today seems like a really great day to go for a walk "
                "in the park with my dog and friends"
            ),
            "expected_wer_pct": 5.0,
            "note": "1 substitution, 20-word reference",
        },
        {
            "filename": "golden_02_wer_10pct.bundle",
            "reference": "please set a quick timer for ten minutes starting now",
            "hypothesis": (
                # 1 substitution: "eleven" instead of "ten" (word 7 of 10)
                "please set a quick timer for eleven minutes starting now"
            ),
            "expected_wer_pct": 10.0,
            "note": "1 substitution, 10-word reference",
        },
        {
            "filename": "golden_03_wer_25pct.bundle",
            "reference": "the meeting starts at three in the afternoon",
            "hypothesis": (
                # 1 deletion ("the") + 1 substitution ("four" for "three") = 2 / 8 = 25%
                "meeting starts at four in the afternoon"
            ),
            "expected_wer_pct": 25.0,
            "note": "1 substitution + 1 deletion, 8-word reference",
        },
    ]

    rows: list[dict[str, str]] = []
    t = 9_000_000.0
    for case in cases:
        sid = case["filename"].removesuffix(".bundle")
        j = InMemoryRingBuffer(capacity=1_000)
        _turn(
            j,
            sid,
            t_start=t,
            stt_text=case["hypothesis"],
            total_gap_ms=1000.0,
        )
        _save(j, sid, case["filename"], into=GOLDEN)
        rows.append(
            {
                "bundle": case["filename"],
                "reference_transcript": case["reference"],
                "expected_wer_pct": f"{case['expected_wer_pct']:.1f}",
                "had_real_barge_in": "0",
                "had_tool_call": "0",
                "note": case["note"],
            }
        )
        t += 100_000

    GOLDEN.mkdir(parents=True, exist_ok=True)
    with GOLDEN_GROUND_TRUTH.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "bundle",
                "reference_transcript",
                "expected_wer_pct",
                "had_real_barge_in",
                "had_tool_call",
                "note",
            ],
        )
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {GOLDEN_GROUND_TRUTH.relative_to(Path.cwd())}")


def main() -> None:
    print("Building evaluation bundles...")
    build_all()
    print("\nBuilding golden WER bundles (reproducible non-zero WER)...")
    build_golden()
    print(
        "\nRun evals against the golden set with:\n"
        f"  uv run python {(HERE / 'evals.py').relative_to(Path.cwd())} "
        f"{GOLDEN.relative_to(Path.cwd())} {GOLDEN_GROUND_TRUTH.relative_to(Path.cwd())}\n"
        "Expected WERs: 5.0%, 10.0%, 25.0%, aggregate 10.5%."
    )


if __name__ == "__main__":
    main()
