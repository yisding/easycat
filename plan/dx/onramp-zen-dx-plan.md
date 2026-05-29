# EasyCat Onramp DX Plan — Radically Simpler, Still Powerful, Zen-Aligned

> Status: implementation-ready. Every change below has been checked against the
> source tree and an adversarial verifier pass. Changes the verifiers marked
> **drop** are not in the ranked list; **adopt-with-changes** caveats are folded
> into each subsection; **adopt** changes are kept as-is.

---

## 1. Executive summary

EasyCat's *minimal* happy path is genuinely best-in-class — `run(EasyConfig.mic(agent=Agent(...)))`
is three effective lines, one credential, one extra, with the asyncio/signal/lifecycle
ceremony hidden by `run()`. The problem is not the floor; it is everything a developer
sees *around* the floor. The README asserts three different "fastest paths" pointing at
three incompatible code shapes (one of which never starts the session and makes no sound),
the most common first-run error (forgotten `OPENAI_API_KEY`) raises a bare misleading
`ValueError` that bypasses the framework's own excellent error catalog, `import easycat`
confronts a newcomer with 84 flat alphabetical names where the two they need are buried at
index 14 and dead-last, and the running hello-world program has zero in-code pointers to the
next rung. **The one big move is to spend the entire budget on signposting, error-teaching,
and namespace/trust hygiene — anointing exactly one canonical shape at every fork and wiring
the already-built error catalog into the path beginners actually take — while leaving the
3-line happy path and the full-power `SessionConfig` escape hatch untouched.** No new
top-level verb, no rename of `EasyConfig`, no removal of capability: the existing shape is
*promoted and made discoverable*, not replaced.

---

## 2. The new hello-world: before vs after

### Before (what a newcomer hits today)

| Step | Command / code | Concepts | Lines |
|---|---|---|---|
| Install | `uv sync --extra quickstart` *(only works inside a cloned repo; no `pip install`/`uv add` documented)* | clone-the-repo, which-extra | 1 |
| Env | `export OPENAI_API_KEY="..."` | 1 env var | 1 |
| Code (README's prominent quickstart, `README.md:101-117`) | see below — **ends at `create_session`, never started, hardcoded key → silence** | EasyConfig, `create_session`, openai_api_key kwarg | ~6 |
| Run | (no runnable invocation shown in that block) | — | 0 |

```python
# README.md:101-117 — the FIRST thing a reader copies. It produces silence.
from easycat import EasyConfig, create_session

config = EasyConfig(openai_api_key="your-api-key", agent=my_agent)
session = create_session(config)   # never .start(); no run(); hardcoded placeholder key
```

The actually-runnable form (`run(EasyConfig.mic(agent=...))`) lives ~560 lines lower
(`README.md:663-670`) and in `examples/openai_agents_voice.py:14-20`, using a *different*
shape. **Three competing "fastest path" claims** at `README.md:27`, `:127`, and `:664`.
**Concept count: ~5 (EasyConfig vs create_session vs run, hardcoded-vs-env key, which-quickstart).**

### After (one anointed, runnable shape everywhere)

| Step | Command / code | Concepts | Lines |
|---|---|---|---|
| Install | `uv add 'easycat[quickstart]'`  *(or `pip install 'easycat[quickstart]'`)* | 1 extra | 1 |
| Env | `export OPENAI_API_KEY=sk-...` | 1 env var | 1 |
| Code | see below — byte-shape-identical in README, `examples/`, and `easycat init` output | EasyConfig, run | 3 |
| Run | `python bot.py` | — | 1 |

```python
"""bot.py — a voice bot in three lines.

Install:   uv add 'easycat[quickstart]'        (or: pip install 'easycat[quickstart]')
Configure: export OPENAI_API_KEY=sk-...
Run:       python bot.py

`easycat init` scaffolds this same shape — the file you'd hand-write is the
shape the CLI generates. One golden path.
"""
from agents import Agent  # the OpenAI Agents SDK (pip name: openai-agents)

from easycat import EasyConfig, run

run(EasyConfig.mic(agent=Agent(name="assistant",
                               instructions="You are a helpful voice assistant.")))

# Next, try (change one token here, or type `easycat.` to browse the surface):
#   stt="deepgram/nova-2"          swap STT (needs DEEPGRAM_API_KEY + easycat[deepgram])
#   tools=[...] on your Agent      tools live on YOUR Agent, not on EasyCat
#   EasyConfig.browser(agent=...)  serve in a browser (needs a server + easycat[webrtc])
#   debug="full"                   record a journal for `easycat inspect`
# Full ground-up ladder: docs/teaching/00-hello-audio/
```

**Concept count: 2 (EasyConfig, run).** Same auto-wiring (`OPENAI_API_KEY` → Realtime STT +
OpenAI TTS, `config.py:553-561`), same hidden lifecycle (`helpers.py:55-102`), but a single
shape with an in-editor breadcrumb to the next rung. The `agents` import is annotated so a
reader knows it is the `openai-agents` SDK.

---

## 3. The progressive learning ladder (toy → production)

Each level is a small, in-editor diff from the one below it. The "signpost" column is the
in-code cue (comment, docstring, autocomplete neighbor) that leads to the next rung **without
leaving the editor**.

| Level | Goal | API it touches | Code sketch | In-code signpost to the next rung |
|---|---|---|---|---|
| **0 — Talking bot, zero config** | Empty dir → hearing a reply; smallest code, one credential. | `EasyConfig.mic(agent=...)` + `run(...)`. `easycat init` scaffolds the same shape. | `run(EasyConfig.mic(agent=Agent(name="assistant", instructions="Be helpful.")))` | README's first block, the package `__doc__`, and `easycat init` output all share this shape. No create_session-vs-run-vs-init choice. |
| **1 — Add a tool / swap a provider** | Make the bot *do* something, or use Deepgram/ElevenLabs. | Tools: your framework's `@function_tool` on the Agent (passed through untouched by `auto_adapt_agent`). Providers: string shortcut `stt="deepgram/nova-2"`; `available_stt_providers()`/`available_tts_providers()` enumerate names; bad name → `EASYCAT_E104` fuzzy "did you mean"; missing key → `EASYCAT_E203` naming the env var. | `run(EasyConfig.mic(agent=my_agent, stt="deepgram/nova-2", tts="elevenlabs"))` | `bot.py`'s `# Next, try:` block + "tools live on YOUR Agent, not EasyCat" note mark the framework boundary. `stt=` line is honest that it needs the key **and** the extra. |
| **2 — Change the surface (mic → browser → phone)** | Serve the same bot to a browser or a phone call. | `EasyConfig.browser()` / `EasyConfig.phone()` presets (each `setdefault`s a transport; browser also flips echo cancellation on). | `config = EasyConfig.browser(agent=my_agent)` | Presets sit next to `.mic()` in autocomplete. Their docstrings now carry: "browser/phone need a server process + the webrtc/telephony extra — see `examples/webrtc_server.py` / `twilio_app.py`," so the real process-model jump is visible. |
| **3 — Observe and debug** | See what the pipeline did; capture a recording when it breaks. | `debug="light"\|"full"` (single flag, `config.py:483`), `record_to=PATH`. CLI: `easycat inspect`, `easycat bundles`. `RunBundle.load()` for replay. | `run(EasyConfig.mic(agent=my_agent, debug="full", record_to="./recordings"))` then `easycat inspect ./recordings/<run>.bundle` | `bot.py` names `debug="full"`. The TTY "what got wired" line shows the resolved providers. `easycat doctor` on a green run prints a success-path next step. |
| **4 — Own the lifecycle (advanced)** | Subscribe to events, run inside an existing loop, do work between turns. | `create_session(EasyConfig)` + the **one** teardown idiom `async with session:` (already wired via `Session.__aenter__/__aexit__`, `_session.py:1113-1132`). `session.subscribe_event(...)`. | `async with easycat.create_session(EasyConfig.mic(agent=my_agent)) as session: session.subscribe_event(STTFinal, ...); await session.wait_closed()` | `run()`'s docstring already points here. The README's create_session block is relabelled **"Advanced: own the lifecycle"** instead of competing as a second "fastest path". |
| **5 — Explicit providers (full escape hatch)** | Inject hand-built provider instances; bypass all auto-wiring. | `SessionConfig` (holds live provider *instances*) + `Session(...)`. Protocols in `easycat.providers` for typing your own. Same `async with` teardown. | `Session(SessionConfig(stt=DeepgramSTT(...), tts=my_custom_tts, transport=my_transport, agent=auto_adapt_agent(my_agent)))` | `SessionConfig`'s docstring opens: "Lowest rung: you supply live provider instances. EasyConfig auto-wires these for you one rung up." Bidirectional with `EasyConfig`'s "drop to SessionConfig for hand-built providers." **Preserved verbatim — no capability lost.** |

---

## 4. Zen of Python scorecard: TODAY vs AFTER

| Aphorism | Today | After | One-line rationale |
|---|:--:|:--:|---|
| #2 Explicit is better than implicit | 2 | 4 | Today: silent key pickup, bool+config flags override an explicit `False`, no "what got wired" echo. After: missing-key warns + raises a coded error, run() prints the resolved STT/TTS/echo-cancel on a TTY. |
| #3 Simple is better than complex | 5 | 5 | The 3-line happy path is untouched and remains best-in-category. |
| #6 Sparse is better than dense | 2 | 3 | `__all__` stays the curated 84 (the snapshot is a tested contract), but the package `__doc__` now leads with a "start here" and `bot.py` gives an in-editor ladder, so the *effective* discovery surface is sparse even though the name list is not shrunk. |
| #7 Readability counts | 3 | 4 | In-code "Next:" signposts, an honest module docstring, and non-ASCII rendered intact in the scaffolded file (no `—` escape at the first customization point). |
| #9 Errors should never pass silently | 2 | 4 | The most common first-run failure now routes through `EASYCAT_E203` with a substituted fix, and `EasyCatError` carries its fix + `easycat explain` hint onto the plain `python bot.py` traceback. |
| #10 Refuse the temptation to guess | 3 | 4 | A structurally-broken `agent=` now fails fast at construction with a coded error (with the real limits of `@runtime_checkable` honestly scoped), instead of a deep first-turn `AttributeError`. |
| #11 One — and preferably only one — obvious way | 1 | 4 | One anointed hello-world across README/examples/`init`; the dueling "fastest path" claims removed; `async with` becomes the one public teardown idiom; create_session relabelled as the explicit advanced door. |
| #13 If hard to explain, it's a bad idea | 3 | 4 | The teardown story collapses from five overlapping verbs to "use `async with` (or `stop()`)"; the ladder is the same shape top to bottom. |
| #14 Namespaces are one honking great idea | 2 | 3 | We do **not** mutate the tested `__all__`, but the existing real submodules (`easycat.events`, `easycat.providers`, `easycat.audio_format`, `easycat.transports`) are surfaced as the documented homes, and the docstring steers altitude. |

*(#1, #4, #5, #8, #12 are improved at the margins — first-artifact cleanliness, DRY base
config, no new forks — but are not the load-bearing wins.)*

---

## 5. Ranked change list (highest leverage first)

Ordering is impact × low-effort. Two proposals from the unified design are **not** here:
`shrink-flat-all` (verifier: **drop** — infeasible, breaks the tested `__all__` snapshot and
the docs-import guard, and `dir()` re-sorts so the promised ordered autocomplete does not
exist) and the literal `validate-missing-key-e203` snippet (verifier: **infeasible as written**
— it referenced an undefined helper and an unimported symbol; the *intent* survives below in a
corrected form).

---

### 5.1 — One canonical hello-world; kill the three "fastest path" claims  *(adopt-with-changes)*

**What / why.** The README asserts three conflicting "fastest" paths and its most prominent
quickstart constructs a session it never starts (silence) using a hardcoded `"your-api-key"`.
Promote the already-runnable `run(EasyConfig.mic(agent=...))` form to the README's first block,
relabel `create_session` as an explicit "Advanced: own the lifecycle" door, and make
`easycat init` scaffold the **same shape** (not byte-identical — the scaffold must stay a
parameterized template).

**Before**
```python
# README.md:101-117 — first block, never started, hardcoded key
config = EasyConfig(openai_api_key="your-api-key", agent=my_agent)
session = create_session(config)
# plus contradictory "fastest path" claims at README.md:27, :127, :664
```
**After**
```python
# README.md first block (same shape as examples/openai_agents_voice.py and `easycat init`)
from agents import Agent
from easycat import EasyConfig, run
run(EasyConfig.mic(agent=Agent(name="assistant",
                               instructions="You are a helpful voice assistant.")))
# create_session shown later under "Advanced: own the lifecycle" using `async with`.
```

**Files touched.** `README.md`; `examples/openai_agents_voice.py` (confirm it is the canonical
form); `src/easycat/cli/scaffold/templates/openai-agents/agent.py` (emit the `run(EasyConfig.mic(...))`
*shape* — keep its `$AGENT_NAME`/`$AGENT_INSTRUCTIONS` substitution and the teaching
`current_time` `@function_tool`).

**Verifier verdict.** Feasible; genuinely simpler (−2 concepts on the happy path); breaks no
power user. **Caveats folded in:** drop the "byte-identical to `easycat init`" claim — the
scaffold is a parameterized `string.Template` (`init.py:249-280`) and must stay so; aim for
*same shape*, with README and `examples/` byte-identical to each other. Note that bare
`EasyConfig(agent=...)` already equals `.mic()` for local (`config.py:461`), so promoting
`.mic()` is a readability choice, not a bug fix — do not imply the bare form was broken. Keep
`create_session` fully documented (it is the documented advanced entry in `helpers.py:69-71`).

**Zen.** #11 one obvious way.

---

### 5.2 — Make `EasyCatError` carry its fix + `explain` hint on programmatic tracebacks  *(adopt-with-changes)*

**What / why.** The doctor→explain teaching loop fires only inside the Typer CLI
(`cli/_output.py:71-74`). A `python bot.py` user who hits even a good code like `EASYCAT_E203`
sees a bare `EASYCAT_E203: Missing API key: OPENAI_API_KEY` traceback with no fix and no hint
that `easycat explain` exists. Render the registry fix onto the exception itself.

**Before** (`errors.py:37-41`)
```python
def __init__(self, code, message, **context):
    self.code, self.message, self.context = code, message, context
    super().__init__(f"{code}: {message}")   # no fix, no explain hint
```
**After**
```python
def __init__(self, code, message, **context):
    self.code, self.message, self.context = code, message, context
    super().__init__(self._render())

def _render(self):
    base = f"{self.code}: {self.message}"
    entry = REGISTRY.get(self.code)
    if entry is None:
        return base
    try:                                   # guard: a future braced fix template
        fix = entry.fix.format(**self.context) if self.context else entry.fix
    except (KeyError, IndexError):
        fix = entry.fix
    return f"{base}\n  Fix: {fix}\n  Run `easycat explain {self.code}` for details."
```

**Files touched.** `src/easycat/errors.py`.

**Verifier verdict.** Feasible and safe — 220 tests pass; the CLI/JSON output reads
`.code/.message/.context`, never `str(err)`, so it is byte-identical there; the
`EasyCatError`-before-`REGISTRY` ordering is fine because `_render` reads the module global at
call time. **Caveat folded in (required before merge):** the `entry.fix.format(**context)` is
guarded with `try/except`, mirroring the factory's headline guard (`errors.py:96-101`), so a
future fix template that gains a `{key}` not in context cannot turn into a constructor-time
`KeyError`. `EasyCatError` stays subclassing `Exception` (do **not** stealth-subclass
`ValueError`). Not a write-time simplification — it improves the runtime traceback only.

**Zen.** #9 errors should never pass silently; #7 readability counts.

---

### 5.3 — Route the missing/empty-key path through `EASYCAT_E203` (corrected)  *(adopt-with-changes; the original snippet was infeasible)*

**What / why.** The statistically #1 first-run mistake — forgetting `OPENAI_API_KEY` on
`EasyConfig.mic(agent=...)` — raises `ValueError: STT configuration is required.`
(`config.py:599`), which names a symptom the user never touched and never mentions the key.
`EASYCAT_E203` already exists with a substituted fix and is already raised on the string path.
Wire the dataclass path into the same catalog so identical intent yields one identical,
actionable error.

**Before** (`config.py:598-606`)
```python
def _validate(self):
    if self.stt is None:
        raise ValueError("STT configuration is required.")
    if self.tts is None:
        raise ValueError("TTS configuration is required.")
    for cfg, kind in ((self.stt, "STT"), (self.tts, "TTS")):
        if hasattr(cfg, "api_key") and not cfg.api_key:
            raise ValueError(f"{_provider_display_name(cfg, kind)} requires an API key.")
```
**After** (corrected — captures the leverage in the None branch, avoids the nonexistent helper)
```python
from easycat.errors import EASYCAT_E203   # add the import (every existing caller does a local one)

def _validate(self):
    # The #1 first-run mistake: no key resolved and nothing configured.
    if (self.stt is None or self.tts is None) and not self.openai_api_key:
        raise EASYCAT_E203(var="OPENAI_API_KEY")
    if self.stt is None:
        raise ValueError("STT configuration is required.")
    if self.tts is None:
        raise ValueError("TTS configuration is required.")
    for cfg, kind in ((self.stt, "STT"), (self.tts, "TTS")):
        if hasattr(cfg, "api_key") and not cfg.api_key:
            # Keep the existing display-name ValueError here — there is no
            # (cfg, kind) -> env-var helper today, and the None-branch fix
            # above captures ~all of the leverage.
            raise ValueError(f"{_provider_display_name(cfg, kind)} requires an API key.")
```

**Files touched.** `src/easycat/config.py`; `tests/test_config.py` (the empty-key assertions
that still expect a `ValueError` stay green because we keep the per-provider `ValueError`);
`tests/test_examples.py:429-434` only needs review if the no-key stderr assertion is exercised
with a genuinely-missing key.

**Verifier verdict.** The **original snippet was infeasible** — it referenced an undefined
`_provider_env_var(cfg, kind)` (only `_provider_display_name` exists, `config.py:421-439`) and
an unimported `EASYCAT_E203` (both `NameError`). **Corrections folded in:** add the import; put
the coded error only in the None/no-key branch; **leave the per-provider empty-key branch as a
`ValueError`** to avoid the missing helper and preserve the stage-specific message and the
existing `pytest.raises(ValueError, ...)` tests (`tests/test_config.py:622-635`). Because
`EasyCatError` subclasses `Exception` (not `ValueError`), the no-key flip from `ValueError` to
`EasyCatError` is an intentional, documented break, not a back-compat hedge.

**Zen.** #9 errors never silent; #2 explicit.

---

### 5.4 — Warn at the silent env-pickup site (folded into the `_validate` message)  *(adopt-with-changes)*

**What / why.** `config.py:536-537` is a bare `if` with no `else`, so a missing key is swallowed
at the pickup site and resurfaces two steps later. The verifier showed that bolting a separate
`logger.warning(... explain E203)` here produces *two* messages for one fault **and** misdirects
to E203 in the exact case that raises a plain `ValueError`, not E203.

**Decision (folded-in correction).** Do **not** add a separate warning with an `explain E203`
pointer. Instead, the corrected #5.3 above already names the fix at the point of failure
(`EASYCAT_E203(var="OPENAI_API_KEY")`), giving one clear, correctly-pointed error. If a
breadcrumb at the pickup site is still wanted, it must say only "no `OPENAI_API_KEY` found and
no `stt`/`tts` configured" with **no** `explain E203` reference (since that branch may raise a
plain `ValueError`).

**Files touched.** `src/easycat/config.py` (none beyond #5.3 if we adopt the single-message
route, which the verifier recommends as simpler and more faithful to Zen #9/#11).

**Verifier verdict.** The bolt-on warning is feasible but **not simpler** and misdirects;
adopt the single-message route in #5.3 instead.

**Zen.** #9 errors never silent.

---

### 5.5 — Filter the scaffold's template copy so cache artifacts stop shipping  *(adopt)*

**What / why.** `_copy_template` walks the live template dir with `rglob("*")` and copies every
non-dir file byte-for-byte with no ignore filter (`init.py:296`), so `__pycache__/*.pyc` and a
foreign tool's `.ruff_cache/` that sit in the template source at install time ship into a
brand-new "clean" project — the first artifact the recommended CLI door produces.

**Before** (`init.py:296`)
```python
for source in sorted(src_root.rglob("*")):
    if source.is_dir():
        continue
    rel = source.relative_to(src_root)
```
**After**
```python
_COPY_IGNORE = {"__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache"}
for source in sorted(src_root.rglob("*")):
    if source.is_dir():
        continue
    if any(part in _COPY_IGNORE for part in source.parts) or source.suffix == ".pyc":
        continue
    rel = source.relative_to(src_root)
```

**Files touched.** `src/easycat/cli/scaffold/init.py`; **add a regression test** asserting no
generated path contains `__pycache__`/`.ruff_cache` or ends in `.pyc` (`tests/cli/test_init.py`
currently only does a subset check at `:95`).

**Verifier verdict.** Bug reproduced live; the filter drops exactly the 4 cache artifacts and
keeps the 5 real files (the legitimate top-level `.gitignore` survives). Adopt; pair with the
regression test so it cannot silently regress. (A git-tracked manifest would be even more
robust but the denylist is the minimal shippable fix.)

**Zen.** #1 beautiful is better than ugly.

---

### 5.6 — Render scaffolded agent instructions with non-ASCII intact  *(adopt)*

**What / why.** `_python_string_literal_contents` uses `json.dumps(value)[1:-1]` (`init.py:246`),
which defaults to `ensure_ascii=True`, so the default instruction's em-dash renders as a literal
`—`-style escape in the generated `agent.py` — the exact line the scaffold README's #1 next
step tells the newcomer to edit.

**Before** (`init.py:246`) → **After**
```python
return json.dumps(value, ensure_ascii=False)[1:-1]
```

**Files touched.** `src/easycat/cli/scaffold/init.py` (one line; optional docstring tweak at
`:244` noting non-ASCII now passes through).

**Verifier verdict.** Clean one-line fix; round-trips to the identical runtime string; escaping
of `\`, `"`, newline is preserved; `ruff` clean under the generated project's config; the
escaping test (`tests/cli/test_init.py:118`) stays green. Adopt.

**Zen.** #7 readability; #1 beautiful.

---

### 5.7 — Lead the package `__doc__` with the 3-line quickstart + altitude steer  *(adopt-with-changes)*

**What / why.** `help(easycat)` and IDE hover render the module docstring; today it states the
lazy-import curation policy (a maintainer concern), not a "start here." Lead with the runnable
snippet and the EasyConfig-vs-SessionConfig steer.

**Before** (`__init__.py:1-10`) → **After**
```python
"""EasyCat — a voice bot in three lines.

Start here (requires `uv add 'easycat[quickstart]'`)::

    from agents import Agent
    from easycat import EasyConfig, run
    run(EasyConfig.mic(agent=Agent(name="assistant", instructions="Be helpful.")))

`EasyConfig` + `run` is the entry path. Drop to `SessionConfig` + `Session`
only when you need to hand-build provider instances.

The top-level package intentionally exposes the app-facing surface only;
providers, stage internals, and telephony/debug helpers stay importable
from their own modules. Exports load lazily via PEP 562 so cold starts stay
cheap.
"""
```

**Files touched.** `src/easycat/__init__.py:1-10`.

**Verifier verdict.** Clean, single-file, zero-risk; serves #11 and #2. **Caveat folded in:**
include the install hint (`uv add 'easycat[quickstart]'`) so the teaser is honestly runnable —
the snippet omits the `try/except ImportError` guard the real example carries, so a literal
copy-paste without `openai-agents` would otherwise raise a raw `ModuleNotFoundError`.

**Zen.** #11 one obvious way; #2 explicit.

---

### 5.8 — Add in-code "Next:" signposts forming an in-editor ladder  *(adopt-with-changes)*

**What / why.** `examples/openai_agents_voice.py:1-20` dead-ends in-code (docstring repeats
setup/run only); the rich "Next steps" content exists only in the scaffold-generated README.
Add a trailing `# Next, try:` block to the canonical example and one-line `Next:` notes to the
`mic()/browser()/phone()` and `SessionConfig` docstrings.

**Before / After.** See the `bot.py` block in §2 and the docstring additions:
- `mic()`: "Next: `stt=`/`tts=` to swap providers; `browser()`/`phone()` for other surfaces."
- `browser()`/`phone()`: "Note: browser/phone need a server process + the webrtc/telephony
  extra — see `examples/webrtc_server.py` / `twilio_app.py`."
- `SessionConfig`: "Lowest rung: you supply live provider instances. EasyConfig auto-wires
  these for you one rung up."

**Files touched.** `examples/openai_agents_voice.py`; `src/easycat/config.py` (preset
docstrings); `src/easycat/session/_types.py` (`SessionConfig` docstring).

**Verifier verdict.** Feasible, low-risk (comments/docstrings only — no behavior change). Not a
concept reduction; it is discoverability (Progressive Disclosure). **Caveats folded in:** the
`stt="deepgram/nova-2"` line must be as honest as the browser/phone note — flag that it needs
`DEEPGRAM_API_KEY` **and** `easycat[deepgram]` (otherwise it recreates the "looks like a
one-token swap" footgun). Soften the `SessionConfig` "one rung up" wording: the two configs
differ in field *type* (live providers vs descriptors), not just convenience.

**Zen.** #7 readability; #13 if easy to explain, may be a good idea.

---

### 5.9 — Validate the agent's shape at construction (fail-fast, honestly scoped)  *(adopt-with-changes)*

**What / why.** `agent=` is the one required happy-path field and the least validated: a bogus
`object()` constructs a valid-looking `Session` and dies with an `AttributeError` seconds into
the first turn. The `@runtime_checkable` `Agent` protocol already exists
(`session/_types.py:32-36`).

**After** (placed **before** the `AgentRunner` wrap, in *both* `create_session` *and*
`create_text_session`)
```python
import inspect
from easycat.session._types import Agent as _AgentProto
from easycat.integrations.agents import ExternalAgentBridge
# `adapted` = auto_adapt_agent(config.agent); check BEFORE wrapping in AgentRunner.
if config.wrap_agent and not isinstance(adapted, ExternalAgentBridge):
    run_attr = getattr(adapted, "run", None)
    if not (isinstance(adapted, _AgentProto)
            and inspect.iscoroutinefunction(run_attr)):
        raise EasyConfigError(  # match config.py's existing error style
            "agent must expose `async run(text) -> str` or be a recognized "
            "framework agent (see auto_adapt_agent's supported list)."
        )
```

**Files touched.** `src/easycat/config.py` (both factories, `:769` and `:1129`); optionally
`src/easycat/errors.py` if a coded error is preferred over `EasyConfigError`.

**Verifier verdict.** Feasible, harmless to power users, but **oversold** — `@runtime_checkable`
only checks *method-name presence*, so a sync `run`, a wrong-arity `run`, or a non-callable
`run` still pass a bare `isinstance`. **Corrections folded in:** (1) placement is load-bearing —
the check must run on `adapted` **before** the `AgentRunner` wrap (`AgentRunner` satisfies both
`Agent` and `ExternalAgentBridge`, so a post-wrap check is a no-op); (2) tighten beyond bare
`isinstance` with `inspect.iscoroutinefunction` (and ideally a 1-arg signature check) to catch
the realistic mistakes; (3) apply to both factories; (4) reconcile with `config.py`'s existing
`EasyConfigError`; (5) keep the `wrap_agent=False` skip so deliberate custom flows pass. This is
fail-fast DX, **not** an onramp simplification (concept delta ≈ 0).

**Zen.** #10 refuse to guess; #9 errors never silent.

---

### 5.10 — Print a "what got wired" summary on the TTY happy path  *(adopt-with-changes)*

**What / why.** `EasyConfig` silently derives STT/TTS/echo-cancellation from one key
(`config.py:553-565`) with no echo of what it chose. Make the auto-wiring audible on a TTY.

**After** (`helpers.py:88`, reusing the existing TTY/PYTEST guard)
```python
if sys.stderr.isatty() and not os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("EASYCAT_QUIET"):
    print(_wired_summary(config), file=sys.stderr)   # lazy import of the catalogs inside
    attach_runtime_feedback(session)
```

**Files touched.** `src/easycat/helpers.py` (new private `_wired_summary`);
`src/easycat/config.py` / `stt/factory.py` / `tts/factory.py` (reuse `_provider_display_name`,
`config.py:421-439`).

**Verifier verdict.** Feasible, gated, breaks nothing (the pytest short-circuit keeps tests
silent). **Corrections folded in:** (1) the proposed `echo-cancel=auto` string is **wrong at
print time** — `echo_cancellation` is already resolved to `enabled=True/False`
(`config.py:564-565`); print the resolved on/off, annotating `(auto)` only when
`enable_echo_cancellation is None`. (2) There is **no transport name registry** (`config.py:325`
is a type→factory dict), so `_wired_summary` must own a small `type → label` map
(`LocalTransportConfig→local-mic`, `WebRTCTransportConfig→browser`, `TwilioTransportConfig→phone`,
…). (3) Add the `EASYCAT_QUIET` opt-out; honor `NO_COLOR`/`CI` to match `_output.py:36-42`. It
is a comprehension win, not a line/concept reduction.

**Zen.** #2 explicit is better than implicit.

---

### 5.11 — Collapse teardown to one public idiom: `async with session:`  *(adopt-with-changes)*

**What / why.** `Session` exposes five teardown-ish verbs (`start/stop/shutdown/close/destroy`,
`_session.py:1187/1270/1340/1421/1439`); `close()` *sounds* like the canonical "I'm done" call
but tears down no backends (its own docstring warns callers off it), and `stop`/`shutdown` are
near-duplicates. Make `async with session:` the one documented public idiom.

**After**
```python
async with create_session(cfg) as session:   # __aenter__/__aexit__ already wired (_session.py:1113-1132)
    await session.wait_closed()
# Keep ONE explicit verb stop(force=False); demote close()/destroy() to underscore-private
# (bodies UNCHANGED so postmortem-journal teardown semantics are preserved).
```

**Files touched.** `src/easycat/session/_session.py`; `src/easycat/helpers.py`.

**Verifier verdict.** The context-manager idiom is **already wired** (`__aenter__/__aexit__/
wait_closed`), so this is surface-curation and signposting, not new code. **Caveats folded in:**
do **not** "add `__aenter__/__aexit__`" as if from scratch (they exist); keep one explicit
graceful-vs-force *parameter* (`stop(force=...)`) rather than two same-shaped methods, so the
advanced capability survives; `run()` still hides all of it for the happy path, so the cost
lands only on advanced `create_session` users. Treat the `close()/destroy()` demotion as a
deliberate (documented) change since they are currently public.

**Zen.** #11 one obvious way; #13 if hard to explain, it's a bad idea.

---

### 5.12 — Extract a shared base for the 9 duplicated config fields (Part A only)  *(adopt Part A; drop Part B)*

**What / why.** `EasyConfig` and `TextSessionConfig` re-declare the same 9 agent/journal/debug
fields with no shared base, and the docstring literally tells users to "copy the shared fields
across" (`config.py:1024`). Extract `_AgentSessionConfig` and have both inherit it.

**After**
```python
@dataclass
class _AgentSessionConfig:
    agent: Any = None
    agent_model: str | None = None
    remote_agent_api_key: str | None = None
    agent_runner: AgentRunnerConfig | None = None
    wrap_agent: bool = True
    debug: Literal["off", "light", "full"] = "off"
    journal_backend: Literal["sqlite", "sqlite+litestream", "libsql"] = "sqlite"
    journal_retention: Literal["archive", "delete"] = "archive"
    mcp_servers: list[str] | None = None

@dataclass
class EasyConfig(_AgentSessionConfig): ...          # audio fields (all with defaults)
@dataclass
class TextSessionConfig(_AgentSessionConfig): ...
```

**Files touched.** `src/easycat/config.py`.

**Verifier verdict.** **Part A (the base) is safe and a clean DRY win** — every construction site
is keyword-only, nothing relies on field order, and the snapshot path uses a name-keyed
allowlist (`config.py:680-687`), not `astuple`. Lets you delete the "copy the shared fields
across" docstring. **Part B is DROPPED:** removing `create_text_session`'s loose-kwargs branch
**breaks** the shipped text-chat scaffold (`text-chat/agent.py:12` uses `create_text_session(agent=agent)`),
~15+ tests, and the explicit regression test `tests/test_config.py:649`, and would force
newcomers onto `TextSessionConfig` (not even in `__all__` today) — a worse onramp. Keep the dual
signature exactly as-is. **Caveat:** this is a maintainer-facing DRY win, essentially invisible to
a newcomer — do not sell it as concept reduction. Future maintainers must keep base fields
all-defaulted so the subclass `field(default_factory=...)` audio fields compose.

**Zen.** #4 complex is better than complicated (DRY).

---

### 5.13 — Correct the false "Silero VAD requires torch" install doc  *(adopt)*

**What / why.** `README.md:675-687` tells newcomers Silero "requires torch" and to
`uv pip install torch`. This is false: Silero ships a bundled ONNX model run via `onnxruntime`
(already in `quickstart`); torch is an optional speed-up that falls back to the bundled ONNX
(`silero.py:32,174`, `vad/factory.py:69`). The doc sends users to install a multi-hundred-MB GPU
library they do not need.

**After.** State that Silero runs on the bundled ONNX model via `onnxruntime` (in `quickstart`),
and list torch only as an optional CPU/GPU speed-up. The lean install becomes
`uv sync --extra local --extra openai --extra openai-agents --extra rnnoise --extra silero-vad`
with no torch line.

**Files touched.** `README.md`.

**Verifier verdict.** Documentation correctness; adopt.

**Zen.** #3 simple; #2 explicit.

---

## 6. Recommended first PR

**Ship §5.2 + §5.3 together: make the most common first-run failure teach the fix, on the path
beginners actually take.** This is the single highest-leverage change because (a) the forgotten
`OPENAI_API_KEY` is statistically the #1 first-run error, (b) the fix re-uses machinery that
already exists (`EASYCAT_E203` + its substituted fix template), and (c) it lands the benefit on
the *programmatic* path (`python bot.py` / `run()`), not just the CLI.

**Concrete diff-level sketch (implementable today):**

1. `src/easycat/errors.py` — add the `_render()` method to `EasyCatError` (§5.2), with the
   `try/except (KeyError, IndexError)` guard around `entry.fix.format(**self.context)` mirroring
   the factory guard at `errors.py:96-101`. Keep `EasyCatError(Exception)` (do not subclass
   `ValueError`).
2. `src/easycat/config.py` —
   - add `from easycat.errors import EASYCAT_E203` (top of file or local, matching existing
     callers);
   - in `_validate` (`config.py:598-606`), insert the no-key/None branch
     `if (self.stt is None or self.tts is None) and not self.openai_api_key: raise EASYCAT_E203(var="OPENAI_API_KEY")`
     **before** the two `ValueError` lines;
   - **leave** the per-provider empty-key `ValueError` (with `_provider_display_name`) untouched —
     there is no `(cfg, kind) → env-var` helper today and the None branch captures ~all the
     leverage.
3. `tests/` —
   - `tests/test_config.py` — add a case asserting `EASYCAT_E203` (an `EasyCatError`) is raised
     when no key and no `stt`/`tts` are supplied; verify the existing `pytest.raises(ValueError, ...)`
     empty-key cases at `:622-635` still pass (they do, because that branch is unchanged);
   - `tests/test_examples.py:429-434` — review only if a no-key stderr assertion is exercised.
4. Manual check: `OPENAI_API_KEY= python bot.py` now prints
   `EASYCAT_E203: Missing API key: OPENAI_API_KEY\n  Fix: Set the env var: ...\n  Run \`easycat explain EASYCAT_E203\` for details.`
   instead of `ValueError: STT configuration is required.`

This PR is self-contained (`errors.py` + `config.py` + tests), introduces no new public name, no
new concept, and breaks no power user — the CLI/JSON output reads `.code/.message/.context`, not
`str(err)`.

---

## 7. What we deliberately KEEP and do NOT change

- **The 3-line happy path.** `run(EasyConfig.mic(agent=...))` is the floor and stays exactly as
  it is. No new top-level verb (`talk()`, `Bot`), no rename of `EasyConfig` to `App`. The
  existing shape is *promoted*, not replaced — the migration is mechanical, not vocabulary-breaking.
- **The full-power `SessionConfig` + `Session` escape hatch.** Level 5 of the ladder is preserved
  verbatim. Hand-built provider instances, custom transports, and the `easycat.providers`
  Protocols remain the documented advanced surface. We add a steering sentence to its docstring;
  we remove no capability.
- **The tested 84-name `__all__` contract.** We do **not** shrink it (verifier: drop). It is a
  deliberately-curated, recently-reviewed release contract (`git` commits "api: trim public
  surface…"), `test_public_api.py` pins it exactly, and `dir()` re-sorts so a "tiered" `__all__`
  would not even change autocomplete order. Discoverability is solved at the docstring/`bot.py`
  layer instead, where it actually moves the needle.
- **Lazy PEP-562 loading + the `TYPE_CHECKING` mirror.** Cold-start cost, go-to-definition, and
  `from easycat import STTFinal` all stay unchanged. Every `_register` call is preserved.
- **The `.mic()/.browser()/.phone()` presets.** A genuinely good "namespaces are one honking
  great idea" application — thin, well-named, degrade gracefully. Kept and signposted.
- **`auto_adapt_agent()` and pass-a-raw-Agent ergonomics.** The construction-time agent check
  (§5.9) is additive and skipped when `wrap_agent=False`; raw framework objects and deliberate
  custom `async run` flows continue to pass.
- **`create_session` and `create_text_session` as entry points.** `create_session` is relabelled
  (not removed) as the advanced lifecycle door; `create_text_session` keeps its dual
  config-or-kwargs signature (Part B of §5.12 dropped) so the shipped text-chat scaffold and the
  one-import `create_text_session(agent=...)` form keep working.
- **The 16-chapter teaching ladder (`docs/teaching/`).** The coherent Build→Operate→Generalise→Ship
  curriculum is untouched; the changes here merely add in-code cross-links *into* it so the
  ground-up path is reachable from the editor.
