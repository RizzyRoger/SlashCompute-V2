"""Every app on a network has to pick the same main server from the mDNS adverts it sees."""

from __future__ import annotations

import asyncio
import math
import socket

import zeroconf
import zeroconf.asyncio

from compute.common.discovery import Advertiser, LanBrowser, LanServer, choose_pool, lan_server

IP = socket.inet_aton("192.168.1.10")


def test_lan_server_decodes_zeroconf_bytes():
    s = lan_server("compute-a._compute._tcp.local.", [IP], 8765, {b"started": b"1700000000.5", b"path": b"/ws"})
    assert s == LanServer("http://192.168.1.10:8765", "compute-a._compute._tcp.local.", 1700000000.5)


def test_lan_server_without_started_sorts_last():
    assert lan_server("old", [IP], 8765, {b"path": b"/ws/agent"}).started == math.inf
    assert lan_server("odd", [IP], 8765, {b"started": b"soon"}).started == math.inf
    assert lan_server("nan", [IP], 8765, {b"started": b"nan"}).started == math.inf
    assert lan_server("none", [IP], 8765, {b"started": None}).started == math.inf


def test_lan_server_needs_an_ipv4_address_and_port():
    assert lan_server("v6-only", [bytes(16)], 8765, {}) is None
    assert lan_server("nothing", [], 8765, {}) is None
    assert lan_server("no-port", [IP], None, {}) is None


def test_choose_pool_is_the_server_that_started_first():
    late = LanServer("http://10.0.0.2:8765", "b", 200.0)
    early = LanServer("http://10.0.0.3:8765", "c", 100.0)
    legacy = LanServer("http://10.0.0.4:8765", "a")
    assert choose_pool([late, legacy, early]) == early
    assert choose_pool([legacy, late]) == late


def test_choose_pool_breaks_ties_by_name_and_handles_none():
    a, b = LanServer("http://10.0.0.2:8765", "a", 5.0), LanServer("http://10.0.0.3:8765", "b", 5.0)
    assert choose_pool([b, a]) == a
    assert choose_pool([]) is None


# ------------------------------------------------- following a network change

class FakeAsyncZeroconf:
    made: list["FakeAsyncZeroconf"] = []

    def __init__(self):
        self.services, self.closed = [], False
        FakeAsyncZeroconf.made.append(self)

    async def async_register_service(self, info, allow_name_change=False):
        self.services.append(info)

    async def async_close(self):
        self.closed = True


async def test_advertiser_moves_to_the_new_network_keeping_its_start_time(monkeypatch):
    FakeAsyncZeroconf.made = []
    monkeypatch.setattr(zeroconf.asyncio, "AsyncZeroconf", FakeAsyncZeroconf)
    ips = ["172.20.10.13"]
    adv = Advertiser(8765, started=123.0, ip_fn=lambda: ips[0], check_every_s=0.01)
    await adv.start()
    ips[0] = "127.0.0.1"   # offline for a moment: keep the advert
    await asyncio.sleep(0.05)
    assert len(FakeAsyncZeroconf.made) == 1
    ips[0] = "192.168.5.64"
    for _ in range(100):
        if adv.ip == "192.168.5.64":
            break
        await asyncio.sleep(0.01)
    old, new = FakeAsyncZeroconf.made
    assert old.closed and not new.closed   # goodbye on the old network
    info = new.services[0]
    assert socket.inet_ntoa(info.addresses[0]) == "192.168.5.64"
    assert info.properties[b"started"] == b"123.000"
    await adv.close()
    assert new.closed


class FakeZeroconf:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_browser_starts_over_when_this_computer_changes_network(monkeypatch):
    made: list[FakeZeroconf] = []
    monkeypatch.setattr(zeroconf, "Zeroconf", lambda: made.append(FakeZeroconf()) or made[-1])
    monkeypatch.setattr(zeroconf, "ServiceBrowser", lambda zc, type_, listener: object())
    ips = ["172.20.10.13"]
    browser = LanBrowser(ip_fn=lambda: ips[0])
    old_zc = made[0]
    browser._servers["a"] = LanServer("http://172.20.10.2:8765", "a", 1.0)
    assert browser.servers() == [LanServer("http://172.20.10.2:8765", "a", 1.0)]
    ips[0] = "192.168.5.64"
    assert browser.servers() == []          # the old network's servers are gone
    assert old_zc.closed and len(made) == 2
    browser.remove_service(old_zc, "t", "a")   # late callbacks from the old browser are ignored
    browser.close()
    assert made[1].closed
