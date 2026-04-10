#!/usr/bin/env bash
# Pull a full snapshot of /data/data/com.dsemu.drastic off the device.
#
# Usage: scripts/backup-data.sh [output_dir]
# Default: backups/YYYYMMDD-HHMMSS/
#
# Useful before experimenting so you can restore your save files, key
# bindings, etc. if a build goes wrong.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PKG="com.dsemu.drastic"
TS="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${1:-backups/$TS}"
DEVICE_TMP="/data/local/tmp"
TAR_NAME="drastic-backup-$TS.tar"

command -v adb >/dev/null || { echo "ERROR: adb not found on PATH"; exit 1; }
adb wait-for-device

if ! adb shell "test -d /data/data/$PKG && echo yes" | tr -d '\r' | grep -q yes; then
    echo "ERROR: /data/data/$PKG does not exist. Is DraStic installed?"
    exit 1
fi

echo "==> Stopping $PKG (so caches/prefs are flushed)"
adb shell "am force-stop $PKG" 2>/dev/null || true

echo "==> Taring on device -> $DEVICE_TMP/$TAR_NAME"
adb shell "cd /data/data && tar cpf $DEVICE_TMP/$TAR_NAME $PKG"

mkdir -p "$OUT_DIR"
echo "==> Pulling -> $OUT_DIR/$TAR_NAME"
adb pull "$DEVICE_TMP/$TAR_NAME" "$OUT_DIR/"

# Clean up temp file on device
adb shell "rm -f $DEVICE_TMP/$TAR_NAME"

echo
echo "==> Backup saved: $OUT_DIR/$TAR_NAME"
ls -la "$OUT_DIR/$TAR_NAME"
