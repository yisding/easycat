"""Chapter 7 — Record inspectable EasyCat journals and bundles.

Dependencies:
    uv sync --group dev
    uv sync --extra debugger --group dev  # browser UI only

Run:
    uv run python docs/using-easycat/07-observability/main.py record PATH.bundle
    uv run python docs/using-easycat/07-observability/main.py pair OUTPUT_DIR
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from easycat import create_text_session

Mode = Literal["record", "pair"]
Variant = Literal["baseline", "candidate"]


@dataclass(frozen=True)
class Options:
    mode: Mode
    output: Path
    variant: Variant


class SupportWorkflow:
    def __init__(self, variant: Variant) -> None:
        self.variant = variant

    async def on_user_turn(self, text: str) -> str:
        lowered = text.casefold()
        if "hour" in lowered or "open" in lowered:
            if self.variant == "baseline":
                return "Support is open from nine to five Pacific time."
            return "Support is open from eight to six Pacific time."
        if "contact" in lowered or "email" in lowered:
            if self.variant == "baseline":
                return "Email support@example.com."
            return "Use the help center before emailing support@example.com."
        return "I can explain support hours and contact options."


def parse_options() -> Options:
    parser = argparse.ArgumentParser(
        description="Record one debug bundle or a baseline/candidate pair."
    )
    parser.add_argument("mode", choices=("record", "pair"))
    parser.add_argument("output", type=Path, help="Bundle path or output directory.")
    parser.add_argument("--variant", choices=("baseline", "candidate"), default="baseline")
    args = parser.parse_args()
    return Options(
        mode=cast(Mode, args.mode),
        output=args.output,
        variant=cast(Variant, args.variant),
    )


async def record_bundle(path: Path, variant: Variant) -> None:
    session = create_text_session(agent=SupportWorkflow(variant), debug="full")
    async with session:
        await session.send_text("What are your support hours?")
        await session.send_text("How do I contact support?")

    records = list(session.journal.read())
    turn_ids = {record.turn_id for record in records if record.turn_id}
    session.export_debug_bundle(str(path), overwrite=True)
    print(f"{variant}: {len(records)} records, {len(turn_ids)} turns -> {path}")


async def run(options: Options) -> None:
    if options.mode == "record":
        await record_bundle(options.output, options.variant)
        return
    await record_bundle(options.output / "baseline.bundle", "baseline")
    await record_bundle(options.output / "candidate.bundle", "candidate")


def main() -> None:
    asyncio.run(run(parse_options()))


if __name__ == "__main__":
    main()
