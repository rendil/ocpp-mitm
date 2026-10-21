"""ocpp-mitm command line."""

import argparse
import asyncio
import socket
import ssl
import sys
from datetime import datetime
from pathlib import Path

from aiohttp import web

from .log import EventLog
from .proxy import Config, Proxy, callback_app, charger_app
from .rewrite import Rewriter


def _host_port(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "0.0.0.0", int(port)


def _lan_ip() -> str:
    # Connecting a UDP socket sends nothing; it only picks the outbound interface.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def _server_tls(cert: str | None, key: str | None) -> ssl.SSLContext | None:
    if not cert:
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    return ctx


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="ocpp-mitm",
        description="Sit between an OCPP charger and its controller, logging every message. "
        "Point the charger's controller URL at --listen; WebSocket upgrades are relayed as OCPP-J, "
        "POSTs as OCPP SOAP.",
    )
    p.add_argument("--upstream", required=True,
                   help="real controller base URL, e.g. wss://csms.example.com/ocpp (the charger's "
                        "request path, usually /<chargePointId>, is appended)")
    p.add_argument("--listen", default="0.0.0.0:9000", type=_host_port, help="charger-facing address (default %(default)s)")
    p.add_argument("--cert", help="PEM certificate for the charger-facing listener (serves wss/https)")
    p.add_argument("--key", help="PEM private key for --cert")
    p.add_argument("--callback-listen", default="0.0.0.0:9001", type=_host_port,
                   help="SOAP only: address the controller calls chargers on (default %(default)s)")
    p.add_argument("--callback-url",
                   help="SOAP only: URL advertised to the controller in place of the charger's own "
                        "(default http(s)://<this machine's LAN IP>:<callback port>)")
    p.add_argument("--callback-cert", help="PEM certificate for the callback listener")
    p.add_argument("--callback-key", help="PEM private key for --callback-cert")
    p.add_argument("--rewrite", action="append", default=[], metavar="CHARGER=CONTROLLER",
                   help="replace CHARGER with CONTROLLER in everything sent to the controller, and the "
                        "reverse towards the charger; repeatable")
    p.add_argument("--insecure", action="store_true", help="do not verify the controller's TLS certificate")
    p.add_argument("--log", type=Path, help="JSONL log file (default ./ocpp-<timestamp>.jsonl)")
    p.add_argument("--full", action="store_true", help="print whole messages on the terminal, not 200 characters")
    return p.parse_args(argv)


async def serve(args: argparse.Namespace) -> None:
    callback_host, callback_port = args.callback_listen
    callback_url = args.callback_url or (
        f"{'https' if args.callback_cert else 'http'}://{_lan_ip()}:{callback_port}"
    )
    config = Config(
        upstream=args.upstream,
        callback_url=callback_url,
        rewriter=Rewriter.parse(args.rewrite),
        verify_upstream=not args.insecure,
    )
    log_path = args.log or Path(f"ocpp-{datetime.now():%Y%m%d-%H%M%S}.jsonl")

    with log_path.open("a", encoding="utf-8") as jsonl:
        async with Proxy(config, EventLog(jsonl=jsonl, full=args.full)) as proxy:
            runners = []
            for app, (host, port), tls in (
                (charger_app(proxy), args.listen, _server_tls(args.cert, args.key)),
                (callback_app(proxy), args.callback_listen, _server_tls(args.callback_cert, args.callback_key)),
            ):
                runner = web.AppRunner(app, access_log=None)
                await runner.setup()
                await web.TCPSite(runner, host, port, ssl_context=tls).start()
                runners.append(runner)

            listen_host, listen_port = args.listen
            scheme = "wss/https" if args.cert else "ws/http"
            print(f"chargers  -> {scheme} on {listen_host}:{listen_port}  (point the charger at {_lan_ip()}:{listen_port})", file=sys.stderr)
            print(f"upstream  -> {args.upstream}", file=sys.stderr)
            print(f"callbacks -> {callback_host}:{callback_port}, advertised as {callback_url} (SOAP)", file=sys.stderr)
            print(f"log       -> {log_path}", file=sys.stderr)
            try:
                await asyncio.Event().wait()
            finally:
                for runner in runners:
                    await runner.cleanup()


def main(argv: list[str] | None = None) -> None:
    try:
        asyncio.run(serve(parse_args(argv)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
