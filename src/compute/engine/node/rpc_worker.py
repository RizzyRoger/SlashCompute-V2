"""Start/stop the llama.cpp RPC server (`rpc-server`, `ggml-rpc-server` in newer builds).

The RPC server has no authentication. It is only started while the node is in a pipeline, bound
to the node's Tailscale IP on a random port, and stopped right after. Flags differ between builds,
so we read `--help` on the pinned build: b11160 has -H/-p/-c/-d and no memory cap (`-m/--mem` is
used only if the build supports it; otherwise the commitment is enforced by the planner + agent).
"""
from __future__ import annotations

import asyncio
import contextlib
import socket


async def detect_flags(binary: str) -> set[str]:
    from compute.engine.node.head import spawn_kwargs
    proc = await asyncio.create_subprocess_exec(binary, '--help', stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, **spawn_kwargs())
    out, _ = await proc.communicate()
    text = out.decode(errors='replace')
    flags = set()
    for f in ('--host', '--port', '--cache', '--device', '--mem', '--threads'):
        if f in text:
            flags.add(f)
    return flags


def free_port(host: str = '127.0.0.1') -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def command(binary: str, host: str, port: int, flags: set[str], device: str | None = None,
            mem_mb: int | None = None, cache: bool = True) -> list[str]:
    cmd = [binary, '-H', host, '-p', str(port)]
    if cache and '--cache' in flags:
        cmd.append('-c')
    if device and '--device' in flags:
        cmd += ['-d', device]
    if mem_mb and '--mem' in flags:
        cmd += ['-m', str(mem_mb)]
    return cmd


async def start(binary: str, host: str, port: int, flags: set[str], device: str | None = None,
                mem_mb: int | None = None) -> asyncio.subprocess.Process:
    from compute.engine.node.head import spawn_kwargs
    return await asyncio.create_subprocess_exec(*command(binary, host, port, flags, device, mem_mb),
                                                stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL, **spawn_kwargs())


async def wait_listening(host: str, port: int, proc: asyncio.subprocess.Process, timeout: float = 30) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if proc.returncode is not None:
            raise RuntimeError(f'rpc-server exited with code {proc.returncode}')
        with contextlib.suppress(OSError):
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 1)
            w.close()
            return
        await asyncio.sleep(0.2)
    raise TimeoutError(f'rpc-server not listening on {host}:{port}')


async def terminate(proc: asyncio.subprocess.Process, grace: float = 5) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
