import asyncio
import json

import aiohttp
from aiohttp import web

CHARGER_IP = "192.168.50.20"
LAPTOP_IP = "192.168.50.99"


def fake_csms(seen: dict):
    async def ws(request: web.Request):
        seen["path"] = request.path
        seen["authorization"] = request.headers.get("Authorization")
        sock = web.WebSocketResponse(protocols=("ocpp1.6",))
        await sock.prepare(request)
        async for msg in sock:
            frame = json.loads(msg.data)
            if frame[0] == 2:
                seen.setdefault("calls", []).append(frame)
                await sock.send_str(json.dumps([3, frame[1], {"status": "Accepted", "interval": 300}]))
                await sock.send_str(json.dumps([2, "csms-1", "GetConfiguration", {}]))
            elif frame[0] == 3:
                seen.setdefault("results", []).append(frame)
                await sock.close()
        return sock

    app = web.Application()
    app.router.add_get("/ocpp/{cp}", ws)
    return app


async def test_relays_rewrites_and_pairs(serve, proxy_for):
    seen: dict = {}
    csms = await serve(fake_csms(seen))
    upstream = str(csms.make_url("/ocpp")).replace("http://", "ws://")
    proxy, front, _ = await proxy_for(upstream, [f"{CHARGER_IP}={LAPTOP_IP}"])

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            front.make_url("/CP001"), protocols=("ocpp2.0.1", "ocpp1.6"),
            headers={"Authorization": "Basic Q1AwMDE6c2VjcmV0"},
        ) as charger:
            assert charger.protocol == "ocpp1.6"
            boot = [2, "m1", "BootNotification", {"chargePointVendor": "V", "meterSerialNumber": f"ip:{CHARGER_IP}"}]
            await charger.send_str(json.dumps(boot))

            result = json.loads((await charger.receive()).data)
            assert result == [3, "m1", {"status": "Accepted", "interval": 300}]

            call = json.loads((await charger.receive()).data)
            assert call[2] == "GetConfiguration"
            await charger.send_str(json.dumps([3, "csms-1", {"configurationKey": [{"key": "Ip", "value": LAPTOP_IP}]}]))

            closing = await asyncio.wait_for(charger.receive(), 5)
            assert closing.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)

    assert seen["path"] == "/ocpp/CP001"
    assert seen["authorization"] == "Basic Q1AwMDE6c2VjcmV0"
    assert seen["calls"][0][3]["meterSerialNumber"] == f"ip:{LAPTOP_IP}"
    # Rewritten only towards the controller; that reply carries the laptop IP verbatim.
    assert seen["results"][0][2]["configurationKey"][0]["value"] == LAPTOP_IP

    frames = [e for e in proxy.log.events if e.get("kind") != "conn"]
    assert [(e["dir"], e["kind"], e.get("action")) for e in frames] == [
        ("cp->csms", "call", "BootNotification"),
        ("csms->cp", "result", "BootNotification"),
        ("csms->cp", "call", "GetConfiguration"),
        ("cp->csms", "result", "GetConfiguration"),
    ]
    assert frames[0]["rewritten"] is True
    assert "latency_ms" in frames[1]


async def test_mirrors_upstream_handshake_refusal(serve, proxy_for):
    async def refuse(request):
        return web.Response(status=401)

    app = web.Application()
    app.router.add_get("/{cp}", refuse)
    csms = await serve(app)
    _, front, _ = await proxy_for(str(csms.make_url("/")).replace("http://", "ws://"))

    async with aiohttp.ClientSession() as session:
        try:
            await session.ws_connect(front.make_url("/CP001"), protocols=("ocpp1.6",))
        except aiohttp.WSServerHandshakeError as e:
            assert e.status == 401
        else:
            raise AssertionError("handshake should have failed")
