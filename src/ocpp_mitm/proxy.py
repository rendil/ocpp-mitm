"""Shared proxy state and the two aiohttp apps: charger-facing and controller-callback."""

from dataclasses import dataclass, field
from typing import NamedTuple
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp
from aiohttp import web

from .log import EventLog
from .rewrite import Rewriter


@dataclass
class Config:
    # Base URL of the real controller. ws(s):// and http(s):// are interchangeable;
    # OCPP-J uses the ws form and SOAP the http form of the same host and path.
    upstream: str
    # URL the controller should use to reach chargers through us (SOAP only).
    callback_url: str
    rewriter: Rewriter = field(default_factory=Rewriter)
    verify_upstream: bool = True
    # Swap the charger's IP for the one it reached us on, per connection.
    auto_rewrite: bool = True
class ChargerRoute(NamedTuple):
    """Where a SOAP charger said it listens, and the rewrites for its traffic."""
    url: str
    rewriter: Rewriter
def _plain_ip(addr: str) -> str:
    return addr.removeprefix("::ffff:")


class Proxy:
    def __init__(self, config: Config, log: EventLog):
        self.config = config
        self.log = log
        self.rewriter = config.rewriter
        # SOAP chargers advertise their own URL in wsa:From; keyed by chargeBoxIdentity.
        self.chargers: dict[str, ChargerRoute] = {}
        self.upstream_ssl = config.verify_upstream
        # Chargers serve self-signed certs as a rule; checking them buys a debugging tool nothing.
        self.charger_ssl = False
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Proxy used outside 'async with'")
        return self._session

    async def __aenter__(self) -> "Proxy":
        # No timeout: OCPP-J sockets live for days, SOAP calls are short.
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        return self

    async def __aexit__(self, *exc) -> None:
        await self.session.close()

    def upstream_url(self, path_qs: str, websocket: bool) -> str:
        scheme, netloc, base, query, _ = urlsplit(self.config.upstream)
        secure = scheme in ("wss", "https")
        scheme = ("wss" if secure else "ws") if websocket else ("https" if secure else "http")
        if path_qs not in ("", "/"):
            base = base.rstrip("/") + path_qs
        return urlunsplit((scheme, netloc, base, query, ""))

    def rewriter_for(self, request: web.Request) -> Rewriter:
        """The configured rewrites, plus charger IP -> our IP for a charger's connection."""
        sockname = request.transport.get_extra_info("sockname") if request.transport else None
        if not self.config.auto_rewrite or not request.remote or not sockname:
            return self.rewriter
        return self.rewriter.with_pair(_plain_ip(request.remote), _plain_ip(sockname[0]))
    def callback_url_for(self, cp_id: str) -> str:
        return f"{self.config.callback_url.rstrip('/')}/{quote(cp_id, safe='')}"


def charger_app(proxy: Proxy) -> web.Application:
    from . import ocppj, soap

    async def route(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await ocppj.handle(proxy, request)
        if request.method == "POST":
            return await soap.handle_from_charger(proxy, request)
        return web.Response(status=405, text="OCPP-J WebSocket upgrade or SOAP POST expected\n")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", route)
    return app


def callback_app(proxy: Proxy) -> web.Application:
    from . import soap

    async def route(request: web.Request) -> web.StreamResponse:
        return await soap.handle_from_csms(proxy, request)

    app = web.Application()
    app.router.add_post("/{cp_id}", route)
    return app
