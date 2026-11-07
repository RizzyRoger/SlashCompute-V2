#!/usr/bin/env bash
# Build dist/compute.app and an unsigned dist/compute-mac.dmg. Run on a Mac from anywhere.
set -euo pipefail
cd "$(dirname "$0")/.."

uv sync --group dev --group build

rm -rf build/compute dist/compute dist/compute.app dist/compute-mac.dmg
uv run pyinstaller --noconfirm --clean --distpath dist --workpath build packaging/compute.spec

if [ ! -d dist/compute.app ]; then
  echo "dist/compute.app was not produced" >&2
  exit 1
fi

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
cp -R dist/compute.app "$staging/"
ln -s /Applications "$staging/Applications"
hdiutil create -volname compute -srcfolder "$staging" -ov -format UDZO dist/compute-mac.dmg

echo "Built dist/compute.app and dist/compute-mac.dmg (unsigned)"
