"""Chapter 8 — Run deterministic EasyCat eval cases and latency budgets.

Dependencies:
    uv sync --group dev

Run:
    uv run python docs/using-easycat/08-testing-evals/main.py
    uv run python docs/using-easycat/08-testing-evals/main.py --max-ms 0
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from easycat.debug.testing import (
    TurnResult,
    assert_exact_match,
    assert_latency,
    assert_llm_judge,
    assert_no_error,
    assert_regex,
    assert_turn_completed,
    run_text_turn,
)


@dataclass(frozen=True)
class EvalCase:
    name: str
    prompt: str
    exact: str | None = None
    pattern: str | None = None


CASES = (
    EvalCase(
        name="hours",
        prompt="What are your support hours?",
        exact="Support is open from nine to five Pacific time.",
    ),
    EvalCase(
        name="refund",
        prompt="I need a refund.",
        pattern=r"refund.*order number\?",
    ),
)


class SupportAgent:
    async def run(self, text: str) -> str:
        lowered = text.casefold()
        if "hour" in lowered or "open" in lowered:
            return "Support is open from nine to five Pacific time."
        if "refund" in lowered:
            return "I can help with a refund. What is your order number?"
        return "I can help with support hours or refunds."


async def deterministic_judge(transcript: str, rubric: str) -> dict[str, int | str]:
    """Exercise the judge injection contract; this is not a semantic judge."""
    assert "User:" in transcript and "Bot:" in transcript
    assert "relevance" in rubric and "fluency" in rubric
    return {
        "relevance": 5,
        "fluency": 5,
        "appropriate_length": 5,
        "reasoning": "deterministic contract stub",
    }


def parse_max_ms() -> float:
    parser = argparse.ArgumentParser(description="Run the offline EasyCat eval suite.")
    parser.add_argument(
        "--max-ms",
        type=float,
        default=5000.0,
        help="P95 text-turn latency budget in milliseconds.",
    )
    return parser.parse_args().max_ms


async def evaluate(max_ms: float) -> None:
    results: list[TurnResult] = []
    for case in CASES:
        result = await run_text_turn(SupportAgent(), case.prompt)
        assert_turn_completed(result, result.turn_id)
        assert_no_error(result, turn_id=result.turn_id)
        if case.exact is not None:
            assert_exact_match(result, expected=case.exact)
        if case.pattern is not None:
            assert_regex(result, pattern=case.pattern)
        verdict = await assert_llm_judge(result, judge=deterministic_judge)
        results.append(result)
        print(
            f"PASS {case.name}: {result.response} "
            f"({result.latency_ms:.1f} ms, relevance={verdict['relevance']})"
        )

    assert_latency(results, max_ms=max_ms, percentile="p95")
    print(f"PASS latency: p95 <= {max_ms:.1f} ms across {len(results)} turns")


def main() -> None:
    asyncio.run(evaluate(parse_max_ms()))


if __name__ == "__main__":
    main()
