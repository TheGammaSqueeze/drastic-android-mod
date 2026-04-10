#!/usr/bin/env bash
# Roll back: uninstall the mod and reinstall the unmodified base APK.
#
# Usage: scripts/rollback.sh [path/to/drastic-base.apk]
# Default input: apks/drastic-base-r2.6.0.4a.apk
#
# This preserves your data by backing up /data/data/com.dsemu.drastic first
# and restoring it with the new UID after reinstall.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IN_APK="${1:-apks/drastic-base-r2.6.0.4a.apk}"
[ -f "$IN_APK" ] || { echo "ERROR: not found: $IN_APK"; exit 1; }

# Just reuse install.sh - same procedure, different APK
exec bash scripts/install.sh "$IN_APK"
