from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.examples._examples_helpers import (
    REPO_ROOT,
    Headers,
    _docker_build_arg_extra_sets,
    _dockerfile_default_extras,
    _documented_support_file_links,
    _support_files_to_document,
    _uses_default_openai_providers,
    _visible_code_line_count,
    pytest,
    tomllib,
)


def test_examples_readme_lists_browser_and_deploy_support_files() -> None:
    documented = _documented_support_file_links()
    expected = _support_files_to_document()

    missing = sorted(expected - documented)
    stale = sorted(documented - expected)

    assert not missing, "examples/README.md missing support-file links for: " + ", ".join(missing)
    assert not stale, "examples/README.md has stale support-file links for: " + ", ".join(stale)


def test_bundled_browser_playground_page_serves_transcript_and_latency_ui() -> None:
    """Served-page smoke: the bundled WebRTC client that ``easycat serve`` and
    ``examples/webrtc_server.py`` serve must ship the playground widgets —
    live transcript, interruption indicator, latency readout, the server →
    browser events data channel, and the debugger UI link."""
    client = (
        REPO_ROOT / "src" / "easycat" / "transports" / "static" / "webrtc_client.html"
    ).read_text(encoding="utf-8")

    assert 'pc.createDataChannel("events")' in client
    assert 'id="transcript"' in client
    assert 'id="latency"' in client
    assert 'id="interruption"' in client
    assert 'id="debuggerLink"' in client
    for message_type in (
        "stt_partial",
        "stt_final",
        "agent_delta",
        "agent_final",
        "turn_started",
        "interruption",
        "turn_latency",
    ):
        assert f'"{message_type}"' in client, f"client misses {message_type} handling"
    # Token forwarding keeps the WS/docker security defaults working when
    # ``easycat serve --token`` prints a tokenized Open URL.
    assert 'new URLSearchParams(location.search).get("token")' in client


def test_ec2_webrtc_deploy_docs_do_not_claim_to_configure_https() -> None:
    server = (REPO_ROOT / "examples" / "webrtc_server.py").read_text(encoding="utf-8")
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "configures HTTPS" not in server
    assert "behind an HTTPS reverse proxy" in server
    assert "Backend HTTP URL: http://$EXTERNAL_IP:8080/webrtc_client.html" in deploy
    assert "Browser URL:      https://<your-domain>/webrtc_client.html" in deploy
    assert (
        "Signaling URL:    https://<your-domain>                     (after TLS proxy)" in deploy
    )
    assert "Signaling URL:    https://<your-domain>/offer" not in deploy
    assert "Client URL:      http://$EXTERNAL_IP:8080/webrtc_client.html" not in deploy


def test_ec2_webrtc_deploy_keeps_browser_turn_credentials_opt_in() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "WEBRTC_EXPOSE_ICE_CREDENTIALS=0" in deploy
    assert "Browser TURN auth remains hidden from /config by default." in deploy
    assert "trusted demos or short-lived TURN creds" in deploy


def test_ec2_webrtc_turns_port_is_optional_until_certs_are_configured() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")
    coturn = (REPO_ROOT / "examples" / "ec2_webrtc" / "coturn.conf").read_text(encoding="utf-8")

    assert "TURN_SERVER_URL=turn:$EXTERNAL_IP:3478" in deploy
    assert "TCP 8080, TCP/UDP 3478, UDP 49152-65535" in deploy
    assert "Optional TURNS: TCP 5349 after coturn cert/pkey are configured" in deploy
    assert "TCP 5349   — TURNS" not in deploy
    assert "# tls-listening-port=5349" in coturn
    assert "\ntls-listening-port=5349" not in coturn


def test_ec2_webrtc_turn_template_handles_generated_password_characters() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "openssl rand -base64 24" in deploy
    assert 'sed -i "s/__TURN_PASSWORD__/$TURN_PASSWORD/"' not in deploy
    assert 'sed -i "s/__EXTERNAL_IP__/$EXTERNAL_IP/"' not in deploy
    assert "<<'PY' | sudo tee /etc/turnserver.conf" in deploy
    assert '.replace("__TURN_PASSWORD__", sys.argv[3])' in deploy


def test_ec2_webrtc_deploy_enables_coturn_across_default_variants() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "grep -Eq '^#?TURNSERVER_ENABLED='" in deploy
    assert "s/^#?TURNSERVER_ENABLED=.*/TURNSERVER_ENABLED=1/" in deploy
    assert "tee -a /etc/default/coturn" in deploy
    assert "s/#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/" not in deploy


def test_ec2_webrtc_deploy_honors_manual_external_ip() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert 'EXTERNAL_IP="${EXTERNAL_IP:-$(detect_external_ip)}"' in deploy
    assert "--max-time 2" in deploy
    assert "export EXTERNAL_IP=1.2.3.4" in deploy
    assert "EXTERNAL_IP=$(curl" not in deploy


def test_ec2_webrtc_deploy_detects_external_ip_with_imdsv2_first() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "detect_external_ip()" in deploy
    assert "latest/api/token" in deploy
    assert "X-aws-ec2-metadata-token-ttl-seconds: 21600" in deploy
    assert "X-aws-ec2-metadata-token: $token" in deploy
    assert deploy.count("latest/meta-data/public-ipv4") == 2


def test_ec2_webrtc_deploy_does_not_copy_local_secret_or_cache_dirs() -> None:
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert 'sudo cp -a "$REPO_ROOT/."' not in deploy
    assert 'tar -C "$REPO_ROOT"' in deploy
    for excluded in (
        "./.agents",
        "./.claude",
        "./.codex",
        "./.coverage",
        "./.coverage.*",
        "./.easycat",
        "./.env",
        "./.env.*",
        "./.git",
        "./.hypothesis",
        "./.mypy_cache",
        "./.mutmut-cache",
        "./.pipecat-bench",
        "./.pytest_cache",
        "./.ruff_cache",
        "./.uv-cache",
        "./.venv",
        "./coverage.xml",
        "./htmlcov",
        "./mutants",
        "./site",
        "__pycache__",
        "*.key",
        "*.pem",
        "*.pyc",
        "*.pyo",
    ):
        assert f"--exclude='{excluded}'" in deploy


def test_ws_server_example_imports():
    import examples.ws_server as ws_server

    assert callable(ws_server.main)


def test_ws_server_uses_config_server_helper() -> None:
    source = (REPO_ROOT / "examples" / "ws_server.py").read_text(encoding="utf-8")

    assert "run_websocket_config_server" in source
    assert "create_session" not in source
    assert 'require_env("OPENAI_API_KEY")' in source


def test_ws_server_settings_default_to_loopback(monkeypatch: pytest.MonkeyPatch):
    from easycat.transports.websocket import websocket_session_server_config_from_env

    monkeypatch.delenv("EASYCAT_WS_HOST", raising=False)
    monkeypatch.delenv("EASYCAT_WS_PORT", raising=False)
    monkeypatch.delenv("EASYCAT_WS_TOKEN", raising=False)
    monkeypatch.delenv("EASYCAT_WS_MAX_SESSIONS", raising=False)

    settings = websocket_session_server_config_from_env()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.auth_token is None
    assert settings.max_sessions == 10


def test_webtransport_server_example_imports():
    import examples.webtransport_server as webtransport_server

    assert callable(webtransport_server.main)


def test_webtransport_server_uses_config_server_helper() -> None:
    path = REPO_ROOT / "examples" / "webtransport_server.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 35
    assert "run_webtransport_config_server" in source
    assert "WebTransportTransportConfig" in source
    assert "WebTransportConnectionTransport" in source
    assert "create_session" not in source
    assert "SessionManager" not in source
    assert "attach_runtime_feedback" not in source
    assert "wait_for_shutdown_signal" not in source
    assert "asyncio.run(" not in source
    assert "await server.start()" not in source
    assert "await server.stop()" not in source


def test_ws_server_authorizes_bearer_or_query_token():
    # The WebSocket handshake now authorizes through the UNIFIED
    # ``BearerTokenAuth``/``from_websocket`` path (the old
    # ``websocket_server_authorized`` helper was removed). Same contract.
    from easycat.server.auth import BearerTokenAuth, from_websocket

    def authorized(headers: Headers, path: str, *, allow_query_token: bool = False) -> bool:
        auth = BearerTokenAuth(token="expected-token", allow_query_token=allow_query_token)
        return auth.authorize(from_websocket(headers, path)).allowed

    headers = Headers([("Authorization", "Bearer expected-token")])

    # Bearer-header auth works regardless of allow_query_token.
    assert authorized(headers, "/")
    # ?token= query auth is OFF by default (breaking change) and ON only when
    # allow_query_token=True (the loopback/dev opt-in for the browser client).
    assert not authorized(Headers(), "/?token=expected-token")
    assert authorized(Headers(), "/?token=expected-token", allow_query_token=True)
    assert not authorized(Headers(), "/")
    assert not authorized(Headers([("Authorization", "Bearer wrong")]), "/")


def test_docker_compose_binds_ws_port_to_loopback_and_requires_token():
    compose = (REPO_ROOT / "docker" / "compose.yaml").read_text()

    assert "EASYCAT_WS_TOKEN: ${EASYCAT_WS_TOKEN:?set EASYCAT_WS_TOKEN" in compose
    assert '"127.0.0.1:8765:8765"' in compose
    assert '- "8765:8765"' not in compose


def test_docker_entrypoint_warns_when_missing_data_dir_parent_is_unwritable(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "read-only"
    parent.mkdir()
    parent.chmod(0o500)
    if os.access(parent, os.W_OK):
        pytest.skip("current user can write through read-only mode bits")

    env = {
        **os.environ,
        "OPENAI_API_KEY": "synthetic-test-key",
        "EASYCAT_DATA_DIR": str(parent / "missing" / "data"),
        "EASYCAT_WS_HOST": "127.0.0.1",
    }
    try:
        result = subprocess.run(
            ["bash", str(REPO_ROOT / "docker" / "entrypoint.sh"), "true"],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )
    finally:
        parent.chmod(0o700)

    assert result.returncode == 0
    assert "cannot be created; nearest existing ancestor" in result.stderr


def test_docker_guide_serves_browser_client_from_localhost():
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    client = (REPO_ROOT / "examples" / "ws_browser_client.html").read_text(encoding="utf-8")

    assert "python -m http.server 8080 --directory examples" in guide
    assert "http://localhost:8080/ws_browser_client.html?token=<EASYCAT_WS_TOKEN>" in guide
    assert "`examples/ws_browser_client.html?token=" not in guide
    assert 'location.hostname + ":8765"' in client


def test_docker_env_secret_file_is_ignored_but_templates_are_allowed():
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "# docker/.env" in guide
    assert "docker compose --env-file docker/.env -f docker/compose.yaml up --build" in guide
    assert "picks it up automatically" not in guide
    assert ".env" in gitignore
    assert ".env.*" in gitignore
    assert "!.env.example" in gitignore
    assert "**/.env" in dockerignore
    assert "**/.env.*" in dockerignore
    assert "!**/.env.example" in dockerignore
    assert "**/*.pem" in dockerignore
    assert "**/*.key" in dockerignore
    assert "`**/*.pem` and `**/*.key`" in guide


def test_dockerignore_excludes_local_cache_and_agent_state() -> None:
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    for pattern in (
        ".hypothesis/",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".uv-cache/",
        ".agents/",
        ".codex",
        ".codex/",
        ".claude/",
        ".pipecat-bench/",
        ".coverage",
        ".coverage.*",
        "coverage.xml",
        "htmlcov/",
        "site/",
        "mutants/",
        ".mutmut-cache",
    ):
        assert pattern in dockerignore
        assert f"`{pattern}`" in guide

    assert "local generated state is not uploaded" in guide
    assert "Generated reports and docs sites" in guide


def test_docker_guide_tracks_default_dockerfile_extras() -> None:
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    image_section = guide.split("## What the image contains", 1)[1].split("## ", 1)[0]
    extras = _dockerfile_default_extras()
    known_extras = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["optional-dependencies"]

    assert extras <= set(known_extras)
    assert "Dockerfile `EXTRAS` default" in image_section
    for extra in extras:
        assert f"`{extra}`" in image_section


def test_dockerfile_default_extras_cover_ws_server_golden_path() -> None:
    extras = _dockerfile_default_extras()
    ws_server = REPO_ROOT / "examples" / "ws_server.py"

    assert _uses_default_openai_providers(ws_server)
    assert "openai" in extras
    assert "openai-agents" in extras


def test_docker_provider_swap_guidance_uses_known_extras_and_easyconfig() -> None:
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "deployment" / "docker.md").read_text(encoding="utf-8")
    swap_section = guide.split("## Swapping STT / TTS providers", 1)[1].split("## ", 1)[0]
    known_extras = set(
        tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "optional-dependencies"
        ]
    )

    build_arg_extra_sets = _docker_build_arg_extra_sets(dockerfile, swap_section)
    assert build_arg_extra_sets
    for extras in build_arg_extra_sets:
        assert extras <= known_extras

    assert "wire the providers into `EasyConfig`" in swap_section
    assert "wire the providers into `SessionConfig`" not in swap_section


def test_ws_supervisor_server_example_imports():
    import examples.ws_supervisor_server as ws_supervisor_server

    assert callable(ws_supervisor_server.main)


def test_ws_supervisor_server_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch):
    import examples.ws_supervisor_server as ws_supervisor_server

    monkeypatch.delenv("EASYCAT_WS_CALLER_HOST", raising=False)
    monkeypatch.delenv("EASYCAT_WS_SUPERVISOR_HOST", raising=False)

    settings = ws_supervisor_server._load_settings()

    assert settings.caller_host == "127.0.0.1"
    assert settings.supervisor_host == "127.0.0.1"


def test_ws_supervisor_server_uses_configured_hosts(monkeypatch: pytest.MonkeyPatch):
    import examples.ws_supervisor_server as ws_supervisor_server

    monkeypatch.setenv("EASYCAT_WS_CALLER_HOST", "0.0.0.0")
    monkeypatch.setenv("EASYCAT_WS_SUPERVISOR_HOST", "127.0.0.1")

    settings = ws_supervisor_server._load_settings()

    assert settings.caller_host == "0.0.0.0"
    assert settings.supervisor_host == "127.0.0.1"


def test_ws_supervisor_server_uses_manager_feedback_lifecycle() -> None:
    path = REPO_ROOT / "examples" / "ws_supervisor_server.py"
    source = path.read_text(encoding="utf-8")

    assert _visible_code_line_count(path) <= 155
    assert "manager.connection(session_id, session, runtime_feedback=True)" in source
    assert "SessionAudioBroadcaster(session)" in source
    assert "serve_supervisor_websocket(" in source
    assert "supervisor_auth_token_from_env()" in source
    assert "create_shutdown_event()" in source
    assert "secrets.token_urlsafe" not in source
    assert "print(supervisor_token)" not in source
    assert "add_signal_handler" not in source
    assert "json.loads(raw)" not in source
    assert "hmac.compare_digest" not in source
    assert "attach_runtime_feedback" not in source


def test_ws_supervisor_client_supports_optional_token() -> None:
    html = (REPO_ROOT / "examples" / "ws_supervisor_client.html").read_text(encoding="utf-8")

    assert 'id="supervisorToken"' in html
    assert "auth_required" in html
    assert "subscribe.token = token" in html


def test_webrtc_observability_example_imports():
    pytest.importorskip("agents")
    import examples.webrtc_observability_server as webrtc_observability

    assert callable(webrtc_observability.main)


def test_webrtc_observability_debugger_url_is_validated():
    html = (REPO_ROOT / "examples/webrtc_static/webrtc_observability.html").read_text()

    assert "function safeDebuggerUrl(value)" in html
    assert 'parsed.protocol === "http:" || parsed.protocol === "https:"' in html
    assert "sameHost || loopbackPair" in html
    assert 'document.getElementById("debugger-frame").src = debuggerUrl' in html
    assert "const url = override ||" not in html


def test_webrtc_examples_default_signaling_to_loopback():
    server = (REPO_ROOT / "examples" / "webrtc_server.py").read_text(encoding="utf-8")
    observability = (REPO_ROOT / "examples" / "webrtc_observability_server.py").read_text(
        encoding="utf-8"
    )
    deploy = (REPO_ROOT / "examples" / "ec2_webrtc" / "deploy.sh").read_text(encoding="utf-8")

    assert "webrtc_transport_config_from_env()" in server
    assert "Bind address (default 127.0.0.1)" in server
    assert 'os.getenv("SIGNALING_HOST", "127.0.0.1")' not in server
    assert "_build_ice_servers" not in server
    assert "_env_flag" not in server
    assert "webrtc_transport_config_from_env(static_dir=_STATIC_DIR)" in observability
    assert 'os.getenv("SIGNALING_HOST", "127.0.0.1")' not in observability
    assert "_build_ice_servers" not in observability
    assert "_env_flag" not in observability
    assert "SIGNALING_HOST=0.0.0.0" in deploy


def test_browser_transport_examples_use_run_session_lifecycle():
    budgets = {
        "examples/ws_browser_example.py": 39,
        "examples/webrtc_server.py": 29,
        "examples/webrtc_observability_server.py": 59,
    }

    for relpath, budget in budgets.items():
        path = REPO_ROOT / relpath
        source = path.read_text(encoding="utf-8")

        assert _visible_code_line_count(path) <= budget
        assert "EasyConfig.browser(" in source
        if relpath == "examples/webrtc_server.py":
            assert "run_webrtc_config_server(config, transport)" in source
            assert "create_session(" not in source
            assert "from easycat.helpers import run_session" not in source
            assert "run_session(session)" not in source
            assert 'require_env("OPENAI_API_KEY")' in source
        else:
            assert "create_session(" in source
            assert "from easycat.helpers import run_session" in source
            assert "run_session(session)" in source
            assert 'require_env("OPENAI_API_KEY")' not in source
        assert "attach_runtime_feedback" not in source
        assert "wait_for_shutdown_signal" not in source
        assert "asyncio.run(" not in source
        assert "await session.start()" not in source
        assert "await session.stop()" not in source
