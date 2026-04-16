# Runtime Migration Guide

## Migrating from EventTraceLogger to `session.journal.follow()`

### Before (EventTraceLogger subscriber)

```python
from easycat.events import EventBus, STTFinal

event_bus = EventBus()

# Old approach: subscribe to specific events on the EventBus.
async def on_stt_final(event: STTFinal):
    print(f"Transcript: {event.text}")

event_bus.on(STTFinal, on_stt_final)
```

### After (Journal live-tail)

```python
import asyncio
from easycat.runtime.records import JournalRecordKind

async def tail_journal(session):
    """Stream all journal records as they are appended."""
    view = session.journal  # JournalView (read-only)
    if view is None:
        return  # journaling disabled (debug="off")

    async for record in view.follow():
        # Filter by kind if you only care about specific record types.
        if record.kind == JournalRecordKind.EVENT:
            print(f"[{record.name}] {record.data}")

# Launch as a background task alongside the session.
asyncio.create_task(tail_journal(session))
```

### Key differences

| Aspect | EventTraceLogger | `journal.follow()` |
|--------|-----------------|---------------------|
| Scope | Single event type per subscriber | All record types in one stream |
| Filtering | By event class | By `JournalRecordKind`, name, tags |
| Ordering | Unordered (async handler dispatch) | Monotonic sequence numbers |
| Persistence | In-memory ring buffer only | In-memory or SQLite |
| Crash recovery | Lost on process exit | SQLite backend survives SIGKILL |

### Point-in-time reads

```python
# Read all records so far.
records = session.journal.read()

# Read from a specific sequence number.
records = session.journal.read(start=42)

# Slice by kind.
events = session.journal.slice(kind=JournalRecordKind.EVENT)

# Check health.
session.journal.enabled   # True when journaling is active
session.journal.degraded  # True if a backend write has failed
```

After a clean `await session.stop()` or `await session.shutdown()`,
`session.journal.read()` still works. EasyCat tears down the live
journal backend first, then keeps a read-only postmortem view for
inspection and `session.export_debug_bundle(...)`.

### Debug levels

```python
from easycat.config import EasyCatConfig

# No journaling (zero overhead).
config = EasyCatConfig(..., debug="off")

# In-memory ring buffer (default) — good for dev, lost on exit.
config = EasyCatConfig(..., debug="light")

# SQLite WAL — crash-durable, exportable, replayable.
config = EasyCatConfig(..., debug="full")
```

## Migrating from InMemoryMetrics

Metrics are dual-written to the journal automatically when a journal
is present. No migration needed — `InMemoryMetrics` continues to work
as before. The journal captures individual data points (each
`record_latency` and `increment_counter` call becomes a METRIC record),
while the legacy `get_metrics()` API returns aggregated views.

To read metric records from the journal:

```python
metrics = session.journal.slice(kind=JournalRecordKind.METRIC)
for r in metrics:
    print(f"{r.name}: {r.data}")
    # e.g. {"metric_type": "latency", "value_ms": 123.4}
    # e.g. {"metric_type": "counter", "amount": 1}
```

## Migrating from Tracer / SpanManager

Spans are dual-written as paired `SPAN_START` / `SPAN_END` records.
The legacy `Tracer` + `TraceExporter` pipeline continues to work.

```python
starts = session.journal.slice(kind=JournalRecordKind.SPAN_START)
ends = session.journal.slice(kind=JournalRecordKind.SPAN_END)
```

Each `SPAN_END` record includes `data["span_id"]`, `data["status"]`,
and `data["duration_ms"]`. Error spans also carry an `error` field
with `ErrorInfo(type=..., message=...)`.

