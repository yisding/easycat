"""Multi-client WebTransport server example for EasyCat.

Setup:

  uv sync --extra openai-agents --extra webtransport
  # Generate a local self-signed cert (any tool works; openssl shown):
  openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
      -days 1 -nodes -subj "/CN=localhost"
  export OPENAI_API_KEY="..."
  uv run python examples/webtransport_server.py --cert cert.pem --key key.pem

Open ``examples/webtransport_browser_client.html`` in Chrome.  WebTransport
requires a trusted certificate; for local development, launch Chrome with
``--ignore-certificate-errors-spki-list=<SPKI>`` (compute the SPKI hash from
your cert with ``openssl x509 ...``) or use ``--ignore-certificate-errors``.

Each connecting browser tab gets its own EasyCat ``Session``.
"""

from __future__ import annotations

import argparse
import asyncio
import signal

from easycat import (
    EasyConfig,
    SessionManager,
    WebTransportConnectionTransport,
    WebTransportServer,
    WebTransportTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
)


async def main(args: argparse.Namespace) -> None:
    api_key = require_env("OPENAI_API_KEY")
    from agents import Agent  # type: ignore[import-untyped]

    manager: SessionManager[int] = SessionManager()

    async def handle_connection(transport: WebTransportConnectionTransport) -> None:
        agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
        session = create_session(
            EasyConfig(
                openai_api_key=api_key,
                transport=transport,
                agent=agent,
            )
        )
        attach_runtime_feedback(session)
        async with manager.connection(id(transport), session):
            await transport.wait_closed()

    server = WebTransportServer(
        WebTransportTransportConfig(
            host=args.host,
            port=args.port,
            certfile=args.cert,
            keyfile=args.key,
            path=args.path,
        ),
        handle_connection,
    )
    await server.start()
    print(
        f"\nServer ready. Connect WebTransport clients to https://{args.host}:{args.port}{args.path}"
    )
    print("Press Ctrl+C to stop.\n")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await server.stop()
    await manager.stop_all()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=4433)
    parser.add_argument("--cert", required=True, help="TLS certificate (PEM)")
    parser.add_argument("--key", required=True, help="TLS private key (PEM)")
    parser.add_argument("--path", default="/easycat")
    asyncio.run(main(parser.parse_args()))
