import aiohttp
from aiohttp import web

from ocpp_mitm.soap import action_of

ENVELOPE = """<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
               xmlns:wsa="http://www.w3.org/2005/08/addressing"
               xmlns:cs="urn://Ocpp/Cs/2015/10/">
  <soap:Header>
    <cs:chargeBoxIdentity>CP001</cs:chargeBoxIdentity>
    <wsa:Action>/{action}</wsa:Action>
    <wsa:MessageID>urn:uuid:1</wsa:MessageID>
    {addressing}
  </soap:Header>
  <soap:Body>{body}</soap:Body>
</soap:Envelope>"""


def envelope(action: str, addressing: str, body: str) -> str:
    return ENVELOPE.format(action=action, addressing=addressing, body=body)


def recorder(seen: list, reply: str):
    async def handle(request: web.Request):
        seen.append(await request.text())
        return web.Response(text=reply, content_type="application/soap+xml")

    app = web.Application()
    app.router.add_post("/{tail:.*}", handle)
    return app


async def test_charger_url_swapped_both_ways(serve, proxy_for):
    charger_seen: list = []
    csms_seen: list = []
    charger = await serve(recorder(charger_seen, envelope("ResetResponse", "", "<status>Accepted</status>")))
    csms = await serve(recorder(csms_seen, envelope("BootNotificationResponse", "", "<status>Accepted</status>")))
    real_charger_url = str(charger.make_url("/ocpp"))

    proxy, front, callback = await proxy_for(str(csms.make_url("/soap")))
    advertised = str(callback.make_url("/CP001"))

    async with aiohttp.ClientSession() as session:
        boot = envelope(
            "BootNotification",
            f"<wsa:From><wsa:Address>{real_charger_url}</wsa:Address></wsa:From>",
            "<cs:bootNotificationRequest><cs:chargePointVendor>V</cs:chargePointVendor></cs:bootNotificationRequest>",
        )
        async with session.post(front.make_url("/"), data=boot, headers={"Content-Type": "application/soap+xml"}) as r:
            assert r.status == 200
            assert "<status>Accepted</status>" in await r.text()

        assert f"<wsa:Address>{advertised}</wsa:Address>" in csms_seen[0]
        assert real_charger_url not in csms_seen[0]
        assert proxy.chargers == {"CP001": real_charger_url}

        # The controller calls back where it was told to, addressing the request there.
        reset = envelope("Reset", f"<wsa:To>{advertised}</wsa:To>", "<cs:resetRequest><cs:type>Soft</cs:type></cs:resetRequest>")
        async with session.post(advertised, data=reset, headers={"Content-Type": "application/soap+xml"}) as r:
            assert r.status == 200
            assert "ResetResponse" in await r.text()

    assert f"<wsa:To>{real_charger_url}</wsa:To>" in charger_seen[0]
    assert [(e["dir"], e["kind"], e["action"]) for e in proxy.log.events] == [
        ("cp->csms", "call", "BootNotification"),
        ("csms->cp", "result", "BootNotification"),
        ("csms->cp", "call", "Reset"),
        ("cp->csms", "result", "Reset"),
    ]


async def test_callback_for_unknown_charger_is_502(proxy_for):
    _, _, callback = await proxy_for("http://127.0.0.1:1/")
    async with aiohttp.ClientSession() as session:
        async with session.post(callback.make_url("/CP404"), data=envelope("Reset", "", "")) as r:
            assert r.status == 502


def test_action_falls_back_to_body_element():
    xml = "<Envelope><Header/><Body><cs:heartbeatRequest/></Body></Envelope>"
    assert action_of(xml) == "heartbeatRequest"
