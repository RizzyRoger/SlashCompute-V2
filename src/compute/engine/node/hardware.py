"""Hardware detection for the node agent (macOS, Windows, Linux).

RAM comes from psutil. GPUs come from ``llama-server --list-devices`` (what llama.cpp will actually
use), with ``nvidia-smi`` as a fallback for the VRAM size.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys

import psutil

from compute.engine.devices import Device, parse_list_devices

GIB = 1024 ** 3
MIB = 1024 ** 2
OS_RESERVE_BYTES = 4 * GIB   # left to the OS when lending unified memory or system RAM


def _run(*cmd: str, timeout: float = 10) -> str | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True,
                              **_no_window()).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _no_window() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def gpu_devices(list_devices_text: str) -> list[Device]:
    """Local GPUs (not RPC, not BLAS/CPU accelerators) from ``llama-server --list-devices``."""
    return [d for d in parse_list_devices(list_devices_text) if not d.is_rpc and not d.is_accel]


def parse_nvidia_smi_mib(text: str) -> list[int]:
    """``nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits`` -> bytes per GPU."""
    out = []
    for line in (text or "").splitlines():
        try:
            out.append(int(float(line.strip())) * MIB)
        except ValueError:
            continue
    return out


def list_devices(llama_server: str | None) -> str:
    if not llama_server:
        return ""
    return _run(llama_server, "--list-devices", timeout=30) or ""


def nvidia_vram(which=shutil.which) -> list[int]:
    if not which("nvidia-smi"):
        return []
    return parse_nvidia_smi_mib(_run("nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits")
                                or "")


def lendable_base_bytes(platform_name: str, total_ram: int, vram: list[int]) -> int:
    """What ``--memory-percent`` is a percentage of: unified RAM minus an OS reserve on a Mac,
    the GPU's VRAM on a GPU PC, else system RAM minus the OS reserve."""
    if platform_name == "darwin" or not vram:
        return max(0, total_ram - OS_RESERVE_BYTES)
    return max(vram)


def memory_gb_for_percent(percent: float, base_bytes: int) -> float:
    pct = min(max(percent, 0.0), 100.0)
    return round(pct / 100 * base_bytes / GIB, 1)


def detect(llama_server: str | None = None, platform_name: str | None = None) -> dict:
    plat = platform_name or sys.platform
    gpus = gpu_devices(list_devices(llama_server))
    vram = [d.total_mib * MIB for d in gpus if d.total_mib] or nvidia_vram()
    total = int(psutil.virtual_memory().total)
    os_name = f"{platform.system()} {platform.release()}"
    if "microsoft" in platform.release().lower():
        os_name += " (WSL2)"
    return {
        "os": os_name,
        "chip": (gpus[0].description if gpus else "") or platform.processor() or platform.machine(),
        "total_mem_bytes": total,
        "gpus": [d.description for d in gpus],
        "gpu_mem_bytes": vram,
        "lendable_base_bytes": lendable_base_bytes(plat, total, vram),
    }


def tailscale_ip() -> str | None:
    out = _run("tailscale", "ip", "-4")
    return out.splitlines()[0].strip() if out else None


def supports_rpc(rpc_server: str) -> bool:
    out = _run(rpc_server, "--help")
    return bool(out) or shutil.which(rpc_server) is not None
