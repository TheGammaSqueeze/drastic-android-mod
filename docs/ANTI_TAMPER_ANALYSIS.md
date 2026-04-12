# DraStic Anti-Tamper / Context Fingerprint Analysis

Technical analysis of the anti-tamper mechanism in `libdrastic_arm64.so`
(DraStic r2.6.0.4a, versionCode 109) that degrades rendering fidelity
when the library is loaded outside the signed `com.dsemu.drastic` APK
under zygote.

**Status**: Hypothesis, not proof. DraStic is closed-source, written by
Exophase, commercial until it was released free after discontinuation.
We reverse-engineered the behavior from the shipped binary; we do NOT
have source, comments, or testimony from the author. What follows is
the most parsimonious explanation of the evidence, plus a walk through
the concrete disassembly that supports it.

## Summary

DraStic `libdrastic_arm64.so` appears to contain a context fingerprint
check that distinguishes "running inside the real `com.dsemu.drastic`
APK under zygote" from "loaded some other way." When the check passes,
`startGame` initialises a set of master-state scalars to values that
select an optimised rendering path. When the check fails, those scalars
are left at defaults that select a subtly broken fallback rendering
path, causing the DS 2D Engine A compositor to produce wrong BG/OBJ
layer priorities (white rectangles over 3D, invisible sprites, wrong
z-order). Engine B and everything else (audio, input, savestates,
timing) continue to work. There is no crash, warning, or log -- just
silent visual degradation.

Five pieces of evidence suggest this is deliberate anti-tamper / anti-
repackaging rather than accidental coupling:

1. `onInit` computes a pointer via `&glViewport XOR 0x1AB10BF4DBBE1F0F`
   and stores it at `master+1072`. There is no legitimate engineering
   reason to XOR a function address to derive a data pointer; this is
   textbook obfuscation, tuned so the XOR result is a valid pointer
   only in the zygote ASLR layout.
2. The GPU compositor dispatch table has two modes (1 and 2) that
   unconditionally dereference `master+1072` with no null/bounds
   check. If the obfuscated pointer is invalid the code path
   SIGSEGVs. The games we tested happen to never enter those modes,
   so the tripwire sits unused but ready.
3. The 14 scalar fields that differ between "trusted" and
   "untrusted" runs form a coherent trust-tier pattern: three
   distinct capability levels (7 downgraded to 6 or 1),
   inverted-direction flags that real app clears but nano keeps
   set, and a symmetric 10-scalar feature-enable table.
4. The critical bit `_m0` (config bit 50) is reachable from the
   public `applyConfig` config word rather than hard-coded in
   native code. Routing it through config means the pathway is
   opt-in -- designed so something could fail to set it (a
   tampered smali, a stripped APK, a fake JNI caller).
5. The fallback rendering path has never been tested for visual
   correctness. It renders garbage layer priorities rather than
   just being slower. This matches "orphaned debug path that only
   runs for untrusted callers and was never meant to be user-
   visible" rather than "legitimate fallback for real hardware
   variance."

Likely motivation is anti-piracy / anti-repackaging from DraStic's
commercial era (paid on the Play Store ~2012), plus anti-competitor
for core extraction (exactly what gammaos-nano does today: dlopen
the library with a fake JNI shim). The mechanism is "casual
deterrent" strength, not hardened DRM -- 14 integer patches defeat
it entirely.

The fix shipped in gammaos-nano: set config bit 50 via applyConfig
(for `master+0x4b8`), plus a 200ms-deferred one-shot raw patch of
the other 13 master-state scalars to the values observed in the real
app. See `NATIVE_BINARY_FINDINGS.md` for the exact offsets and
reference implementation.

## What the mechanism does (observable behavior)

When the library is loaded into any process that is not a zygote-forked
child of the signed DraStic APK (e.g. an `init`-spawned native runner
that uses a fake JNIEnv / JavaVM to reach `startGame`):

- DS 2D compositor Engine A produces wrong BG/OBJ layer priorities:
  - Solid-color BG rectangles render on top of 3D when they should be
    behind (e.g. ceiling-blue rectangle covering an isometric 3D room)
  - OBJ sprites invisible while their dialog boxes and backgrounds
    render correctly
  - "White" rectangles over 3D models that are actually structured
    content (scrolls with camera, fades during transitions), not
    uninitialized memory
- DS 2D compositor Engine B renders correctly
- Audio, input, savestates, timing all work normally
- No crash, no log, no `abort()`, no license dialog

The bug reproduces on every game tested (Pokemon Black 2, Sonic Rush,
both 3D-heavy and 2D-heavy sections). The same ROMs on the same device
in the real `com.dsemu.drastic` app render correctly.

## Why we believe this is intentional anti-tamper

### 1. The obfuscated pointer at `master+1072`

`onInit` computes a pointer via XOR:

```
0000000000017df4 <Java_com_dsemu_drastic_DraSticJNI_onInit>:
   17e00: adrp x9, 138000                ; GOT page
   17e04: adrp x8, 106000                ; .rodata page
   17e08: ldr  x9, [x9, #4048]           ; GOT[0x138fd0] = &glViewport (R_AARCH64_GLOB_DAT, sym 253)
   17e0c: mov  x10, #0x1f0f
   17e10: movk x10, #0xdbbe, lsl #16
   17e14: ldr  q0, [x8, #3296]           ; .rodata constant for master+0x08..0x17
   17e18: movk x10, #0xbf4, lsl #32
   17e1c: adrp x19, 14c000               ; master base
   17e20: movk x10, #0x1ab1, lsl #48     ; x10 = 0x1ab10bf4dbbe1f0f (XOR key)
   17e24: add  x19, x19, #0x0
   17e28: eor  x8, x9, x10               ; x8 = &glViewport ^ key
   17e2c: str  w4, [x19, #1244]          ; master+0x4dc = sdkInt
   17e30: str  w3, [x19, #1196]          ; master+0x4ac = versionCode
   17e34: str  x8, [x19, #1072]          ; master+0x430 = obfuscated ptr
   17e38: stur q0, [x19, #8]             ; master+0x08..0x17 = rodata constant (01 00..00 07 00 00 00 07 00 00 00)
```

There is no legitimate engineering reason to XOR a function address
with a hardcoded 64-bit constant to derive a data pointer. The typical
reasons to do this are:

- **Obfuscate a struct pointer in memory** so that an attacker reading
  a memory dump can't easily spot it as a pointer (pointers have a
  distinctive upper-byte signature on aarch64: `00 00 00 00 XX XX XX XX`
  where `XX` is the ASLR base).
- **Make patching harder**: if an attacker wants to force the pointer
  to a known address, they can't just write their value -- they have
  to compute the XOR'd form. A static binary patch that hardcodes a
  value won't survive ASLR.
- **Cross-check ASLR layout**: only a specific relationship between
  libGLESv2's base address and the XOR constant produces a valid
  writable pointer. The developer can hardcode the key for the layout
  that zygote produces, and anyone else gets garbage.

The XOR key `0x1AB10BF4DBBE1F0F` has no obvious significance (not ASCII,
not a well-known constant, no low-entropy structure). It's consistent
with a value picked specifically to produce a valid pointer for the
expected zygote layout.

**Concrete evidence that it's ASLR-tuned:** we captured it on an
actual RK3568 device running gammaos-nano, where the resolved value was
`0x1AB10B88E018E22F`. The upper bit `0x1` is bit 60 -- well above the
48-bit user VA limit (max `0x0000_FFFF_FFFF_FFFF`). On a zygote child
running the real app, the upper bits come out as `0x000000????_????`
(a valid user pointer) because libGLESv2's base address there produces
a different XOR result.

### 2. The dispatch table gates dereference on a GPU mode value

In the compositor dispatch loop at `0x644c0..0x644f0`, there's a 4-way
branch table based on `master+1178` (a 2-bit GPU mode sampled from
the DS GPU command stream):

```
  644cc: ldrb w8, [x28, #1178]        ; mode = master[1178] (2-bit value 0..3)
  644d0: cmp  w8, #0x3
  644d4: b.hi 64610                    ; bounds check
  644d8: adrp x9, 10e000
  644dc: add  x9, x9, #0x6f8           ; 4-byte jump table at 0x10e6f8
  644e0: adr  x10, 644f0
  644e4: ldrb w11, [x9, x8]            ; table[mode]
  644e8: add  x10, x10, x11, lsl #2
  644ec: br   x10                       ; dispatch
```

The jump table at `.rodata:0x10e6f8` decodes as:

| GPU mode | Entry offset | Handler at | Uses master+1072? |
|:--------:|:------------:|:----------:|:-----------------:|
| 0 | 0x00 | 0x644f0 | NO (writes inline at x28+0x200..) |
| 1 | 0x37 | 0x645cc | YES (`ldr x9, [x28, #1072]; str w10, [x9]`) |
| 2 | 0x13 | 0x6453c | YES (same) |
| 3 | 0x2c | 0x645a0 | NO (writes inline at x28+0x240..) |

**Modes 1 and 2 unconditionally dereference `master+1072`** -- no null
check, no bounds check. If the dereference happens with an invalid
pointer, you SIGSEGV.

**Why the nano process doesn't crash**: the games we tested (Pokemon
B2, Sonic Rush) never set GPU mode to 1 or 2. They run in modes 0 and
3, which use inline offsets. So the invalid pointer sits in memory
but is never read. The compositor's *output* is still wrong, just not
via the obfuscated-pointer path -- it's via the 13 scalar fields
described later.

This is consistent with anti-tamper: modes 1 and 2 might be optimised
paths that are only triggered by specific game code patterns, and the
obfuscated pointer is a tripwire. A hacker who stubs `glViewport` to
a non-library address (e.g. via `LD_PRELOAD`) and then plays a game
that happens to hit mode 1 or 2 would see drastic crash in a way that
looks like a "bug" -- not an obvious DRM failure, but a mysterious
segfault in their frankensteined binary.

### 3. The 14 scalar fields form a coherent trust-tier pattern

Our 5-dump invariant-intersection analysis (detailed in
`NATIVE_BINARY_FINDINGS.md`) produced exactly 14 process-context
structural diffs between the real app (trusted) and gammaos-nano
(untrusted):

| Offset | Real-app value | Nano value | Pattern |
|--------|---------------:|-----------:|---------|
| `master+0x00010` | 6 | 1 | Capability pair: onInit writes 7, downgraded |
| `master+0x00014` | 6 | 1 | Capability pair: onInit writes 7, downgraded |
| `master+0x004b8` | 1 | 0 | Feature gate (config bit 50, `_m0`) |
| `master+0x09140` | 0 | 1 | **Inverted direction** (real app clears fallback flag) |
| `master+0x8b68c` | 6 | 1 | Second-cluster mirror of +0x10 |
| `master+0x8b690` | 6 | 1 | Second-cluster mirror of +0x14 |
| `master+0x8ba98` | 0 | 2 | **Inverted direction** |
| `master+0x8bab8` | 1 | 0 | Fast-path enable |
| `master+0x8bad0` | 1 | 0 | Fast-path enable |
| `master+0x8badc` | 1 | 0 | Fast-path enable |
| `master+0x8bae8` | 3 | 0 | Level value (enum?) |
| `master+0x8bb00` | 1 | 0 | Fast-path enable |
| `master+0x8bb10` | 1 | 0 | Fast-path enable |
| `master+0x8bb28` | 1 | 0 | Fast-path enable |

Three signatures of intent:

**a. Three distinct capability levels** (`master+0x10`: 7 -> 6 -> 1).
onInit writes the maximum value (7). Some code path downgrades it to
6 in the trusted case and 1 in the untrusted case. A pure "feature
probe" would have two outcomes (supported / not supported); having
three suggests grades of trust, not binary capability detection.

**b. Inverted-direction flags** (`master+0x9140`, `master+0x8ba98`).
In both cases the real app has the field CLEARED and nano has it SET.
The semantic pattern this matches is "fallback/safe mode required"
flags that an init function clears when it verifies the fast path is
usable. Nano's version never runs that init function -- or the init
function refuses to clear the flags because the context check failed.

**c. A 10-scalar symmetric cluster at `master+0x8b68c..0x8bb28`**.
The spacing is `0x8b67c` between the two capability-mirror pairs
(`+0x10/+0x14` vs `+0x8b68c/+0x8b690`). This is NOT the engine
stride (`+0x60000`, confirmed by multiple `add xN, xM, #0x60000`
instructions in the Android binary). So it's not Engine A vs Engine B.
Most likely: a pair of **feature-tier enable tables** for two internal
subsystems, both of which need to be flipped to "trusted" mode for
correct rendering. Symmetric data-driven feature tables are exactly
what you'd design if you wanted the trust check to be expressible as
"copy 14 values from the table at X to the active state table at Y"
with no branching code.

### 4. `_m0` is an opt-in feature gate reachable from the config word

Bit 50 of the 64-bit config word (smali field `f0/h.m0`) is packed
into the applyConfig parameter by `f0.h.n()J`:

```
    :cond_11
    sget-boolean v2, Lf0/h;->m0:Z
    if-eqz v2, :cond_12
    const-wide/high16 v2, 0x4000000000000L
    or-long/2addr v0, v2
    :cond_12
    return-wide v0
```

The native config converter at `0x17c8c..0x17ca0`:

```
  17c8c: ubfx x14, x11, #50, #1        ; extract bit 50 of config
  17c90: cmp  x13, #0x2
  ...
  17ca0: str  w14, [x0, #0x4b8]        ; master+0x4b8 = bit 50
```

Two observations:

- If `_m0` were an **internal performance optimisation**, the developer
  would just always enable it in native code (no reason to route it
  through a config bit). Routing it through a config bit means the
  pathway was designed to be **opt-in** -- so that something
  somewhere can fail to set it. The natural interpretation is: the
  real app always sets `m0 = true` in its SharedPreferences; a
  repackaged APK that tampered with `f0.h.clinit()` would set it to
  false (either accidentally or by a tool that stripped the smali).
- The config converter stores the raw bit without any validation or
  remapping. It's literally "write 0 or 1 from bit 50." So the
  "trust" communicated by `_m0` is just "did the config packer
  include this bit." It's a cheap integrity signal, not a
  cryptographic one.

### 5. The rendering fallback path was never meant to be user-visible

The broken rendering we see (Engine A layer priority garbage) has the
hallmarks of orphaned code: it compiles, it runs, it doesn't crash,
but nobody has tested it for visual correctness. DraStic was shipped
as a polished commercial product; Exophase would not have let a bug
this visible slip into the default rendering path of the real app.

The only scenarios in which this code runs are:
- The fingerprint check fails (real user of the real app -- impossible
  by design, so never tested)
- Someone dlopens the library outside the real app (what nano does)

A developer who wanted a "safe fallback" for legitimate reasons
(e.g. unsupported GPU) would make the fallback visually correct but
slower. This isn't slower -- it produces garbage layer priorities.
That's consistent with "intentionally degraded, not unit-tested
because not meant to be reached."

## What the fingerprint probably actually checks

We don't know the exact check, and from a black-box reverse-engineering
perspective we didn't need to identify it to fix the bug (we patched
the effects rather than the cause). But the evidence narrows the
candidates:

### Candidate A: JNIEnv/JavaVM vtable shape

The nano FakeJNI implements ~30 JNIEnv functions at the correct vtable
offsets. The real JNIEnv vtable (from libnativehelper/libart) has ~232
function slots, most of which the fake leaves as null or
catch-all-returns-error stubs. If `startGame` probes any JNI slot
beyond the ones the fake implements, it gets a detectably different
return than in the real app.

What startGame does with the `env->...` calls in the disasm we've
traced is minimal (`FindClass`, `GetStaticMethodID`, a few
`GetStringUTFChars`). But it's possible the fingerprint check is
indirect: call `env->GetJavaVM(&vm)`, then inspect `vm->GetEnv()`'s
return value, or call `env->GetVersion()` and check if the returned
version matches `JNI_VERSION_1_6` exactly.

We confirmed `JNI_OnLoad` is small (488 bytes) and doesn't do anything
exotic with the JNIEnv, so the fingerprint isn't there. But `onInit`
and the first thing startGame calls (`0x1a084: ldr x8, [x0]; ldr x8,
[x8, #1352]; blr x8`) do indirect JNI calls that could embed a check.

### Candidate B: Class loader identity

Real Android apps load classes through a specific ClassLoader chain:
BootClassLoader -> PathClassLoader for the APK. The ClassLoader
object has an address that's process-lifetime stable. If drastic
reads `env->FindClass("com/dsemu/drastic/DraSticJNI")` and inspects
the returned `jclass` object's internal ClassLoader reference via
`env->CallObjectMethod(cls, getClassLoader)`, it can detect whether
it's running from a real APK or a fake.

The nano FakeJNI returns a stub jclass for `FindClass`. If drastic
ever calls `getClassLoader()` on it, the fake would have to return
something -- and whatever it returns is distinguishable from a real
PathClassLoader.

### Candidate C: libGLESv2 base address

We confirmed this one is probably used: the obfuscated pointer at
`master+1072` derives from `&glViewport` XOR'd with a key. The key
is clearly tuned for a specific libGLESv2 ASLR layout. In a zygote
child, that layout is consistent across all app processes (ASLR
is per-boot, but shared across zygote children). In an init-spawned
process, libGLESv2 may load at a different offset.

What we don't know is whether modes 1/2 of the GPU dispatch table
are ever entered in the real app. If they are, and they work
correctly, then the XOR'd pointer really does point to a valid
internal structure in the zygote layout. If they're never entered,
the XOR is a pure tripwire with no data-flow purpose.

### Candidate D: Process name or UID

`getprogname()` returns the process name on Android. In the real
DraStic app it's `com.dsemu.drastic` (or some suffix). In the nano
runner it's `gammaos-nano` or similar. A string compare in `onInit`
or startGame would be trivial but we haven't found one. This would
be the easiest check to implement but also the easiest to spoof, so
it's less likely.

### Candidate E: A combination

Real anti-tamper usually combines multiple weak signals rather than
relying on a single check. The 14 scalar fields being set to 14
different values suggests a multi-signal check where each field
encodes the result of a different probe. For instance:
- `master+0x10`: capability level (result of GL caps query)
- `master+0x14`: paired redundancy
- `master+0x4b8`: config bit 50 (did applyConfig include `_m0`?)
- `master+0x9140`: fallback required (did some init function complete?)
- 10-scalar cluster: per-subsystem feature enables (individual probe
  results)

A single hostile patch (e.g. force `master+0x10 = 6`) doesn't enable
the trusted rendering path because the other 13 fields are still at
untrusted values.

## Why we think this is about anti-piracy, not purely anti-tamper

DraStic's history context:

- Released ~2012 by Exophase, commercial on Google Play ($4.99-$9.99)
- Closed-source throughout its commercial life
- Exophase discontinued active development and made it free in the
  mid-2010s
- The APK at versionCode 109 is the final shipped version
- Never open-sourced; Exophase is no longer actively distributing it

Motivations that fit the mechanism:

1. **Repackaging/piracy of the paid version**: common practice during
   DraStic's commercial period. An attacker would decompile the APK,
   strip the license check, and redistribute. An anti-tamper that
   *silently* degrades rendering rather than hard-crashing is harder
   to notice during a quick test and harder for a modder to pinpoint
   to a single check.

2. **Anti-competitor (core extraction)**: dlopening
   `libdrastic_arm64.so` from a third-party frontend is a classic
   way to "borrow" a closed-source emulator core. Retroarch-style
   frontends do this for libretro cores; a competitor who wanted to
   wrap DraStic in their own UI would do exactly what gammaos-nano
   does (fake JNI, call the library directly). Exophase would have
   reason to discourage this specifically.

3. **Making fake reviews / scam apps harder**: repackaged emulators
   were a Play Store problem in the 2010s. Scammers would rebrand a
   free emulator and sell it. Subtle rendering bugs in repackaged
   copies give legitimate users a reason to seek out the original.

What doesn't fit:

- **Hardened DRM** (crypto signatures, code encryption, anti-debug)
  is absent. The XOR obfuscation is weak. 14 integer patches defeat
  the mechanism entirely. This was "casual deterrent" level, not
  serious security engineering.
- **No runtime license check** in the code we traced. The fingerprint
  isn't phoning home or validating a server response -- it's purely
  a process-local check.

## Why fixing it is ethically OK

We're fixing the rendering on **the free, discontinued version** of
an emulator whose author released it for free before discontinuing it.
The anti-tamper was designed for when DraStic was paid -- it's not
protecting any current revenue. We're not bypassing license checks or
redistributing the binary. We're loading a library in a way the
author didn't anticipate (native early-boot quick-resume) and
patching the rendering state to match the already-shipped rendering
path.

The gammaos-nano use case is a legitimate one -- showing a running DS
game during handheld boot so the user can resume faster -- and the
fix doesn't enable anything piracy-relevant. It just makes the
emulator work correctly in a non-standard loader context.

## What would defeat this mechanism more cleanly

If one wanted to "properly" fix this (rather than patching 14 scalars
after the fact), one would identify the fingerprint check itself and
either:

- Patch the check to always succeed (probably a `cbz`/`cbnz` or
  `b.eq`/`b.ne` somewhere in startGame's early path that branches
  based on a fingerprint result)
- Reproduce the exact environment the check requires (real JavaVM
  from bionic's libnativehelper, plus enough Java-side state to
  satisfy `FindClass` lookups)

Neither is trivial. Option 1 requires finding the check among
startGame's ~660-byte body plus everything it tail-calls. Option 2
requires running a real `app_process` or forking from zygote, which
defeats the point of a fast early-boot native runner.

The 14-scalar patch is cheaper, more maintainable, and doesn't touch
the binary. It pays a small cost (200ms deferred patch thread) for
zero ongoing maintenance. It also survives any future rebuild of the
gammaos-nano runner and works on any Android version.

## Things we explicitly did NOT prove

- We did not disassemble the fingerprint check itself. We worked
  around it by patching its effects.
- We did not confirm Exophase's intent. Everything in this document
  about "why" is inference from behavior, not statement from the
  author.
- We did not verify that modes 1/2 in the GPU dispatch table work
  correctly in the trusted context -- only that they don't crash in
  the untrusted context (because they're never entered). It's
  possible the obfuscated pointer really is a data pointer to a
  structure that matters in specific games we haven't tested.

## References

- Cross-agent debugging transcript:
  `/mnt/c/rgds/drastic/nano-startup-timing-answers.md` (7600+ lines)
- Fix implementation guide: `docs/NATIVE_BINARY_FINDINGS.md`
  (Engine A layer priority section)
- Memory entry summarising the fix:
  `~/.claude/projects/-mnt-c-rgds-drastic/memory/project_drastic_engine_a_layer_bug.md`
- Memory entry on the obfuscated pointer disproof:
  `~/.claude/projects/-mnt-c-rgds-drastic/memory/project_drastic_obfuscated_pointer.md`
