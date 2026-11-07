"""LAN discovery of the main server over mDNS.

Everyone on a network shares one pool: every app sees the same adverts and picks the same server with
``choose_pool`` (the one that started first), so nobody ends up in a pool of their own.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import math
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from compute.common.config import MDNS_SERVICE_TYPE

log = logging.getLogger(__name__)
RESOLVE_TIMEOUT_MS = 2000
ADVERT_TTL_S = 120   # every advert record; browsers re-ask at ~80%, so a dead server goes in ~2 min
NETWORK_CHECK_S = 5.0   # how often a server checks whether its LAN address changed
MDNS_GROUP = ("224.0.0.251", 5353)
EMPTY_MDNS_QUERY = bytes(12)   # a DNS header with no questions: every responder ignores it


def lan_ip() -> str:
    """Best-effort primary LAN address (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_network_blocked(ip: Optional[str] = None) -> bool:
    """Whether the OS refuses to let this app reach the LAN at all. macOS answers "No route to host"
    (EHOSTUNREACH) to an app whose Local Network access is off: no mDNS adverts or browsing (only loopback,
    so the app even sees its own server) and no connections to any other device. Sends one empty mDNS query."""
    ip = ip or lan_ip()
    if ip.startswith("127."):
        return False   # offline: no LAN to be blocked from
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
        s.sendto(EMPTY_MDNS_QUERY, MDNS_GROUP)
        return False
    except OSError as e:
        return e.errno == errno.EHOSTUNREACH
    finally:
        s.close()


@dataclass(frozen=True)
class LanServer:
    """A main server advertised on the LAN. ``started`` is inf when the advert has none (older servers)."""

    url: str
    name: str
    started: float = math.inf


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return "" if value is None else str(value)


def _started(properties: dict) -> float:
    raw = {_text(k): _text(v) for k, v in (properties or {}).items()}.get("started", "")
    try:
        value = float(raw)
    except ValueError:
        return math.inf
    return value if math.isfinite(value) else math.inf


def lan_server(name: str, addresses: list[bytes], port: Optional[int], properties: dict) -> Optional[LanServer]:
    """A ``LanServer`` from a resolved zeroconf record, or None when it has no IPv4 address or port."""
    ipv4 = [a for a in addresses or [] if len(a) == 4]
    if not ipv4 or not port:
        return None
    return LanServer(f"http://{socket.inet_ntoa(ipv4[0])}:{port}", name, _started(properties))


def choose_pool(servers: Iterable[LanServer]) -> Optional[LanServer]:
    """The network's one pool: the server that started first (ties: by name). Every app agrees on it."""
    return min(servers, key=lambda s: (s.started, s.name), default=None)


class Advertiser:
    """Advertises the main server from inside its event loop (AsyncZeroconf: a blocking ``Zeroconf`` there
    waits on its own loop). ``close`` sends goodbyes so apps move on at once; the short TTL makes a server
    that died without them drop out of every app's list within a couple of minutes, not 75.

    A host that changes network (hotspot to office Wi-Fi, say) re-advertises on the new one with the same
    ``started``, so it stays the network's pool and the apps follow it to its new address."""

    def __init__(self, port: int, host_ip: Optional[str] = None, started: Optional[float] = None,
                 version: str = "", ip_fn: Callable[[], str] = lan_ip,
                 check_every_s: float = NETWORK_CHECK_S) -> None:
        self._port = port
        self._fixed_ip = host_ip
        self._ip_fn = ip_fn
        self._check_every_s = check_every_s
        self._name = f"compute-{socket.gethostname().split('.')[0]}.{MDNS_SERVICE_TYPE}"
        self._properties = {"path": "/ws/agent", "started": f"{started or time.time():.3f}", "version": version}
        self._azc: Any = None
        self._watch: Optional[asyncio.Task] = None
        self.ip = ""

    def _service(self, ip: str) -> Any:
        from zeroconf import ServiceInfo

        return ServiceInfo(MDNS_SERVICE_TYPE, self._name, addresses=[socket.inet_aton(ip)], port=self._port,
                           properties=self._properties, host_ttl=ADVERT_TTL_S, other_ttl=ADVERT_TTL_S)

    async def _register(self, ip: str) -> None:
        from zeroconf.asyncio import AsyncZeroconf

        # A fresh AsyncZeroconf each time: it only uses the network interfaces present when it starts.
        azc = AsyncZeroconf()
        try:
            # A second server on the same computer gets "name (2)" instead of failing.
            await azc.async_register_service(self._service(ip), allow_name_change=True)
        except BaseException:
            await azc.async_close()
            raise
        self._azc, self.ip = azc, ip

    async def _unregister(self) -> None:
        azc, self._azc = self._azc, None
        if azc is not None:
            await azc.async_close()   # unregisters (goodbyes) first

    async def start(self) -> None:
        await self._register(self._fixed_ip or self._ip_fn())
        if not self._fixed_ip:
            self._watch = asyncio.create_task(self._follow_network())

    async def _follow_network(self) -> None:
        while True:
            await asyncio.sleep(self._check_every_s)
            ip = self._ip_fn()
            if ip == self.ip or ip.startswith("127."):   # same network, or offline for a moment
                continue
            log.info("network changed: advertising on %s instead of %s", ip, self.ip)
            try:
                await self._unregister()
                await self._register(ip)
            except Exception as e:  # noqa: BLE001 - retried on the next check
                log.warning("mDNS re-advertising on %s failed: %r", ip, e)

    async def close(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch
        await self._unregister()


class LanBrowser:
    """Keeps an up-to-date list of the main servers on the LAN. Best-effort: on any mDNS failure it
    logs and reports no servers, and the app keeps the address it already has.

    zeroconf only listens on the interfaces it started with, so when this computer's address changes
    (it joined another network) the browser starts over on the new one."""

    def __init__(self, ip_fn: Callable[[], str] = lan_ip) -> None:
        self._ip_fn = ip_fn
        self._servers: dict[str, LanServer] = {}
        self._lock = threading.Lock()           # the server list (zeroconf's thread writes it)
        self._restart_lock = threading.Lock()   # status polls can arrive together
        self._zc: Any = None
        self._ip = ""
        self._start()

    def _start(self) -> None:
        self._ip = self._ip_fn()
        try:
            from zeroconf import ServiceBrowser, Zeroconf

            self._zc = Zeroconf()
            self._browser = ServiceBrowser(self._zc, MDNS_SERVICE_TYPE, self)
        except Exception as e:  # noqa: BLE001 - discovery is best-effort
            log.warning("mDNS browsing unavailable: %r", e)
            self.close()

    # zeroconf ServiceListener callbacks (they run on zeroconf's thread)

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=RESOLVE_TIMEOUT_MS)
        server = lan_server(name, info.addresses, info.port, info.properties) if info else None
        with self._lock:
            if zc is not self._zc:
                return   # from a browser that was replaced after a network change
            if server:
                self._servers[name] = server
            else:
                self._servers.pop(name, None)

    update_service = add_service

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        with self._lock:
            if zc is self._zc:
                self._servers.pop(name, None)

    def servers(self) -> list[LanServer]:
        with self._restart_lock:
            ip = self._ip_fn()
            if ip != self._ip:
                log.info("network changed (%s -> %s): browsing again", self._ip, ip)
                self.close()
                self._start()
        with self._lock:
            return list(self._servers.values())

    def close(self) -> None:
        with self._lock:
            zc, self._zc = self._zc, None
            self._servers.clear()
        if zc is not None:
            zc.close()
