# Journal Durability Guarantees

This page is the single source for the journal's numeric guarantees — the
batch-commit window, the `PRAGMA synchronous` setting, and the per-backend
crash-recovery matrix. The deployment guides
([docker.md](../../../docs/deployment/docker.md),
[production-servers.md](../../../docs/deployment/production-servers.md)) link
here for those numbers instead of restating them, so a change to
`SqliteJournal._batch_commit_interval_s` / `_batch_commit_records` in
[`journal_sql.py`](journal_sql.py) is one prose edit, not three.

Maintenance check: after changing journal persistence, recovery, storage
layout, or teardown semantics, run
`uv run pytest tests/runtime/test_sqlite_journal.py`.

Route discovery: use
`uv run easycat docs --audience operators-and-maintainers` for the focused
operator/maintainer route set
(`uv run easycat docs --audience operators-and-maintainers --json` emits the
same route entries and command hints). Coding agent? Use the root
[AGENTS.md](../../../AGENTS.md) for repository coding rules; use
[llms.txt](../../../llms.txt) for machine-readable docs route discovery or run
`uv run easycat explain json-schema`.

Operator inspection: after a run, inspect a live SQLite journal with
`uv run easycat inspect .easycat/journals/<session_id>.sqlite`; add `--json`
(`uv run easycat inspect .easycat/journals/<session_id>.sqlite --json`) for a
parseable summary. Inspect promoted crash dumps
with `uv run easycat inspect .easycat/crash-dumps/<session_id>.sqlite --json`.

## Application-crash durability (default)

The SQLite journal backend (`debug="full"`) survives:

- **SIGKILL** — process killed by OOM killer, orchestrator, or `kill -9`
- **Unhandled exceptions** — Python traceback exits
- **Segfaults** — native library crashes
- **Telephony disconnects** — remote peer hangup, network loss

**Every committed batch survives.** `SqliteJournal.append()` adds the record
to an open transaction. The transaction commits after at most 100 ms or 100
records, whichever comes first, and commits immediately at `turn_started`,
`turn_ended`, `flush()`, `finalize()`, and `close()`. The elapsed-time commit
runs on a shared journal coordinator thread. The worst-case
application-crash loss window is therefore the current batch (at most 100 ms
or 100 records), never an entire turn. Returning from `append()` means the
record is visible to live readers; it does not by itself promise that the
current batch has committed.

This is inherent to the write path: SQLite commits go through `write()`
into the kernel page cache under `PRAGMA synchronous=NORMAL`. The kernel
owns the dirty pages and flushes them to the block device regardless of
Python process state. No `fsync()` is called on the hot path. High-rate stage
appends are also sent through a worker thread, so SQLite inserts and the
occasional count/turn-boundary commit do not block the asyncio audio loop.

The only deliberate exception is the first post-`finalize()` append. It is
wrapped in a `SAVEPOINT` and intentionally left uncommitted until another
append starts a batch or the next `close()`/`finalize()`, so that an isolated
late write followed by a crash leaves the durable database looking cleanly
closed (see "Session teardown contract").

### Why this works

1. `write()` transfers data from userspace to kernel page cache.
2. The kernel marks pages dirty and schedules writeback.
3. Even if the Python process dies immediately after `write()`,
   the kernel still owns those pages and will flush them.
4. `synchronous=NORMAL` means SQLite considers the commit
   complete after `write()` returns — no `fsync()` needed.

### Filesystem requirements

This guarantee holds on all standard filesystems:

- **ext4, xfs, btrfs, APFS, HFS+** — standard Linux/macOS filesystems
- **tmpfs** — uses the page cache; data survives process death but
  is lost on reboot (acceptable for tests and ephemeral containers)
- **EBS, Persistent Disk, Azure Disk** — block devices with standard
  page cache semantics
- **NFS, EFS** — writes are buffered in the client page cache;
  application-crash durability holds but server-crash durability
  depends on the NFS server's flush policy

### Performance implications

Because no `fsync()` is called during the session and records share commits:

- Each transaction amortizes WAL and commit bookkeeping across many records
- No dependency on storage I/O latency (same on NVMe, EBS, or NFS)
- `PRAGMA wal_autocheckpoint=1000` folds committed pages back into the main
  database during long sessions so the WAL is reused instead of growing for
  the entire call
- Clean close still runs `PRAGMA wal_checkpoint(TRUNCATE)` to shrink the WAL

## Kernel-crash durability (best-effort)

A kernel panic, hypervisor failure, or power loss can lose WAL pages
not yet written back to the block device. Under the bounded auto-checkpoint
strategy:

- **Window of loss:** bounded by the OS dirty-page writeback schedule,
  typically 5-30 seconds on Linux (`/proc/sys/vm/dirty_expire_centisecs`)
- **What survives:** all records committed before the last kernel
  writeback
- **What may be lost:** records committed in the last few seconds
  before the kernel crash

This is acceptable because kernel-level crashes are overwhelmingly
ops failures (bad deploy, hardware fault, hypervisor bug), not
application bugs. The journal's primary purpose is debugging
application-level voice pipeline issues.

### Improving kernel-crash durability

For environments where kernel-crash durability matters:

1. **Litestream** (`journal_backend="sqlite+litestream"`) — ships WAL
   segments to S3 every ~1 second, bounding loss to the replication
   interval
2. **libSQL** (`journal_backend="libsql"`) — embedded replica with
   async remote sync, bounding loss to the sync interval
3. **`synchronous=FULL`** — forces `fsync()` on every commit; adds
   storage-dependent latency (~1-10ms per turn on SSD, ~50-200ms on
   EBS). Not recommended for real-time voice.

## In-memory backend (`debug="light"`)

The in-memory ring buffer waives both crash-durability guarantees.
All data is lost when the process exits, whether cleanly or by crash.
A startup log line documents this:

```
In-memory journal: crash-durability waived (data lost on process exit)
```

This is appropriate for development and testing where persistence is
not needed.

## Crash recovery

When the SQLite backend detects an unclean shutdown (journal file
exists without a `clean_close` marker):

1. The prior journal is copied to `.easycat/crash-dumps/<session_id>.sqlite`
   with a dump-owned `<session_id>.artifacts/` snapshot alongside it. Repeated
   crashes for a reused session id receive a numeric suffix rather than
   overwriting an earlier post-mortem.
2. A `RecoveredSessionMarker` record is emitted at `sequence=0`
3. The new session starts fresh at `sequence=1`
4. The crash dump is loadable offline for post-mortem analysis

SQLite's native WAL recovery handles any uncheckpointed WAL pages
automatically — no special handling is needed.

### Backend support

Crash recovery (crash-dump promotion + `RecoveredSessionMarker` at
`sequence=0` + truncating the live journal to start fresh at
`sequence=1`) is provided by the **SQLite** and **`sqlite+litestream`**
backends only.

The **libSQL** backend (`journal_backend="libsql"`) does **not**
implement crash recovery. It mirrors only the clean-reuse truncation:
if a session id is reused after a clean close, the prior records are
deleted. If a libSQL session is reused after an unclean shutdown, it
continues appending into the existing table with a continued sequence
counter and emits **no** recovery marker. Use the SQLite backend if
crash-recovery semantics are required.

## Storage layout

```
.easycat/                          # EASYCAT_DATA_DIR (default: .easycat/)
  journals/
    <session_id>.sqlite            # live journal (one per session)
  artifacts/
    <session_id>/
      .easycat-artifact-epoch-v1.json # current live-journal ownership epoch
      .easycat-artifact-retirement-v1.json # resumable crash-sweep retirement
      <sha256[:2]>/
        <sha256>.bin               # content-addressable artifacts (0600)
        <sha256>.owner             # durable live-journal epoch ownership
  crash-dumps/
    <session_id>.sqlite            # promoted from journals/ on unclean shutdown
    <session_id>.artifacts/        # immutable snapshot for that crash dump
  archive/
    <session_id>.tar.gz            # retention-archived sessions
```

- Root directory: configurable via `EASYCAT_DATA_DIR` env var
- Directories: created lazily on first journal or artifact use
- Permissions: files `0600`, directories `0700` (secret-adjacent data)

The filesystem artifact store records ownership separately from cancellation
tokens. On journal startup, after crash recovery or clean-reuse truncation has
finished, it rotates the live ownership epoch. Artifacts still referenced by
the surviving journal are adopted into the new epoch; managed artifacts from a
prior epoch with no surviving journal reference are deleted with the same
crash-recoverable accounting protocol used by explicit deletes. Artifacts
written before the first journal exists are initially unbound and adopted by
that first journal. Unknown or invalid ownership metadata is preserved rather
than guessed at, and artifacts created before ownership metadata was introduced
remain unmanaged for backward compatibility.

### Crashed-journal sweep

A session whose process dies before a clean close leaves its journal in
`journals/` with no `clean_close` marker.  Because the in-session recovery
path only fires when the **same** `session_id` is reopened, an orphaned id
would otherwise linger.  Every `SqliteJournal` open first runs
`runtime/crash_sweep.py::sweep_crashed_journals`, which scans `journals/` and
**promotes each crashed-but-unswept journal to `crash-dumps/`** (checkpointing
WAL pages first, snapshotting referenced artifacts, then removing the source).
Before source removal it seals the old live-artifact epoch behind a durable
retirement intent. After the dump owns its artifact snapshot, the sweep
reclaims only blobs from that sealed epoch; a replacement epoch and unbound or
unknown ownership remain conservative. Interrupted retirement resumes on a
later sweep, including after the source journal is already gone. The sweep
retains the source journal and old live epoch if any referenced artifact cannot
be copied into the all-or-nothing snapshot. It skips the journal the opening
session owns, skips locked/live databases, skips cleanly-closed or empty files,
and never raises into journal startup. Both the in-session promoter and the sweep share
`crash_sweep.py::_copy_journal_to_crash_dump`.

Promoted crash dumps surface in `uv run easycat bundles list` with a `status`
column: a crashed journal still in `journals/` shows `crashed (uncommitted)`,
a swept one shows `crash-dump`, a live/locked journal shows `live`, and a
cleanly-closed journal shows `bundle`.  Inspecting an errored crash dump
(`uv run easycat inspect <path> --json`) reports `error_type` and
`failing_turn_id` for the first failure.

## Session teardown contract

EasyCat performs logical finalization and physical backend teardown as one
internally owned operation:

- `Session._finalize_debug_backends()` writes the journal's clean-close marker,
  closes live resources such as SQLite connections, Litestream sidecars,
  libSQL sync threads, and in-memory artifact stores, then installs the
  preserved read-only postmortem view.
- `await session.stop()` is the one public teardown verb: `force=False`
  (default) drains in-flight work gracefully, `force=True` cancels it
  first. Both end by finalizing the same backends — the difference is
  cancellation strategy, not whether resources are released.
  `async with session:` is the preferred idiom (it calls
  `stop(force=True)` on exit).

Post-stop inspection is still supported: after a clean `stop()`,
`session.journal.read()` and `session.export_debug_bundle(...)` continue
to work through a read-only postmortem view. New journal writes are no
longer accepted.
