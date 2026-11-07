"""Find or download the llama.cpp binaries (``llama-server`` and the RPC server).

Search order: ``$COMPUTE_LLAMA_DIR``, the pinned download in ``<home>/llama.cpp/<build>/<flavor>/``,
``vendor/llama.cpp`` (scripts/build_llama.sh), then PATH. ``ensure_llama`` downloads the pinned
GitHub release build (``-DGGML_RPC=ON -DLLAMA_BUILD_TOOLS=ON``) when none of those has both binaries.
Every archive is checked against the sha256 ``digest`` the GitHub API reports for that asset.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

REPO = Path(__file__).resolve().parents[4]
LLAMA_BUILD = "b12176"
RELEASE_API = f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{LLAMA_BUILD}"
DOWNLOAD_URL = "https://github.com/ggml-org/llama.cpp/releases/download/{build}/{name}"
SERVER_NAMES = ("llama-server",)
RPC_NAMES = ("rpc-server", "ggml-rpc-server")

Progress = Callable[[str, int, int], None]   # (asset name, bytes done, bytes total)


class LlamaDownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Flavor:
    name: str                 # directory name under <home>/llama.cpp/<build>/
    assets: tuple[str, ...]   # release assets unpacked into that directory


def _exe(names: tuple[str, ...], platform_name: str) -> tuple[str, ...]:
    return tuple(n + ".exe" for n in names) if platform_name == "win32" else names


def _system32() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def pick_flavor(platform_name: str | None = None, machine: str | None = None,
                which: Callable[[str], Optional[str]] = shutil.which,
                system32: Path | None = None) -> Flavor:
    """The release build for this machine (macOS arm64; Windows CUDA, Vulkan or CPU; Linux CPU)."""
    plat = platform_name or sys.platform
    arch = (machine or platform.machine()).lower()
    b = LLAMA_BUILD
    if plat == "darwin":
        tag = "arm64" if arch in ("arm64", "aarch64") else "x64"
        return Flavor(f"macos-{tag}", (f"llama-{b}-bin-macos-{tag}.tar.gz",))
    if plat == "win32":
        if which("nvidia-smi"):
            return Flavor("win-cuda-12.4", (f"llama-{b}-bin-win-cuda-12.4-x64.zip",
                                            "cudart-llama-bin-win-cuda-12.4-x64.zip"))
        if ((system32 or _system32()) / "vulkan-1.dll").exists():
            return Flavor("win-vulkan", (f"llama-{b}-bin-win-vulkan-x64.zip",))
        return Flavor("win-cpu", (f"llama-{b}-bin-win-cpu-x64.zip",))
    tag = "arm64" if arch in ("arm64", "aarch64") else "x64"
    return Flavor(f"ubuntu-{tag}", (f"llama-{b}-bin-ubuntu-{tag}.tar.gz",))


def install_dir(home: Path, flavor: Flavor) -> Path:
    return Path(home).expanduser() / "llama.cpp" / LLAMA_BUILD / flavor.name


def _default_home() -> Path:
    from compute.common.config import default_home

    return default_home()


def search_dirs(home: Path | None = None) -> list[Path]:
    dirs = []
    if env := os.environ.get("COMPUTE_LLAMA_DIR"):
        dirs.append(Path(env).expanduser())
    dirs.append(install_dir(home or _default_home(), pick_flavor()))
    dirs += [REPO / "vendor/llama.cpp/build/bin", Path("~/.compute/llama.cpp/build/bin").expanduser()]
    return dirs


def find_in_tree(root: Path, names: tuple[str, ...]) -> Path | None:
    """First file named one of ``names`` anywhere under ``root`` (release archives nest a build dir)."""
    if not root.is_dir():
        return None
    for n in names:
        if (root / n).is_file():
            return root / n
    for n in names:
        for p in sorted(root.rglob(n)):
            if p.is_file():
                return p
    return None


def find_pair(root: Path, platform_name: str | None = None) -> tuple[str, str] | None:
    """(llama-server, rpc-server) when both live under ``root``."""
    plat = platform_name or sys.platform
    server = find_in_tree(root, _exe(SERVER_NAMES, plat))
    rpc = find_in_tree(root, _exe(RPC_NAMES, plat))
    return (str(server), str(rpc)) if server and rpc else None


def installed_pair(home: Path | None = None) -> tuple[str, str] | None:
    """Both binaries from one install dir, else both from PATH, else None."""
    for d in search_dirs(home):
        if pair := find_pair(d):
            return pair
    plat = sys.platform
    server = next((p for n in _exe(SERVER_NAMES, plat) if (p := shutil.which(n))), None)
    rpc = next((p for n in _exe(RPC_NAMES, plat) if (p := shutil.which(n))), None)
    return (server, rpc) if server and rpc else None


def find_binary(*names: str, home: Path | None = None) -> str:
    names = _exe(names, sys.platform) if sys.platform == "win32" else names
    for d in search_dirs(home):
        if found := find_in_tree(d, names):
            return str(found)
    for n in names:
        if found := shutil.which(n):
            return found
    return names[0]


def find_llama_server(home: Path | None = None) -> str:
    return find_binary(*SERVER_NAMES, home=home)


def find_rpc_server(home: Path | None = None) -> str:
    return find_binary(*RPC_NAMES, home=home)


# ---------------------------------------------------------------- download


def parse_digests(release: dict) -> dict[str, str]:
    """asset name -> sha256 hex from the GitHub release JSON (``digest`` = ``sha256:<hex>``)."""
    out = {}
    for a in release.get("assets") or []:
        digest = a.get("digest") or ""
        if a.get("name") and digest.startswith("sha256:"):
            out[a["name"]] = digest.split(":", 1)[1].lower()
    return out


def fetch_digests(get: Callable | None = None) -> dict[str, str]:
    import httpx

    get = get or (lambda url: httpx.get(url, timeout=20, follow_redirects=True,
                                         headers={"Accept": "application/vnd.github+json"}))
    try:
        r = get(RELEASE_API)
        r.raise_for_status()
        return parse_digests(r.json())
    except (httpx.HTTPError, ValueError) as e:
        raise LlamaDownloadError(
            f"cannot reach the GitHub API to verify llama.cpp {LLAMA_BUILD} ({e}). Check the internet "
            f"connection, or install llama.cpp yourself and set COMPUTE_LLAMA_DIR to its bin folder") from e


def download_asset(name: str, sha256: str, dest_dir: Path, progress: Progress | None = None,
                   stream: Callable | None = None) -> Path:
    """Download one release asset to ``dest_dir/name`` and verify its sha256. Raises on mismatch."""
    import httpx

    url = DOWNLOAD_URL.format(build=LLAMA_BUILD, name=name)
    dest_dir.mkdir(parents=True, exist_ok=True)
    part = dest_dir / (name + ".part")
    h = hashlib.sha256()
    stream = stream or (lambda u: httpx.stream("GET", u, timeout=60, follow_redirects=True))
    try:
        with stream(url) as r, open(part, "wb") as fh:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress:
                    progress(name, done, total)
    except httpx.HTTPError as e:
        part.unlink(missing_ok=True)
        raise LlamaDownloadError(f"download of {name} failed: {e}") from e
    if h.hexdigest() != sha256.lower():
        part.unlink(missing_ok=True)
        raise LlamaDownloadError(f"{name}: sha256 mismatch (got {h.hexdigest()}, expected {sha256})")
    final = dest_dir / name
    part.replace(final)
    return final


def unpack(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
        return
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)   # zipfile drops absolute paths and ".." components
        return
    raise LlamaDownloadError(f"unknown archive type: {archive.name}")


def prepare_macos(paths: list[str]) -> None:
    """chmod +x and drop the quarantine flag so Gatekeeper does not block the unsigned binaries."""
    for p in paths:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            os.chmod(p, os.stat(p).st_mode | 0o111)
        parent = str(Path(p).parent)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", parent], capture_output=True, timeout=30)


def download_llama(home: Path, flavor: Flavor | None = None, progress: Progress | None = None,
                   digests: dict[str, str] | None = None, fetch: Callable[[str, str, Path], Path] | None = None
                   ) -> tuple[str, str]:
    """Download, verify and unpack the pinned build into ``<home>/llama.cpp/<build>/<flavor>/``."""
    flavor = flavor or pick_flavor()
    target = install_dir(home, flavor)
    digests = digests if digests is not None else fetch_digests()
    missing = [a for a in flavor.assets if a not in digests]
    if missing:
        raise LlamaDownloadError(f"llama.cpp {LLAMA_BUILD} has no verified asset {missing} on GitHub")
    staging = target.with_name(target.name + ".tmp")
    shutil.rmtree(staging, ignore_errors=True)
    downloads = target.parent / "downloads"
    fetch = fetch or (lambda name, sha, d: download_asset(name, sha, d, progress))
    for asset in flavor.assets:
        archive = fetch(asset, digests[asset], downloads)
        unpack(archive, staging)
        archive.unlink(missing_ok=True)
    pair = find_pair(staging)
    if not pair:
        shutil.rmtree(staging, ignore_errors=True)
        raise LlamaDownloadError(f"llama.cpp {LLAMA_BUILD} ({flavor.name}) has no llama-server/rpc-server binary")
    shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)
    shutil.rmtree(downloads, ignore_errors=True)
    pair = find_pair(target)
    assert pair
    if sys.platform == "darwin":
        prepare_macos(list(pair))
    return pair


def ensure_llama(home: Path | None = None, progress: Progress | None = None) -> tuple[str, str]:
    """(llama-server, rpc-server): an existing install, else the pinned release, downloaded once."""
    home = Path(home).expanduser() if home else _default_home()
    if pair := installed_pair(home):
        return pair
    return download_llama(home, progress=progress)
