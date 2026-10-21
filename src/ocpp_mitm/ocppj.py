"""OCPP-J: relay one WebSocket per charger, logging and pairing every frame."""

import asyncio
import json
import time
from typing import Any
from urllib.parse import unquote

import aiohttp
from aiohttp import web

from .log import CP_TO_CSMS, CSMS_TO_CP
from .proxy import Proxy
from .rewrite import Rewriter

# 5 (CALLRESULTERROR) and 6 (SEND) arrived in OCPP 2.1.
_KINDS = {2: "call", 3: "result", 4: "error", 5: "result-error", 6: "send"}
# Security profiles 1 and 2 put the charger's Basic credentials here.
_FORWARDED_HEADERS = ("Authorization", "User-Agent")


def _opposite(direction: str) -> str:
    return CSMS_TO_CP if direction == CP_TO_CSMS else CP_TO_CSMS


def parse_frame(text: str) -> list[Any] | None:
    try:
        frame = json.loads(text)
    except ValueError:
        return None
    if (
        isinstance(frame, list)
        and len(frame) >= 3
        and frame[0] in _KINDS
        and isinstance(frame[1], str)
    ):
        return frame
    return None


class Pairing:
    """Matches results and errors to the call they answer, by direction and message id."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], tuple[str, float]] = {}

    def describe(self, direction: str, frame: list[Any]) -> dict[str, Any]:
        kind = _KINDS[frame[0]]
        msg_id = frame[1]
        if kind in ("call", "send"):
            action = frame[2] if isinstance(frame[2], str) else None
            if kind == "call":
                self._pending[(direction, msg_id)] = (action, time.monotonic())
            return {"kind": kind, "id": msg_id, "action": action, "payload": frame[3] if len(frame) > 3 else None}

        described: dict[str, Any] = {"kind": kind, "id": msg_id}
        call = self._pending.pop((_opposite(direction), msg_id), None)
        if call:
            described["action"] = call[0]
            described["latency_ms"] = round((time.monotonic() - call[1]) * 1000)
        if kind == "result":
            described["payload"] = frame[2]
        else:
            described["payload"] = {"code": frame[2], "description": frame[3] if len(frame) > 3 else None,
                                    "details": frame[4] if len(frame) > 4 else None}
        return described


async def handle(proxy: Proxy, request: web.Request) -> web.StreamResponse:
    cp_id = unquote(request.path.rstrip("/").rsplit("/", 1)[-1]) or "?"
    offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
    url = proxy.upstream_url(request.path_qs, websocket=True)
    headers = {name: request.headers[name] for name in _FORWARDED_HEADERS if name in request.headers}
    log = proxy.log
    rewriter = proxy.rewriter_for(request)

    # Connect upstream before accepting the charger, so the charger gets the
    # controller's real handshake answer: its chosen subprotocol, or its 401.
    try:
        upstream = await proxy.session.ws_connect(
            url, protocols=offered, headers=headers, ssl=proxy.upstream_ssl, autoping=True
        )
    except aiohttp.WSServerHandshakeError as e:
        log.emit(proto="ocpp-j", cp=cp_id, kind="conn", detail=f"upstream {url} refused handshake: {e.status} {e.message}")
        return web.Response(status=e.status, text=f"upstream refused: {e.message}\n")
    except (aiohttp.ClientError, OSError) as e:
        log.emit(proto="ocpp-j", cp=cp_id, kind="conn", detail=f"upstream {url} unreachable: {e!r}")
        return web.Response(status=502, text="upstream unreachable\n")

    charger = web.WebSocketResponse(protocols=(upstream.protocol,) if upstream.protocol else (), autoping=True)
    await charger.prepare(request)
    log.emit(proto="ocpp-j", cp=cp_id, kind="conn",
             detail=f"open {request.remote} -> {url} offered={offered} chosen={upstream.protocol} "
                    f"rewrites={list(rewriter.pairs)}")

    pairing = Pairing()
    await asyncio.gather(
        _pump(proxy, rewriter, cp_id, pairing, charger, upstream, CP_TO_CSMS),
        _pump(proxy, rewriter, cp_id, pairing, upstream, charger, CSMS_TO_CP),
    )
    log.emit(proto="ocpp-j", cp=cp_id, kind="conn",
             detail=f"closed charger={charger.close_code} upstream={upstream.close_code}")
    return charger


async def _pump(proxy: Proxy, rewriter: Rewriter, cp_id: str, pairing: Pairing, src, dst, direction: str) -> None:
    rewrite = rewriter.to_csms if direction == CP_TO_CSMS else rewriter.to_charger
    async for msg in src:
        if msg.type == aiohttp.WSMsgType.TEXT:
            forwarded = rewrite(msg.data)
            frame = parse_frame(msg.data)
            event = pairing.describe(direction, frame) if frame else {"kind": "unparsed", "detail": msg.data}
            proxy.log.emit(proto="ocpp-j", cp=cp_id, dir=direction, **event, frame=msg.data,
                           **({"rewritten": True, "forwarded": forwarded} if forwarded != msg.data else {}))
            await dst.send_str(forwarded)
        elif msg.type == aiohttp.WSMsgType.BINARY:
            proxy.log.emit(proto="ocpp-j", cp=cp_id, dir=direction, kind="binary", detail=msg.data.hex())
            await dst.send_bytes(msg.data)
        elif msg.type == aiohttp.WSMsgType.ERROR:
            proxy.log.emit(proto="ocpp-j", cp=cp_id, dir=direction, kind="conn", detail=f"socket error: {src.exception()!r}")
            break
    if not dst.closed:
        await dst.close(code=src.close_code or aiohttp.WSCloseCode.OK)
