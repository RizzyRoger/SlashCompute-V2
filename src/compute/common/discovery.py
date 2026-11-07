"""LAN discovery of the coordinator over mDNS."""

from __future__ import annotations

import socket
import time
from typing import Optional

from compute.common.config import MDNS_SERVICE_TYPE


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


class Advertiser:
    def __init__(self, port: int, host_ip: Optional[str] = None) -> None:
        from zeroconf import ServiceInfo, Zeroconf

        ip = host_ip or lan_ip()
        name = f"compute-{socket.gethostname().split('.')[0]}"
        self._zc = Zeroconf()
        self._info = ServiceInfo(
            MDNS_SERVICE_TYPE, f"{name}.{MDNS_SERVICE_TYPE}",
            addresses=[socket.inet_aton(ip)], port=port, properties={"path": "/ws/agent"},
        )
        self._zc.register_service(self._info)

    def close(self) -> None:
        self._zc.unregister_service(self._info)
        self._zc.close()


def discover(timeout: float = 5.0) -> Optional[str]:
    """Return ``http://host:port`` of the first coordinator found, or None."""
    from zeroconf import ServiceBrowser, Zeroconf

    found: list[str] = []

    class _Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if info and info.addresses:
                found.append(f"http://{socket.inet_ntoa(info.addresses[0])}:{info.port}")

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, MDNS_SERVICE_TYPE, _Listener())
        deadline = time.monotonic() + timeout
        while not found and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        zc.close()
    return found[0] if found else None
