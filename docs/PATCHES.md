# Patch-by-patch walkthrough

The patch set is distributed as a single unified diff at
[`patches/drastic.patch`](../patches/drastic.patch) and applied
automatically by `scripts/build.sh` via `patch -p1`. It touches
several files:

- `smali/com/dsemu/drastic/DraSticEmuActivity.smali` - patches 1, 2, 3
- `smali/n0/h.smali` - new helper for patch 4
- `smali/m0/y.smali` - radial menu input fix (patch 4)
- `smali/com/dsemu/drastic/{DraSticActivity,ui/RomSelector,ui/StateMenu,ui/Cheats,ui/CheatEditor,ui/CheatsCustom,ui/GameMenu,ui/Help,ui/Settings}.smali` - dispatchKeyEvent translation hooks (patch 4)

DraSticEmuActivity, ui/KeyMapper, and ui/KeyMapperTV are deliberately
NOT touched by patch 4: in-game DS button mapping and the keymap-capture
screens still see raw, unrewritten gamepad keycodes.

This document explains what each hunk does, why it's there, and how it
interacts with the rest of DraStic.

---

## Patch 1 - ESC key: quick-save then terminate

### Location

Inside `onKeyDown(I, KeyEvent)`, right after the existing
`getKeyCode()` / `getRepeatCount()` calls and **before** DraStic's own
keymap loop (`Lf0/h;->i1:[I`).

### Before

```smali
.method public onKeyDown(ILandroid/view/KeyEvent;)Z
    .locals 4

    const-wide/16 v0, 0x1
    :try_start_0
    invoke-static {v0, v1}, Ljava/lang/Thread;->sleep(J)V
    :try_end_0
    .catch Ljava/lang/InterruptedException; {:try_start_0 .. :try_end_0} :catch_0

    :catch_0
    invoke-virtual {p2}, Landroid/view/KeyEvent;->getKeyCode()I
    move-result p1

    invoke-virtual {p2}, Landroid/view/KeyEvent;->getRepeatCount()I
    move-result p2

    const/4 v0, 0x0
    const/4 v1, 0x0

    :goto_0
    ...
```

### After

```smali
.method public onKeyDown(ILandroid/view/KeyEvent;)Z
    .locals 4

    const-wide/16 v0, 0x1
    :try_start_0
    invoke-static {v0, v1}, Ljava/lang/Thread;->sleep(J)V
    :try_end_0
    .catch Ljava/lang/InterruptedException; {:try_start_0 .. :try_end_0} :catch_0

    :catch_0
    invoke-virtual {p2}, Landroid/view/KeyEvent;->getKeyCode()I
    move-result p1

    invoke-virtual {p2}, Landroid/view/KeyEvent;->getRepeatCount()I
    move-result p2

    const/16 v0, 0x6f                  # KEYCODE_ESCAPE = 111

    if-ne p1, v0, :cond_esc_skip       # not ESC -> fall through to DraStic's own handling

    if-nez p2, :cond_esc_skip          # ignore auto-repeat (KeyEvent.getRepeatCount != 0)

    const/16 v0, 0x9                   # slot 9 - see "Slot 9 reservation" below
    const/4 v1, 0x1                    # <- sync = TRUE (critical, see Patch 1 notes)

    invoke-static {v0, v1}, Lcom/dsemu/drastic/DraSticJNI;->saveState(IZ)Z

    invoke-virtual {p0}, Landroid/app/Activity;->finishAffinity()V

    const/4 v0, 0x1
    return v0

    :cond_esc_skip
    const/4 v0, 0x0
    const/4 v1, 0x0

    :goto_0
    ...
```

### Why `sync = true` is mandatory

`Java_com_dsemu_drastic_DraSticJNI_saveState` in `libdrastic_arm64.so`
(function at `0x17f84`) has this prologue:

```
0x17f84  stp   x20, x19, [sp, #-0x20]!
0x17f88  stp   x29, x30, [sp, #0x10]
0x17f8c  add   x29, sp, #0x10
0x17f90  adrp  x8, #0x14c000
0x17f94  tst   w3, #0xff               <- tests the Z (boolean) arg
0x17f98  add   x8, x8, #0x4b4
0x17f9c  mov   w9, #1
0x17fa0  strb  wzr, [x8, #0xc]
0x17fa4  strb  w2, [x8]                <- writes slot number into state struct
0x17fa8  strb  w9, [x8, #1]
0x17fac  b.eq  #0x17ff0                <- if sync==false: JUMP STRAIGHT TO RETURN
0x17fb0  ...                           (poll loop for sync==true case)
0x17ff0  ldp   x29, x30, [sp, #0x10]
0x17ff4  mov   w0, #1
0x17ff8  ldp   x20, x19, [sp], #0x20
0x17ffc  ret
```

If you pass `sync = false`, saveState **sets the slot-save request and
returns immediately**. The actual serialisation runs on the emulator
worker thread. `finishAffinity()` tears that thread down before it can
flush, leaving behind a 0-byte `_savestate_temp.dss` and no `_9.dss`.

With `sync = true`, the function polls `[master+0x4b5]` in a 10 ms loop
until the worker signals completion, then returns. Only then does
`finishAffinity()` run, so the save always lands.

---

## Patch 2 - Force `LOADSLOT = 9` on `DraSticEmuActivity.onCreate`

### Location

Inside `onCreate(Bundle)`, at `:cond_8` (the label that ends the block
reading the `GAMEPATH` parcelable and optional `LOADSLOT` integer from
the launch intent).

### Before

```smali
    :cond_8
    invoke-static {}, Ljava/lang/System;->currentTimeMillis()J
    ...
```

### After

```smali
    :cond_8
    iget-object v0, p0, Lcom/dsemu/drastic/DraSticEmuActivity;->n:Lcom/dsemu/drastic/filesystem/b;

    if-eqz v0, :cond_force_skip        # only if a ROM is set

    const/16 v0, 0x9
    iput v0, p0, Lcom/dsemu/drastic/DraSticEmuActivity;->o:I    # field o = LOADSLOT

    :cond_force_skip
    invoke-static {}, Ljava/lang/System;->currentTimeMillis()J
    ...
```

### Why

Field `n` is the ROM file (`Lcom/dsemu/drastic/filesystem/b;`).
Field `o` is the LOADSLOT int that gets passed as arg 2 to
`startGame(Ljava/lang/String;IJIZJ)Z` further down in the activity's
`t()` method. If `o` is `-1`, the native side sees the sign bit set
(`tbnz w22, #0x1f, ...` in `Java_com_dsemu_drastic_DraSticJNI_startGame`
at `0x1a04c`) and skips the load. If `o` is a non-negative slot
number, the native side stores `[master+0x488] = 0x1e` (load-request
opcode) and `[master+0x4b4] = slot`, and the emulator's main thread
picks it up at next tick.

DraStic already has an "auto-resume" path via
`DraSticActivity.W0(romFile)` -> `Y(context, romFile)` -> `Y0(romFile, slot)`
that sets `LOADSLOT` for us, but:

1. It's gated on `_ShortcutAutoResume` being true in shared prefs, and
2. Not every launch intent routes through `W0()` - the code path
   depends on which state the `DraSticActivity` state machine is in
   when the intent arrives.

Forcing `o = 9` in `EmuActivity.onCreate` directly sidesteps all of
that. Every time `EmuActivity` is created with a ROM, slot 9 is
attempted. If no `_9.dss` exists yet the native load fails silently
and the game boots fresh - exactly the right behaviour the first time
you play a ROM.

### Why slot 9

DraStic reserves `_9.dss` as its "autosave-on-pause" slot when the
user enables `_AutosaveMode = 1` in the stock settings (see
`EmuActivity.onPause` in the unmodified binary). The mod extends that
convention. Slots 0-8 remain available for the user's own manual
quick-save slots via the in-game state menu and are never overwritten
by the mod.

---

## Patch 3 - `onPause`: always save, regardless of AutosaveMode, synchronously

### Location

Inside `onPause()`, the block that originally gated the save on
`f0/h.x == 1` (`_AutosaveMode`).

### Before

```smali
    :cond_2
    sget v1, Lf0/h;->x:I            # v1 = AutosaveMode setting
    const/4 v2, 0x0
    if-ne v1, v0, :cond_3           # if AutosaveMode != 1, skip save
    const/4 v1, 0x1
    goto :goto_0

    :cond_3
    const/4 v1, 0x0

    :goto_0
    invoke-virtual {p0}, Landroid/app/Activity;->isChangingConfigurations()Z
    move-result v3
    if-eqz v3, :cond_4
    const/4 v1, 0x0                 # never save on rotation

    :cond_4
    if-eqz v1, :cond_5
    iget-boolean v1, p0, Lcom/dsemu/drastic/DraSticEmuActivity;->x:Z
    xor-int/2addr v0, v1            # sync = 1 XOR (activity.x flag)
    const/16 v1, 0x9
    invoke-static {v1, v0}, Lcom/dsemu/drastic/DraSticJNI;->saveState(IZ)Z

    :cond_5
```

### After

```smali
    :cond_2
    sget v1, Lf0/h;->x:I            # still loaded (keeps register type valid)
    const/4 v2, 0x0
    nop                             # was: if-ne v1, v0, :cond_3
    const/4 v1, 0x1                 # ALWAYS save
    goto :goto_0

    :cond_3                         # unreachable now, but still valid
    const/4 v1, 0x0

    :goto_0
    invoke-virtual {p0}, Landroid/app/Activity;->isChangingConfigurations()Z
    move-result v3
    if-eqz v3, :cond_4
    const/4 v1, 0x0                 # still skipped on rotation

    :cond_4
    if-eqz v1, :cond_5
    iget-boolean v1, p0, Lcom/dsemu/drastic/DraSticEmuActivity;->x:Z
    xor-int/2addr v0, v1
    const/4 v0, 0x1                 # <- override: force sync = TRUE
    const/16 v1, 0x9
    invoke-static {v1, v0}, Lcom/dsemu/drastic/DraSticJNI;->saveState(IZ)Z

    :cond_5
```

### Why two sub-changes

**Change 3a - `if-ne v1, v0, :cond_3` -> `nop`.**
This neutralises the `AutosaveMode == 1` gate. Execution now always
falls through to `const/4 v1, 0x1; goto :goto_0`, so `v1` is
unconditionally `1` ("do save") before the `isChangingConfigurations`
check. The `:cond_3` label and the `const/4 v1, 0x0` beneath it
become unreachable but remain valid Dalvik bytecode.

**Change 3b - insert `const/4 v0, 0x1` before the saveState call.**
The original computes `sync = 1 XOR activity.x`. Field `activity.x`
is set to `1` in the `u()` method - which is called from both the
MENU keycode handler and from the "Open game menu" shortcut - meaning
whenever the user opens the in-game menu, `activity.x = 1` and the
original `xor` flips `sync` to `0` (async). That was fine in the stock
build because the in-game menu is a *transient* pause (EmuActivity
isn't finishing, just backgrounding), but for the "save on any exit
path" behaviour sync=true is required in every case. The extra
`const/4 v0, 0x1` overrides whatever the xor produced.

### Why rotation is still excluded

`isChangingConfigurations()` returns true during orientation changes
and some other config-driven recreations. Android tears the activity
down and immediately recreates it. Saving here would block the
config-change teardown on a 100-500 ms `saveState` call and then the
fresh `onCreate` would immediately reload the slot that was just written -
wasteful and visibly laggy. Keeping the `v1 = 0 on isChangingConfigurations`
skip preserves the smooth stock behaviour for rotations.

### Exit paths this covers

Any path that invokes `Activity.onPause()` on `DraSticEmuActivity`:

- ESC key (via `finishAffinity()` in Patch 1)
- BACK key (when not disabled by `_DisableBackButton`)
- HOME key
- App switcher / recents
- Starting another activity on top (e.g., the in-game GameMenu)
- `finish()` from `onActivityResult` after GameMenu -> Exit
- Process being swapped out for another app
- Screen lock / device sleep
- Low-memory activity kill (onPause runs before onStop/onDestroy when time permits)

The only real exit path it doesn't cover is a hard SIGKILL (e.g.,
`am force-stop`, `adb kill`, OOM killer with no grace period). Nothing
in user space can intercept those.

---

## Side effects and trade-offs

- **Slot 9 becomes the "auto" slot.** If you previously used slot 9 as
  one of your manual saves, the mod will overwrite it on every exit.
  Move your manual save to slot 0-8 before installing.
- **Saving on every pause has a cost.** `saveState` is not free - on
  an RK3568 (Cortex-A55 @ 2 GHz) saving Sonic Rush takes roughly
  50-150 ms depending on game state. The user sees the activity
  teardown take a beat longer than stock. It's subtle but noticeable
  on a handheld.
- **Saving on transient pauses can surprise you.** Pulling down the
  notification shade triggers `onPause`. Closing it triggers
  `onResume`. Both intermediate states are saved - again, harmless but
  noticeable.
- **The stock `_AutosaveMode` setting is now effectively ignored.**
  The UI toggle in Settings -> Controls -> Autosave Mode still changes
  the shared pref, but the modded `onPause` no longer reads it. If you
  want the ability to disable auto-save entirely, you'd need a further
  patch to gate on a different boolean.

---

## Patch 4 - Nintendo-style A/B in all menus and the radial in-game menu

### Symptom

On a GammaOS handheld (RK3568, gammapad-virtualised controller), the
physical face button labelled "A" did not confirm menu selections.
Instead, the physical "B" button was acting as confirm, with "A" doing
nothing or cancelling. Reported as "really inconsistent behaviour
using the B button as the confirmation button" by the user.

### Root cause

The handheld's physical face buttons follow Nintendo conventions:

- right face button = "A" (Nintendo's primary action button)
- bottom face button = "B" (Nintendo's secondary action button)

Android's `KEYCODE_BUTTON_A` and `KEYCODE_BUTTON_B` are named after
the **Xbox** convention, where:

- `KEYCODE_BUTTON_A` (96) = Xbox A = bottom face button
- `KEYCODE_BUTTON_B` (97) = Xbox B = right face button

The `gammapad` service on this device exposes a virtual gamepad
(`Location: gammapad-virtual`) that maps physical positions to
Android keycodes by **physical position**, not by label. So pressing
the physical button labelled "A" (the right one) emits
`KEYCODE_BUTTON_B`, and pressing the physical "B" (the bottom one)
emits `KEYCODE_BUTTON_A`.

DraStic's stock menu input code maps `KEYCODE_BUTTON_A` to confirm
and `KEYCODE_BUTTON_B` to cancel, following the Xbox convention.
That is the opposite of what a Nintendo-style handheld user expects.

### Fix

Patch 4 swaps the meaning of `KEYCODE_BUTTON_A` (96) and
`KEYCODE_BUTTON_B` (97) **in every menu input path**. After the
patch:

- `KEYCODE_BUTTON_B` (97) -> confirm (the user's physical "A")
- `KEYCODE_BUTTON_A` (96) -> cancel  (the user's physical "B")

DS gameplay button mapping is **not** affected. The user keymap
loop in `DraSticEmuActivity.onKeyDown` continues to consume raw
keycodes against `f0/h.i1[]` exactly as the user has configured
them. Patch 4 only intercepts the input paths that drive **menu
navigation**, not gameplay.

There are three input paths in DraStic that needed the fix, because
controller events arrive through different channels depending on
where the user is:

#### Path 4a - Direct controller callback in `m0/y.a(Ld0/h;)V`

The radial in-game menu (`com.dsemu.drastic.ui.GameMenu`) uses an
`Lm0/y;` instance that registers itself as a `Ld0/c;` listener with
the `Ld0/b;` controller manager. When the user presses a controller
button while the radial menu is open, gammapad delivers a `Ld0/h;`
event via Binder, the controller manager dispatches it on a Handler,
and `m0/y.a(Ld0/h;)V` is called directly. **This bypasses Android's
KeyEvent / dispatchKeyEvent / onKeyDown chain entirely.**

The original code in `m0/y.a()`:

```smali
const/16 v0, 0x60
if-eq p1, v0, :cond_2    # BUTTON_A -> :cond_2 -> y$b.i (confirm)
const/16 v0, 0x61
if-eq p1, v0, :cond_1    # BUTTON_B -> :cond_1 -> y$b.j (cancel)
```

Patched:

```smali
const/16 v0, 0x61
if-eq p1, v0, :cond_2    # BUTTON_B -> :cond_2 -> y$b.i (confirm)
const/16 v0, 0x60
if-eq p1, v0, :cond_1    # BUTTON_A -> :cond_1 -> y$b.j (cancel)
```

Only the two constants are swapped. The `:cond_1`/`:cond_2`
destinations and the rest of the method are unchanged. This is the
hunk that actually fixes the radial menu, because the radial menu
does not go through Android's dispatchKeyEvent.

#### Path 4b - Keyboard fallback in `m0/y.n(I)Z`

Same `m0/y` class also has `n(I)Z`, which is called from
`GameMenu.onKeyDown` for events that DO arrive through Android's
standard KeyEvent path (e.g., a USB keyboard or the system fallback
when gammapad is not active). The same swap applies for consistency:

```smali
# was: const/16 v3, 0x60   # BUTTON_A -> goto_2 (confirm)
const/16 v3, 0x61          # BUTTON_B -> goto_2 (confirm)

# was: const/16 v0, 0x61   # BUTTON_B -> goto_0 (cancel)
const/16 v0, 0x60          # BUTTON_A -> goto_0 (cancel)
```

This also incidentally fixes a separate shadowing bug in `n(I)Z`:
the user-keymap aliases `i1[2]` (DS START) and `i1[3]` (DS SELECT)
were tested before the hardcoded BUTTON_A/B cases. With the user's
DS START mapped to a face button keycode, that test fired first and
routed presses to the wrong action regardless of what the explicit
BUTTON_A/B fallback said. The swap doesn't fix the precedence
problem on its own, but the dispatch-time translation in patch 4c
does, by rewriting the keycode before `n(I)` even sees it.

#### Path 4c - dispatchKeyEvent translation for all UI activities

The third path is Android's standard dispatchKeyEvent flow used by
every non-game DraStic UI activity (rom selector, state menu,
cheat editor, settings, help, in-game GameMenu, etc.). DraStic
already routes every key event in those activities through
`Ln0/h;->a(KeyEvent)KeyEvent` for an unrelated iCade/8-bitty
Bluetooth controller compat hack.

Patch 4 adds a new method `Ln0/h;->aUi(KeyEvent)KeyEvent` that
rewrites the keycode if it is `KEYCODE_BUTTON_A` or `KEYCODE_BUTTON_B`,
then chains into the existing `a()`. The translation is:

- `KEYCODE_BUTTON_B` (97) -> `KEYCODE_ENTER` (66) - confirm
- `KEYCODE_BUTTON_A` (96) -> `KEYCODE_BACK` (4)  - cancel
- anything else -> unchanged

The patch then changes each non-gameplay UI activity's
`dispatchKeyEvent` method to call `aUi()` instead of `a()`. The
activities patched:

- `com.dsemu.drastic.DraSticActivity`
- `com.dsemu.drastic.ui.RomSelector`
- `com.dsemu.drastic.ui.StateMenu`
- `com.dsemu.drastic.ui.Cheats`
- `com.dsemu.drastic.ui.CheatEditor`
- `com.dsemu.drastic.ui.CheatsCustom`
- `com.dsemu.drastic.ui.GameMenu`
- `com.dsemu.drastic.ui.Help`
- `com.dsemu.drastic.ui.Settings`

These activities are NOT patched (they intentionally still use `a()`):

- `com.dsemu.drastic.DraSticEmuActivity` - in-game; raw keycodes
  must reach the keymap loop unchanged so DS button mapping works.
- `com.dsemu.drastic.ui.KeyMapper`,
  `com.dsemu.drastic.ui.KeyMapperTV` - the keymap-capture screens.
  These activities exist specifically to record raw keycodes from
  the user, so any rewriting would prevent the user from binding
  their physical face buttons to DS A/B.

Each per-activity hunk is a single-token edit:

```smali
# was:
invoke-virtual {v0, p1}, Ln0/h;->a(Landroid/view/KeyEvent;)Landroid/view/KeyEvent;

# now:
invoke-virtual {v0, p1}, Ln0/h;->aUi(Landroid/view/KeyEvent;)Landroid/view/KeyEvent;
```

### Why this is gammapad-specific

The swap is correct for any user whose face buttons report keycodes
in **Nintendo-by-physical-position** order, which is the gammapad
default on RK3568 handhelds. A user with a stock Xbox controller
plugged into a regular phone or tablet (where `KEYCODE_BUTTON_A` is
their physical A label) would see this patch invert their menu
controls. If you are building this mod for that kind of device,
revert patch 4 by editing `patches/drastic.patch` to undo the four
constant swaps in `m0/y.smali` and the swap in `n0/h.aUi`.

### What this does not change

- DS gameplay button mapping. The user keymap loop in
  `DraSticEmuActivity.onKeyDown` consumes raw keycodes against
  `f0/h.i1[]` exactly as configured.
- KeyMapper / KeyMapperTV. Both can still capture raw keycodes for
  DS A/B remapping.
- The iCade/8-bitty Bluetooth controller path inside `Ln0/h;->a()`,
  which is preserved unchanged because `aUi()` chains into `a()`
  after its own translation.
