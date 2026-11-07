"""One-way delay to peers over Tailscale: median TCP connect time / 2.

A refused connection still costs one round trip, so any port works; each agent also runs a tiny
accept-and-close listener on its probe port (bound to its Tailscale IP) to make this reliable.
Falls back to `tailscale ping` if TCP gets no answer.
"""
from __future__ import annotations

import asyncio
import re
import statistics
import time


async def tcp_rtt_ms(ip: str, port: int, timeout: float = 1.0) -> float | None:
    t0 = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
        w.close()
    except ConnectionRefusedError:
        pass
    except (OSError, asyncio.TimeoutError):
        return None
    return (time.perf_counter() - t0) * 1000


async def tailscale_ping_ms(ip: str) -> float | None:
    proc = await asyncio.create_subprocess_exec('tailscale', 'ping', '--c', '3', ip, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    times = [float(x) for x in re.findall(r'in ([\d.]+)ms', out.decode(errors='replace'))]
    return min(times) if times else None


async def measure(peers: list[dict], samples: int = 5) -> dict[str, float | None]:
    async def one(p):
        rtts = [r for r in [await tcp_rtt_ms(p['ip'], p.get('port') or 7391) for _ in range(samples)] if r is not None]
        if not rtts:
            rtt = await tailscale_ping_ms(p['ip'])
            return p['node_id'], (rtt / 2 if rtt is not None else None)
        return p['node_id'], statistics.median(rtts) / 2
    return dict(await asyncio.gather(*[one(p) for p in peers]))


async def serve_probe(ip: str, port: int) -> asyncio.base_events.Server:
    async def handle(reader, writer):
        writer.close()
    return await asyncio.start_server(handle, ip, port)
