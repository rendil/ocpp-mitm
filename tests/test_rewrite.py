from unittest import mock

import pytest
from aiohttp.test_utils import make_mocked_request

from ocpp_mitm.log import EventLog
from ocpp_mitm.proxy import Config, Proxy
from ocpp_mitm.rewrite import Rewriter


def test_swaps_each_way():
    r = Rewriter.parse(["10.0.0.5=192.168.1.9"])
    assert r.to_csms('{"ip":"10.0.0.5"}') == '{"ip":"192.168.1.9"}'
    assert r.to_charger('{"ip":"192.168.1.9"}') == '{"ip":"10.0.0.5"}'


@pytest.mark.parametrize("text", ["10.0.0.50", "110.0.0.5", "10.0.0.5.1", "host10.0.0.5"])
def test_leaves_longer_tokens_alone(text):
    assert Rewriter.parse(["10.0.0.5=192.168.1.9"]).to_csms(text) == text


@pytest.mark.parametrize("text", ["ws://10.0.0.5:8080/", "ip=10.0.0.5.", "<a>10.0.0.5</a>"])
def test_matches_token_in_context(text):
    assert "192.168.1.9" in Rewriter.parse(["10.0.0.5=192.168.1.9"]).to_csms(text)


def test_rejects_spec_without_equals():
    with pytest.raises(ValueError):
        Rewriter.parse(["10.0.0.5"])


def _request(peer: str, local: str):
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda key, default=None: {
        "peername": (peer, 50000), "sockname": (local, 9000),
    }.get(key, default)
    return make_mocked_request("GET", "/CP001", transport=transport)


def test_adds_charger_ip_to_own_ip_per_connection():
    proxy = Proxy(Config(upstream="ws://x/", callback_url="", rewriter=Rewriter.parse(["a=b"])), EventLog(console=None))
    rewriter = proxy.rewriter_for(_request("::ffff:192.168.1.50", "192.168.1.47"))
    assert rewriter.pairs == (("a", "b"), ("192.168.1.50", "192.168.1.47"))


def test_auto_rewrite_can_be_turned_off():
    proxy = Proxy(Config(upstream="ws://x/", callback_url="", auto_rewrite=False), EventLog(console=None))
    assert proxy.rewriter_for(_request("192.168.1.50", "192.168.1.47")).pairs == ()
