#!/usr/bin/env bash
# Builds the Decky Loader plugin zip by hand, in the layout the Decky CLI produces
# (release.yml "Package plugin"), without its container: a top-level romm-tender/
# folder holding dist/index.js, bin/, py_modules/, main.py, plugin.json,
# package.json and LICENSE, with defaults/ flattened into the folder's root.
#
# Usage: scripts/package_decky_zip.sh [version-suffix]
#   version-suffix  stamped onto the packaged plugin.json and package.json only,
#                   e.g. "netplay.1" packages 0.33.0 as 0.33.0-netplay.1.
# Output: build/Tender.zip and build/Tender.zip.sha256

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUFFIX="${1:-}"
OUT_DIR="$ROOT/build"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/romm-tender"

cd "$ROOT"
pnpm build

mkdir -p "$STAGE/dist" "$OUT_DIR"
cp dist/index.js "$STAGE/dist/"
cp -a bin "$STAGE/bin"
rsync -a --exclude '__pycache__' --exclude '*.pyc' py_modules/ "$STAGE/py_modules/"
cp main.py plugin.json package.json LICENSE "$STAGE/"
cp -a defaults/. "$STAGE/"

if [ -n "$SUFFIX" ]; then
    python3 - "$STAGE/plugin.json" "$STAGE/package.json" "$SUFFIX" <<'PYEOF'
import json
import sys

*paths, suffix = sys.argv[1:]
for path in paths:
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["version"] = f"{manifest['version']}-{suffix}"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
PYEOF
fi

rm -f "$OUT_DIR/Tender.zip"
(cd "$WORK" && zip -qrX "$OUT_DIR/Tender.zip" romm-tender)

# The release workflow's smoke test, run against this zip.
ZIP="$OUT_DIR/Tender.zip"
top="$(unzip -Z1 "$ZIP" | cut -d/ -f1 | sort -u)"
[ "$top" = "romm-tender" ] || { echo "ERROR: top-level folder is '$top', not 'romm-tender'" >&2; exit 1; }
for required in main.py plugin.json package.json config.json dist/index.js bin/rom-launcher \
    py_modules/bootstrap/__init__.py py_modules/bootstrap/adapters.py py_modules/bootstrap/services.py \
    py_modules/db/migrations/001_initial.sql py_modules/native/libgavel-x86_64-linux.so \
    py_modules/_vendor/atlas/__init__.py py_modules/_vendor/atlas/data/system_ids.json; do
    unzip -Z1 "$ZIP" | grep -qx "romm-tender/$required" || { echo "ERROR: zip is missing $required" >&2; exit 1; }
done
perms="$(unzip -Z "$ZIP" romm-tender/bin/rom-launcher | awk '{print $1}')"
[[ "$perms" == *x* ]] || { echo "ERROR: bin/rom-launcher is not executable ($perms)" >&2; exit 1; }
if unzip -Z1 "$ZIP" | grep -q '\.map$'; then
    echo "ERROR: zip contains source maps" >&2
    exit 1
fi

(cd "$OUT_DIR" && sha256sum Tender.zip > Tender.zip.sha256)
echo "Built $ZIP ($(du -h "$ZIP" | cut -f1)), version $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$STAGE/plugin.json")"
