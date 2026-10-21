import pytest

from ocpp_mitm.rewrite import Rewriter


def test_swaps_each_way():
    r = Rewriter.parse(["10.0.0.5=192.168.1.9"])
    assert r.to_csms('{"ip":"10.0.0.5"}') == '{"ip":"192.168.1.9"}'
    assert r.to_charger('{"ip":"192.168.1.9"}') == '{"ip":"10.0.0.5"}'


def test_rejects_spec_without_equals():
    with pytest.raises(ValueError):
        Rewriter.parse(["10.0.0.5"])
