from __future__ import annotations

from tests.observability._observability_helpers import (
    _LATENCY_DEFAULT_ROW,
    _LATENCY_DOC,
    REPO_ROOT,
    _latency_config_classes,
    json,
    logging,
    pytest,
)


def test_observability_doc_lists_debugger_ui_entry_points() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    journal = doc.split("### C — ExecutionJournal", 1)[1].split(
        "### D — OpenTelemetry facade",
        1,
    )[0]

    for token in (
        "uv sync --extra debugger --group dev",
        "uv add 'easycat[debugger]'",
        "from easycat.debugger import serve_bundle, serve_session",
        'serve_bundle("runs/session.bundle", port=8765)',
        "serve_session(session, port=8765, in_thread=True)",
        "loopback-only by default",
        "allow_remote=True",
    ):
        assert token in journal


def test_observability_doc_tracks_logging_configuration_vocabulary() -> None:
    from easycat._logging import _JsonFormatter
    from easycat.config.easy import _EASYCAT_LOG_LEVELS, _VALID_DEBUG

    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    config = doc.split("## Configuration and orthogonality", 1)[1].split(
        "### Correlation ids in logs", 1
    )[0]
    record = logging.LogRecord(
        "easycat.tests",
        logging.INFO,
        __file__,
        1,
        "hello %s",
        ("world",),
        None,
    )
    record.session_id = "session-1"  # type: ignore[attr-defined]
    record.turn_id = "turn-1"  # type: ignore[attr-defined]
    json_fields = set(json.loads(_JsonFormatter().format(record)))

    missing_levels = sorted(level for level in _EASYCAT_LOG_LEVELS if f"`{level}`" not in config)
    missing_debug_modes = sorted(mode for mode in _VALID_DEBUG if f'"{mode}"' not in config)
    missing_json_fields = sorted(field for field in json_fields if f"`{field}`" not in config)

    assert not missing_levels, "Observability guide missing log levels: " + ", ".join(
        missing_levels
    )
    assert not missing_debug_modes, "Observability guide missing debug modes: " + ", ".join(
        missing_debug_modes
    )
    assert not missing_json_fields, "Observability guide missing JSON log fields: " + ", ".join(
        missing_json_fields
    )
    assert "`EASYCAT_LOG_FORMAT=json|text|human`" in config
    assert "`text` renders plain non-Rich text" in config
    assert "`human` renders the Rich-capable interactive formatter" in config
    assert "`EASYCAT_ENV=dev|prod`" in config
    assert "`prod` / `production` uses single-line JSON" in config
    assert "`exc`" in config


def test_observability_doc_tracks_error_note_context() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    journal = doc.split("### C — ExecutionJournal", 1)[1].split(
        "### D — OpenTelemetry facade",
        1,
    )[0]

    for token in (
        "PEP 678 exception notes",
        "`ErrorInfo.notes`",
        "`stage`",
        "`provider`",
        "`turn_id`",
        "`elapsed_ms`",
        "`sequence`",
        "`record_key`",
        "failing input",
        "`ExceptionGroup`",
        "both child errors",
    ):
        assert token in journal


def test_observability_doc_tracks_record_to_auto_capture() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    journal = doc.split("### C — ExecutionJournal", 1)[1].split(
        "### D — OpenTelemetry facade",
        1,
    )[0]

    for token in (
        '`record_to="runs"`',
        "`EasyConfig`",
        "`create_text_session(...)`",
        "timestamped debug bundle",
    ):
        assert token in journal


def test_observability_doc_tracks_advanced_config_knobs() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    config = doc.split("## Configuration and orthogonality", 1)[1].split(
        "### Correlation ids",
        1,
    )[0]
    caveats = doc.split("## Honesty caveats", 1)[1]
    config_text = " ".join(config.split())

    for token in (
        "`debugger_autolaunch=True`",
        "`warmup=False`",
        "safe debug-bundle config snapshots",
        "structural provider/model `warmup()` hooks",
        "`warmup_completed` timing records",
        "The bundled providers now implement those hooks",
    ):
        assert token in config_text

    assert "Latency is reported, not gated" in caveats
    assert "`turn_total_latency_ms`" in caveats
    assert "`text_turn_latency_ms`" in caveats
    assert "`easycat validate latency`" in caveats


def test_latency_docs_defaults_match_code() -> None:
    """Every default documented in docs/latency.md matches the code.

    The latency page promises operators a table of latency-adding
    defaults with their *current* values; a drifted number would send
    someone tuning the wrong knob.  Each ``ClassName.field`` row is
    resolved against the real dataclass default.
    """
    import dataclasses

    doc = _LATENCY_DOC.read_text(encoding="utf-8")
    rows = _LATENCY_DEFAULT_ROW.findall(doc)
    assert rows, "no `ClassName.field` | `value` rows found in docs/latency.md"

    classes = _latency_config_classes()
    for cls_name, field_name, doc_value in rows:
        assert cls_name in classes, f"docs/latency.md documents unknown class {cls_name}"
        fields = {f.name: f for f in dataclasses.fields(classes[cls_name])}
        assert field_name in fields, (
            f"docs/latency.md documents unknown field {cls_name}.{field_name}"
        )
        default = fields[field_name].default
        assert default is not dataclasses.MISSING, (
            f"{cls_name}.{field_name} has no plain default to document"
        )
        assert float(doc_value) == pytest.approx(float(default)), (
            f"docs/latency.md says {cls_name}.{field_name} defaults to {doc_value}, "
            f"but the code default is {default}"
        )


def test_latency_docs_cover_core_latency_defaults() -> None:
    """The big response-path knobs must stay documented."""
    doc = _LATENCY_DOC.read_text(encoding="utf-8")
    documented = {(cls, field) for cls, field, _value in _LATENCY_DEFAULT_ROW.findall(doc)}

    required = {
        ("TurnManagerConfig", "end_of_turn_silence_ms"),
        ("TurnManagerConfig", "pre_roll_ms"),
        ("TurnManagerConfig", "stt_segment_silence_ms"),
        ("VADConfig", "min_silence_duration_ms"),
        ("VADConfig", "min_speech_duration_ms"),
        ("SmartTurnConfig", "timeout_s"),
        ("AgentRunnerConfig", "timeout"),
    }
    missing = required - documented
    assert not missing, f"docs/latency.md dropped required latency defaults: {sorted(missing)}"


def test_latency_docs_name_the_cli_waterfall_milestones() -> None:
    """The doc explains the same milestone keys the CLI waterfall emits."""
    from easycat.debug._turn_timeline import turn_waterfall

    doc = _LATENCY_DOC.read_text(encoding="utf-8")
    record = {
        "sequence": 1,
        "turn_id": "t1",
        "name": "stt_final",
        "timing": {"wall_ns": 1},
    }
    (turn,) = turn_waterfall([record])
    for milestone_key in turn["milestones"]:
        assert f"`{milestone_key}`" in doc, (
            f"docs/latency.md does not document milestone {milestone_key}"
        )
