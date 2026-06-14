# Neo Open Questions

Status: active decision log.

This file tracks decisions that should be answered before freezing the
next-major API. Each question includes a recommended default so implementation
can proceed unless maintainers choose otherwise.

## Product/API Questions

### Q1 — Should `VoiceApp` be top-level?

**Question:** Should users import `VoiceApp` from `easycat`, `easycat.app`, or
both?

**Recommended default:** Export top-level `VoiceApp` because it is the new
headline user surface. Also keep implementation in `src/easycat/voice_app.py`
for simplicity.

### Q2 — What should `easycat serve` mean?

**Question:** Should `easycat serve` remain a browser playground command or
become the unified dev/prod server command?

**Recommended default:** In Phase 1, keep `easycat serve` browser-first and add
`--mode`. In Phase 2, add `--manifest` and let the same command run
`VoiceServer` when a manifest is present.

### Q3 — Is `VoiceApp.run("browser")` always WebRTC?

**Question:** Should browser mode abstract over WebRTC and WebSocket browser
clients?

**Recommended default:** Yes for the product concept eventually, but start with
browser = WebRTC because that is the current strongest browser path. Keep raw
WebSocket as `mode="websocket"`.

### Q4 — How much Twilio belongs in Phase 1?

**Question:** Should Phase 1 include full Twilio server support or just set the
API shape?

**Recommended default:** Ship local/browser/websocket first, then extract Twilio
server helper as the next PR. Do not block `VoiceApp` on Twilio.

## Manifest Questions

### Q5 — TOML or YAML?

**Question:** Should project manifests be `easycat.toml`, `easycat.yaml`, or
both?

**Recommended default:** Start with `easycat.toml` because Python 3.11 includes
`tomllib`, avoiding a YAML dependency. Add YAML later only if demand is strong.

### Q6 — How should agents be referenced in manifests?

**Question:** What syntax should point to Python agent factories?

**Recommended default:** Use `python:module.path:function_name`, for example:

```toml
agent = "python:app:create_agent"
```

This is explicit and avoids importing arbitrary files by path until the loader
has validated the manifest.

### Q7 — Should secrets be allowed as literal values?

**Question:** Should manifests allow literal API keys or only env references?

**Recommended default:** Do not support literal secrets in first version. Use env
references such as `bearer-env:EASYCAT_SERVER_TOKEN`.

## Server Questions

### Q8 — Should `VoiceServer` use aiohttp or FastAPI?

**Question:** Which framework should own the core server routes?

**Recommended default:** Use aiohttp internally. Existing WebRTC signaling and
debugger server already use aiohttp-style infrastructure, and it avoids making
FastAPI a core dependency.

### Q9 — Should health endpoints be always enabled?

**Question:** Can users disable health endpoints?

**Recommended default:** Health endpoints are always enabled for `VoiceServer`.
They are low risk and critical for deployment.

### Q10 — What is the default auth policy?

**Question:** Should a local server require auth?

**Recommended default:** Loopback server can run without auth. Non-loopback
requires explicit auth unless `unsafe_allow_no_auth=True` is set.

## Debug/Eval Questions

### Q11 — What exactly does “always-on debugger” mean?

**Question:** Does it mean always recording, always serving a debugger, or always
opening the browser?

**Recommended default:** In dev mode, always record and serve a loopback
debugger. Browser opening is still explicit or dev-only. In production,
recording/debugging follows observability config and never autolaunches UI by
default.

### Q12 — Should `easycat.evals` depend on pytest?

**Question:** Should eval APIs import pytest or remain test-framework neutral?

**Recommended default:** Core `easycat.evals` must be pytest-free. Put pytest
fixtures/adapters in `easycat.evals.pytest`.

### Q13 — Should promotion generate code or data?

**Question:** Should `easycat eval promote` generate pytest files, scenario
files, bundle fixtures, or all of the above?

**Recommended default:** Generate a pytest skeleton and a scenario/bundle
fixture reference. Let users choose `--mode record-assertion` or
`--mode artifact-replay`.

### Q14 — How strict should redaction be?

**Question:** Should promotion refuse to write unredacted fixtures?

**Recommended default:** Warn loudly by default and require `--allow-pii` for
unredacted transcript/tool text once robust detection exists. Until then, keep
`--redact` available and default to no audio.

## Budget Questions

### Q15 — Should `LatencyBudget` move packages?

**Question:** Should `LatencyBudget` move from `easycat.validation.latency` to
`easycat.budgets`?

**Recommended default:** Add `easycat.budgets.LatencyBudget` as the preferred
import and keep `easycat.validation.latency.LatencyBudget` as an alias.

### Q16 — Should budget violations stop sessions?

**Question:** Should latency budget violations trigger runtime action like cost
budgets do?

**Recommended default:** Not by default. Latency budget violations should record
and report. Cost budget can keep stop behavior because it is explicit spend
control.
