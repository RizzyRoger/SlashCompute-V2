"""v2 node: llama.cpp download manager, memory percent, model scan (symlinks, mmproj), CPU head,
cross-platform process handling and per-link auto transport."""
import asyncio
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import httpx
import pytest

from inf_gguf_fixtures import write_gguf
from inf_harness import FakeNode, chat, fast_settings, start_harness
from test_inf_models import tiny_gguf
from test_inf_relay import EchoEngine
from compute.engine.coordinator.layers import synthetic_layout
from compute.engine.devices import cpu_head_split, layer_ranges_from_vector, parse_list_devices
from compute.engine.node import binaries, hardware
from compute.engine.node.__main__ import committed_memory_gb, read_pid, watch_stop_file
from compute.engine.node.agent import scan_models
from compute.engine.node.engine import head_extra_args, head_layout, reap_orphans

GIB = 1024 ** 3
GB = 10 ** 9


# ------------------------------------------------------------ binaries


def test_flavor_per_platform(tmp_path):
    none = lambda _: None  # noqa: E731
    assert binaries.pick_flavor("darwin", "arm64", none).assets == ("llama-b12176-bin-macos-arm64.tar.gz",)
    cuda = binaries.pick_flavor("win32", "AMD64", lambda n: "C:/nvidia-smi.exe" if n == "nvidia-smi" else None)
    assert cuda.assets == ("llama-b12176-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip")
    (tmp_path / "vulkan-1.dll").write_bytes(b"")
    assert binaries.pick_flavor("win32", "AMD64", none, tmp_path).assets == ("llama-b12176-bin-win-vulkan-x64.zip",)
    assert binaries.pick_flavor("win32", "AMD64", none, tmp_path / "nope").name == "win-cpu"


def test_parse_digests_keeps_only_sha256():
    rel = {"assets": [{"name": "a.zip", "digest": "sha256:ABC"}, {"name": "b.zip", "digest": None}]}
    assert binaries.parse_digests(rel) == {"a.zip": "abc"}


def test_unreachable_github_api_fails_clearly():
    def get(url):
        raise httpx.ConnectError("offline")
    with pytest.raises(binaries.LlamaDownloadError, match="COMPUTE_LLAMA_DIR"):
        binaries.fetch_digests(get)


def _tarball(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Resp:
    def __init__(self, data: bytes):
        self.data, self.headers = data, {"content-length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_bytes(self, n):
        yield from (self.data[i:i + n] for i in range(0, len(self.data), n))


def test_download_verifies_sha256(tmp_path):
    data = b"archive bytes"
    good = hashlib.sha256(data).hexdigest()
    seen = []
    out = binaries.download_asset("x.tar.gz", good, tmp_path, progress=lambda *a: seen.append(a),
                                  stream=lambda url: _Resp(data))
    assert out.read_bytes() == data and seen[-1] == ("x.tar.gz", len(data), len(data))
    with pytest.raises(binaries.LlamaDownloadError, match="sha256 mismatch"):
        binaries.download_asset("y.tar.gz", "0" * 64, tmp_path, stream=lambda url: _Resp(data))
    assert not (tmp_path / "y.tar.gz").exists() and not (tmp_path / "y.tar.gz.part").exists()


def test_download_unpacks_and_finds_nested_binaries(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    flavor = binaries.Flavor("test", ("llama-b12176-bin-test.tar.gz",))
    archive = _tarball({"build/bin/llama-server": b"#!/bin/sh\n", "build/bin/ggml-rpc-server": b"#!/bin/sh\n"})

    def fetch(name, sha, d):
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(archive)
        return d / name

    server, rpc = binaries.download_llama(tmp_path, flavor, digests={flavor.assets[0]: "x"}, fetch=fetch)
    root = tmp_path / "llama.cpp" / "b12176" / "test"
    assert Path(server) == root / "build/bin/llama-server" and Path(rpc) == root / "build/bin/ggml-rpc-server"
    assert binaries.find_pair(root) == (server, rpc)
    with pytest.raises(binaries.LlamaDownloadError, match="no verified asset"):
        binaries.download_llama(tmp_path, flavor, digests={})


def test_windows_binaries_need_exe(tmp_path):
    (tmp_path / "llama-server.exe").write_bytes(b"")
    (tmp_path / "rpc-server.exe").write_bytes(b"")
    assert binaries.find_pair(tmp_path, "linux") is None
    assert binaries.find_pair(tmp_path, "win32") == (str(tmp_path / "llama-server.exe"),
                                                     str(tmp_path / "rpc-server.exe"))


def test_ensure_llama_prefers_compute_llama_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / "llama-server").write_bytes(b"")
    (tmp_path / "rpc-server").write_bytes(b"")
    monkeypatch.setenv("COMPUTE_LLAMA_DIR", str(tmp_path))
    monkeypatch.setattr(binaries, "download_llama", lambda *a, **k: pytest.fail("must not download"))
    assert binaries.ensure_llama(tmp_path / "home") == (str(tmp_path / "llama-server"), str(tmp_path / "rpc-server"))


# ------------------------------------------------------------ memory


def test_lendable_memory_per_platform():
    ram = 32 * GIB
    assert hardware.lendable_base_bytes("darwin", ram, [12 * GIB]) == 28 * GIB      # unified memory - 4 GiB
    assert hardware.lendable_base_bytes("win32", ram, [8 * GIB, 24 * GIB]) == 24 * GIB
    assert hardware.lendable_base_bytes("win32", ram, []) == 28 * GIB               # CPU only
    assert hardware.memory_gb_for_percent(50, 24 * GIB) == 12.0
    assert committed_memory_gb(0, 50, 24 * GIB) == 12.0
    assert committed_memory_gb(6, 50, 24 * GIB) == 6                                # --memory-gb wins


def test_gpu_memory_from_list_devices_and_nvidia_smi():
    text = ("Available devices:\n  CUDA0: NVIDIA GeForce RTX 4090 (24564 MiB, 23000 MiB free)\n"
            "  BLAS: OpenBLAS (0 MiB, 0 MiB free)\n")
    gpus = hardware.gpu_devices(text)
    assert [g.name for g in gpus] == ["CUDA0"] and gpus[0].total_mib == 24564
    assert hardware.parse_nvidia_smi_mib("24564\n8192\n") == [24564 * 1024 ** 2, 8192 * 1024 ** 2]
    assert hardware.nvidia_vram(which=lambda n: None) == []


def test_detect_uses_psutil_and_list_devices(monkeypatch):
    out = "Available devices:\n  MTL0: Apple M4 Max (98304 MiB, 98000 MiB free)\n"
    monkeypatch.setattr(hardware, "_run", lambda *cmd, **kw: out if "--list-devices" in cmd else None)
    info = hardware.detect("llama-server", platform_name="darwin")
    assert info["chip"] == "Apple M4 Max" and info["gpus"] == ["Apple M4 Max"]
    assert info["total_mem_bytes"] > 0 and info["lendable_base_bytes"] == max(0, info["total_mem_bytes"] - 4 * GIB)


# ------------------------------------------------------------ model scan


def _mmproj_gguf() -> bytes:
    return write_gguf({"general.architecture": "clip", "general.type": "mmproj"}, [("v.patch_embd.weight",
                                                                                     (16, 16), 0)])


def _can_symlink(tmp_path) -> bool:
    """Windows only allows symlinks with Developer Mode or admin rights."""
    try:
        (tmp_path / "probe-link").symlink_to(tmp_path)
    except OSError:
        return False
    (tmp_path / "probe-link").unlink()
    return True


def test_scan_follows_symlinks_and_pairs_mmproj(tmp_path):
    if not _can_symlink(tmp_path):
        pytest.skip("this OS account cannot create symlinks")
    blobs, snap = tmp_path / "hub/models--x/blobs", tmp_path / "hub/models--x/snapshots/abc"
    blobs.mkdir(parents=True)
    snap.mkdir(parents=True)
    (blobs / "sha-model").write_bytes(tiny_gguf())
    (blobs / "sha-mmproj").write_bytes(_mmproj_gguf())
    (snap / "Tiny-Q4.gguf").symlink_to("../../blobs/sha-model")
    (snap / "mmproj-F16.gguf").symlink_to("../../blobs/sha-mmproj")
    plain = tmp_path / "models"
    plain.mkdir()
    (plain / "other.gguf").write_bytes(tiny_gguf())

    files = scan_models([str(tmp_path / "hub"), str(plain)])
    by_name = {f["name"]: f for f in files}
    assert set(by_name) == {"Tiny-Q4.gguf", "other.gguf"}             # the mmproj is not a model
    assert by_name["Tiny-Q4.gguf"]["size"] == (blobs / "sha-model").stat().st_size
    assert by_name["Tiny-Q4.gguf"]["mmproj"] == "mmproj-F16.gguf"
    assert "mmproj" not in by_name["other.gguf"]


async def test_models_list_reports_vision(tmp_path):
    h = await start_harness(fast_settings(), tmp=str(tmp_path))
    try:
        d = tmp_path / "m"
        d.mkdir()
        (d / "tiny.gguf").write_bytes(tiny_gguf())
        (d / "mmproj.gguf").write_bytes(_mmproj_gguf())
        await h.add_node(FakeNode("head", 16, may_be_head=True), 0, gguf_files=scan_models([str(d)]))
        async with httpx.AsyncClient(base_url=h.url) as c:
            data = (await c.get("/v1/models")).json()["data"]
        assert [(m["id"], m["vision"]) for m in data] == [("tiny.gguf", True)]
    finally:
        await h.stop()


def test_head_passes_mmproj_to_llama_server(tmp_path):
    assert head_extra_args({}, tmp_path / "mmproj.gguf") == ("--mmproj", str(tmp_path / "mmproj.gguf"))
    assert head_extra_args({"n_gpu_layers": 41}, None) == ("-ngl", "41")


@pytest.mark.skipif(not list(Path("~/.cache/huggingface/hub/models--unsloth--Qwen3.8-27B-GGUF/snapshots")
                             .expanduser().glob("*/mmproj-F16.gguf")), reason="demo model not downloaded")
def test_real_qwen_snapshot_pairs_mmproj():
    snap = Path("~/.cache/huggingface/hub/models--unsloth--Qwen3.8-27B-GGUF/snapshots").expanduser()
    files = scan_models([str(snap)])
    qwen = next(f for f in files if f["name"] == "Qwen3.8-27B-UD-Q4_K_XL.gguf")
    assert qwen["mmproj"] == "mmproj-F16.gguf" and all(f["name"] != "mmproj-F16.gguf" for f in files)


# ------------------------------------------------------------ CPU-only head


def test_cpu_only_head_keeps_first_layers_on_cpu():
    devices = parse_list_devices("Available devices:\n  RPC0: 10.0.0.2:5000 (16000 MiB, 15000 MiB free)\n"
                                 "  RPC1: 10.0.0.3:5000 (16000 MiB, 15000 MiB free)\n")
    spec = {"head_layers": 8, "workers": [{"endpoint": "10.0.0.2:5000", "node_id": "a", "n_layers": 20},
                                          {"endpoint": "10.0.0.3:5000", "node_id": "b", "n_layers": 12}]}
    layout = head_layout(devices, spec, "head")
    assert layout["order"] == ["head", "a", "b"]
    assert layout["tensor_split"] == [20, 13] == cpu_head_split(devices, {"10.0.0.2:5000": 20, "10.0.0.3:5000": 12})
    n_layer = 40
    assert layout["n_gpu_layers"] == n_layer + 1 - 8
    # llama.cpp: layers < n_layer + 1 - ngl on the CPU, the rest split over the RPC devices
    ranges = layer_ranges_from_vector(layout["tensor_split"], n_layer - 8)
    assert [(a + 8, b + 8) for a, b in ranges] == [(8, 28), (28, 40)]


def test_gpu_head_still_goes_last():
    devices = parse_list_devices("  RPC0: 10.0.0.2:5000 (16000 MiB, 15000 MiB free)\n"
                                 "  CUDA0: RTX (24000 MiB, 23000 MiB free)\n")
    layout = head_layout(devices, {"head_layers": 10, "workers": [
        {"endpoint": "10.0.0.2:5000", "node_id": "a", "n_layers": 30}]}, "head")
    assert layout["order"] == ["a", "head"] and layout["tensor_split"] == [30, 11] and "n_gpu_layers" not in layout


# ------------------------------------------------------------ processes


def test_reaper_uses_psutil_and_kills_only_llama(tmp_path, monkeypatch):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        pid_file = tmp_path / "pids"
        pid_file.write_text(json.dumps([other.pid]))
        assert reap_orphans(pid_file) == [] and other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_read_pid_ignores_dead_processes(tmp_path):
    (tmp_path / "inference").mkdir()
    (tmp_path / "inference" / "node.pid").write_text("999999\n")
    assert read_pid(tmp_path) is None
    (tmp_path / "inference" / "node.pid").write_text(f"{__import__('os').getpid()}\n")
    assert read_pid(tmp_path) is not None


async def test_stop_file_shuts_the_node_down(tmp_path):
    stop, shutdown = tmp_path / "stop", asyncio.Event()
    task = asyncio.create_task(watch_stop_file(stop, shutdown, interval=0.02))
    await asyncio.sleep(0.05)
    assert not shutdown.is_set()
    stop.write_text("1")
    await asyncio.wait_for(task, 2)
    assert shutdown.is_set() and not stop.exists()


def test_windows_children_get_their_own_process_group(monkeypatch):
    from compute.engine.node import head
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert head.spawn_kwargs() == {"creationflags": 0x200 | 0x08000000}
    monkeypatch.setattr(sys, "platform", "darwin")
    assert head.spawn_kwargs() == {}


# ------------------------------------------------------------ auto transport

QWEN = "Qwen3.8-27B-UD-Q4_K_XL.gguf"


class UnreachableEchoEngine(EchoEngine):
    """Advertises a closed port (the head cannot connect), but serves the relay bridge locally."""

    async def start_worker(self, pipeline_id, spec):
        res = await super().start_worker(pipeline_id, spec)
        return {"endpoint": "127.0.0.1:1", "local_endpoint": res["endpoint"]}


async def test_auto_transport_falls_back_to_relay_per_link():
    h = await start_harness(fast_settings(TRANSPORT="auto"))
    try:
        h.add_model(synthetic_layout(QWEN, n_layers=64, total_bytes=int(26 * GB), head_bytes=1 * GB))
        await h.add_node(FakeNode("head", 12, may_be_head=True, files=(QWEN,)), 0, engine_cls=EchoEngine)
        await h.add_node(FakeNode("near", 12), 1, engine_cls=EchoEngine)
        await h.add_node(FakeNode("far", 12), 2, engine_cls=UnreachableEchoEngine)
        assert all(a.transport == "auto" for a in h.agents.values())
        r = await chat(h, QWEN, max_tokens=4)
        assert r.status_code == 200, r.text
        pid = r.json()["network"]["pipeline_id"]
        assert {m["node"] for m in r.json()["network"]["members"]} == {"head", "near", "far"}
        proxies = h.agents["head"].proxies[pid]
        assert [p.worker_id for p in proxies] == [h.ids["far"]]          # only the unreachable link is relayed

        reader, writer = await asyncio.open_connection("127.0.0.1", proxies[0].port)
        writer.write(b"ping over the relay")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(19), 10) == b"ping over the relay"
        writer.close()
    finally:
        await h.stop()


def test_auto_is_default_and_uses_relay_hop_limit():
    from compute.engine.config import InferenceSettings
    s = InferenceSettings()
    assert s.TRANSPORT == "auto" and s.max_hop_ms == s.MAX_HOP_MS_RELAY
    assert s.replace(TRANSPORT="direct").max_hop_ms == s.MAX_HOP_MS
