# EasyCat Architecture Review (2026-02-11)

## Scope

Reviewed core runtime/config wiring, provider factories, packaging metadata, and representative tests/docs.

Files sampled heavily:

- `src/easycat/config.py`
- `src/easycat/session.py`
- `src/easycat/stt/factory.py`
- `src/easycat/tts/factory.py`
- `src/easycat/__init__.py`
- `README.md`
- `pyproject.toml`

## Key findings

### 1) Duplicate provider-construction paths (architectural duplication)

There are currently two distinct ways providers get instantiated:

- `create_session()` in `config.py` does explicit `isinstance(...)` branching for STT/TTS/transport provider configs.
- Standalone factories (`stt/factory.py` and `tts/factory.py`) have their own provider registries and validation logic.

This creates a drift risk where one path gains features/validation and the other silently diverges.

**Recommendation:** Consolidate to one provider registry/factory mechanism and have `create_session()` call that abstraction.

### 2) `create_session()` mutates caller-owned config objects (odd side effects)

`create_session()` mutates incoming config state in-place in multiple places:

- Adds `event_bus` into Deepgram/ElevenLabs config objects when missing.
- Mutates `turn_config.endpoint_detector` to install smart-turn detector.

If the same `EasyCatConfig` instance is reused across sessions, behavior may become non-obvious and cross-session state can leak.

**Recommendation:** Treat config objects as immutable inputs; clone/copy nested config before injecting runtime concerns.

### 3) Runtime behavior attached through private attributes (encapsulation leak)

Telephony helpers are attached via:

- `session._dtmf_aggregator = ...`
- `session._voicemail_detector = ...`

These ad-hoc private attributes make lifecycle ownership implicit and fragile.

**Recommendation:** Add typed fields and lifecycle hooks on `Session` (or a helper manager) rather than monkey-patching private attributes.

### 4) Public API surface (`easycat.__init__`) is broad and includes many optional-provider imports

Top-level package exports a large set of classes/configs/providers. This is user-friendly, but it increases import-coupling and makes API evolution harder.

**Recommendation:** Keep ergonomic top-level exports for common API only, and move advanced provider-level symbols to explicit submodule imports in docs.

### 5) Tooling/docs mismatch discovered during checks

- `README.md` stated optional dependency extras were "not yet in this repo".
- `pyproject.toml` already defines many extras (`openai`, `deepgram`, `elevenlabs`, etc.).

This has now been corrected in this branch.

## Possible dead or low-value code areas to verify

These were not proven dead, but are candidates for a quick usage audit:

- Utility/provider factory overlap (`config.py` creation helpers vs `stt/tts` factory modules).
- `src/easycat/tts/test_harness.py` appears as runtime code but may be more appropriate under `tests/` if only used for experimentation.

## Priority order

1. **High:** Remove config mutation side effects in `create_session()`.
2. **High:** Unify provider creation into one abstraction.
3. **Medium:** Replace private-attribute telephony helper attachment with explicit lifecycle-managed composition.
4. **Low/Medium:** Trim top-level exports to a smaller stable API.

