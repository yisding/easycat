"""Prove the stable postmortem journal view without providers.

This inspection probe runs a real text turn against EasyCat's echo agent, using
a full SQLite journal inside a temporary data directory. It observes the live
backend before stop, the preserved backend after stop, and a bundle exported
from the stopped session.

Run with::

    uv run python docs/teaching/15-operate-in-production/postmortem_probe.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from easycat import create_text_session
from easycat.debug.bundle import RunBundle


async def probe() -> dict[str, object]:
    previous_data_dir = os.environ.get("EASYCAT_DATA_DIR")
    session = None
    try:
        with TemporaryDirectory() as temp_dir:
            os.environ["EASYCAT_DATA_DIR"] = temp_dir
            session = create_text_session(
                agent=None,
                session_id="ch15-postmortem-probe",
                debug="full",
                journal_backend="sqlite",
            )
            view = session.journal
            assert view is not None

            backend_before = type(view._journal).__name__  # noqa: SLF001 — inspection probe
            append_before = hasattr(view, "append")
            response = await session.send_text("postmortem check")
            before = view.read()

            await session.stop()
            backend_after = type(view._journal).__name__  # noqa: SLF001 — inspection probe
            after = view.read()

            bundle_path = Path(temp_dir) / "postmortem.bundle"
            session.export_debug_bundle(bundle_path)
            bundle = RunBundle.load(bundle_path)
            bundled = list(bundle.records())

            return {
                "response": response,
                "journal_view": {
                    "type": type(view).__name__,
                    "same_object_after_stop": view is session.journal,
                    "append_exposed_before_stop": append_before,
                    "append_exposed_after_stop": hasattr(view, "append"),
                    "backend_before_stop": backend_before,
                    "backend_after_stop": backend_after,
                    "records_before_stop": len(before),
                    "records_after_stop": len(after),
                    "records_preserved": after[: len(before)] == before,
                },
                "bundle": {
                    "exported_after_stop": bundle_path.is_file(),
                    "record_count": len(bundled),
                    "matches_postmortem_view": [record.get("name") for record in bundled]
                    == [record.name for record in after],
                },
            }
    finally:
        if session is not None:
            await session.stop(force=True)
        if previous_data_dir is None:
            os.environ.pop("EASYCAT_DATA_DIR", None)
        else:
            os.environ["EASYCAT_DATA_DIR"] = previous_data_dir


def main() -> None:
    print(json.dumps(asyncio.run(probe()), indent=2))


if __name__ == "__main__":
    main()
