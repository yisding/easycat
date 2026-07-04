# Code Quality — Priority Bugs (High + Medium)

The correctness defects worth fixing first. Every one was traced through the actual code by an
adversarial verifier. Where a sibling implementation "does it right," it is cited so the fix can
mirror an in-repo reference rather than invent one.

| # | Sev | File:line | Bug | Fix approach |
|---|-----|-----------|-----|--------------|
| 1 | High | `integrations/agents/_openai_agents_events.py:63` | Tool-result `call_id` read via `getattr` off a dict → always `""` | Mirror SDK's `ToolCallOutputItem.call_id` accessor |
| 2 | High | `integrations/agents/openai_agents.py:205` | Barge-in never `result.cancel()`s the streamed run | Call `result.cancel()`; mirror `LlamaAgentsBridge` |
| 3 | Med | `turn_manager.py:603` | PTT press during PROCESSING silently dropped | Route to `_handle_barge_in()` |
| 3c | Med | `config/easy.py:827` | `enable_echo_cancellation=True` silently ignored | Fold flag into config in `__post_init__` |
| 4 | Med | `cli/debug/bundles.py:2388` | `tail`/`follow` retry re-emits/drops records | Resume from last yielded sequence |
| 5 | Med | `runtime/journal_sql.py:1018` | Libsql sync thread bypasses single-writer lock | Acquire `self._lock` around `conn.sync()` |
| 6 | Med | `turn_manager.py:603` | (see #3 — same guard) | — |
| 7 | Med | `validation/runner.py:196` | Lane skips exact secret redaction on stdout/stderr | Apply `redact_runtime_secrets` like sibling lanes |
| 11 | Med | `integrations/agents/_agent_runner.py:199` | `aclose()` not propagated down generator chain | `try/finally` aclose in AgentStage + AgentRunner |
| 12 | Med | `stt/elevenlabs_provider.py:555` | Batch STT has zero retry | Shared bounded-retry helper + `max_retries` |
| 13 | Med | `runtime/journal_sql.py:319` | Crash-dump promotion can crash journal startup | Move `mkdir` into `try`, broaden `except` |
| 14 | Med | `config/_telephony_wiring.py:233` | Outbound manager silently skipped on blank creds | Validate + fail-fast (or at least warn) |

> **Anchor note:** line numbers are from the audit snapshot; re-confirm against the file before
> editing (the repo moves fast). The described code, not the exact line, is authoritative.

---

## Bug #1 — OpenAI Agents tool-result `call_id` is always empty (High)

- **Severity / Category:** High / bug
- **Location:** `integrations/agents/_openai_agents_events.py:63` (`map_run_item`); pairs with #2
  and the dead flag #24

**Failure scenario.** On any turn where the agent calls a tool, a barge-in during that turn causes
the bridge to consume the *entire* remaining SDK run instead of stopping — the drain condition can
never fire. Every emitted `tool_result` event ships with `call_id=""`, so start/result can't be
paired and the journal records an empty tool name.

**Root cause.** `map_run_item` handles `tool_call_output_item` with
`raw = getattr(item, "raw_item", None); call_id = getattr(raw, "call_id", "")`. In the installed SDK
(0.17.5) `ToolCallOutputItem.raw_item` is a `FunctionCallOutput` **dict** at runtime
(`ItemHelpers.tool_call_output_item` returns a literal `{"call_id":…, "output":…, "type":"function_call_output"}`),
so `getattr` on a dict returns `""` every time. The `tool_call_item` branch stores the real call_id
as the pending-dict key (its `raw_item` is a pydantic model), so `pending.pop(call_id)` never
matches and `if not pending_tool_calls: break` (`openai_agents.py:197`) can't fire. The two existing
tests mask this by using attribute-bearing fakes (`SimpleNamespace(call_id=…)`).

**Fix steps.**
1. Mirror the SDK's own `ToolCallOutputItem.call_id` accessor. Read `call_id` robustly:
   `getattr(item, "call_id", None)` first, then if `raw` is a dict `raw.get("call_id") or raw.get("id")`,
   else `getattr(raw, "call_id", "")`, finally `or ""`.
2. Confirm the `tool_call_item` branch's pending-key derivation matches (it reads a pydantic model,
   so it's already correct) — the pop will now match.

**Regression test.** Add a test whose `raw_item` is a **plain dict** matching `FunctionCallOutput`,
asserting (a) the emitted result event carries the real id, and (b) `pending` empties after the
matching output. Do not use an attribute-bearing fake — that's exactly what hid the bug.

**Validation.** `uv run pytest tests/integrations/agents/` (the OpenAI Agents bridge tests).

**Risk & rollback.** Low; the accessor is strictly more correct. Land before/with #2.

---

## Bug #2 — OpenAIAgentsBridge never cancels the streamed run on barge-in (High)

- **Severity / Category:** High / bug
- **Location:** `integrations/agents/openai_agents.py:205`; **reference implementation:**
  `integrations/agents/llama_agents.py:470-496` (does this correctly and names the hazard)

**Failure scenario.** On cancel-token cancellation, `invoke()` just `break`s out of
`result.stream_events()` and never calls `result.cancel()`. `Runner.run_streamed` runs the agent
loop in a background task; abandoning the generator doesn't stop it. When the generator is
GC-finalized, the SDK's `finally` takes the non-cancelled path and **awaits `run_loop_task` to
completion** — so the interrupted run always finishes: remaining tool side-effects fire *after* the
user cut the bot off, tokens are billed, and the `finally` snapshots `to_input_list()` /
`last_response_id` from a still-mutating run, feeding `apply_interruption` and next-turn
`previous_response_id` nondeterministic state.

**Root cause.** The primary barge-in path is cooperative (`CancelToken.cancel()`, not task
cancellation), so the SDK's cancel-on-`CancelledError` arm never fires; the bridge must explicitly
cancel.

**Fix steps.**
1. In the cancel branch, call `result.cancel()` **before** breaking — use `mode="after_turn"` when
   `pending_tool_calls` exist (drain in-flight tools), else `"immediate"`.
2. Add a `GeneratorExit`/`CancelledError` arm mirroring `llama_agents.py:470-496`.
3. Keep consuming `stream_events()` as the SDK requires after cancel.
4. Capture `to_input_list()` / `last_response_id` **only after** cancellation settles.
5. Delete the now-consistent dead `interrupted` flag (#24) — it is the residue of this dropped guard.

**Regression test.** Drive a tool-using turn, cancel mid-run, and assert `result.cancel()` was
called and no post-cancel tool side-effects fire; assert the captured input-list is stable.

**Validation.** `uv run pytest tests/integrations/agents/`.

**Risk & rollback.** Medium — touches interruption semantics. Mirror the Llama bridge closely and
lean on the regression test. **Do not** blindly port the `responses_api` chain-state guard (this
bridge intentionally chains-then-annotates).

---

## Bug #3 — Push-to-talk press during PROCESSING is silently dropped (Med)

- **Severity / Category:** Medium / bug
- **Location:** `turn_manager.py:603` (`start_turn`, the PTT public API)

**Failure scenario.** A PTT user presses to speak while the manager is in PROCESSING (agent
latency). `start_turn()` guards `if self._state not in (IDLE, BOT_SPEAKING): return` — a no-op: no
new turn, no cancel of the stale agent run; the user's speech survives only in the ~300 ms pre-roll.
The shipped `push_to_talk` toggle desyncs because it flips its `speaking` flag while the manager
no-ops.

**Root cause.** VAD mode treats the same situation as barge-in (`turn_manager.py:384-388 →
_handle_barge_in`), but the PTT path has no equivalent — and PROCESSING is exactly the window where
a PTT user most wants to retract, with no VAD to rescue them.

**Fix steps.**
1. Before the guard in `start_turn()`, add:
   `if self._state == TurnManagerState.PROCESSING: await self._handle_barge_in(); return`
   (consider `USER_PAUSED` too), cancelling the stale token and starting a fresh USER_SPEAKING turn.

**Regression test.** A PTT press during PROCESSING → asserts the stale token is cancelled and a new
USER_SPEAKING turn begins.

**Validation.** `uv run pytest tests/` for the turn_manager tests (e.g. `tests/test_turn_manager.py`
/ `tests/session/`).

**Risk & rollback.** Low-medium; coordinate with the #26 turn-start dedup (same file) — fix #3 first.

---

## Bug #3c — `enable_echo_cancellation` silently ignored (Med)

- **Severity / Category:** Medium / bug
- **Location:** `config/easy.py:827`; pipeline flag derived at `config/_factory.py:404-408`

**Failure scenario.** `EasyConfig.browser(echo_cancellation=EchoCancellationConfig(fallback_policy="error"))`
— a preset that promises AEC on and sets the flag — silently runs with AEC **off**
(`PassthroughAEC`), and the `fallback_policy="error"` hard-fail never fires. Reproduced at runtime.

**Root cause.** `enable_echo_cancellation`'s only reader is
`_default_echo_cancellation_for_transport`, called *only* when `self.echo_cancellation is None`.
`create_session` then derives the pipeline flag purely from `EchoCancellationConfig.enabled` (which
defaults to `False`), so a supplied config object ignores the flag entirely.

**Fix steps.**
1. In `__post_init__`, when `enable_echo_cancellation is not None` and `echo_cancellation` is a
   config object, `replace(cfg, enabled=flag)`.
2. Warn or raise on a conflict with a pre-built `EchoCanceller` instance.

**Regression test.** Assert `EasyConfig.browser(enable_echo_cancellation=True,
echo_cancellation=EchoCancellationConfig(...)).echo_cancellation.enabled is True`.

**Validation.** `uv run pytest tests/config/`.

**Risk & rollback.** Low.

---

## Bug #4 — `tail`/`journal follow` retry restarts from the original sequence (Med)

- **Severity / Category:** Medium / bug
- **Location:** `cli/debug/bundles.py:2388` (`_runner` wrapping `_stream_follow`)

**Failure scenario.** On a mid-stream `FileNotFoundError` / `sqlite3.OperationalError` ("database is
locked" against a file a live session is writing), `_runner` re-calls `_stream_follow` with the
*original* `from_sequence`. With `--from-sequence 0` it re-emits every already-printed record;
default `None` resumes at `latest_sequence+1` computed at retry time, silently **skipping** records
written during the outage with no `follow_gap` notice.

**Root cause.** `JournalView.follow` keeps its cursor in generator-local state; on restart that
cursor is lost and the original argument is reused.

**Fix steps.**
1. Have `_stream_follow` track the highest yielded `record.sequence`.
2. On retry, resume from `last_seq + 1`, keeping the original `from_sequence` only for the first
   attempt (before any record has streamed).

**Regression test.** Simulate an `OperationalError` mid-stream; assert no record is duplicated or
skipped across the retry boundary.

**Validation.** `uv run pytest tests/cli/test_bundles.py` (and `just guard-ops`).

**Risk & rollback.** Low. Coordinate with QS2 (bundles.py split) and bugs #35/#36 in the same file
— fix the bugs first, split after.

---

## Bug #5 — Libsql sync thread bypasses the single-writer lock (Med)

- **Severity / Category:** Medium / bug
- **Location:** `runtime/journal_sql.py:1018` (`_sync_loop`); lock discipline at `_do_append` (~978)

**Failure scenario.** With a remote `sync_url` configured, a daemon `_sync_loop` calls
`self._conn.sync()` every 10 s **without the lock** while the session thread may be mid-append
(commit pending) on the same libsql connection. `append()` treats any `_do_append` exception as
fatal (`_enter_degraded` → returns `-1` forever), so a race-induced error permanently drops every
subsequent record for the session — and observability is the single source of truth.

**Root cause.** The class's stated discipline is "single-writer via `threading.Lock`," but
`_sync_loop`, `flush()`, and `finalize()` touch the connection unlocked.

**Fix steps.**
1. Acquire `self._lock` around `conn.sync()` in `_sync_loop`, and around the connection access in
   `flush()` and `finalize()` (sync is short vs the 10 s interval).
2. Alternative: move syncing onto the append thread/queue.

**Regression test.** With a `sync_url`, append concurrently with sync ticks and assert no degraded
transition / no dropped records.

**Validation.** `uv run pytest tests/runtime/test_sqlite_journal.py` (and journal tests under
`tests/runtime/`).

**Risk & rollback.** Low; strictly adds locking. Independent of #13 (same file, different region).

---

## Bug #7 — Validation lane skips exact secret redaction (Med)

- **Severity / Category:** Medium / bug (security-adjacent)
- **Location:** `validation/runner.py:196` (`run_validation_slice`); sibling lanes at
  `run_latency`/`run_live`/`run_release_validation`

**Failure scenario.** A failing test echoes a plain-hex `DEEPGRAM_API_KEY`; it lands verbatim in
`stdout.log`/`stderr.log` and in `ValidationFailure.message`, which are shared/uploadable artifacts.
`run_release_validation` invokes this slice (~1109), so release inherits the gap.

**Root cause.** The lane computes `runtime_secret_values` (line 190) but applies them only to
`junit.xml`; stdout/stderr get pattern-only `redact_text()` (matching only `sk-/sess-/key-/tok-`
prefixes) and the failure message gets no exact-secret pass. Every other lane applies
`redact_runtime_secrets` to stdout/stderr and pre-redacts the message — this lane wiring the secrets
into only the junit write is the tell that it's an oversight.

**Fix steps.**
1. Change lines ~196-197 to `redact_runtime_secrets(result.stdout/stderr, runtime_secret_values)`.
2. Wrap the failure message (~250) with the same exact-secret redaction — matching the three sibling
   lanes exactly.

**Regression test.** Run the slice with a secret-valued env var echoed by a failing test; assert the
secret appears nowhere in stdout/stderr logs or `failures[].message`.

**Validation.** `just guard-validation` (`tests/cli/test_validate_runner.py` and related).

**Risk & rollback.** Low. Coordinate with QS5 (LaneHarness extraction) — fix #7 first so the shared
harness inherits correct redaction.

---

## Bug #11 — `aclose()` not propagated down the agent generator chain (Med)

- **Severity / Category:** Medium / error-handling
- **Location:** `integrations/agents/_agent_runner.py:199` (`AgentRunner.invoke`) and
  `AgentStage.execute_streaming`; **reference:** `llama_agents.py:182-194` (propagates `aclose` with
  a comment on this exact failure mode)

**Failure scenario.** On barge-in only the top stream is closed. Neither `AgentStage.execute_streaming`
(its `finally` never `aclose`s the wrapped `bridge.invoke(...)`) nor `AgentRunner.invoke`'s bridge
loop forwards the close, so `GeneratorExit` leaves the inner bridge suspended and finalized only by
the asyncgen GC hook — one loop tick per layer. Each bridge's `BaseException` arm carries
ordering-sensitive work: LangChain/LangGraph persist the *partial* turn so a follow-up
`apply_interruption` truncates *this* turn's assistant message. In the narrow window where the
consumer stops pulling before the bridge re-checks cooperative cancel, `apply_interruption` runs
first and rewrites the *prior* turn's message — intermittent, journal-visible next-turn corruption.

**Fix steps.**
1. Wrap the consumption loops in **both** `AgentStage.execute_streaming` and `AgentRunner.invoke` in
   `try/finally` that awaits `stream.aclose()` / `inner_iter.aclose()` (generalize the timeout-only
   aclose that already exists on one branch). No-op on normal completion.

**Regression test.** Drive `AgentStage → AgentRunner → LangChainBridge` where the consumer breaks on
the first post-cancel delta; assert the bridge's cleanup runs before `apply_interruption` and the
prior turn's message is untouched.

**Validation.** `uv run pytest tests/integrations/agents/`.

**Risk & rollback.** Medium — interruption ordering. Test-heavy. Related to bug #2's cluster.

---

## Bug #12 — ElevenLabs batch STT has zero retry (Med)

- **Severity / Category:** Medium / error-handling
- **Location:** `stt/elevenlabs_provider.py:555` (`_transcribe_batch`); **reference:**
  `stt/openai_provider.py:168-196` (retries 429/`TransportError`/`TimeoutException` with backoff)

**Failure scenario.** On the mid-stream cap → finalize path, a transient 429 from a single unretried
`client.post` + `raise_for_status` propagates into the per-chunk pipeline error policy the finalize
mechanism exists to avoid — discarding an utterance the OpenAI provider would have kept.

**Root cause.** `ElevenLabsSTT._transcribe_batch` does one unretried POST; `ElevenLabsSTTConfig` has
no `max_retries` field.

**Fix steps.**
1. Hoist the bounded retry loop into a shared `stt/base.py` helper.
2. Have `_transcribe_batch` call it.
3. Add `max_retries: int = 3` (with `>= 0` validation) to `ElevenLabsSTTConfig`.

**Regression test.** Mock a transient 429 followed by success; assert the utterance survives.

**Validation.** `uv run pytest tests/stt/` (ElevenLabs + shared base tests).

**Risk & rollback.** Low. Coordinate with #42 (same config class — add both fields together) and #31
(batch-flush dedup) after.

---

## Bug #13 — Crash-dump promotion can crash journal startup (Med)

- **Severity / Category:** Medium / error-handling
- **Location:** `runtime/journal_sql.py:319` (`_promote_crash_dump`)

**Failure scenario.** A bot can't restart after a crash — precisely when robust startup matters.
Two escapes break the "best-effort" contract: `mkdir_private(crash_dir)` (319) runs **before** the
`try` (326), so an `OSError` (full/read-only FS, or `crash-dumps` existing as a file) propagates out
of `SqliteJournal.__init__`; and inside the `try`, `_copy_journal_to_crash_dump` only swallows
`sqlite3.OperationalError`, so a `sqlite3.DatabaseError` from a malformed WAL (exactly the crash
being recovered) escapes the `except OSError`.

**Root cause.** `mkdir` outside the `try`; `except` narrower than the exceptions actually raised.
The authors already wrap the *sweep* call correctly (`except (OSError, sqlite3.DatabaseError)` with
`mkdir_private` inside its `try`) — this path just diverged.

**Fix steps.**
1. Move `mkdir_private` inside the `try`.
2. Broaden the handler to `except (OSError, sqlite3.Error)`, routing to
   `_reopen_after_failed_crash_dump`.

**Regression test.** Seed an unclean same-id journal whose checkpoint raises `DatabaseError`; assert
`SqliteJournal.__init__` succeeds (degrades, doesn't raise).

**Validation.** `uv run pytest tests/runtime/test_sqlite_journal.py`.

**Risk & rollback.** Low; strictly widens error tolerance on a best-effort path.

---

## Bug #14 — Outbound call manager silently skipped on blank Twilio creds (Med)

- **Severity / Category:** Medium / error-handling
- **Location:** `config/_telephony_wiring.py:233` (`_build_outbound_helpers`)

**Failure scenario.** With `enable_outbound_call_manager=True` but blank creds (both fields default
to `""` with no env auto-load — the common misconfiguration), `create_session` succeeds, then the
app fails much later with a `NoneType` `AttributeError` when it reaches for the manager.

**Root cause.** `OutboundCallManager` is constructed only `if oc.twilio_account_sid and
oc.twilio_auth_token:` with **no `else`** — no log, no error — swallowing the `ValueError` the
constructor would raise. This contradicts the config layer's fail-fast for missing STT/TTS keys
(and the `ImportError` branch one line below at least logs a warning).

**Fix steps.**
1. Minimum: add the `else` warning.
2. Better: validate in `EasyConfig._validate` / `TelephonyConfig` and raise when the manager is
   enabled but a credential is blank.

**Regression test.** `enable_outbound_call_manager=True` + blank creds → assert a clear error (or
warning) at config/session build time, not a later `AttributeError`.

**Validation.** `uv run pytest tests/config/` / `tests/telephony/`.

**Risk & rollback.** Low; makes a silent misconfig loud.
