# Code Quality — Low-Severity Bugs & Error-Handling

Lower urgency, but mostly small, well-scoped fixes. Several are **narrow-reachability** (single-
threaded asyncio makes the race windows tiny, or they require unusual custom transports) — recorded
as latent-gap hardening, not firefighting. The reachability caveat is stated honestly per item.

| # | File:line | Issue | Fix |
|---|-----------|-------|-----|
| 18 | `providers.py:296` | `clear_audio` mandatory in Protocol but documented optional | Move into optional-capabilities comment block |
| 19 | `transports/twilio_media.py:627` | Reconnect race tears down replacement stream | Copy the `websocket.py` `if self._ws is ws:` guard |
| 20 | `runtime/artifacts.py:144` | In-memory eviction ignores ref-counts | Refuse-at-cap + log, like filesystem store |
| 21 | `vad/silero.py:210` | Leftover buffer misframed on rate change | Track `_buffer_rate`, discard stale remainder |
| 22 | `config/_factory.py:611` | Env-flag truthiness drift | Shared `is_truthy` in `_env.py` |
| 32 | `session/_session.py:1224` | close/destroy guard misses stopping window | Check `_stopping` too |
| 33 | `stt/websocket_base.py:159` | Terminal WS death → empty transcript, no Error | Expose died-abnormally signal, emit provider error |
| 34 | `transports/webrtc.py:1360` | Recoverable negotiation failure marked fatal | Emit `fatal=False` |
| 35 | `cli/debug/bundles.py:558` | `export --force` rmtrees arbitrary dirs | Require pack marker / reject cwd ancestors |
| 36 | `cli/debug/bundles.py:1604` | `diff --turn <non-int>` silently empty | `emit_command_error(exit_code=2)` |
| 37 | `session/_stt_committer.py:414` | `end_stream()` failure swallowed silently | Log at debug with `exc_info` |

> **Anchor note:** re-confirm each line against the file before editing.

---

## #18 — Transport `clear_audio` Protocol contradiction

- **Category / Severity:** bug / Low
- **Location:** `providers.py:296`; contract kit at `testing/contracts.py:329` and `:349-353`

**Problem & trigger.** `clear_audio` is declared in the `runtime_checkable` Transport Protocol body,
yet its own docstring says it's optional — and the same file states optional capabilities are kept
*out* of the body precisely to avoid `isinstance` rejection. Reproduced: a transport omitting
`clear_audio` fails `isinstance(x, Transport)`. The shipped contract kit is self-contradictory —
`contracts.py:329` asserts the isinstance while `:349-353` skips when `clear_audio` is absent.

**Fix steps.**
1. Move `clear_audio` out of the Protocol body into the optional-capabilities comment block
   (runtime already discovers it via `getattr`).

**Regression test.** A transport without `clear_audio` passes `isinstance(x, Transport)` and the
contract kit.

**Validation.** `just guard-contracts` (`tests/contracts`, `tests/testing`).

**Risk.** Low; aligns declaration with documented intent.

---

## #19 — Twilio reconnect race tears down the replacement stream

- **Category / Severity:** bug / Low (near-unreachable in single-threaded asyncio, but latent)
- **Location:** `transports/twilio_media.py:627` (and the same pattern at `~1048-1068`);
  **reference:** `transports/websocket.py:412-426` (guarded correctly)

**Problem & trigger.** `send_audio`/`send_mark` clear `self._ws` on `ConnectionClosed`, allowing a
new connection to be accepted before the old handler's `finally` runs. Unlike `websocket.py`
(guarded with `if self._ws is ws:` / `elif self._ws is None:`), the Twilio `finally`
**unconditionally** nulls `_ws`, wipes `_stream_sid`, emits a spurious `CallEnded` (carrying the new
call's sid), and enqueues the receive sentinel — killing the freshly reconnected call.

**Fix steps.**
1. Copy the WebSocket transport's identity guard into the Twilio `finally`: only tear down if the
   handler still owns the current `_ws`. Apply to both sites (627 and ~1048-1068).

**Regression test.** Simulate a reconnect where a stale handler's `finally` runs after a new `_ws` is
installed; assert the new call survives.

**Validation.** `uv run pytest tests/transports/` (Twilio media tests).

**Risk.** Low. **Coordination:** this fix lands in the duplicated protocol code; if #27 (dual-class
dedup) lands first, put the guard in the single shared copy. Fix #19 first otherwise.

---

## #20 — `InMemoryArtifactStore` eviction ignores journal ref-counts

- **Category / Severity:** bug / Low (ephemeral data; degraded-replay symptom only)
- **Location:** `runtime/artifacts.py:144` (`_evict_if_needed`); contrast `FilesystemArtifactStore`

**Problem & trigger.** The default `debug="light"` pairing is `InMemoryRingBuffer` (ref-counted) +
`InMemoryArtifactStore` (50 MB). The store's `_evict_if_needed` pops by insertion order with no
knowledge of ref-counts, deleting blobs whose records are still buffered → bundles/replay with
dangling `input_ref`/`output_ref` and no overflow signal. `FilesystemArtifactStore` refuses new
writes for this exact reason.

**Fix steps.**
1. Mirror the filesystem policy: refuse-new-at-cap + log once; **or** share the ring buffer's
   `_ref_counts` so eviction skips referenced blobs.

**Regression test.** Fill past the byte cap while records still reference early blobs; assert no
dangling refs (or an explicit overflow signal).

**Validation.** `uv run pytest tests/runtime/` (artifact store tests).

**Risk.** Low.

---

## #21 — SileroVAD rate-change misframing

- **Category / Severity:** bug / Low (reachable only via custom transports that renegotiate rate;
  bounded to a single boundary frame)
- **Location:** `vad/silero.py:210`

**Problem & trigger.** `_buffer` accumulates sub-frame remainders but `target_rate`/`frame_bytes`
are recomputed per call; a mid-stream 8k↔16k switch concatenates old-rate leftover bytes with
new-rate bytes and slices at the new frame size → one garbled frame. (The report notes "silently
degrades detection" overstates it — it's a single boundary frame.)

**Fix steps.**
1. Track `_buffer_rate`; on a rate change, discard the stale remainder (do **not** raise — VAD is
   continuous).

**Regression test.** Feed 8k then 16k audio across the boundary; assert no misframed frame.

**Validation.** `uv run pytest tests/vad/`.

**Risk.** Low.

---

## #22 — Env-flag truthiness drift (`EASYCAT_DEV` / `EASYCAT_EMERGENCY_EXPORT`)

- **Category / Severity:** bug / Low
- **Location:** `config/_factory.py:611`; canonical parser used by `easycat serve` / `dev.py`

**Problem & trigger.** `EASYCAT_DEV` is gated on bare `os.getenv` (so `"0"`/`"false"` are truthy)
while `serve`/`dev.py` use the canonical set-parser (those are falsy); `EASYCAT_EMERGENCY_EXPORT`
arms only on exactly `"1"`, rejecting `"true"`/`"yes"` that sibling flags accept. So
`EASYCAT_DEV=0 easycat serve` still imports the 3006-line debugger module (cost only — arm
re-checks correctly), and `EASYCAT_EMERGENCY_EXPORT=true` silently never arms.

**Fix steps.**
1. Hoist `is_truthy` into a shared `src/easycat/_env.py` and use it at both sites (and any other flag
   readers).

**Regression test.** Parametrize `"0"/"false"/"1"/"true"/"yes"` and assert consistent truthiness
across all env-flag readers.

**Validation.** `uv run pytest tests/config/` / `tests/cli/`.

**Risk.** Low.

---

## #32 — `close()`/`destroy()` guard misses the stopping window

- **Category / Severity:** error-handling / Low (requires a second task calling a deprecated alias
  mid-stop)
- **Location:** `session/_session.py:1224`

**Problem & trigger.** `stop()` flips `_is_running=False` before ~10 awaits of teardown, during
which `_stopping` is True; both `close()`/`destroy()` guards check only `_is_running`, so a
concurrent call passes and can write the clean-close marker early or swap in a read-only journal that
silently drops the teardown tail.

**Fix steps.**
1. Change the guard to `if self._is_running or self._stopping:` (the `finally` resets `_stopping`,
   so legit post-stop calls still pass).

**Regression test.** Call `close()` from a second task mid-`stop()`; assert it no-ops and the
teardown tail is preserved.

**Validation.** `uv run pytest tests/session/`.

**Risk.** Low. **Coordination:** sequence #32 → QW8 (which decorates these same methods).

---

## #33 — Terminal WS STT death → clean empty transcript, no Error

- **Category / Severity:** error-handling / Low (only the budget-exhausted reconnect path is silent;
  the common partition path *is* journaled)
- **Location:** `stt/websocket_base.py:159`; **reference:** `_multi_context_ws.py:358-373` (TTS side
  hardened against this exact downgrade)

**Problem & trigger.** All WS STT providers install a no-op reconnect hook, so `recv_iter` returns
(never raises) on terminal drop; `_receive_loop`'s `finally` queues the None sentinel, `events()`
ends cleanly, and the committer's `resolve_pending(turn, "")` hands the turn an empty transcript with
no Error emitted.

**Fix steps.**
1. Expose a died-abnormally signal from `ReconnectingWebSocket`.
2. Call `_emit_provider_error` before the sentinel when the stream died abnormally (mirror the TTS
   hardening).

**Regression test.** Force a terminal WS death mid-utterance; assert an Error event is emitted (not a
silent empty transcript).

**Validation.** `uv run pytest tests/stt/`.

**Risk.** Low.

---

## #34 — Recoverable WebRTC negotiation failure marked fatal

- **Category / Severity:** error-handling / Low
- **Location:** `transports/webrtc.py:1360`

**Problem & trigger.** A failed `/offer` returns 400 with the transport fully alive, yet emits
`TransportDegraded(fatal=True)`. Fatal events are exempt from the 1 s coalescing *and* the 64-task
cap and are journaled as CONTROL, so a client looping junk SDP floods the journal with "fatal"
records for a healthy server, masking real fatal events. Sibling emitters + webtransport pair fatal
with `close_connection`.

**Fix steps.**
1. Emit `fatal=False` for the recoverable negotiation-failure path.

**Regression test.** Post malformed SDP repeatedly; assert the emitted degraded events are non-fatal
and subject to coalescing.

**Validation.** `uv run pytest tests/transports/` (webrtc). Coordinate with QS6 (WebRTC convergence)
and QW9 (ruff ASYNC hits in this file) — land the small fixes before the big extraction.

**Risk.** Low.

---

## #35 — `bundles export --force` rmtrees arbitrary dirs

- **Category / Severity:** error-handling / Low (requires explicit `--force` + unusual `-o`)
- **Location:** `cli/debug/bundles.py:558` (`_prepare_output_dir`)

**Problem & trigger.** The guard covers only the FS root and cwd, then `shutil.rmtree`s any other
existing `--output` dir. `export run.zip -o .. --force` resolves to cwd's parent (neither root nor
cwd) and deletes the parent tree including cwd.

**Fix steps.**
1. Refuse dirs lacking a pack marker, **or** reject ancestors of cwd / `Path.home()` before
   `rmtree`.

**Regression test.** `export -o <ancestor-of-cwd> --force` → refused with a clear error, no deletion.

**Validation.** `uv run pytest tests/cli/test_bundles.py`.

**Risk.** Low but the failure mode is destructive — worth doing. Fix before QS2 splits bundles.py.

---

## #36 — `diff --turn <non-int>` silently empty

- **Category / Severity:** error-handling / Low
- **Location:** `cli/debug/bundles.py:1604`

**Problem & trigger.** `int(turn)` failure sets `wanted=None`, filtering out every turn; the command
exits 0 with `turns=[]` and prints "No aligned turns to diff." A user typing a turn-*id* (which
`replay` accepts) wrongly concludes the runs had no comparable turns. `replay --turn` validates up
front and exits 2.

**Fix steps.**
1. On a non-integer `--turn`, `emit_command_error(..., exit_code=2)` (match `replay`).

**Regression test.** `diff --turn abc` → exit 2 with a usage error.

**Validation.** `uv run pytest tests/cli/test_bundles.py`.

**Risk.** Low.

---

## #37 — STT `end_stream()` failure swallowed silently

- **Category / Severity:** error-handling / Low (cancel-path only; observability loss)
- **Location:** `session/_stt_committer.py:414`

**Problem & trigger.** Bare `except Exception: pass` with no logging, unlike the codebase norm
(`logger.debug(..., exc_info=True)`) used everywhere else — including two other spots in this same
file.

**Fix steps.**
1. Replace the bare `pass` with `logger.debug("STT end_stream during cancel raised", exc_info=True)`.

**Regression test.** None strictly required; optionally assert the debug log fires on a raising
`end_stream`.

**Validation.** `uv run pytest tests/session/`.

**Risk.** None of consequence.
