"""Require every production listener bind site to be reviewed and classified."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests.ratchets._bind_inventory import (
    CLASSIFICATIONS,
    BindSite,
    format_delta,
    inventory_delta,
    scan_bind_sites,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPO_ROOT / "src" / "easycat"
MANIFEST_PATH = Path(__file__).with_name("bind-manifest.json")


def test_bind_manifest_is_a_classified_source_bijection(pytestconfig: pytest.Config) -> None:
    findings = scan_bind_sites(SOURCE_ROOT)
    actual = {finding.site for finding in findings}
    manifest = _load_manifest() if MANIFEST_PATH.exists() else {"entries": []}
    if pytestconfig.getoption("--update-baseline"):
        _write_updated_manifest(
            actual,
            manifest=manifest,
            update_rationale=_required_rationale(pytestconfig),
        )
        manifest = _load_manifest()

    entries = [_parse_entry(record) for record in manifest["entries"]]
    expected = {site for site, _classification, _rationale in entries}
    added, removed = inventory_delta(expected, actual)
    assert not added and not removed, (
        format_delta(added, removed, findings=findings)
        + "\nUse --update-baseline --baseline-rationale 'reviewed reason' to refresh "
        "the source skeleton, then classify every new bind site."
    )

    unclassified = [
        site.as_record()
        for site, classification, rationale in entries
        if classification not in CLASSIFICATIONS or not rationale.strip()
    ]
    assert not unclassified, (
        "Classify every bind-manifest entry and give its rationale:\n  "
        + "\n  ".join(unclassified)
    )
    assert manifest["counts"] == _counts(entries)


def test_scanner_resolves_backend_and_socket_binder_aliases(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "easycat"
    _write_module(
        source_root,
        "sample.py",
        """
import socket as sockets
import websockets as ws
from aiohttp import web
from websockets.asyncio.server import serve as imported_ws_serve

async def run():
    sock = sockets.socket(sockets.AF_INET, sockets.SOCK_STREAM)
    binder = sock.bind
    websocket_binder = ws.serve
    site_factory = web.TCPSite
    app_runner = web.run_app
    aioquic_server = load_backend()
    quic_binder = aioquic_server.serve
    await websocket_binder(handler, "127.0.0.1", 0)
    await imported_ws_serve(handler, "127.0.0.1", 0)
    site_factory(runner, "127.0.0.1", 0)
    app_runner(app, host="127.0.0.1", port=0)
    await quic_binder("127.0.0.1", 0)
    binder(("127.0.0.1", 0))

def bind_injected_socket(sock):
    injected_binder = sock.bind
    injected_binder(("127.0.0.1", 0))
""",
    )

    findings = scan_bind_sites(source_root)

    assert Counter(finding.site.backend for finding in findings) == {
        "aiohttp_run_app": 1,
        "aiohttp_tcp_site": 1,
        "aioquic_serve": 1,
        "socket_bind": 2,
        "websockets_serve": 2,
    }


def test_unrelated_bind_method_is_not_a_socket_bind(tmp_path: Path) -> None:
    source_root = tmp_path / "src" / "easycat"
    _write_module(
        source_root,
        "sample.py",
        """
def attach(scope, runtime_scope):
    scope.bind(runtime_scope)
    binder = scope.bind
    binder(runtime_scope)
""",
    )

    assert scan_bind_sites(source_root) == []


def test_new_bind_in_new_or_existing_file_changes_inventory(tmp_path: Path) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    after_root = tmp_path / "after" / "src" / "easycat"
    source = "import websockets\nasync def run():\n    await websockets.serve(handler, 'x', 1)\n"
    _write_module(before_root, "existing.py", source)
    _write_module(
        after_root,
        "existing.py",
        source.replace(
            "    await websockets.serve(handler, 'x', 1)\n",
            "    await websockets.serve(handler, 'x', 1)\n"
            "    await websockets.serve(another_handler, 'y', 2)\n",
        ),
    )
    _write_module(
        after_root,
        "new.py",
        "from aiohttp import web\nsite = web.TCPSite(runner, 'x', 1)\n",
    )

    before = {finding.site for finding in scan_bind_sites(before_root)}
    after = {finding.site for finding in scan_bind_sites(after_root)}
    added, removed = inventory_delta(before, after)

    assert [site.backend for site in added] == ["aiohttp_tcp_site", "websockets_serve"]
    assert {site.path for site in added} == {"existing.py", "new.py"}
    assert not removed


def test_changed_binder_statement_changes_location_free_hash(tmp_path: Path) -> None:
    before_root = tmp_path / "before" / "src" / "easycat"
    after_root = tmp_path / "after" / "src" / "easycat"
    source = "import websockets\nasync def run():\n    await websockets.serve(handler, host, 1)\n"
    _write_module(before_root, "sample.py", source)
    _write_module(after_root, "sample.py", source.replace("host, 1", "replacement, 2"))

    before = {finding.site for finding in scan_bind_sites(before_root)}
    after = {finding.site for finding in scan_bind_sites(after_root)}
    added, removed = inventory_delta(before, after)

    assert len(added) == len(removed) == 1
    assert added[0].ast_hash != removed[0].ast_hash


def test_manifest_update_preserves_reviews_and_marks_new_sites(tmp_path: Path) -> None:
    reviewed = BindSite(
        "socket_bind",
        "reviewed.py",
        "probe",
        "call @socket.bind",
        "aaaaaaaaaaaaaaaa",
        0,
    )
    added = BindSite(
        "websockets_serve",
        "new.py",
        "serve",
        "call websockets.serve",
        "bbbbbbbbbbbbbbbb",
        0,
    )
    manifest = {
        "entries": [f"{reviewed.as_record()}\tloopback_probe\tReviewed loopback-only probe."]
    }
    manifest_path = tmp_path / "manifest.json"

    _write_updated_manifest(
        {reviewed, added},
        manifest=manifest,
        update_rationale="Deliberate fixture update",
        manifest_path=manifest_path,
    )

    updated = json.loads(manifest_path.read_text(encoding="utf-8"))
    parsed = {_parse_entry(record)[0]: _parse_entry(record)[1:] for record in updated["entries"]}
    assert parsed[reviewed] == ("loopback_probe", "Reviewed loopback-only probe.")
    assert parsed[added] == ("unclassified", "")
    assert updated["counts"]["classifications"] == {
        "loopback_probe": 1,
        "unclassified": 1,
    }


def _load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _parse_entry(record: object) -> tuple[BindSite, str, str]:
    parts = str(record).split("\t", maxsplit=7)
    assert len(parts) == 8, f"Malformed bind-manifest entry: {record!r}"
    return BindSite.from_record("\t".join(parts[:6])), parts[6], parts[7]


def _counts(entries: list[tuple[BindSite, str, str]]) -> dict[str, dict[str, int]]:
    return {
        "backends": dict(
            sorted(Counter(site.backend for site, _classification, _reason in entries).items())
        ),
        "classifications": dict(
            sorted(Counter(classification for _site, classification, _reason in entries).items())
        ),
    }


def _required_rationale(pytestconfig: pytest.Config) -> str:
    rationale = str(pytestconfig.getoption("--baseline-rationale") or "").strip()
    if not rationale:
        raise pytest.UsageError("--update-baseline requires a non-empty --baseline-rationale")
    return rationale


def _write_updated_manifest(
    actual: set[BindSite],
    *,
    manifest: dict[str, Any],
    update_rationale: str,
    manifest_path: Path = MANIFEST_PATH,
) -> None:
    prior = {
        site: (classification, rationale)
        for site, classification, rationale in (
            _parse_entry(record) for record in manifest.get("entries", [])
        )
    }
    entries = [(site, *prior.get(site, ("unclassified", ""))) for site in sorted(actual)]
    payload = {
        "version": 1,
        "update_rationale": update_rationale,
        "counts": _counts(entries),
        "entries": [
            f"{site.as_record()}\t{classification}\t{rationale}"
            for site, classification, rationale in entries
        ],
    }
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_module(source_root: Path, relative_path: str, source: str) -> None:
    path = source_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source.lstrip(), encoding="utf-8")
