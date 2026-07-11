"""Read-only debugger HTTP routes.

This module deliberately receives aiohttp's ``web`` namespace from the app
composer. Importing debugger helpers therefore remains safe when the optional
debugger extra is not installed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from easycat.debug._issues import build_issues
from easycat.debug._turn_timeline import summarise_turns, turn_waterfall
from easycat.debugger._records import (
    _UNSAFE_REGEX_MESSAGE,
    _build_transcript,
    _filter_and_paginate,
    _filter_records,
    _search_records,
)
from easycat.debugger._sources import DebuggerSource, _safe_ref

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RecordsQuery:
    stage: str | None
    turn_id: str | None
    names: tuple[str, ...]
    from_seq: int | None
    to_seq: int | None
    search: str | None
    use_regex: bool
    errors_only: bool
    limit: int | None
    offset: int


def _parse_records_query(params: Any) -> _RecordsQuery:
    try:
        from_seq = int(params["from"]) if "from" in params else None
        to_seq = int(params["to"]) if "to" in params else None
        limit = int(params["limit"]) if "limit" in params else None
        offset = int(params["offset"]) if "offset" in params else 0
    except ValueError as exc:
        raise ValueError("from/to/limit/offset must be integers") from exc

    if offset < 0 or (limit is not None and limit <= 0):
        raise ValueError("invalid query parameters")

    return _RecordsQuery(
        stage=params.get("stage") or None,
        turn_id=params.get("turn") or None,
        names=tuple(name for name in params.getall("name", ()) if name),
        from_seq=from_seq,
        to_seq=to_seq,
        search=params.get("q") or None,
        use_regex=params.get("regex") == "1",
        errors_only=params.get("errors") == "1",
        limit=limit,
        offset=offset,
    )


@dataclass(slots=True)
class _CoreRoutes:
    """Handlers for the debugger's read-only source inspection API."""

    source: DebuggerSource
    static_dir: Path
    web: Any

    async def index(self, _request: Any) -> Any:
        return self.web.FileResponse(self.static_dir / "index.html")

    async def manifest(self, _request: Any) -> Any:
        return self.web.json_response(self.source.manifest())

    async def records(self, request: Any) -> Any:
        try:
            query = _parse_records_query(request.query)
        except ValueError as exc:
            logger.warning("Invalid records query: %s", exc)
            return self.web.Response(status=400, text=str(exc))

        scan_truncated = False
        try:
            if query.search is None:
                page, total = _filter_and_paginate(
                    self.source.records(),
                    stage=query.stage,
                    turn_id=query.turn_id,
                    name=query.names or None,
                    from_seq=query.from_seq,
                    to_seq=query.to_seq,
                    errors_only=query.errors_only,
                    limit=query.limit,
                    offset=query.offset,
                )
            else:
                subset = _filter_records(
                    self.source.records(),
                    stage=query.stage,
                    turn_id=query.turn_id,
                    name=query.names or None,
                    from_seq=query.from_seq,
                    to_seq=query.to_seq,
                    errors_only=query.errors_only,
                    limit=None,
                    offset=0,
                )
                matched, scan_truncated = await asyncio.to_thread(
                    _search_records,
                    subset,
                    query=query.search,
                    use_regex=query.use_regex,
                )
                total = len(matched)
                page = matched[query.offset :]
                if query.limit is not None:
                    page = page[: query.limit]
        except ValueError as exc:
            logger.warning("Invalid records query: %s", exc)
            if str(exc) in {"invalid regex", _UNSAFE_REGEX_MESSAGE}:
                text = str(exc)
            else:
                text = "invalid query parameters"
            return self.web.Response(status=400, text=text)
        return self.web.json_response(
            {
                "records": page,
                "page_size": len(page),
                "total": total,
                "offset": query.offset,
                "limit": query.limit,
                "scan_truncated": scan_truncated,
            }
        )

    async def turns(self, _request: Any) -> Any:
        return self.web.json_response({"turns": summarise_turns(self.source.records())})

    async def timeline(self, _request: Any) -> Any:
        return self.web.json_response({"timeline": turn_waterfall(self.source.records())})

    async def transcript(self, _request: Any) -> Any:
        return self.web.json_response({"transcripts": _build_transcript(self.source.records())})

    async def issues(self, _request: Any) -> Any:
        return self.web.json_response(
            build_issues(
                self.source.records(),
                artifact_resolver=self.source.artifact_for_analysis,
            )
        )

    async def artifact(self, request: Any) -> Any:
        try:
            ref = _safe_ref(request.match_info["ref"])
        except ValueError:
            return self.web.Response(status=400, text="invalid artifact ref")
        blob = self.source.artifact(ref)
        if blob is None:
            return self.web.Response(status=404, text=f"artifact {ref} not found")
        return self.web.Response(body=blob, content_type="application/octet-stream")


def register_core_routes(
    app: Any,
    source: DebuggerSource,
    *,
    static_dir: Path,
    web: Any,
) -> None:
    """Register the debugger's source-inspection routes on *app*."""
    routes = _CoreRoutes(source=source, static_dir=static_dir, web=web)
    app.router.add_get("/", routes.index)
    app.router.add_get("/api/manifest", routes.manifest)
    app.router.add_get("/api/records", routes.records)
    app.router.add_get("/api/turns", routes.turns)
    app.router.add_get("/api/timeline", routes.timeline)
    app.router.add_get("/api/transcript", routes.transcript)
    app.router.add_get("/api/issues", routes.issues)
    app.router.add_get("/api/artifact/{ref}", routes.artifact)
