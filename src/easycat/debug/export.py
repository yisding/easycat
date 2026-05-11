"""Export API for debug bundles.

``export_debug_bundle`` is the primary entry point: given a Session
(or session-like object), it writes a portable ``.zip`` bundle
containing the journal, artifacts, and manifest metadata.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from easycat.debug.bundle import (
    FORMAT_VERSION,
    BundleExists,
    DebugCaptureDisabledError,
    Manifest,
)


def export_debug_bundle(
    session: Any,
    path: str | Path,
    *,
    inline_artifacts: bool = False,
    overwrite: bool = False,
) -> None:
    """Export a debug bundle from a running or cleanly stopped session."""
    path = Path(path)

    journal = getattr(session, "_journal", None) or getattr(session, "journal", None)

    # Infer debug mode: check explicit attributes first, then fall back to
    # whether a journal is present (real Session objects created by
    # create_session / create_text_session don't store a _debug attribute,
    # but they do store _journal when debug != "off").
    debug_mode = getattr(session, "_debug", None) or getattr(session, "debug", None)
    if debug_mode is None:
        debug_mode = "off" if journal is None else "light"
    if isinstance(debug_mode, str) and debug_mode == "off":
        raise DebugCaptureDisabledError("Debug capture is disabled (debug='off')")

    if path.exists() and not overwrite:
        raise BundleExists(f"Bundle already exists: {path}. Use overwrite=True to replace.")

    # Build journal NDJSON
    journal_lines: list[str] = []
    if journal is not None:
        records = journal.read() if hasattr(journal, "read") else []
        for record in records:
            journal_lines.append(json.dumps(_record_to_dict(record), default=str))
    journal_ndjson = "\n".join(journal_lines).encode("utf-8")

    # Collect artifacts. The refs are already content-addressed
    # SHA-256 hex digests produced by ``ArtifactStore.put`` — we just
    # copy the bytes; the bundle does not carry separate checksums.
    artifact_data: dict[str, bytes] = {}
    artifact_store = getattr(session, "_artifact_store", None)
    if artifact_store is not None:
        if hasattr(artifact_store, "_store"):
            # InMemoryArtifactStore — iterate the in-memory dict.
            for ref, data in artifact_store._store.items():
                raw = data if isinstance(data, bytes) else data.encode()
                artifact_data[ref] = raw
        elif hasattr(artifact_store, "_dir"):
            # FilesystemArtifactStore — read .bin files from disk.
            artifact_dir = artifact_store._dir
            if artifact_dir.is_dir():
                for f in artifact_dir.iterdir():
                    if f.suffix == ".bin" and f.is_file():
                        ref = f.stem
                        artifact_data[ref] = f.read_bytes()

    # Provider versions
    provider_versions = _collect_provider_versions(session)

    # Safe config snapshot (use safe_defaults allowlist)
    config_snapshot = _safe_config_snapshot(session)

    # Sharing banner
    try:
        from easycat.runtime.safe_defaults import DEV_BUNDLE_BANNER

        banner = DEV_BUNDLE_BANNER
    except ImportError:
        banner = "This debug bundle is for development use only."

    manifest = Manifest(
        format_version=FORMAT_VERSION,
        provider_versions=provider_versions,
        config_snapshot=config_snapshot,
        sharing_banner=banner,
    )

    manifest_dict = _manifest_to_dict(manifest)
    if inline_artifacts and artifact_data:
        manifest_dict["inline_artifacts"] = {
            ref: base64.b64encode(data).decode("ascii") for ref, data in artifact_data.items()
        }

    # Write zip atomically
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        tmp = tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False)
        tmp_name = tmp.name
        tmp.close()  # Release the fd; ZipFile will open the path itself.
        with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest_dict, indent=2))
            zf.writestr("journal.ndjson", journal_ndjson)
            if not inline_artifacts:
                for ref, data in artifact_data.items():
                    zf.writestr(f"artifacts/{ref}.bin", data)
        Path(tmp_name).rename(path)
    except Exception:
        if tmp_name and Path(tmp_name).exists():
            Path(tmp_name).unlink()
        raise


def _record_to_dict(record: Any) -> dict[str, Any]:
    """Convert a journal record to a JSON-safe dict."""
    value = _json_safe_value(record)
    return value if isinstance(value, dict) else record


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "value") and not isinstance(value, (str, bytes, int, float, bool)):
        return _json_safe_value(value.value)
    if dataclasses.is_dataclass(value):
        return {
            field.name: _json_safe_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if not field.name.startswith("_")
        }
    if isinstance(value, Mapping):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, frozenset):
        return sorted((_json_safe_value(v) for v in value), key=repr)
    if isinstance(value, set):
        return sorted((_json_safe_value(v) for v in value), key=repr)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe_value(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k: _json_safe_value(v) for k, v in value.__dict__.items() if not k.startswith("_")}
    return value


def _manifest_to_dict(manifest: Manifest) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for k, v in manifest.__dict__.items():
        if not k.startswith("_"):
            d[k] = v
    return d


def _collect_provider_versions(session: Any) -> dict[str, Any]:
    versions: dict[str, Any] = {}
    for attr in ("stt", "tts", "transport", "vad", "noise_reducer", "echo_canceller"):
        provider = getattr(session, attr, None)
        if provider is not None and hasattr(provider, "version_info"):
            try:
                versions[attr] = provider.version_info()
            except Exception:
                pass
    return versions


def _safe_config_snapshot(session: Any) -> dict[str, Any]:
    """Extract only allowlisted config fields, redacting secrets.

    Prefers ``_easycat_config`` (the original user-facing config) over
    ``_config`` (SessionConfig with live provider instances) so the
    snapshot captures meaningful settings like debug mode, journal
    backend, and turn-taking policy instead of ``<object at 0x…>``
    repr strings.
    """
    try:
        from easycat.runtime.safe_defaults import safe_config_snapshot

        config = getattr(session, "_easycat_config", None) or getattr(session, "_config", None)
        if config is None:
            return {}
        return safe_config_snapshot(config)
    except ImportError:
        return {}
