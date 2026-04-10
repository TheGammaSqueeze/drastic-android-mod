#!/usr/bin/env bash
# Build the DraStic ESC-save mod APK from a clean base.
#
# Usage: scripts/build.sh [path/to/drastic-base.apk] [output.apk]
#
# Defaults:
#   input  = apks/drastic-base-r2.6.0.4a.apk
#   output = build/drastic-esc-mod.apk
#
# Requires:
#   - java     (JDK 8+)
#   - zipalign (Android build-tools)
#   - apksigner (Android build-tools)
#   - a debug keystore at ~/.android/debug.keystore (auto-created if missing)
#
# The apktool jar is bundled at tools/apktool-2.11.1.jar so you don't
# have to install apktool separately.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IN_APK="${1:-apks/drastic-base-r2.6.0.4a.apk}"
OUT_APK="${2:-build/drastic-esc-mod.apk}"

APKTOOL="tools/apktool-2.11.1.jar"
PATCH_FILE="patches/drastic.patch"
BUILD_DIR="build"
DECODED_DIR="$BUILD_DIR/decoded"
UNSIGNED_APK="$BUILD_DIR/unsigned.apk"
ALIGNED_APK="$BUILD_DIR/aligned.apk"

# ---------- sanity checks ----------
command -v java      >/dev/null || { echo "ERROR: java not found on PATH";      exit 1; }
command -v zipalign  >/dev/null || { echo "ERROR: zipalign not found on PATH (install Android build-tools)";  exit 1; }
command -v apksigner >/dev/null || { echo "ERROR: apksigner not found on PATH (install Android build-tools)"; exit 1; }

[ -f "$IN_APK" ]     || { echo "ERROR: input APK not found: $IN_APK"; exit 1; }
[ -f "$APKTOOL" ]    || { echo "ERROR: apktool not found: $APKTOOL"; exit 1; }
[ -f "$PATCH_FILE" ] || { echo "ERROR: patch file not found: $PATCH_FILE"; exit 1; }

# ---------- debug keystore ----------
KEYSTORE="$HOME/.android/debug.keystore"
if [ ! -f "$KEYSTORE" ]; then
    echo "==> Creating debug keystore at $KEYSTORE"
    mkdir -p "$HOME/.android"
    keytool -genkey -v -keystore "$KEYSTORE" \
        -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
        -storepass android -keypass android \
        -dname "CN=Android Debug,O=Android,C=US"
fi

# ---------- clean build dir ----------
echo "==> Cleaning $BUILD_DIR"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# ---------- decode ----------
echo "==> Decoding $IN_APK -> $DECODED_DIR"
java -jar "$APKTOOL" d -f -o "$DECODED_DIR" "$IN_APK"

# ---------- apply patch ----------
echo "==> Applying $PATCH_FILE"
# The patch was generated with paths relative to the repo root
# (decoded/smali/... vs decoded2/smali/...). Adjust to our build layout.
sed -e 's|^--- decoded/|--- a/|' \
    -e 's|^+++ decoded2/|+++ b/|' \
    "$PATCH_FILE" > "$BUILD_DIR/normalized.patch"

(cd "$DECODED_DIR" && patch -p1 --no-backup-if-mismatch < "$REPO_ROOT/$BUILD_DIR/normalized.patch")

# ---------- rebuild ----------
echo "==> Rebuilding -> $UNSIGNED_APK"
java -jar "$APKTOOL" b "$DECODED_DIR" -o "$UNSIGNED_APK"

# ---------- zipalign ----------
echo "==> Zipaligning -> $ALIGNED_APK"
zipalign -p -f 4 "$UNSIGNED_APK" "$ALIGNED_APK"

# ---------- sign ----------
echo "==> Signing -> $OUT_APK"
mkdir -p "$(dirname "$OUT_APK")"
apksigner sign \
    --ks "$KEYSTORE" \
    --ks-pass pass:android \
    --ks-key-alias androiddebugkey \
    --key-pass pass:android \
    --v1-signing-enabled true \
    --v2-signing-enabled true \
    --v3-signing-enabled true \
    --out "$OUT_APK" \
    "$ALIGNED_APK"

# ---------- verify ----------
echo "==> Verifying"
apksigner verify --verbose "$OUT_APK" | head -6

echo
echo "==> Build complete: $OUT_APK"
ls -la "$OUT_APK"
