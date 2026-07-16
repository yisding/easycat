# Chapter 11 Exercises

The first six exercises are offline. Apply the last two only in a controlled
staging environment.

## 1. Keep and inspect the journal

```bash
uv run python docs/using-easycat/11-production-ops/main.py --data-dir .easycat/tutorial/ch11
uv run easycat inspect .easycat/tutorial/ch11/journals/chapter-11-ops-checkpoint.sqlite
uv run easycat inspect .easycat/tutorial/ch11/journals/chapter-11-ops-checkpoint.sqlite --json
```

Find the event and metric records, session ID, sequence order, and data fields.
Explain why the journal accepts no late append through its postmortem view.

## 2. Make readiness fail for each reason

Construct `VoiceServerHealth` values for:

- draining;
- `active_sessions == max_sessions`;
- route stack unavailable;
- manifest not loaded;
- a provider plan with blocking errors.

For each, inspect `readiness_failures()`, `checks()`, and `to_payload()`. Verify
the output contains no configured token or connection identifier.

## 3. Test the metric cardinality firewall

Try route templates from the server's enumerated set, then try a raw path with
a query token and a path containing a user ID. The latter values must raise
before emission.

Design bounded labels for provider, transport, result, server state, and auth
result. Decide where session-level correlation belongs instead.

## 4. Run and inspect quick validation

```bash
uv run easycat validate quick --json
uv run easycat validate report .easycat/validation/latest.json --json
```

Locate the run ID, lane, status, checks, failures, artifact paths, and timing.
Write a CI condition using structured fields rather than matching console text.

## 5. Define release evidence

Create a release-record template with:

- source revision and wheel/image digest;
- dependency lock and selected extras;
- configuration/manifest revision without secret values;
- quick/socket/contracts/stress/live/latency report links as applicable;
- deployment region/provider identity;
- approver, rollout time, rollback target, and retention period.

State which missing fields block promotion.

## 6. Design journal durability

Choose `sqlite`, `sqlite+litestream`, or `libsql` for a hypothetical service.
Specify RPO, RTO, retention, encryption, tenancy boundaries, access audit,
redaction, quota, and deletion behavior.

Describe a crash drill and a restore drill. Include how you prove the restored
journal is complete enough to inspect/replay and how you prevent a bundle with
PII from leaving the approved boundary.

## 7. Rehearse a rolling shutdown in staging

With at least one long-lived test session:

1. observe ready/active/draining metrics;
2. initiate shutdown;
3. verify readiness fails before new connection attempts;
4. let one session stop gracefully;
5. make one session exceed the drain window and observe force escalation;
6. verify both journals and process exit before the orchestrator deadline.

Record timestamps so the configured time budget can be compared with reality.

## 8. Run a safe failure game day

Pick one provider failure, one ingress failure, and one storage/observability
failure. For each, predict and then verify:

- caller-visible behavior;
- readiness/liveness state;
- bounded metric changes;
- journal/debug evidence;
- alert delivery and runbook usefulness;
- cleanup, retry, rollback, and data-retention outcome.

Do not inject failure into unapproved production traffic.

## Done when

You can produce a release record and answer:

- what was validated and which exact artifact was promoted;
- why the process is or is not ready;
- which signals are safe metrics versus sensitive forensic records;
- what storage failure the journal survives;
- how admission, graceful drain, force escalation, and the outer process
  deadline compose;
- how an operator detects, diagnoses, mitigates, and later reviews a failure.
