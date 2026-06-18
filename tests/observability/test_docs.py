from __future__ import annotations

from tests.observability._observability_helpers import (
    _LATENCY_DEFAULT_ROW,
    _LATENCY_DOC,
    REPO_ROOT,
    _latency_config_classes,
    json,
    logging,
    pytest,
    re,
)


def test_observability_doc_explains_journal_redaction_boundary() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    caveats = " ".join(doc.split("## Honesty caveats", 1)[1].split())

    assert "safe config/environment snapshots" in caveats
    assert "selected agent-bridge metadata" in caveats
    assert "obvious secret-like journal fields through `apply_write_filter`" in caveats
    assert "transcript text, agent output, and tool-result text for replay" in caveats


def test_observability_doc_lists_journal_cli_entry_points() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    journal = doc.split("### C — ExecutionJournal", 1)[1].split(
        "### D — OpenTelemetry facade",
        1,
    )[0]

    for command in (
        "easycat bundles list",
        "easycat bundles list --json",
        "easycat bundles show <path>",
        "easycat bundles show <path> --json",
        "easycat inspect <path>",
        "easycat inspect <path> --json",
        "easycat replay <path>",
        "easycat replay <path> --json",
        "easycat bundles export <path>",
        "easycat bundles export <path> --output DIR --json",
    ):
        assert command in journal
    assert "parseable summary" in journal


def test_observability_doc_points_operators_to_filtered_docs_route() -> None:
    doc = (REPO_ROOT / "docs" / "observability.md").read_text(encoding="utf-8")
    intro = doc.split("## The four layers", 1)[0]
    normalized_intro = re.sub(r"\s+", " ", intro)

    assert "uv run easycat docs --audience operators" in intro
    assert "uv run easycat docs --audience operators --json" in intro
    assert "operator-facing route slice" in intro
    assert "same operator map with command hints" in intro
    assert "deployment, observability, and journal durability" in normalized_intro


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
        "`ObservabilityConfig`",
        "`latency_budget=LatencyBudget(...)`",
        "`warmup=False`",
        "`max_session_cost_usd=0.50`",
        "safe debug-bundle config snapshots",
        '`LatencyBudget(stage="tts", max_ms=500)`',
        "`latency_budget_exceeded`",
        '`LatencyBudget(stage="total_ms", max_ms=...)`',
        "turn-level `latency_budget_exceeded` metric records",
        "`max_session_cost_usd` budget status from cost records",
        "`easycat.runtime.cost_budget_status(...)`",
        "`cost_budget_warning` / `cost_budget_exceeded`",
        "`cost_budget_stop_requested`",
        "`stop(force=True)`",
        "structural provider/model `warmup()` hooks",
        "`warmup_completed` timing records",
        "`stt_final_latency_ms`",
        "`first_audio_ms`",
        ("Provider cost-record emission and provider-specific warmup coverage are still planned"),
    ):
        assert token in config_text

    assert "tag and alert, but they do not reject turns yet" in caveats
    assert '`latency_budget=LatencyBudget(stage="tts", max_ms=500)`' in caveats
    assert "`latency_budget_violations`" in caveats
    assert "`total_ms`" in caveats
    assert "`llm_ttft_ms`" in caveats
    assert "`tts_ttfb_ms`" in caveats


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
