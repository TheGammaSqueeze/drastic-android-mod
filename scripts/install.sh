#!/usr/bin/env bash
# Install the modded DraStic APK on a connected Android device via adb.
#
# Usage: scripts/install.sh [path/to/drastic-esc-mod.apk]
#
# Default input: build/drastic-esc-mod.apk (output of scripts/build.sh)
#                or apks/drastic-esc-mod.apk (pre-built one shipped in repo)
#
# This script:
#   1. Force-stops any running DraStic
#   2. Backs up the existing /data/data/com.dsemu.drastic if present
#   3. Uninstalls the current DraStic (if any)
#   4. Installs the modded APK (bypassing Play Protect verification)
#   5. Restores files/ and shared_prefs/ from the backup with the new UID
#   6. Re-grants runtime permissions (RECORD_AUDIO, BLUETOOTH_CONNECT)
#
# Requires adb with root access (adb root) or the device must have su.
# On handheld retro devices (GammaOS, ArkOS, etc.) adb runs as root by
# default so this Just Works.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IN_APK="${1:-}"
if [ -z "$IN_APK" ]; then
    if [ -f "build/drastic-esc-mod.apk" ]; then
        IN_APK="build/drastic-esc-mod.apk"
    elif [ -f "apks/drastic-esc-mod.apk" ]; then
        IN_APK="apks/drastic-esc-mod.apk"
    else
        echo "ERROR: no mod APK found. Run scripts/build.sh first, or pass a path."
        exit 1
    fi
fi

[ -f "$IN_APK" ] || { echo "ERROR: not found: $IN_APK"; exit 1; }

PKG="com.dsemu.drastic"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_NAME="drastic-backup-$TS"
DEVICE_TMP="/data/local/tmp"

command -v adb >/dev/null || { echo "ERROR: adb not found on PATH"; exit 1; }
adb wait-for-device

# ---------- force stop ----------
echo "==> Force-stopping $PKG"
adb shell "am force-stop $PKG" 2>/dev/null || true

# ---------- backup existing data (if installed) ----------
if adb shell "test -d /data/data/$PKG && echo yes" | tr -d '\r' | grep -q yes; then
    echo "==> Backing up existing /data/data/$PKG -> $DEVICE_TMP/$BACKUP_NAME"
    adb shell "cp -aR /data/data/$PKG $DEVICE_TMP/$BACKUP_NAME"
    adb shell "du -sh $DEVICE_TMP/$BACKUP_NAME"
    HAVE_BACKUP=1
else
    echo "==> No existing install to back up"
    HAVE_BACKUP=0
fi

# ---------- uninstall ----------
if adb shell "pm list packages | grep -q $PKG" 2>/dev/null; then
    echo "==> Uninstalling $PKG"
    adb shell "pm uninstall $PKG" >/dev/null || true
fi

# ---------- push + install ----------
echo "==> Pushing $IN_APK"
adb push "$IN_APK" "$DEVICE_TMP/drastic-mod.apk" >/dev/null

echo "==> Installing (bypassing Play Protect verification)"
# Disable the package verifier so installs don't time out on signature-stamp mismatch
adb shell "settings put global package_verifier_enable 0" 2>/dev/null || true
adb shell "settings put global upload_apk_enable 0"      2>/dev/null || true
adb shell "pm install --bypass-low-target-sdk-block $DEVICE_TMP/drastic-mod.apk"

# ---------- capture new uid ----------
NEW_UID="$(adb shell "stat -c '%u' /data/data/$PKG" | tr -d '\r')"
NEW_CACHE_GID="$((NEW_UID + 10000))"
echo "==> New package UID: $NEW_UID (cache gid: $NEW_CACHE_GID)"

# ---------- restore data ----------
if [ "$HAVE_BACKUP" -eq 1 ]; then
    echo "==> Restoring files/ and shared_prefs/ with new ownership"
    adb shell "rm -rf /data/data/$PKG/files /data/data/$PKG/shared_prefs" 2>/dev/null || true
    adb shell "cp -aR $DEVICE_TMP/$BACKUP_NAME/files       /data/data/$PKG/files       2>/dev/null || true"
    adb shell "cp -aR $DEVICE_TMP/$BACKUP_NAME/shared_prefs /data/data/$PKG/shared_prefs 2>/dev/null || true"
    adb shell "chown -R $NEW_UID:$NEW_UID /data/data/$PKG/files /data/data/$PKG/shared_prefs 2>/dev/null || true"
    adb shell "restorecon -RF /data/data/$PKG"
fi

# ---------- grant perms ----------
echo "==> Granting runtime permissions"
adb shell "pm grant $PKG android.permission.RECORD_AUDIO"     2>/dev/null || true
adb shell "pm grant $PKG android.permission.BLUETOOTH_CONNECT" 2>/dev/null || true

# ---------- done ----------
echo
echo "==> Install complete."
if [ "$HAVE_BACKUP" -eq 1 ]; then
    echo "    Backup of previous data is at $DEVICE_TMP/$BACKUP_NAME on the device."
    echo "    It will survive until the device is rebooted or you delete it manually."
fi
adb shell "dumpsys package $PKG | grep -E 'versionName|signatures'" | head -2
