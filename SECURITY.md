# Security Policy

EasyCat runs a live audio pipeline that holds provider credentials, accepts
network connections, and writes conversation recordings to disk. Please treat
findings in any of those areas as security-relevant.

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.**

Report privately through GitHub's private vulnerability reporting:

1. Go to <https://github.com/yisding/easycat/security/advisories/new>.
2. Describe the problem, the affected version or commit, and the impact.
3. Include the smallest reproduction you have — a config, a request, or a
   failing test is ideal.

If private advisories are unavailable to you, open a normal issue that says
only that you have a security report and asks for a private channel; do not
include details there.

Please give us a chance to ship a fix before publishing. There is no bounty
program.

### What to include

- Affected version or commit, plus the provider/transport in play.
- What an attacker can do, and what access they need first (network reachable,
  local user on the host, valid caller, and so on).
- A reproduction. Redact your own credentials before sending logs, journals,
  or debug bundles — `easycat bundles export` writes a redacted context pack
  and is usually the right thing to attach.

## Supported versions

EasyCat is pre-release and is not published on PyPI. Only the latest `main` is
supported: fixes land there, and there are no backports to older commits or
tags. If you are running a pinned commit, expect to move forward to pick up a
fix.

| Version | Supported |
| --- | --- |
| `main` | ✅ |
| anything older | ❌ — rebase onto `main` |

## Out of scope

- Vulnerabilities in a third-party provider's own service or SDK. Report those
  to that provider; if EasyCat *misuses* their API in a way that weakens
  security, that is in scope here.
- Results that require a configuration the docs warn against — for example
  binding a non-loopback host with authentication explicitly disabled, or
  serving a debug bundle directory publicly.
- Missing hardening on a single-user development machine where the reporter is
  already the local user, unless it also affects a shared or production host.

## Hardening guidance for deployers

These pages carry the security-relevant configuration. They are the fastest
way to check whether a deployment is exposed, and the right place for a
reporter to confirm intended behaviour before filing. To list every
operator-facing route from a checkout:

```bash
uv run easycat docs --audience operators
uv run easycat docs --audience operators --json
```

- **Network exposure, auth, and capacity** —
  [`docs/deployment/production-servers.md`](docs/deployment/production-servers.md):
  bearer-token auth (`BearerTokenAuth`, constant-time comparison, opt-in
  `?token=` query auth), the guard that refuses a non-loopback bind without a
  token, session capacity and draining, and readiness endpoints.
- **Container and replica targets** —
  [`docs/deployment/docker.md`](docs/deployment/docker.md): passing credentials
  by environment rather than baking them into an image, and the Litestream /
  libSQL replica topologies.
- **Multi-caller isolation** —
  [`docs/using-easycat/09-multi-caller/README.md`](docs/using-easycat/09-multi-caller/README.md):
  per-caller session isolation and bounded rejection under load.
- **Telephony webhook trust** —
  [`docs/using-easycat/10-telephony/README.md`](docs/using-easycat/10-telephony/README.md):
  webhook signature validation, why a valid signature is not authorization for
  an outbound-call endpoint, and the one-use media-stream token.
- **Recording and redaction** —
  [`docs/observability.md`](docs/observability.md) and
  [`src/easycat/runtime/DURABILITY.md`](src/easycat/runtime/DURABILITY.md):
  what the journal stores, `journal_redaction="secrets"` (the default) versus
  the irreversible `"pii"` mode, and what a redacted export does and does not
  remove.
- **Error and diagnostic output** — [`docs/cli.md`](docs/cli.md): journal
  search/follow output and `bundles export` context packs are redacted; raw
  journals are not.

## Credentials

EasyCat reads provider credentials from the environment (or a `.env` you
point it at) and never writes them to a journal or an exported bundle in
plaintext. If you find a path that does leak a credential — a log line, a
journal record, an exported bundle, a subprocess command line, an error
message — that is a vulnerability worth reporting.
