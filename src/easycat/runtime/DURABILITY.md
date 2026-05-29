# Journal Durability Guarantees

## Application-crash durability (default)

The SQLite journal backend (`debug="full"`) survives:

- **SIGKILL** — process killed by OOM killer, orchestrator, or `kill -9`
- **Unhandled exceptions** — Python traceback exits
- **Segfaults** — native library crashes
- **Telephony disconnects** — remote peer hangup, network loss

**Zero committed records are lost.** This is inherent to the write
path: SQLite commits go through `write()` into the kernel page cache
under `PRAGMA synchronous=NORMAL`. The kernel owns the dirty pages
and flushes them to the block device regardless of Python process
state. No `fsync()` is called on the hot path.

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

Because no `fsync()` is called during the session:

- Write latency is bounded by memcpy + B-tree insert (~10-50µs)
- No dependency on storage I/O latency (same on NVMe, EBS, or NFS)
- No sporadic stalls from WAL autocheckpoint (disabled via
  `PRAGMA wal_autocheckpoint=0`)
- Checkpoint runs once at clean session close when latency is no
  longer a concern

## Kernel-crash durability (best-effort)

A kernel panic, hypervisor failure, or power loss can lose WAL pages
not yet written back to the block device. Under the checkpoint-on-close
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
      <sha256>.bin                 # content-addressable artifacts (0600)
  crash-dumps/
    <session_id>.sqlite            # promoted from journals/ on unclean shutdown
  archive/
    <session_id>.tar.gz            # retention-archived sessions
```

- Root directory: configurable via `EASYCAT_DATA_DIR` env var
- Directories: created lazily on first write
- Permissions: files `0600`, directories `0700` (secret-adjacent data)

## Session teardown contract

EasyCat distinguishes between logical finalization and physical backend
teardown:

- `Session._close()` (internal) writes the journal's clean-close marker
  but keeps the live backend open. This is a low-level primitive, not a
  public teardown entry point.
- `Session._destroy()` (internal) closes live backend resources such as
  SQLite connections, Litestream sidecars, libSQL sync threads, and
  in-memory artifact stores.
- `await session.stop()` is the one public teardown verb: `force=False`
  (default) drains in-flight work gracefully, `force=True` cancels it
  first. Both end by calling `_destroy()` — the difference is
  cancellation strategy, not whether resources are released.
  `async with session:` is the preferred idiom (it calls
  `stop(force=True)` on exit); `session.shutdown()` is a thin alias for
  `stop(force=True)`.

Post-stop inspection is still supported: after a clean `stop()`,
`session.journal.read()` and `session.export_debug_bundle(...)` continue
to work through a read-only postmortem view. New journal writes are no
longer accepted.
