# drastic-android-mod

Smali patches for [DraStic](https://play.google.com/store/apps/details?id=com.dsemu.drastic)
that add **"ESC to quick-save and quit, auto-resume on next launch"**
behaviour to the Nintendo DS emulator, plus a save-on-any-exit-path
extension. Built and tested on a GammaOS handheld (Rockchip RK3568,
4x Cortex-A55, Android 14), but nothing in the mod is device-specific -
it works on any ARM64 Android running DraStic r2.6.0.4a.

Everything here - source APKs, decompiled trees, the unified-diff patch,
a full native-binary reverse-engineering writeup, the Python analysis
tooling, and automated build/install scripts - is in this repo. A fresh
checkout can rebuild the modded APK end-to-end with one command.

> **Legal**: DraStic was a commercial Nintendo DS emulator by Exophase
> until it was discontinued and released free of charge by the author.
> The original APK is included in `apks/` on that basis. All patch code
> and tooling in this repo is original work and is MIT-licensed (see
> [LICENSE](LICENSE)).

---

## Table of contents

- [What the mod does](#what-the-mod-does)
- [Repo layout](#repo-layout)
- [Quick start - use the prebuilt APK](#quick-start--use-the-prebuilt-apk)
- [Build from source](#build-from-source)
- [Install on a device](#install-on-a-device)
- [Rolling back](#rolling-back)
- [How it was reverse-engineered](#how-it-was-reverse-engineered)
- [How the patches work](#how-the-patches-work)
- [Troubleshooting](#troubleshooting)
- [Known limitations and side effects](#known-limitations-and-side-effects)
- [Credits and license](#credits-and-license)

---

## What the mod does

### 1. ESC = quick-save and exit

Pressing `KEYCODE_ESCAPE` (Android keycode `111`) while
you're in a game does two things atomically:

1. Synchronously writes a full save state into slot 9 of the currently
   loaded ROM.
2. Calls `Activity.finishAffinity()` - tears down the entire DraStic
   task.

No UI, no confirmation dialog, no state-menu navigation. Press ESC,
DraStic disappears, and the save is on disk.

### 2. Auto-resume on next launch

Every time `DraSticEmuActivity.onCreate` runs with a ROM set (i.e.
every time you open a game), the mod forces `LOADSLOT = 9` into the
activity's `startGame` call. If a `<rom>_9.dss` exists for that ROM,
DraStic loads it and resumes at the saved state. If not,
the native load silently fails and the game boots fresh. This is the right behaviour for a game with no existing quick-save.

This works no matter how the game was launched: from DraStic's own rom
selector, from a homescreen shortcut, from a `file://` or `content://`
VIEW intent, from Daijisho / LaunchBox / any other frontend. The
decision is made inside `EmuActivity.onCreate`, which every launch
path must go through.

### 3. Save on **any** exit path (not just ESC)

The original ESC feature only triggers on the ESC keycode. The third
patch extends the same save-to-slot-9 behaviour to every other way a
game session can end:

- BACK key (if not disabled by `_DisableBackButton`)
- HOME key -> activity backgrounded
- Opening the in-game menu
- Exiting the game via the in-game menu's Exit option
- App switcher / swiping DraStic out of recents
- Another app stealing focus
- Screen lock / device sleep

It does this by rewriting a single comparison in `EmuActivity.onPause`
so that the save block runs unconditionally (instead of only when
`_AutosaveMode` is set), and forcing the `saveState` sync flag to
`true` so the save always completes before teardown.

The only exit path it doesn't catch is a hard SIGKILL (the OOM killer,
`am force-stop`, pulling the battery). That is a limit of the Android lifecycle; no user-space hook can intercept it.

---

## Repo layout

```
drastic-android-mod/
|-- README.md                         <- this file
|-- LICENSE                           <- MIT (for the mod's own code)
|-- .gitignore
|
|-- apks/
|   |-- drastic-base-r2.6.0.4a.apk    <- unmodified DraStic r2.6.0.4a (versionCode 109)
|   `-- drastic-esc-mod.apk           <- prebuilt modded APK, ready to install
|
|-- patches/
|   `-- drastic.patch                      <- unified diff, applied by build.sh
|
|-- scripts/
|   |-- build.sh                      <- decode + patch + rebuild + sign
|   |-- install.sh                    <- uninstall, install, restore data, grant perms
|   |-- rollback.sh                   <- reinstall the unmodified base APK
|   `-- backup-data.sh                <- pull /data/data/com.dsemu.drastic off the device
|
|-- tools/
|   |-- apktool-2.11.1.jar            <- bundled so you don't have to download it
|   |-- analyze.py                    <- capstone + pyelftools static analyser for the native lib
|   `-- disasm.py                     <- per-function disassembler with PLT/string xref annotation
|
|-- decoded/                          <- full apktool decompile of the clean base APK
|   |-- smali/
|   |-- res/
|   |-- AndroidManifest.xml
|   `-- ...
|
|-- decoded2/                         <- same tree with the three patches applied (reference copy)
|   `-- ...
|
|-- native/                           <- reverse-engineering workspace for libdrastic_arm64.so
|   |-- libdrastic_arm64.so           <- lifted out of the base APK (arm64-v8a)
|   |-- libdrastic_cpu.so             <- CPU feature helper
|   |-- libdrastic_arm64.dis          <- full .text disassembly via aarch64-linux-gnu-objdump (245k lines)
|   |-- libdrastic_arm64.elf.txt      <- ELF headers, sections, dynsym, relocations
|   |-- data.dump                     <- .data section hex dump
|   |-- rodata_relro.dump             <- .data.rel.ro hex dump
|   |-- funcs.json                    <- 755 identified function ranges with sizes
|   |-- plt.json                      <- PLT address -> external symbol map
|   |-- calls.json                    <- per-function BL/BLR call list
|   |-- strings.json                  <- rodata strings keyed by address
|   |-- strxref.json                  <- per-function string xrefs
|   `-- summary.txt                   <- top-50 PLT call frequencies, largest fns, mutex/cond callers
|
`-- docs/
    |-- PATCHES.md                    <- line-by-line walkthrough of all three patches
    `-- NATIVE_BINARY_FINDINGS.md     <- full reverse-engineering writeup
```

---

## Quick start - use the prebuilt APK

If you just want to install the mod without building it yourself, the
already-signed APK is in the repo at `apks/drastic-esc-mod.apk`. Plug in
your device, make sure `adb` sees it, then:

```bash
./scripts/install.sh
```

The script force-stops DraStic, backs up `/data/data/com.dsemu.drastic`
to `/data/local/tmp/drastic-backup-<timestamp>` (on the device), installs
the modded APK, restores `files/` and `shared_prefs/` with the new UID,
runs `restorecon`, and re-grants `RECORD_AUDIO` and `BLUETOOTH_CONNECT`.

Your save files, key bindings, cheats and ROM list are all preserved.

On most handheld retro devices (GammaOS, ArkOS, Batocera, etc.) `adb`
runs as root and the install proceeds without further setup. On a stock phone you'll need
root (`adb root` succeeding) because the data-restore step uses `cp -aR`
and `chown` under `/data/data/`.

---

## Build from source

Building the mod from scratch takes about 30 seconds and requires:

### Host prerequisites

| Tool        | Install (Debian/Ubuntu)                                      | Notes                               |
|-------------|--------------------------------------------------------------|-------------------------------------|
| `java`      | `sudo apt install openjdk-17-jdk-headless`                   | JDK 8 or newer                      |
| `zipalign`  | `sudo apt install android-sdk-build-tools` or the standalone build-tools package | part of Android SDK build-tools     |
| `apksigner` | same as zipalign                                             | part of Android SDK build-tools     |
| `adb`       | `sudo apt install android-tools-adb`                         | only needed for `install.sh`        |
| `patch`     | `sudo apt install patch`                                     | GNU patch, used by `build.sh`       |

`apktool` itself is **not** a host dependency - the 2.11.1 jar is
bundled in the repo at `tools/apktool-2.11.1.jar` and the build script
invokes it directly. Any Java 8+ runtime is enough.

### Build command

```bash
./scripts/build.sh
```

The script runs:

```bash
java -jar tools/apktool-2.11.1.jar d -f \
    -o build/decoded apks/drastic-base-r2.6.0.4a.apk

(cd build/decoded && patch -p1 < ../../patches/drastic.patch)

java -jar tools/apktool-2.11.1.jar b build/decoded -o build/unsigned.apk

zipalign -p -f 4 build/unsigned.apk build/aligned.apk

apksigner sign \
    --ks ~/.android/debug.keystore --ks-pass pass:android \
    --ks-key-alias androiddebugkey --key-pass pass:android \
    --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true \
    --out build/drastic-esc-mod.apk build/aligned.apk

apksigner verify --verbose build/drastic-esc-mod.apk
```

Output lands at `build/drastic-esc-mod.apk`. It should match the
reference build in `apks/drastic-esc-mod.apk` bit-for-bit (modulo
signing timestamps).

If `~/.android/debug.keystore` does not exist, the build script creates
one with the standard Android defaults (CN=Android Debug, alias
androiddebugkey, password android).

### Building against a different DraStic base

```bash
./scripts/build.sh path/to/your/drastic-base.apk build/my-custom.apk
```

**Caveat**: the patch is authored against DraStic **r2.6.0.4a**
(versionCode 109). The hunks target specific line numbers in
`DraSticEmuActivity.smali`; if you try to apply them to a different
release of DraStic, `patch` will probably fuzz or fail, and you'll need
to port the edits by hand. See [docs/PATCHES.md](docs/PATCHES.md) for
the exact anchor text to look for.

---

## Install on a device

```bash
./scripts/install.sh                                 # use build/drastic-esc-mod.apk (or apks/ fallback)
./scripts/install.sh build/drastic-esc-mod.apk       # explicit path
./scripts/install.sh apks/drastic-esc-mod.apk        # install the prebuilt
```

The install script:

1. **Force-stops** any running `com.dsemu.drastic`.
2. **Backs up** the existing `/data/data/com.dsemu.drastic` to
   `/data/local/tmp/drastic-backup-<timestamp>` on the device (if the
   package was installed). This preserves your save files, key maps,
   cheats, recent-games list, etc.
3. **Uninstalls** the existing package.
4. **Pushes** the new APK to `/data/local/tmp/` and installs via
   `pm install --bypass-low-target-sdk-block`, with the Play Protect
   package verifier disabled first (`settings put global
   package_verifier_enable 0`) so the install doesn't time out.
5. **Reads the new UID** that Android just assigned to the freshly
   installed package.
6. **Restores** `files/` and `shared_prefs/` from the backup, then
   `chown -R`s them to the new UID and runs `restorecon -RF` to fix
   SELinux labels.
7. **Re-grants** `android.permission.RECORD_AUDIO` and
   `android.permission.BLUETOOTH_CONNECT` via `pm grant`, because
   Android clears runtime permissions on every uninstall.

### Taking a manual backup first

If you'd rather not rely on the auto-backup-to-tmp step (it's bound to
the `/data/local/tmp/` lifetime on your device, which often doesn't
survive a reboot), take a full offline backup first:

```bash
./scripts/backup-data.sh
```

Creates `backups/<timestamp>/drastic-backup-<timestamp>.tar` on your
host machine. That's a `tar cpf` of `/data/data/com.dsemu.drastic` with
permissions preserved. To restore it later, push it back to
`/data/local/tmp/`, untar under `/data/data/`, and `chown` to whatever
UID the current install has.

---

## Rolling back

```bash
./scripts/rollback.sh
```

Uses the same install/restore pipeline but swaps the modded APK for the
unmodified base in `apks/drastic-base-r2.6.0.4a.apk`. Your data is
preserved (via the same backup-on-device step as `install.sh`).

---

## How it was reverse-engineered

The full writeup is at [docs/NATIVE_BINARY_FINDINGS.md](docs/NATIVE_BINARY_FINDINGS.md).
Highlights:

- **`libdrastic_arm64.so`** is a 1.3 MB stripped ELF containing the DS
  emulator core - ARM7/ARM9 interpreters/JITs, the software 3D
  rasteriser pool, and all the JNI entry points.
- The analysis script at `tools/analyze.py` uses Capstone + pyelftools
  to build a **function map** (755 identified functions), **PLT call
  graph**, **string xref table**, and a summary of mutex/condvar
  callers. Everything it produces is checked into `native/*.json`.
- Per-function disassembly is done via `tools/disasm.py`, which
  annotates BL/BLR targets with PLT symbol names and ADRP+ADD string
  references inline. Usage: `python3 tools/disasm.py 0x1a04c`.
- Key findings that shaped the mod:
  - `Java_com_dsemu_drastic_DraSticJNI_saveState` at `0x17f84`:
    `tst w3, #0xff` on the sync boolean arg at `0x17f94` - pass
    `false` and the function returns immediately without waiting for
    the save thread. This is why the first version of the patch
    produced 0-byte save files.
  - `Java_com_dsemu_drastic_DraSticJNI_startGame` at `0x1a04c`: arg 2
    is the load slot. `tbnz w22, #0x1f` at `0x1a17c` - if the slot is
    negative (sign bit set), the load is skipped. Setting the slot to
    `9` in `EmuActivity.onCreate` makes `startGame` queue the load.
  - Earlier investigations of a "bump rasterizer thread count from 3
    to 4" mod revealed that the `_AutosaveMode`'s `q` shared-pref
    field (packed into config bits 16-19) is **dead code** in the
    native binary - it only indexes a string table at `0x144290` used
    for debug output. The actual thread pool in `0x5f614` is
    hardcoded to 3 workers plus 1 dispatcher with no config gate.
    That's documented in the findings file and is the reason this
    repo does **not** contain a thread-count mod.

The reverse-engineering process and the specific `objdump`/`capstone`/
`sed` commands are all captured in the findings doc, so anyone can
re-run the analysis on a different DraStic build.

---

## How the patches work

See [docs/PATCHES.md](docs/PATCHES.md) for the full line-by-line
walkthrough with before/after smali listings for all three hunks. Short
version:

1. **`onKeyDown`** gets an ESC keycode check inserted before the
   existing keymap loop. On ESC (keycode `0x6f`, repeat count 0) it
   calls `DraSticJNI.saveState(9, true)` - synchronous - then
   `Activity.finishAffinity()`, then returns `true` to consume the
   event.

2. **`onCreate`** at `:cond_8` gets a 5-instruction block inserted
   that forces `field o = 9` if `field n` (the ROM file) is non-null.
   Field `o` is the `LOADSLOT` int that eventually becomes arg 2 of
   `startGame`. Any launch path, any intent, any entry point - all
   of them land here.

3. **`onPause`** has the `if-ne v1, v0, :cond_3` (the AutosaveMode
   gate) replaced with `nop`, and a `const/4 v0, 0x1` inserted
   immediately before the existing `saveState` call. The first change
   makes the save block run unconditionally on every pause; the
   second forces the sync flag to `true` so teardown can't race the
   save-thread flush.

The patches only touch `smali/com/dsemu/drastic/DraSticEmuActivity.smali`.
Nothing else in the APK is modified - no native libraries, no
resources, no manifest. The binary delta between the unmodified APK
and the modded APK is exclusively the new `classes.dex`.

---

## Troubleshooting

### `INSTALL_FAILED_VERIFICATION_FAILURE: Integrity verification timed out`

Android's package verifier (Play Protect) is trying to verify the APK
signature against a remote server and timing out. The `install.sh` script works
around this by setting `settings put global package_verifier_enable 0`
before `pm install`. If you're running `pm install` manually, either
disable the verifier first, or push the APK to
`/data/local/tmp/` first and install from there.

### `INSTALL_FAILED_UPDATE_INCOMPATIBLE: Existing package signatures do not match`

You have the Play-signed DraStic installed and you're trying to install
the debug-signed mod (or vice versa). You **must** uninstall first -
Android refuses to swap signing keys on an existing package. The
`install.sh` and `rollback.sh` scripts do this automatically.

### `adb shell input keyevent KEYCODE_ESCAPE` does nothing

A few possibilities:

1. **Notification shade is open.** `dumpsys window | grep mCurrentFocus`
   will show `NotificationShade` as the focused window. The shade
   swallows keyevents. On some devices pulling the shade down is sticky
   and doesn't dismiss on swipe-up. Workaround:

   ```bash
   adb shell "cmd statusbar expand-notifications && cmd statusbar collapse"
   ```

   The explicit expand before collapse forces an animation state that
   the collapse then clears.

2. **Multi-display device.** The device has two logical displays and
   DraStic is on a display other than the one `adb shell input`
   targets. Try passing a display ID:

   ```bash
   adb shell "input -d 0 keyevent 111"
   adb shell "input -d 2 keyevent 111"
   ```

3. **DraStic keymap is eating the key.** If your user config has
   mapped ESC to an in-game button via the DraStic key mapper, the
   keymap loop in `onKeyDown` consumes the event before the mod's ESC
   check would fire. **The mod's ESC check runs BEFORE the keymap
   loop**, so this case should not normally arise. If it does, check
   `_KeyMapConfigs_*` in the DraStic shared_prefs XML and remove any
   mapping that points at keycode `111`.

### Save file shows as 0 bytes, or `_savestate_temp.dss` exists but `_9.dss` doesn't

You're running a build without Patch 1's `sync = true` fix. Either:

- You manually applied an earlier version of the patch that had
  `const/4 v1, 0x0` in the ESC handler, or
- Your build is from an older commit. Rebuild from HEAD with
  `./scripts/build.sh` and reinstall.

Verify the installed APK matches the repo by MD5:

```bash
adb shell "pm path com.dsemu.drastic"  # get the path
adb pull <that path> /tmp/installed.apk
md5sum /tmp/installed.apk build/drastic-esc-mod.apk
```

### Auto-resume doesn't fire - game starts at title screen despite slot 9 existing

Check that the ROM file's basename matches the save file name exactly.
DraStic stores slot 9 as `<rom_basename>_9.dss` in
`/data/data/com.dsemu.drastic/files/DraStic/savestates/`. If your
file is `S Sonic Rush (Eng).nds`, the save should be
`S Sonic Rush (Eng)_9.dss`. Renaming the ROM invalidates the save
because the filename no longer matches.

Also check that `field n` is set in `EmuActivity` - the mod's
`onCreate` patch only forces LOADSLOT=9 **if** the ROM file got set via
the launch intent. If your frontend passes the game via an unusual
intent format (not `GAMEPATH` Parcelable extra, not a `VIEW` intent
with a file/content URI), `field n` may be null and the mod won't
fire. You can verify by checking logcat - the mod doesn't log, but the
stock DraStic does emit `Data: file:///...` in `Lcom/dsemu/drastic/ui/q`
if it received an intent URI.

---

## Known limitations and side effects

- **Slot 9 is reserved.** Any manual save to slot 9 will get
  overwritten on the next exit. Move your manual saves to slots 0-8.
- **Save on every pause has a small perf cost.** On the dev device
  (RK3568, Cortex-A55 @ 2 GHz) saving Sonic Rush takes 50-150 ms.
  Activity teardown feels the pause. On faster SoCs it's imperceptible.
- **Saving on transient pauses (notification shade, app switcher,
  screen off)** is wasted work. The file is written, the user returns,
  nothing changes. Wasteful but harmless.
- **The stock `_AutosaveMode` setting is effectively ignored.** The
  Settings UI toggle still changes the shared pref, but the modded
  `onPause` no longer reads it. If you want to disable the save-on-
  pause behaviour after installing the mod, you'd need to patch it
  back in or revert.
- **Rotation is still excluded from saving** (the mod preserves the
  stock `isChangingConfigurations()` skip). This is intentional -
  saving during a rotation would block teardown on a 100 ms save and
  then immediately reload the same state on recreate.
- **Hard kill is not covered.** `am force-stop`, the OOM killer, the
  user pulling the battery - none of these fire `onPause`. Nothing in
  user space can save state there. It's a fundamental limit of the
  Android lifecycle.

---

## Credits and license

- **DraStic** is by [Exophase](https://www.exophase.com/) and was
  originally a commercial product. It was made freely available by
  the author after commercial distribution ended.
- **Mod, patches, tooling, documentation** in this repo are MIT-licensed.
  See [LICENSE](LICENSE).
- **Apktool** bundled in `tools/` is Apache 2.0 licensed. Source:
  https://github.com/iBotPeaches/Apktool.
- **Capstone** and **pyelftools** (used by the analysis scripts) are
  BSD-licensed.

Bug reports and ports to newer DraStic releases are welcome via pull request.
