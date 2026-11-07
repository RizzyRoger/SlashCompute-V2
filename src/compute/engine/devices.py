"""llama.cpp device order and `--tensor-split` mapping.

Facts from llama.cpp b11160 (src/llama.cpp, src/llama-model.cpp):
- The model's device list (what `--tensor-split` indexes) is: RPC devices first (in `--rpc` order),
  then local GPUs (src/llama.cpp: "add RPC servers at the front of the list").
- `--list-devices` prints the *registry* order instead (local backends first, RPC last), e.g.
  `MTL0, BLAS, RPC0`, as `  <name>: <description> (<total> MiB, <free> MiB free)`. RPC devices are
  named `RPC<n>` with the endpoint `host:port` as their description. Verified by `make smoke`.
- `--list-devices` also prints accelerator devices (e.g. `BLAS: Accelerate (0 MiB, ...)`) that the
  model does *not* split layers over, so they are left out of the `--tensor-split` vector.
- Layer `il` (0..n_layer, where n_layer is the output layer) goes to the first device whose
  normalised cumulative split is > il / (n_layer + 1). So passing per-device layer counts, with +1
  on the device that holds the output layer, reproduces planned contiguous ranges exactly.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

import struct

_LINE = re.compile(r'^\s*(\S+?): (.*) \((\d+) MiB, (\d+) MiB free\)\s*$')
_ACCEL_PREFIXES = ('BLAS', 'AMX', 'CPU', 'ACCEL')
_ENDPOINT = re.compile(r'(\[[0-9a-fA-F:.]+\]:\d+|[\w.-]+:\d+)')


@dataclass(frozen=True)
class Device:
    name: str
    description: str
    total_mib: int
    free_mib: int

    @property
    def is_rpc(self) -> bool:
        return self.name.upper().startswith('RPC')

    @property
    def is_accel(self) -> bool:
        """Accelerator backends (BLAS, AMX, ...) are listed but never hold layers."""
        return not self.is_rpc and (self.total_mib == 0 or self.name.upper().startswith(_ACCEL_PREFIXES))

    @property
    def endpoint(self) -> str | None:
        if not self.is_rpc:
            return None
        m = _ENDPOINT.search(self.description) or _ENDPOINT.search(self.name)
        return m.group(1) if m else None


def parse_list_devices(text: str) -> list[Device]:
    out = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if m:
            out.append(Device(m.group(1), m.group(2), int(m.group(3)), int(m.group(4))))
    return out


def model_devices(devices: list[Device]) -> list[Device]:
    """The devices llama.cpp splits layers over, in the order `--tensor-split` indexes them:
    RPC devices first, then local GPUs (accelerators such as BLAS never hold layers)."""
    rpc = [d for d in devices if d.is_rpc]
    local = [d for d in devices if not d.is_rpc and not d.is_accel]
    return rpc + local


def default_device_order(worker_endpoints: list[str], local_names: list[str] | None = None) -> list[str]:
    """The order llama.cpp uses: RPC endpoints in `--rpc` order, then local devices."""
    return list(worker_endpoints) + list(local_names or ['LOCAL'])


def has_local_gpu(devices: list[Device]) -> bool:
    return any(not d.is_rpc and not d.is_accel for d in devices)


def cpu_head_split(devices: list[Device], layer_counts: dict[str, int]) -> list[int]:
    """Tensor split for a head with no GPU (e.g. a CPU-only Windows PC).

    `--list-devices` never shows the CPU, so the split covers only the RPC devices. The head keeps
    its layers on the CPU by running with `-ngl n_layer + 1 - head_layers`: llama.cpp puts layers
    below `n_layer + 1 - ngl` on the CPU, so the head holds the *first* layers and the last RPC
    device that holds layers also takes the output layer (+1).
    """
    seen, vec = set(), []
    rpc = [d for d in model_devices(devices) if d.is_rpc]
    for d in rpc:
        ep = d.endpoint
        if ep in layer_counts and ep not in seen:
            vec.append(layer_counts[ep])
            seen.add(ep)
        else:
            vec.append(0)
    missing = set(layer_counts) - seen
    if missing:
        raise ValueError(f'devices not reported by llama.cpp: {sorted(missing)}')
    if any(vec):
        vec[max(i for i, v in enumerate(vec) if v)] += 1
    return vec


def cpu_head_gpu_layers(n_layer: int, head_layers: int) -> int:
    """`-ngl` for a CPU head: everything but the head's first `head_layers` layers is offloaded."""
    return max(n_layer + 1 - head_layers, 0)


def tensor_split_for(devices: list[Device], layer_counts: dict[str, int], head_layers: int,
                     head_device: str | None = None) -> list[int]:
    """Per-device vector in llama.cpp device order.

    layer_counts: RPC endpoint -> layers that worker holds.
    head_layers:  layers on the head's local device (which also takes the output layer, +1).
    head_device:  which local device the head uses (default: the first non-RPC device).
    Every RPC endpoint in layer_counts must appear in `devices`.
    """
    devices = model_devices(devices)
    local = [d for d in devices if not d.is_rpc]
    head_name = head_device or (local[0].name if local else None)
    seen = set()
    vec = []
    for d in devices:
        if d.is_rpc:
            ep = d.endpoint
            # one RPC server can expose several GPUs (same endpoint): the block goes on the first
            if ep in layer_counts and ep not in seen:
                vec.append(layer_counts[ep])
                seen.add(ep)
            else:
                vec.append(0)
        elif d.name == head_name:
            vec.append(head_layers + 1)
            seen.add('__head__')
        else:
            vec.append(0)
    missing = set(layer_counts) - seen
    if missing:
        raise ValueError(f'devices not reported by llama.cpp: {sorted(missing)}')
    if '__head__' not in seen:
        raise ValueError('no local device for the head')
    # The output layer goes to the last device with a non-zero count. Make sure that's the head.
    last_nonzero = max(i for i, v in enumerate(vec) if v)
    if devices[last_nonzero].name != head_name:
        raise ValueError('head local device must come after every RPC device that holds layers')
    return vec


def _f32(x: float) -> float:
    return struct.unpack('<f', struct.pack('<f', x))[0]


def simulate_llamacpp_assignment(vector: list[float], n_layer: int) -> list[int]:
    """Device index for layers 0..n_layer (the last entry is the output layer), mirroring llama.cpp's
    float32 split-point maths with -ngl >= n_layer + 1."""
    splits = []
    total = 0.0
    for v in vector:
        total = _f32(total + v)
        splits.append(total)
    splits = [_f32(s / total) for s in splits]
    act = n_layer + 1
    return [bisect.bisect_right(splits, _f32(il / act)) for il in range(n_layer + 1)]


def layer_ranges_from_vector(vector: list[int], n_layer: int) -> list[tuple[int, int]]:
    """[start, end) of repeating layers per device according to llama.cpp's assignment."""
    owners = simulate_llamacpp_assignment(vector, n_layer)[:n_layer]
    ranges = []
    for dev in range(len(vector)):
        idx = [i for i, o in enumerate(owners) if o == dev]
        ranges.append((idx[0], idx[-1] + 1) if idx else (0, 0))
    return ranges
