# Agent Guide — $PROJECT_NAME

Guidance for coding agents (and humans) working in this scaffolded
EasyCat provider package. The custom provider lives in `custom_vad.py`;
`agent.py` tries it in a live mic session with the agent named
"$AGENT_NAME".

## Commands

```bash
uv sync                                       # Install dependencies
cp .env.example .env                          # Then fill in API keys
uv run easycat doctor --env-file .env         # Preflight checks
uv run easycat doctor --env-file .env --json  # Parseable preflight
uv run --env-file .env python agent.py  # Run
uv run pytest                                 # Offline tests (no API keys needed)
uv run ruff check agent.py custom_vad.py test_custom_vad.py           # Lint
uv run ruff check --fix agent.py custom_vad.py test_custom_vad.py     # Auto-fix lint findings
uv run easycat docs                           # Maintained docs routes
```

## Testing and evals

`test_custom_vad.py` pins the provider contract: `EnergyVAD` must keep
satisfying EasyCat's `VADProvider` Protocol and only emit events on
speech-state transitions. `tests/test_agent.py` runs offline: a
deterministic stub agent stands in for the LLM while EasyCat's real
turn machinery (the send_text path, journal, and latency metrics) runs
end to end. Build on it with the helpers in `easycat.debug.testing`:

- `run_text_turn(agent_or_config, "...")` drives one real text turn
  and returns a journal-backed `TurnResult`.
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
