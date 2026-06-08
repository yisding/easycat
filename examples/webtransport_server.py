"""Multi-client WebTransport server example for EasyCat.

Setup:

  uv sync --extra openai --extra openai-agents --extra webtransport --group dev
  # Generate a local self-signed cert (any tool works; openssl shown):
  openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem \
      -days 1 -nodes -subj "/CN=localhost"
  export OPENAI_API_KEY="..."
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run python examples/webtransport_server.py --cert cert.pem --key key.pem
  uv run --env-file .env python examples/webtransport_server.py --cert cert.pem --key key.pem

Open ``examples/webtransport_browser_client.html`` in Chrome.  WebTransport
requires a trusted certificate; for local development, launch Chrome with
``--ignore-certificate-errors-spki-list=<SPKI>`` (compute the SPKI hash from
your cert with ``openssl x509 ...``) or use ``--ignore-certificate-errors``.

Each connecting browser tab gets its own EasyCat ``Session``.
"""

from __future__ import annotations

import argparse

from easycat import (
    EasyConfig,
    WebTransportConnectionTransport,
    WebTransportTransportConfig,
    require_env,
)
from easycat.transports import run_webtransport_config_server


def main(args: argparse.Namespace) -> None:
    require_env("OPENAI_API_KEY")

    def config(transport: WebTransportConnectionTransport) -> EasyConfig:
        from agents import Agent  # type: ignore[import-untyped]

        agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
        return EasyConfig(transport=transport, agent=agent)

    run_webtransport_config_server(
        config,
        WebTransportTransportConfig(
            host=args.host, port=args.port, certfile=args.cert, keyfile=args.key, path=args.path
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=4433)
    parser.add_argument("--cert", required=True, help="TLS certificate (PEM)")
    parser.add_argument("--key", required=True, help="TLS private key (PEM)")
    parser.add_argument("--path", default="/easycat")
    main(parser.parse_args())
