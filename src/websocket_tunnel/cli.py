"""Command line interface for wtunnel."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .client import TunnelClient
from .config import ConfigError, load_client_config, load_server_config
from .server import TunnelServer


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wtunnel",
        description="WebSocket-based intranet penetration tool (frp-like).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("server", help="run the tunnel server")
    server.add_argument("-c", "--config", type=Path, required=True, help="path to server TOML config")
    server.add_argument("--listen", metavar="HOST:PORT", help="override listen address")
    server.add_argument("--token", help="override shared auth token")
    server.add_argument("--tls-cert", metavar="PATH", help="override TLS certificate path")
    server.add_argument("--tls-key", metavar="PATH", help="override TLS private key path")
    server.add_argument("-v", "--verbose", action="count", default=0, help="increase log verbosity (repeatable)")

    client = subparsers.add_parser("client", help="run the tunnel client")
    client.add_argument("-c", "--config", type=Path, required=True, help="path to client TOML config")
    client.add_argument("--server", metavar="HOST:PORT", help="override tunnel server address")
    client.add_argument("--token", help="override shared auth token")
    client.add_argument("--tls", action="store_true", default=None, help="override: use wss transport")
    client.add_argument(
        "--tls-skip-verify",
        action="store_true",
        default=None,
        help="override: skip TLS certificate verification",
    )
    client.add_argument("-v", "--verbose", action="count", default=0, help="increase log verbosity (repeatable)")
    return parser


def _setup_logging(verbosity: int) -> None:
    level = logging.DEBUG if verbosity else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        if args.command == "server":
            config = load_server_config(
                args.config,
                listen=args.listen,
                token=args.token,
                tls_cert=args.tls_cert,
                tls_key=args.tls_key,
            )
            return asyncio.run(TunnelServer(config).run())
        config = load_client_config(
            args.config,
            server=args.server,
            token=args.token,
            tls=args.tls,
            tls_skip_verify=args.tls_skip_verify,
        )
        return asyncio.run(TunnelClient(config).run())
    except ConfigError as exc:
        print(f"wtunnel: configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
