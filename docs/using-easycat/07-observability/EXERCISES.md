# Chapter 7 Exercises

Generate the pair once before starting:

```bash
uv run python docs/using-easycat/07-observability/main.py pair .easycat/tutorial/ch07
```

## 1. Read the JSON envelope

Run:

```bash
uv run easycat bundles show .easycat/tutorial/ch07/baseline.bundle --json
```

Find these fields without depending on their exact timing values:

- `schema_version`, `command`, and `status`;
- `turn_count`, `records`, and `errors`;
- the two entries under `turns`;
- `issues.summary` and `artifact_count`.

Why is asserting `turn_count == 2` more stable than asserting one local
`agent_request_to_first_token_ms` value?

## 2. Inspect issue triage

Run both forms:

```bash
uv run easycat inspect .easycat/tutorial/ch07/baseline.bundle --issues
uv run easycat inspect .easycat/tutorial/ch07/baseline.bundle --issues --json
```

The teaching bundle is clean. Compare the human display with the structured
`issues` object. Which form belongs in a terminal investigation, and which
belongs in automation?

## 3. Filter replay

Start with safe full-bundle replay:

```bash
uv run easycat replay .easycat/tutorial/ch07/baseline.bundle --fidelity artifact --tool-policy deny --json
```

Copy one turn ID from `bundles show --json`, then add `--turn TURN_ID`. Compare
the frame count. Next add `--stage agent` and confirm the stage list remains
limited.

Do not change to `--tool-policy allow` as a reflex. On a tool-bearing bundle,
first decide whether the effect is safe, should be stubbed, or should block the
replay.

## 4. Explain the diff

Run:

```bash
uv run easycat diff .easycat/tutorial/ch07/baseline.bundle .easycat/tutorial/ch07/candidate.bundle --json
```

Verify that both positional turns are matched and both transcript change flags
are true. Notice that transcript bodies are redacted in stdout.

Open the source script and identify the two response changes. Explain why
redacted CLI output can report drift without making the source bundle safe to
share.

## 5. Compare light and full capture

Change `record_bundle()` temporarily from `debug="full"` to `debug="light"`
and write a new bundle. Both modes can export, but their live storage differs:
light uses a bounded in-memory ring and full uses a persistent configured
backend plus filesystem artifacts.

Restore `debug="full"`. Find the generated SQLite journals under the current
EasyCat data directory and run `bundles show` directly on one `.sqlite` path.

## 6. Use automatic capture

Replace explicit post-stop export with:

```python
session = create_text_session(
    agent=SupportWorkflow(variant),
    debug="full",
    record_to=path.parent,
)
```

Observe the timestamped output name created during stop. Which style is better
for always-on production capture? Which is better when a test needs a stable
filename? Restore the explicit export afterward.

## 7. Open the debugger locally

After installing the optional extra, run:

```bash
uv run easycat debugger serve .easycat/tutorial/ch07/baseline.bundle --no-open-browser
```

Open the loopback URL manually and locate the two turns, agent spans, and raw
records. Stop the server with Ctrl+C.

Do not use `--allow-remote` on an untrusted network. The debugger has no
authentication and the source artifact is sensitive.

## 8. Export a smaller sharing surface

Run:

```bash
uv run easycat bundles export .easycat/tutorial/ch07/baseline.bundle --output .easycat/tutorial/ch07/context --json
```

Inspect the generated context pack before sharing it. Explain why “redacted”
reduces risk but does not eliminate the need for review and access control.

## Done when

You can explain:

- the difference between live events, a journal, and a bundle;
- what `off`, `light`, and `full` capture modes own;
- when to summarize, inspect, replay, diff, search, or open the debugger;
- why tool replay defaults to deny;
- why normal bundles require sensitive-data handling.
