from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from ocpp_mitm.log import EventLog
from ocpp_mitm.proxy import Config, Proxy, callback_app, charger_app
from ocpp_mitm.rewrite import Rewriter


class RecordingLog(EventLog):
    def __init__(self) -> None:
        super().__init__(jsonl=None, console=None)
        self.events: list[dict[str, Any]] = []

    def emit(self, **event: Any) -> None:
        self.events.append(event)


@pytest.fixture
async def serve():
    """Start an aiohttp app on a free port; returns its TestServer."""
    servers: list[TestServer] = []

    async def start(app: web.Application) -> TestServer:
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return server

    yield start
    for server in servers:
        await server.close()


@pytest.fixture
async def proxy_for(serve):
    """Run the proxy in front of `upstream` with a callback listener; returns (proxy, charger-facing, callback)."""
    opened: list[Proxy] = []

    async def start(upstream: str, rewrites: list[str] = ()):
        # The callback URL embeds the callback listener's port, so start it first.
        proxy = Proxy(Config(upstream=upstream, callback_url="", rewriter=Rewriter.parse(list(rewrites))), RecordingLog())
        await proxy.__aenter__()
        opened.append(proxy)
        callback = await serve(callback_app(proxy))
        proxy.config.callback_url = str(callback.make_url("/"))
        front = await serve(charger_app(proxy))
        return proxy, front, callback

    yield start
    for proxy in opened:
        await proxy.__aexit__(None, None, None)
