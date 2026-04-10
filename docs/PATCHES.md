# Patch-by-patch walkthrough

The patch set is distributed as a single unified diff at
[`patches/drastic.patch`](../patches/drastic.patch) and applied
automatically by `scripts/build.sh` via `patch -p1`. It touches
several files:

- `smali/com/dsemu/drastic/DraSticEmuActivity.smali` - patches 1, 2, 3, 5
- `smali/com/dsemu/drastic/DraSticGlView.smali` - field visibility for patch 5
- `smali/n0/h.smali` - new helper for patch 4
- `smali/com/dsemu/drastic/{DraSticActivity,ui/RomSelector,ui/StateMenu,ui/Cheats,ui/CheatEditor,ui/CheatsCustom,ui/GameMenu,ui/Help,ui/Settings}.smali` - dispatchKeyEvent translation hooks (patch 4)
- `smali/com/dsemu/drastic/ui/{RomSelector,StateMenu,Help,Settings}.smali` - detail-panel ENTER bypass (patch 6)

ui/KeyMapper and ui/KeyMapperTV are deliberately NOT touched by patch
4: the keymap-capture screens still see raw, unrewritten gamepad
keycodes so the user can bind their physical face buttons to DS A/B.
The in-game DS button mapping in DraSticEmuActivity also still uses
the user's raw keymap; patch 5 only intercepts BUTTON_A/BUTTON_B
**while the radial in-game menu is open**, and only inside that
narrow window.

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

## Patch 4 - A=confirm, B=cancel in non-gameplay UI activities

### Symptom

On the test device (Xbox-layout controller exposed via gammapad), the
right face button (`KEYCODE_BUTTON_B`) was acting as confirm in
DraStic's UI menus while the bottom face button (`KEYCODE_BUTTON_A`)
was acting as cancel or back. Reported as "really inconsistent
behaviour using the B button as the confirmation button" by the user.
The user wants the standard Xbox-style mapping: A confirms, B cancels.

### Root cause

DraStic's stock UI activities all route their key events through a
helper class `Ln0/h;->a(KeyEvent)KeyEvent` which exists to support
iCade and 8-bitty Bluetooth controllers. The default handler then
matches `KEYCODE_BUTTON_B` (97) against the system "back" semantics
in some places and against confirm semantics in others, with
inconsistent results across activities. The behaviour the user saw
was the result of that inconsistency, not of a single dispatch table.

### Fix

Patch 4 inserts a small **rewrite shim** in front of the existing
helper. A new method `Ln0/h;->aUi(KeyEvent)KeyEvent` rewrites
controller face buttons into well-known keyboard keycodes that every
DraStic UI menu already handles consistently:

- `KEYCODE_BUTTON_A` (96) -> `KEYCODE_ENTER` (66) - confirm
- `KEYCODE_BUTTON_B` (97) -> `KEYCODE_BACK`  (4)  - cancel
- anything else -> unchanged

After translating, `aUi()` chains into the existing `a()` so the
iCade/8-bitty translation table still runs unchanged. Each non-game
UI activity's `dispatchKeyEvent` is then redirected to call `aUi()`
instead of `a()`:

```smali
# was:
invoke-virtual {v0, p1}, Ln0/h;->a(Landroid/view/KeyEvent;)Landroid/view/KeyEvent;

# now:
invoke-virtual {v0, p1}, Ln0/h;->aUi(Landroid/view/KeyEvent;)Landroid/view/KeyEvent;
```

The activities patched:

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

### What this does not change

- DS gameplay button mapping. The user keymap loop in
  `DraSticEmuActivity.onKeyDown` consumes raw keycodes against
  `f0/h.i1[]` exactly as configured.
- KeyMapper / KeyMapperTV. Both can still capture raw keycodes for
  DS A/B remapping.
- The iCade/8-bitty Bluetooth controller path inside `Ln0/h;->a()`,
  which is preserved unchanged because `aUi()` chains into `a()`
  after its own translation.
- The radial in-game menu. That menu does not flow through
  `dispatchKeyEvent` at all - see patch 5.

---

## Patch 5 - A=confirm, B=cancel in the radial in-game menu

### Symptom

After patch 4 was applied, all of the standalone DraStic UI
activities (rom selector, state menu, settings, etc.) correctly
treated `KEYCODE_BUTTON_A` as confirm. The **radial in-game menu**
(the circular overlay drawn on top of the GL surface during gameplay)
still required `KEYCODE_BUTTON_B` to make a selection.

### Root cause

The radial menu is not a separate Activity. It is rendered by
`Lo0/b;` directly on top of the running emulator's GLSurfaceView,
and its input does not flow through `dispatchKeyEvent` or any of the
helpers that patch 4 touched. The dispatch chain is:

```
DraSticEmuActivity.onKeyDown(int, KeyEvent)
  -> for each i in f0/h.i1[0..28]:
       if keyCode == i1[i]:
         DraSticGlView.w0(i, true)         # i is the user's mapped DS button index
           -> if DraSticGlView.G == true (radial menu open):
                Lo0/b;->k(i, true)         # update bitmask, fire confirm/cancel transitions
              else:
                Ln0/i;->C(i, true)         # in-game DS button press
```

`Lo0/b;->k(I, Z)V` interprets the DS button index by **bit
position**:

- bit 2 = DS START -> press transition fires confirm (`g(t, true)`)
- bit 3 = DS SELECT -> release transition fires cancel (`m.o()`)

The user has DS START mapped to `KEYCODE_BUTTON_B` and DS SELECT
mapped to `KEYCODE_BUTTON_A` in their keymap. So pressing physical
B routes through the keymap loop, becomes DS START, sets bit 2,
fires confirm. Pressing physical A becomes DS SELECT, sets bit 3,
which only fires cancel on release. That is exactly the
"B confirms, A does nothing useful" symptom the user reported.

Patch 4 cannot fix this on its own because `dispatchKeyEvent` is
never called for these events: in EmuActivity, `onKeyDown` consumes
the event before it would reach `dispatchKeyEvent`, and the keymap
loop translates the raw keycode into a DS index using the user's
configuration.

### Fix

Patch 5 adds a **keymap-independent intercept** at the very top of
`DraSticEmuActivity.onKeyDown` and `onKeyUp`. The intercept:

1. Reads the EmuActivity's `e:DraSticGlView` field.
2. Reads `DraSticGlView.G:Z` (radial-menu-open flag).
3. If both are non-null/true and the event is not an auto-repeat,
   rewrites `KEYCODE_BUTTON_A` and `KEYCODE_BUTTON_B` to the DS bit
   indices the radial menu actually wants:
   - `KEYCODE_BUTTON_A` (96) -> `DraSticGlView.w0(2, pressed)` = DS
     START bit, fires confirm on press
   - `KEYCODE_BUTTON_B` (97) -> `DraSticGlView.w0(3, pressed)` = DS
     SELECT bit, fires cancel on release
4. Returns `true` so the rest of `onKeyDown` (the user keymap loop)
   does not also fire.

Crucially this is **only active while the radial menu is open**.
When the menu is closed (`G == false`), `onKeyDown` falls through
to the user keymap loop unchanged, so the user's in-game DS button
mapping is untouched.

`DraSticGlView.G` is `private` in the stock APK; patch 5 changes it
to `public` so EmuActivity can read it via `iget-boolean` without
needing reflection or a getter helper.

The full inserted block in `onKeyDown`:

```smali
:cond_esc_skip
iget-object v0, p0, Lcom/dsemu/drastic/DraSticEmuActivity;->e:Lcom/dsemu/drastic/DraSticGlView;

if-eqz v0, :cond_radial_skip

iget-boolean v1, v0, Lcom/dsemu/drastic/DraSticGlView;->G:Z

if-eqz v1, :cond_radial_skip

if-nez p2, :cond_radial_skip          # ignore auto-repeat

const/16 v1, 0x60                     # KEYCODE_BUTTON_A
if-ne p1, v1, :cond_radial_b

const/4 v1, 0x2                       # DS START bit position
const/4 v2, 0x1                       # pressed = true
invoke-virtual {v0, v1, v2}, Lcom/dsemu/drastic/DraSticGlView;->w0(IZ)V

const/4 v0, 0x1
return v0

:cond_radial_b
const/16 v1, 0x61                     # KEYCODE_BUTTON_B
if-ne p1, v1, :cond_radial_skip

const/4 v1, 0x3                       # DS SELECT bit position
const/4 v2, 0x1
invoke-virtual {v0, v1, v2}, Lcom/dsemu/drastic/DraSticGlView;->w0(IZ)V

const/4 v0, 0x1
return v0

:cond_radial_skip
```

A symmetric block lives in `onKeyUp`, calling `w0(2, false)` and
`w0(3, false)`. The release call on bit 3 is what actually fires
the cancel callback (`m.o()`) inside `Lo0/b;->k()`, which closes
the radial menu without selecting an item.

### What this does not change

- The user's in-game DS button keymap. Outside the radial menu
  (`G == false`), `onKeyDown` falls through to the keymap loop
  exactly as before, and BUTTON_A/B are routed through the user's
  configured DS mappings.
- KeyMapper / KeyMapperTV. The radial menu cannot be open during
  keymap capture, so the intercept never fires there.
- The iCade/8-bitty Bluetooth controller path. Patch 5 only adds
  code to EmuActivity; `Ln0/h;->a()` is untouched.

---

## Patch 6 - Route ENTER past the top-left back icon in detail panels

### Symptom

After patch 4 was applied, UI menus correctly treated the bottom face
button (`KEYCODE_BUTTON_A`) as confirm. But on the ROM list's
"Start Game" detail screen (and similar detail panels in StateMenu,
Help, and Settings), pressing A via the controller did **not** start
the game. Instead it went back to the previous screen. Tapping the
button with the touchscreen worked fine. Tapping any blank area of
the detail panel once and *then* pressing A also worked.

### Root cause

DraStic detail panels have two independent selection states:

1. **DraStic's own selection index** (`o:I` in RomSelector, similar
   fields in the others). This is what draws the orange highlight
   you see around "Start Game" or other panel items. DraStic's
   `onKeyDown` uses it as an index into an array of button IDs
   (`h:[I`) and calls `findViewById(h[o]).performClick()` when the
   user presses confirm.
2. **Android view focus**. When the detail panel is shown via
   controller navigation, Android auto-focuses the first focusable
   view in traversal order, which for every detail panel is the
   top-left `btn_settings_back_rompick` icon (a `LinearLayout` that
   acts as the back arrow).

`dispatchKeyEvent` translates `KEYCODE_BUTTON_A` -> `KEYCODE_ENTER`
via `aUi()` and then forwards to `super.dispatchKeyEvent`.
`super.dispatchKeyEvent` delivers the event to the currently focused
view - the back icon. The back icon's click listener fires, which
calls `setDisplayedChild(0)`, returning the user to the main list.
DraStic's own `onKeyDown` detail-panel handler (which would have
called `performClick` on Start Game) is never invoked because the
event was already consumed by the focused back view.

Touch works because touch dispatch (`dispatchTouchEvent`) bypasses
focus entirely. Tapping blank space clears focus on the back icon,
so a subsequent A press has no focused view for `super.dispatchKeyEvent`
to deliver ENTER to, and the event falls through to the Activity's
`onKeyDown`, which consults DraStic's own `o` index and clicks the
right button.

### Fix

In each affected activity's `dispatchKeyEvent`, after `aUi()`
translation, detect when:

1. The activity's `ViewAnimator` is showing a non-zero child
   (detail panel, not main list), and
2. The translated keycode is `KEYCODE_ENTER` (66).

When both are true, bypass `super.dispatchKeyEvent` entirely and
invoke the Activity's own `onKeyDown(int, KeyEvent)` or
`onKeyUp(int, KeyEvent)` directly, then return `true`. This makes
DraStic's own handler authoritative for confirm in detail panels
regardless of which view currently holds Android focus.

The inserted block (RomSelector variant):

```smali
iget-object v0, p0, Lcom/dsemu/drastic/ui/RomSelector;->i:Landroid/widget/ViewAnimator;
if-eqz v0, :cond_super
invoke-virtual {v0}, Landroid/widget/ViewAnimator;->getDisplayedChild()I
move-result v0
if-eqz v0, :cond_super                   # main list -> normal path

invoke-virtual {p1}, Landroid/view/KeyEvent;->getKeyCode()I
move-result v0
const/16 v1, 0x42                        # KEYCODE_ENTER
if-ne v0, v1, :cond_super

invoke-virtual {p1}, Landroid/view/KeyEvent;->getAction()I
move-result v1
if-nez v1, :cond_up_direct                # action != DOWN -> onKeyUp

invoke-virtual {p0, v0, p1}, Lcom/dsemu/drastic/ui/RomSelector;->onKeyDown(ILandroid/view/KeyEvent;)Z
const/4 v2, 0x1
return v2

:cond_up_direct
invoke-virtual {p0, v0, p1}, Lcom/dsemu/drastic/ui/RomSelector;->onKeyUp(ILandroid/view/KeyEvent;)Z
const/4 v2, 0x1
return v2

:cond_super
invoke-super {p0, p1}, Landroid/app/Activity;->dispatchKeyEvent(Landroid/view/KeyEvent;)Z
move-result p1
return p1
```

The same block is inserted into `StateMenu`, `Help`, and `Settings`,
with the field name adjusted (`StateMenu.i`, `Help.i`, `Settings.e`).

### Why not just fix focus?

An alternative fix would be to `requestFocus()` on the Start Game
button (or whatever the DraStic-highlighted item is) whenever the
detail panel is shown. That approach was rejected because:

- It would require tracking the currently-highlighted item's view ID
  and re-requesting focus whenever DraStic updates its `o` index
  (e.g., on every DPAD navigation press), because Android focus
  and DraStic's internal selection would otherwise drift apart.
- It would also silently make the back icon unreachable from keyboard
  Tab navigation, which could break accessibility on other form
  factors.
- The detail panels also have non-ViewAnimator-managed focusable
  views (stats, thumbnails) that would all need to be deliberately
  skipped.

Bypassing `super.dispatchKeyEvent` for the specific case of
"detail panel open + ENTER pressed" leaves focus handling untouched
and gives DraStic's own handler authority over the one key that
actually matters, which is the minimal change that fixes the bug.

### What this does not change

- Main ROM list behaviour. When `displayedChild == 0`, the
  intercept is skipped and `super.dispatchKeyEvent` runs as
  before, so the ListView still receives ENTER to trigger
  `onItemClick`.
- DPAD navigation inside the detail panel. DPAD_UP/DOWN still go
  through `super.dispatchKeyEvent`, so Android view focus still
  moves normally.
- Touch. Tapping the back icon still works because touch events
  bypass `dispatchKeyEvent` entirely.
- Activities without a ViewAnimator (GameMenu, Cheats,
  CheatEditor, CheatsCustom). They don't have this pattern and
  are not patched.
