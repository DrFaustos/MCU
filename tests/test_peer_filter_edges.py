"""Граничные случаи PeerFilter: IPv6, некорректные CIDR, None."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import PeerFilter  # noqa: E402


def test_empty_allows_everything():
    assert PeerFilter([]).allows("1.2.3.4")
    assert PeerFilter([]).allows("2001:db8::1")
    assert PeerFilter([]).allows(None)


def test_single_ipv4():
    f = PeerFilter(["10.0.0.5"])
    assert f.allows("10.0.0.5")
    assert not f.allows("10.0.0.6")


def test_ipv4_cidr():
    f = PeerFilter(["192.168.1.0/24"])
    assert f.allows("192.168.1.1")
    assert not f.allows("192.168.2.1")


def test_single_ipv6():
    f = PeerFilter(["2001:db8::1"])
    assert f.allows("2001:db8::1")
    assert not f.allows("2001:db8::2")


def test_ipv6_cidr():
    f = PeerFilter(["2001:db8::/32"])
    assert f.allows("2001:db8::1")
    assert f.allows("2001:db8:ffff::1")
    assert not f.allows("2001:db9::1")


def test_invalid_pattern_is_ignored_not_crash():
    f = PeerFilter(["not-an-ip", "999.1.1.0/24", "10.0.0.5"])
    assert f.allows("10.0.0.5")
    assert not f.allows("8.8.8.8")


def test_none_and_garbage_denied_when_filtered():
    f = PeerFilter(["10.0.0.0/8"])
    assert not f.allows(None)
    assert not f.allows("")
    assert not f.allows("not-an-ip")


def test_ipv4_vs_ipv6_family_mismatch():
    f = PeerFilter(["10.0.0.0/8"])
    assert not f.allows("2001:db8::1")
