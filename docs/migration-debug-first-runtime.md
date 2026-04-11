# Migration Guide: Legacy Observability to Debug-First Runtime

EasyCat's legacy observability modules (`easycat.event_logging`,
`easycat.tracing`, `easycat.metrics`, `easycat._span_manager`) and legacy
agent adapters (`easycat.agents.*`) have been **removed**. The import
paths no longer exist — there are no deprecation shims. Code that still
references them must be updated before upgrading.

The replacement systems are:

- **`session.journal`** (`ExecutionJournal`) for all observability
- **`easycat.integrations.agents`** bridges for agent framework integration

## EventTraceLogger → session.journal

### Before

```python
from easycat.event_logging import EventTraceLogger, EventLoggingConfig

config = EventLoggingConfig(enabled=True, json_mode=True)
logger = EventTraceLogger(event_bus, config)
logger.start()

# Later: retrieve recent events
recent = logger.snapshot_recent_events()
```

### After

```python
from easycat.runtime import JournalRecordKind

journal = session.journal  # available on every Session

# Query events
events = journal.slice(kind=JournalRecordKind.EVENT)
```

## Tracer / Span → journal stage operations

### Before

```python
from easycat.tracing import Tracer, TraceContext, SpanStatus

tracer = Tracer()
ctx = TraceContext()
span = tracer.start_span("stt", ctx)
# ... do work ...
tracer.finish_span(span, SpanStatus.OK)
```

### After

```python
# Spans are recorded automatically by Session into the journal.
from easycat.runtime import JournalRecordKind

spans = journal.slice(kind=JournalRecordKind.SPAN_START)
```

## InMemoryMetrics → journal query

### Before

```python
from easycat.metrics import InMemoryMetrics

metrics = InMemoryMetrics()
metrics.record_latency("stt_latency_ms", 123.4)
metrics.increment_counter("turns_completed")
stats = metrics.get_metrics()
```

### After

```python
from easycat.runtime import JournalRecordKind

# Metrics are recorded to the journal automatically.
metric_records = journal.slice(kind=JournalRecordKind.METRIC)
latencies = [r for r in metric_records if r.data.get("metric_type") == "latency"]
```

## Agent adapters → bridges

### Before

```python
from easycat.agents import (
    BaseAgentAdapter,
    OpenAIAgentsAdapter,
    PydanticAIAdapter,
    PydanticAIWorkflowAdapter,
)
from easycat.agents.openai_agents import build_openai_agents_adapter
```

### After

```python
from easycat.integrations.agents import (
    OpenAIAgentsBridge,
    PydanticAIBridge,
    GenericWorkflowBridge,
    ExternalAgentBridge,
    auto_adapt_agent,
)

bridge = OpenAIAgentsBridge(agent=my_openai_agent)
# or
bridge = PydanticAIBridge(agent=my_pydantic_agent)

# Or let EasyCat auto-detect the framework:
adapted = auto_adapt_agent(my_agent)
```

## EASYCAT_LEGACY_OBS_DUAL_WRITE removal

The `EASYCAT_LEGACY_OBS_DUAL_WRITE` environment variable has been removed.
Remove any references to it from your deployment configuration.

## Summary of removed modules

| Removed module | Replacement |
|---|---|
| `easycat.event_logging` | `session.journal` / `JournalRecordKind.EVENT` |
| `easycat.tracing` | `session.journal` (auto-recorded spans) |
| `easycat.metrics` | `session.journal` (metric records) |
| `easycat._span_manager` | `session.journal` (auto-recorded spans) |
| `easycat.agent_runner` | `easycat.integrations.agents._agent_runner.AgentRunner` |
| `easycat.agents.*` | `easycat.integrations.agents.*` (bridges) |
