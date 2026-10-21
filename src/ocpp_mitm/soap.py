"""OCPP 1.5/1.6 SOAP: relay HTTP both ways and swap the charger's advertised URL for ours.

The charger names its own endpoint in the wsa:From header of each request, and
the controller later POSTs its calls there. We record that URL, replace it with
our callback listener, and forward the controller's calls back to the recorded
URL. Edits are regex substitutions in the SOAP header so every other byte
reaches the far side unchanged.
"""

import re
import time
from urllib.parse import unquote

import aiohttp
from aiohttp import web

from .log import CP_TO_CSMS, CSMS_TO_CP
from .proxy import Proxy

ANONYMOUS = "http://www.w3.org/2005/08/addressing/anonymous"
_FORWARDED_HEADERS = ("Content-Type", "SOAPAction", "Authorization")

_P = r"(?:[\w.-]+:)?"  # optional namespace prefix
_HEADER = re.compile(rf"<{_P}Header\b[^>]*>.*?</{_P}Header\s*>", re.S)
_BODY_FIRST = re.compile(rf"<{_P}Body\b[^>]*>\s*<{_P}([\w.-]+)", re.S)
_CHARGE_BOX = re.compile(rf"<{_P}chargeBoxIdentity\b[^>]*>\s*([^<]*?)\s*</", re.S)
_ACTION = re.compile(rf"<{_P}Action\b[^>]*>\s*([^<]*?)\s*</", re.S)
_FROM = re.compile(rf"(<{_P}From\b[^>]*>\s*<{_P}Address\b[^>]*>\s*)([^<]*?)(\s*</)", re.S)
_TO = re.compile(rf"(<{_P}To\b[^>]*>\s*)([^<]*?)(\s*</)", re.S)


def _header(xml: str) -> re.Match | None:
    return _HEADER.search(xml)


def header_value(pattern: re.Pattern, xml: str) -> str | None:
    header = _header(xml)
    if not header:
        return None
    m = pattern.search(header.group(0))
    if not m:
        return None
    return m.group(2) if pattern.groups == 3 else m.group(1)


def replace_header_value(pattern: re.Pattern, xml: str, value: str) -> str:
    header = _header(xml)
    if not header:
        return xml
    edited = pattern.sub(lambda m: m.group(1) + value + m.group(3), header.group(0), count=1)
    return xml[: header.start()] + edited + xml[header.end():]


def action_of(xml: str) -> str | None:
    action = header_value(_ACTION, xml)
    if action:
        return re.split(r"[/:]", action)[-1] or action
    m = _BODY_FIRST.search(xml)
    return m.group(1) if m else None


async def handle_from_charger(proxy: Proxy, request: web.Request) -> web.StreamResponse:
    """Charger → controller request, controller → charger response."""
    raw = await request.text()
    cp_id = header_value(_CHARGE_BOX, raw) or "?"
    advertised = header_value(_FROM, raw)

    forwarded = proxy.rewriter.to_csms(raw)
    if advertised and advertised != ANONYMOUS:
        proxy.chargers[cp_id] = advertised
        forwarded = replace_header_value(_FROM, forwarded, proxy.callback_url_for(cp_id))

    return await _exchange(
        proxy, request, cp_id, raw, forwarded,
        url=proxy.upstream_url(request.path_qs, websocket=False),
        ssl=proxy.upstream_ssl, direction=CP_TO_CSMS,
        note=f"wsa:From {advertised} -> {proxy.callback_url_for(cp_id)}" if advertised and advertised != ANONYMOUS else None,
    )


async def handle_from_csms(proxy: Proxy, request: web.Request) -> web.StreamResponse:
    """Controller → charger request (on the callback listener), charger → controller response."""
    raw = await request.text()
    cp_id = unquote(request.match_info["cp_id"])
    target = proxy.chargers.get(cp_id)
    if target is None:
        proxy.log.emit(proto="soap", cp=cp_id, dir=CSMS_TO_CP, kind="error", action=action_of(raw),
                       detail="no wsa:From seen for this charger yet; it must send a request through the proxy first",
                       body=raw)
        return web.Response(status=502, text=f"unknown charger {cp_id!r}\n")

    forwarded = proxy.rewriter.to_charger(raw)
    if header_value(_TO, forwarded) is not None:
        forwarded = replace_header_value(_TO, forwarded, target)

    return await _exchange(proxy, request, cp_id, raw, forwarded, url=target,
                           ssl=proxy.charger_ssl, direction=CSMS_TO_CP, note=None)


async def _exchange(proxy, request, cp_id, raw, forwarded, *, url, ssl, direction, note) -> web.StreamResponse:
    log = proxy.log
    action = action_of(raw)
    log.emit(proto="soap", cp=cp_id, dir=direction, kind="call", action=action, detail=note or raw,
             url=url, body=raw, **({"rewritten": True, "forwarded": forwarded} if forwarded != raw else {}))

    headers = {name: request.headers[name] for name in _FORWARDED_HEADERS if name in request.headers}
    reply_dir = CSMS_TO_CP if direction == CP_TO_CSMS else CP_TO_CSMS
    reply_rewrite = proxy.rewriter.to_charger if direction == CP_TO_CSMS else proxy.rewriter.to_csms
    started = time.monotonic()
    try:
        async with proxy.session.post(url, data=forwarded.encode(), headers=headers, ssl=ssl) as resp:
            reply = await resp.text()
            status, content_type = resp.status, resp.headers.get("Content-Type")
    except (aiohttp.ClientError, OSError) as e:
        log.emit(proto="soap", cp=cp_id, dir=reply_dir, kind="error", action=action, detail=f"{url} unreachable: {e!r}")
        return web.Response(status=502, text="upstream unreachable\n")

    latency_ms = round((time.monotonic() - started) * 1000)
    reply_forwarded = reply_rewrite(reply)
    log.emit(proto="soap", cp=cp_id, dir=reply_dir, kind="result" if status < 400 else "error",
             action=action, status=status, latency_ms=latency_ms, detail=reply, body=reply,
             **({"rewritten": True, "forwarded": reply_forwarded} if reply_forwarded != reply else {}))
    return web.Response(status=status, body=reply_forwarded.encode(),
                        headers={"Content-Type": content_type} if content_type else None)
