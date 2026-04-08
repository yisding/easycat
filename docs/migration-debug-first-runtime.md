# Migration Guide: Legacy Observability to Debug-First Runtime

This guide covers migrating from EasyCat's legacy observability modules
(`event_logging`, `tracing`, `metrics`, `_span_manager`) and legacy agent
adapters (`easycat.agents`) to the new journal-based debug-first runtime
and bridge-based agent integrations.

## Why migrate?

The legacy observability stack (`EventTraceLogger`, `Tracer`/`Span`,
`InMemoryMetrics`) and the legacy agent adapters (`OpenAIAgentsAdapter`,
`PydanticAIAdapter`) are deprecated. They will emit `DeprecationWarning`
on import and will be removed in a future release.

The replacement systems are:

- **`session.journal`** (`ExecutionJournal`) for all observability
- **`easycat.integrations.agents`** bridges for agent framework integration

## EventTraceLogger to session.journal

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
from easycat.runtime import ExecutionJournal, JournalRecordKind, JournalView

journal = session.journal  # available on every Session

# Query events
view = JournalView(journal)
events = journal.slice(kind=JournalRecordKind.EVENT)
```

## Tracer / Span to journal stage operations

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
# Query them via:
from easycat.runtime import JournalRecordKind

spans = journal.slice(kind=JournalRecordKind.SPAN_START)
```

## InMemoryMetrics to journal query

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

## AgentRunner to bridge/stage

### Before

```python
from easycat.agent_runner import AgentRunner, AgentRunnerConfig

runner = AgentRunner(my_agent, AgentRunnerConfig(timeout=30.0))
response = await runner.run("Hello")
```

### After

```python
from easycat.integrations.agents import OpenAIAgentsBridge, PydanticAIBridge

# Use bridges directly -- they integrate with the journal automatically.
bridge = OpenAIAgentsBridge(agent=my_openai_agent)
# Or for PydanticAI:
bridge = PydanticAIBridge(agent=my_pydantic_agent)

# Pass to SessionConfig or use auto_adapt_agent():
from easycat.agents.factory import auto_adapt_agent
adapted = auto_adapt_agent(my_agent)  # auto-detects and wraps in bridge
```

## Legacy agent adapter imports to new imports

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
)
```

## EASYCAT_LEGACY_OBS_DUAL_WRITE removal

The `EASYCAT_LEGACY_OBS_DUAL_WRITE` environment variable has been removed.
Journal writes from legacy observability modules are now unconditional.
Remove any references to this variable from your deployment configuration.

## Summary of deprecated modules

| Deprecated module | Replacement |
|---|---|
| `easycat.event_logging` | `session.journal` / `JournalView` |
| `easycat.tracing` | `session.journal` (auto-recorded spans) |
| `easycat.metrics` | `session.journal` (metric records) |
| `easycat._span_manager` | `session.journal` (auto-recorded spans) |
| `easycat.agent_runner` | `easycat.integrations.agents` bridges |
| `easycat.agents.*` | `easycat.integrations.agents.*` |
