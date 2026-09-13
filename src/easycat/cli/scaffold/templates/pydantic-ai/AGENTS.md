# Agent Guide — $PROJECT_NAME

Guidance for coding agents (and humans) working in this scaffolded
EasyCat project. The agent is named "$AGENT_NAME"; its entry point is
`agent.py`. Keep `agent.py` import-safe: `make_agent()` / `make_config()`
build things, and only the `if __name__ == "__main__":` guard runs one.
Plain, SDK-free helpers belong in `tools.py`.

## Commands

```bash
uv sync                                       # Install dependencies
cp .env.example .env                          # Then fill in API keys
uv run easycat doctor --env-file .env         # Preflight checks
uv run easycat doctor --env-file .env --json  # Parseable preflight
uv run --env-file .env python agent.py  # Run
uv run pytest                                 # Offline tests (no API keys needed)
uv run ruff check agent.py tools.py       # Lint
uv run ruff check --fix agent.py tools.py # Auto-fix lint findings
uv run easycat docs                           # Maintained docs routes
```

## Testing and evals

`tests/test_agent.py` runs offline: `tools.py` runs for real, `agent.py`'s
wiring (name, instructions, tool list) is asserted as built behind
`pytest.importorskip("pydantic_ai")` with a `TestModel` injected into
`make_agent(...)`, and `ScriptedReasoning` stands in for the model while
EasyCat's real turn machinery (the send_text path, the audio pipeline,
journal, and latency metrics) runs end to end. `make_config()` is not called
offline: `EasyConfig` validates credentials at construction, so the offline
test stops at the agent. A green run means the app is wired and the pipeline
is healthy; it says nothing about live model quality — an offline stub cannot
decide which tool a model calls, so `assert_tool_called` belongs in a live or
bundle-backed test. Build on it with the helpers in `easycat.debug.testing`:

- `run_text_turn(agent_or_config, "...")` drives one real text turn
  and returns a journal-backed `TurnResult`.
- `run_text_turns(agent_or_config, ["...", "..."])` runs several turns
  against one session and returns one `TurnResult` per input.
- `run_scripted_audio_turn(agent)` drives one turn through the real
  audio pipeline with scripted stub I/O — it checks pipeline wiring,
  not speech quality.
- `assert_turn_completed` / `assert_no_error` / `assert_regex` /
  `assert_exact_match` / `assert_tool_called` check journal records —
  the same helpers work on exported debug bundles via `load_bundle`.
- `assert_latency(result, max_ms=...)` budgets turn latency at a
  percentile (p50/p90/p95/p99).
- `assert_llm_judge(result, ...)` scores conversational quality with
  an LLM rubric (needs `OPENAI_API_KEY` unless you inject `judge=`).

## Conventions

- Python >= 3.11, 4-space indent, 99-char lines (`uv run ruff check`).
- Keep secrets in `.env` (gitignored); commit `.env.example` updates
  instead so collaborators see every key they need.
- Run `uv run easycat doctor --env-file .env` before debugging
  provider issues by hand; run `uv run pytest` before and after edits.
