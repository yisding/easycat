"""Architecture contracts for debugger HTTP route groups."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from easycat.debugger._core_routes import register_core_routes
from easycat.debugger._sources import DebuggerSource


class _RecordingRouter:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []

    def add_get(self, path: str, handler: Any) -> None:
        self.routes.append(("GET", path, handler))


def test_core_route_group_owns_read_only_source_endpoints() -> None:
    router = _RecordingRouter()
    app = SimpleNamespace(router=router)
    source = DebuggerSource(
        label="test",
        _records_fn=lambda: [],
        _progress_fn=lambda: (0, 0),
        _artifact_fn=lambda _ref: None,
        _manifest_fn=lambda: {},
        _bundle_fn=None,
        _replay_fn=None,
        is_live=False,
    )

    register_core_routes(
        app,
        source,
        static_dir=Path("static"),
        web=SimpleNamespace(),
    )

    assert [(method, path) for method, path, _handler in router.routes] == [
        ("GET", "/"),
        ("GET", "/api/manifest"),
        ("GET", "/api/records"),
        ("GET", "/api/turns"),
        ("GET", "/api/timeline"),
        ("GET", "/api/transcript"),
        ("GET", "/api/issues"),
        ("GET", "/api/artifact/{ref}"),
    ]
