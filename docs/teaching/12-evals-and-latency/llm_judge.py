"""Chapter 12 — LLM-as-judge for conversational quality.

Sends a bundle's transcript to an LLM with a 1-5 rubric and prints
the structured score. Requires OPENAI_API_KEY; after setting it, run
``uv run easycat doctor`` from the repo root. If the key lives in
``.env``, run ``uv run easycat doctor --env-file .env``. Use
``uv run easycat doctor --env-file .env --json`` for parseable checks.
Add ``--env-file .env`` after ``uv run`` on script commands if keys live in
``.env``.

    uv run python docs/teaching/12-evals-and-latency/llm_judge.py \\
        docs/teaching/12-evals-and-latency/bundles/turn_01_fast.bundle

This is *not* a replacement for human evaluation. Judge agreement
depends on the rubric, model, prompt, and dataset; calibrate it against
human labels before using scores as a gate. A score of 5 only means the
judge found no text-level problem under this prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from openai import AsyncOpenAI

from easycat.debug.testing import load_bundle

JUDGE_MODEL = "gpt-4o-mini"
SCORE_KEYS = ("relevance", "fluency", "appropriate_length")

RUBRIC = """You are evaluating a single voice-bot turn.

Score each dimension 1 (awful) to 5 (excellent):

- relevance: did the bot answer what was actually asked?
- fluency: was the reply well-phrased for speech?
- appropriate_length: was the reply the right length for a voice turn?

Return JSON with keys {relevance, fluency, appropriate_length, reasoning}.
"""


def extract_transcript(bundle_path: Path) -> str:
    bundle = load_bundle(bundle_path)
    user_lines = []
    bot_lines = []
    for r in bundle.records():
        if r["name"] == "stt.final":
            user_lines.append(r["data"].get("text", ""))
        elif r["name"] == "stage.tts.execute":
            bot_lines.append(r["data"].get("text", ""))
    return "User: " + " ".join(user_lines) + "\nBot: " + " ".join(bot_lines)


def parse_judgment(raw: str) -> dict:
    """Validate the JSON object's score ranges before reporting it."""
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "judge returned non-JSON", "raw": raw}
    if not isinstance(result, dict):
        return {"error": "judge returned a non-object", "raw": raw}
    for key in SCORE_KEYS:
        score = result.get(key)
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            return {"error": f"judge returned invalid {key} score", "raw": raw}
    if not isinstance(result.get("reasoning"), str):
        return {"error": "judge returned invalid reasoning", "raw": raw}
    return result


async def judge(bundle_path: Path) -> dict:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY to run the LLM judge.")
    transcript = extract_transcript(bundle_path)
    async with AsyncOpenAI() as client:
        resp = await client.chat.completions.create(
            model=JUDGE_MODEL,
            messages=[
                {"role": "system", "content": RUBRIC},
                {"role": "user", "content": transcript},
            ],
            response_format={"type": "json_object"},
        )
    raw = resp.choices[0].message.content or "{}"
    return parse_judgment(raw)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", type=Path)
    args = ap.parse_args()
    if not args.bundle.exists():
        sys.exit(f"{args.bundle} does not exist.")
    result = asyncio.run(judge(args.bundle))
    print(f"=== {args.bundle.name} ===")
    if "error" in result:
        print(f"  {'error':>22}: {result['error']}")
        print(f"  {'raw':>22}: {result.get('raw', '')}")
        return
    for k in SCORE_KEYS:
        print(f"  {k:>22}: {result.get(k)}")
    reasoning = result.get("reasoning", "")
    print(f"  {'reasoning':>22}: {reasoning}")


if __name__ == "__main__":
    main()
