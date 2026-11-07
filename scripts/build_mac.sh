#!/usr/bin/env bash
# Build dist/compute.app and an unsigned dist/compute-mac.dmg. Run on an Apple Silicon Mac from anywhere.
#
# The app carries uv's portable CPython with the locked dependencies, and its executable is a small
# script that execs that Python (the layout the first /compute app used). macOS then asks whether
# "python3.13" may use the local network, which works for ad-hoc signed code. A PyInstaller bundle
# doesn't: macOS looks its executable up by bundle ID, needs an Apple Team ID for that, and without one
# blocks the LAN silently, never asks, and never lists the app under Privacy & Security > Local Network.
# Windows still builds with PyInstaller (scripts/build_windows.ps1).
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"

[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo "Build on an Apple Silicon Mac." >&2; exit 1; }
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

VERSION="$(sed -nE 's/^version = "([^"]+)"/\1/p' pyproject.toml | head -1)"
PY_VERSION="${PY_VERSION:-$(cat .python-version)}"
APP="dist/compute.app"
CONTENTS="$APP/Contents"
RES="$CONTENTS/Resources"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

rm -rf build/compute dist/compute "$APP" dist/compute-mac.dmg
mkdir -p "$CONTENTS/MacOS" "$RES"

echo "==> Python $PY_VERSION (uv's portable CPython)"
uv python install "$PY_VERSION" >/dev/null
# Outside any project and venv, so uv returns its portable CPython rather than a .venv.
PY_SRC="$(cd / && env -u VIRTUAL_ENV uv python find --managed-python --no-project "$PY_VERSION")"
case "$PY_SRC" in */.venv/*|*/venv/*) echo "refusing to bundle a virtualenv: $PY_SRC" >&2; exit 1 ;; esac
cp -R "$(cd "$(dirname "$PY_SRC")/.." && pwd -P)" "$RES/python"
PY="$RES/python/bin/python3"
rm -f "$RES"/python/lib/python3.*/EXTERNALLY-MANAGED

echo "==> dependencies (uv.lock) and compute"
uv export --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt -o "$WORK/requirements.txt" >/dev/null
uv pip install --python "$PY" --no-cache -q -r "$WORK/requirements.txt"
uv pip install --python "$PY" --no-cache -q --no-deps "$REPO"

# Installers may leave symlinks to the build machine's Python: keep the bundle self-contained.
REAL_PY="$(find "$RES/python/bin" -maxdepth 1 -type f -name 'python3.*' ! -name '*-config' | head -1)"
for link in "$RES"/python/bin/*; do
  if [[ -L "$link" && "$(readlink "$link")" == /* ]]; then
    ln -sf "$(basename "$REAL_PY")" "$link"
  fi
done

SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
# The app never uses PyObjC's test suite or pip.
rm -rf "$SITE/PyObjCTest" "$SITE/pip" "$SITE"/pip-*.dist-info "$RES"/python/bin/pip*

# resources.terms_file() looks for docs/TERMS.md beside the installed package.
mkdir -p "$SITE/docs"
cp docs/TERMS.md "$SITE/docs/TERMS.md"

"$PY" -c "
import compute.cli, compute.app.main, compute.server.main, compute.engine.node.__main__, webview, WebKit
from compute.app.resources import admin_dir, static_dir, terms_file
for path in (static_dir() / 'index.html', admin_dir(), terms_file()):
    assert path.exists(), path
" || { echo "the bundled Python cannot run /compute" >&2; exit 1; }

# Compile now: the launcher stops Python writing .pyc files into the signed bundle.
"$PY" -m compileall -q -j 0 "$RES/python/lib" >/dev/null 2>&1 || true

echo "==> app bundle"
cat > "$CONTENTS/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>en</string>
  <key>CFBundleDisplayName</key><string>/compute</string>
  <key>CFBundleExecutable</key><string>compute</string>
  <key>CFBundleIdentifier</key><string>ai.compute.app</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>compute</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSLocalNetworkUsageDescription</key><string>/compute finds and joins compute pools on your local network.</string>
  <key>NSBonjourServices</key><array><string>_compute._tcp</string></array>
</dict>
</plist>
EOF

cat > "$CONTENTS/MacOS/compute" <<'EOF'
#!/bin/bash
# Run /compute on the bundled Python. exec keeps it one process, so macOS asks for local network
# access as python3.13 (see scripts/build_mac.sh).
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV
exec "$RES/python/bin/python3" -m compute "$@"
EOF
chmod +x "$CONTENTS/MacOS/compute"

echo "==> ad-hoc signing"
xattr -cr "$APP"   # codesign refuses Finder info / provenance attributes on copied files
find "$RES/python" -type f \( -name '*.so' -o -name '*.dylib' \) -exec codesign --force -s - {} \; 2>/dev/null
find "$RES/python/bin" -maxdepth 1 -type f -name 'python3.*' ! -name '*-config' -exec codesign --force -s - {} \;
codesign --force -s - "$APP"
codesign --verify --deep --strict "$APP"

echo "==> dist/compute-mac.dmg"
mkdir "$WORK/dmg"
cp -R "$APP" "$WORK/dmg/"
ln -s /Applications "$WORK/dmg/Applications"
hdiutil create -quiet -volname compute -srcfolder "$WORK/dmg" -ov -format UDZO dist/compute-mac.dmg

echo "Built dist/compute.app and dist/compute-mac.dmg ($(du -h dist/compute-mac.dmg | awk '{print $1}'), unsigned)"
