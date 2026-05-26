"""Chapter 12 — LLM-as-judge for conversational quality.

Sends a bundle's transcript to an LLM with a 1-5 rubric and prints
the structured score. Requires OPENAI_API_KEY.

    uv run python docs/teaching/12-evals-and-latency/llm_judge.py \\
        docs/teaching/12-evals-and-latency/bundles/turn_01_fast.bundle

This is *not* a replacement for human evaluation. Studies place
LLM-as-judge at ~95% agreement with humans on most rubrics — a
fast triage layer, nothing more. A score of 5 does not guarantee
a good turn; it means the judge couldn't find something to complain
about from the transcript alone.
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


async def judge(bundle_path: Path) -> dict:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY to run the LLM judge.")
    client = AsyncOpenAI()
    transcript = extract_transcript(bundle_path)
    resp = await client.chat.completions.create(
        model=JUDGE_MODEL,
        messages=[
            {"role": "system", "content": RUBRIC},
            {"role": "user", "content": transcript},
        ],
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # response_format=json_object makes this rare, not impossible.
        # Surface the raw text so a reader can still see what the judge said.
        return {"error": "judge returned non-JSON", "raw": raw}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle", type=Path)
    args = ap.parse_args()
    if not args.bundle.exists():
        sys.exit(f"{args.bundle} does not exist.")
    result = asyncio.run(judge(args.bundle))
    print(f"=== {args.bundle.name} ===")
    for k in ("relevance", "fluency", "appropriate_length"):
        print(f"  {k:>22}: {result.get(k)}")
    reasoning = result.get("reasoning", "")
    print(f"  {'reasoning':>22}: {reasoning}")


if __name__ == "__main__":
    main()
