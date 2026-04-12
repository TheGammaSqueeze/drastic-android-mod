# libdrastic_arm64.so - Native Binary Investigation

Reverse-engineering notes for the native emulator core that ships inside
DraStic r2.6.0.4a (versionCode 109). All addresses, offsets, and
instruction details refer to the specific binary extracted from that
release and committed to `native/libdrastic_arm64.so` in this repo.

## Library facts

| Item | Value |
|---|---|
| Path inside the APK | `lib/arm64-v8a/libdrastic_arm64.so` |
| Size | 1,356,096 bytes (1.3 MB) |
| Build ID | `2318f180e6c9aca21dd6dc52fb2706ea3855b34a` |
| Architecture | aarch64 (ELF64 LE), stripped, BIND_NOW |
| NEEDED libs | liblog, libz, libGLESv2, libOpenSLES, libc, libm, libstdc++, libdl |
| .text | 0x16b40 - 0x106cc0 (~960 KB code) |
| .rodata | 0x106cc0 - 0x1169d8 (~64 KB) |
| .data.rel.ro | 0x133830 - 0x138220 (~18 KB, vtables/relocs) |
| .data | 0x139000 - 0x14c000 (~76 KB initialised state) |
| .bss | 0x14c000 + ~64 MB virtual (DS RAM, framebuffers, JIT cache) |
| Functions identified | 755 |
| Strings extracted | 1,251 |
| PLT externs | 259 |

The `0x14c000` address is the master DraStic state struct base. Most
loads and stores in the emulator core live relative to this.

## JNI surface map

`extfx*` is the high-resolution renderer variant; `fx*` is the regular
renderer. Each JNI export is a 12-60 byte arg shuffler that tail-calls
into the real implementation:

| JNI export | Tail target | Size | Role |
|---|---|---|---|
| `extfxRender` | `0x1d3f8` | 308 | hi-res render path: lock mutex -> glActiveTexture -> glBindTexture -> glTexSubImage2D -> tail to `0x20690` (post-process) |
| `extfxSetup` | `0x1d3d0` (`0x20418`) | small | hi-res setup |
| `extfxLoad` | direct, 124 | 124 | shader/init load |
| `fxRender` | `0x1d1d8` | 480 | regular renderer |
| `fxSetup` | `0x1d1b0` (`0x1ff78`) | small | regular setup |
| `renderFrame` | `0x1ceac` | 348 | per-frame render entry |
| `renderFrameTex` | `0x1d008` | 200 | regular tex render |
| `renderFrameTexExt` | `0x1d0d0` | 200 | hi-res tex render |
| `applyConfig` | direct | 128 | stores config word at `master+0x468`, calls `0x17c58` (config converter), `0x1cbc0`, `0x1d728` |
| `updateFrame` | direct | 156 | does NOT render: only writes input/touch state at `master+0x14c48c` and reads FPS/perf counter |
| `saveState` | `0x17f84` | 124 | see "saveState internals" below |
| `startGame` | `0x1a04c` | 660 | see "startGame internals" below |
| `signalScreen`/`waitScreen` | trivial | 4 | producer/consumer flags |

## How the rendering pipeline actually works

1. The Java `GLThread` calls `updateFrame(IIII)` to push input and
   touch state, then calls `renderFrame*`/`extfxRender` to upload the
   previously rendered framebuffer to GL via `glTexSubImage2D` and
   draw a quad.
2. The native main DS CPU thread (spawned by `0x3d3c4` system init)
   runs the ARM7+ARM9 emulation continuously, producing frames into
   the master state's framebuffer pool.
3. The native rasterizer worker pool (spawned by `0x5f614` 3D init)
   runs the software 3D rasterizer in parallel.
4. The Java side and native side rendezvous via the mutex at
   `0x3f2db84` and the buffer-index toggle at `0x3f2db50`.

Framebuffer pool layout (visible in both `extfxRender` and `renderFrame`):

- Per-screen stride is hardcoded as `0xc0000` = 786,432 bytes = exactly
  512x384x4 RGBA. The framebuffer slot is always sized for hi-res mode
  even when hi-res is OFF.
- In native (256x192) mode only the upper-left 192 KB of each 768 KB
  slot is used.

## Rasterizer worker count is hardcoded, `q` is dead

### `0x5f614` - 3D rasterizer pool init (992 bytes)

This function:

1. Builds a 256-entry reciprocal table at `0x3f2e128`/`0x3f2f128`
   (precomputed `0x40000000 / N` for span interpolation).
2. Spawns exactly 4 threads with NO loop (fully unrolled):
   - `pthread_create` @ `0x5f718` -> fn `0x5f53c` (worker 0, ctx slot 1)
   - `pthread_create` @ `0x5f79c` -> fn `0x5f53c` (worker 1, ctx slot 2)
   - `pthread_create` @ `0x5f820` -> fn `0x5f53c` (worker 2, ctx slot 3)
   - `pthread_create` @ `0x5f924` -> fn `0x5f2c8` (dispatcher, tail call)
3. Initialises 8 mutexes and 8 condvars across the 4 threads.

Per-thread context layout (offsets from each worker's base):

- `[+0x00]` = master state pointer
- `[+0x08]` = sub-pointer
- `[+0xd0..0xd1]` = state flags
- `[+0xd2]` = thread index (literal `strb wzr/w10/w8/w8` writes)

### `q` (config bits 16-19, set by smali `f0/h.q` and `threads.cfg`)

| Step | Where | Operation |
|---|---|---|
| Set | smali `f0/h.smali:5527` | `q = (CPU>=4 ? 3 : CPU>=2 ? 2 : 1)`, overridable by `threads.cfg` |
| Pack | smali `f0/h.smali:3587-3595` | `cfg \|= (q & 0xf) << 16` |
| Pass | `applyConfig` JNI `0x1a4a0` | `str x2, [master, #0x468]` |
| Extract | `0x17c58` config converter at `0x17d24` | `ubfx w14, cfg, #16, #4`, `str w14, [master+0x8a658+0x490]` |
| Reads | NONE in the rasterizer/renderer | - |

The only places reading bits 16-19 of the config word
(`ubfx ..., #16, #4`) are inside `0x7e270` (a 3,256-byte function in
the 0x7e000 region) and a few similar call sites. Those uses index
into a string table at `0x144290`/`0x1442e0` whose entries are 3-byte
snippets like `"x86"`, `"vfp"`, etc. They are debug/info channel
labels.

The native code never uses `q` to control thread count, work
distribution, or anything performance-relevant. Setting `threads.cfg`
to any value from 1 to 8 has no measurable effect because the value
is ignored by the rasterizer.

### What actually controls the rasterizer threading

The relevant switch is `_Threaded3D` (smali field `f0/h.g0`, copied to
`f0/h.Q0`, bit 28 of the config word, mask `0x10000000`).

- Extracted in `0x17c58` at offset `0x17d18`:
  `ubfx w14, cfg, #28, #1`
- Stored at `[(master+0x8a658)+0x468] = [master+0x8aac0]`
- From the DraStic README: "Multi-threaded 3D rendering - If you have
  multiple cores DraStic is sped up by being able to offload screen
  update tasks to another thread... can cause graphical glitches and
  instability."

`_Threaded3D = true` is the condition that spawns the 3 worker
threads visible in `top`.

### The slice-count field at `[ctx+0xd3]`

The worker function `0x596a4` opens with:

```
ldrb  w8, [x9, #211]    ; load slice count
cmp   w8, #0xc          ; cap at 12
b.hi  skip
udiv  w11, #0xc, w8     ; w11 = 12 / w8
```

Work is divided into `12 / N` units. 12 matches
`192 lines / 16 lines per tile`. This field defaults to 0 (zeroed
`.bss`) and is set dynamically per frame from a write location that
is not a literal `#211` offset. The most likely path is via a wider
`str` covering offsets 208-211. Changing it is risky because the
matching dispatch loops in `0x59bb4` would also need to know about
the change.

## saveState internals (JNI `0x17f84`)

`Java_com_dsemu_drastic_DraSticJNI_saveState` has this prologue:

```
0x17f84  stp   x20, x19, [sp, #-0x20]!
0x17f88  stp   x29, x30, [sp, #0x10]
0x17f8c  add   x29, sp, #0x10
0x17f90  adrp  x8, #0x14c000
0x17f94  tst   w3, #0xff               ; test the Z (boolean) arg
0x17f98  add   x8, x8, #0x4b4
0x17f9c  mov   w9, #1
0x17fa0  strb  wzr, [x8, #0xc]
0x17fa4  strb  w2, [x8]                ; writes slot number
0x17fa8  strb  w9, [x8, #1]
0x17fac  b.eq  #0x17ff0                ; if sync==false: JUMP STRAIGHT TO RETURN
0x17fb0  ...                           ; (poll loop for sync==true case)
0x17ff0  ldp   x29, x30, [sp, #0x10]
0x17ff4  mov   w0, #1
0x17ff8  ldp   x20, x19, [sp], #0x20
0x17ffc  ret
```

When called with `sync = false`, saveState sets the slot-save request
fields at `[master+0x4b4]` and returns immediately. The actual
serialisation runs on the emulator worker thread. Any subsequent
`finishAffinity()` or `System.exit()` that tears the worker thread
down before it flushes the temp file produces a 0-byte
`_savestate_temp.dss` and no `<rom>_9.dss`.

With `sync = true`, the function polls `[master+0x4b5]` in a 10 ms
loop until the worker signals completion, then returns. Only after
that does the Java caller continue. Synchronous calls are safe to
follow with `finishAffinity()`.

This distinction is what broke the first version of the ESC patch.

## startGame internals (JNI `0x1a04c`)

`Java_com_dsemu_drastic_DraSticJNI_startGame(String, int, long, int, boolean, long)`
receives, in native ABI order:

- x2 = jstring filename
- w3 = jint slot  (this is the key field for auto-load)
- x4 = jlong
- w5 = jint
- w6 = jboolean
- x7 = jlong

After getting the UTF-8 chars and storing a handful of config-derived
bytes into the state struct, the function reaches this block at
`0x1a17c`:

```
0x1a17c  tbnz  w22, #0x1f, #0x1a1a4   ; if w22 < 0 (slot sign bit set) skip load
0x1a180  cmp   w23, #0                ; w23 = arg 4 (int), zero in both smali call sites
0x1a184  b.gt  #0x1a1a4
0x1a188  adrp  x8, #0x14c000
0x1a18c  add   x8, x8, #0x488
0x1a190  mov   w9, #0x1e              ; load-request opcode (30)
0x1a194  mov   w10, #1
0x1a198  str   w9, [x8]               ; [master+0x488] = 30
0x1a19c  strb  w22, [x8, #0x2c]       ; [master+0x4b4] = slot byte
0x1a1a0  strb  w10, [x8, #0x2e]       ; [master+0x4b6] = 1
0x1a1a4  ...                          ; continues with other start-time setup
```

In other words: if the Java-side passes a non-negative slot integer
in arg 2, the native side writes a load request that the main
emulator thread picks up at next tick. Setting field `o = 9` in
`DraSticEmuActivity.onCreate` is sufficient to trigger an auto-load
from slot 9 every time the activity starts with a ROM.

## Profile evidence

Threads observed in `top` during normal Sonic Rush play on a 4-core
RK3568 with `_Hires3D=true` and `_Threaded3D=true`:

| TID | %CPU | Identity |
|---|---|---|
| 3128 | 74.3% | Main DS CPU thread (spawned by `0x3d3c4`) |
| 3140 | 30.7% | Rasterizer worker (`0x5f53c` instance) |
| 3139 | 25.6% | Rasterizer worker |
| 3142 | 23.0% | Rasterizer worker |
| 3125 | 20.5% | Java GLThread (texture upload) |
| (idle) | 0.0% | Dispatcher (`0x5f2c8`) - sleeps when no work |

Total user CPU was ~226% / 400% available. System idle was 92%. The
workers were waiting on the main thread, not the other way around,
which means adding more workers cannot help: the bottleneck is the
producer (main DS CPU emulation + scheduler), not the consumers.

## Observations on performance knobs

The following user-side settings change what the rasterizer has to do
per frame and are substantially higher-impact than any thread-count
tweak:

1. `_GlUse16Bit = true`. Switches the framebuffer and texture upload
   from RGBA8 to RGB565, halving texture upload bandwidth and
   reducing per-pixel write cost. Cost: mild colour banding in
   gradients.
2. `_DisableEdgeMarking = true`. Skips DraStic's software
   post-process pass that draws the DS hardware's "edge mark" effect
   over the framebuffer. That pass is a full sweep of the 768 KB
   framebuffer per frame. Most games are visually fine without it.
3. `_FrameskipType = 1` (manual), `_FrameskipValue = 1` or `2`, or
   `_FrameskipSafe = true`. Auto frameskip with max 4 lets frames
   stack up before reacting; a constant manual skip stabilises pacing
   on a CPU-limited device.
4. A/B testing `_Threaded3D = false`. When the workers spend most of
   their wall time waiting on a slower producer, the lock and
   condvar overhead can exceed the parallelism gain.

These are all reversible from the DraStic settings UI and do not
require any binary patching.

## Binary patches considered and rejected

| # | What | Where | Risk | Expected gain |
|---|---|---|---|---|
| 1 | Force `_DisableEdgeMarking` semantics: NOP the call to the edge-mark post-process pass | Exact callsite not yet identified. Would require tracing `0x59bb4` (2,880b) -> `0x545cc` (2,464b) -> other pipeline stages. | Medium: risk of black screens for affected games | Moderate. Similar to the setting toggle but unconditional. |
| 2 | Spawn a 4th rasterizer worker | Patch `0x5f614` to insert a 4th `pthread_create`; provide context slot; teach `0x59bb4` dispatcher to use 4 threads. | Very high. The dispatcher's work-split is hardcoded for 3 workers via the table at `0x3f2e128`. Adding a 4th thread without rewriting the splitter will deadlock or produce garbage. | Probably zero. Workers are already starved by the producer. |
| 3 | Reduce to 2 workers | NOP one `pthread_create` and one `pthread_join` in `0x5f614`. Patch the dispatcher to skip waiting on the missing worker. | Very high. Same dispatcher issue as #2. | Marginal. |
| 4 | Patch the framebuffer stride from `0xc0000` to `0x30000` (192 KB native size) | Hardcoded in many places (`mov w?, #0xc0000`). Would have to find and patch every site. | Very high. Missing a single site = memory corruption. | Possibly significant if memory bandwidth is the bottleneck, but does not help hi-res mode (which legitimately needs 768 KB). |

None of these are safe to ship without substantially more disassembly
work, and the most promising one (#1) has a free, reversible
equivalent in the user settings.

## Artifacts in this repo

All outputs of the analysis that produced this document are committed
under `native/`:

| File | Contents |
|---|---|
| `libdrastic_arm64.so` | Original binary (read-only copy from the APK) |
| `libdrastic_cpu.so` | CPU detection helper (only exports `getCpuType`, returns 3 on arm64) |
| `libdrastic_arm64.dis` | Full `.text` disassembly via `aarch64-linux-gnu-objdump -d`, 245k lines |
| `libdrastic_arm64.elf.txt` | ELF headers, sections, dynsym, relocations from `readelf -a` |
| `funcs.json` | 755 function ranges with sizes and (where known) JNI names |
| `plt.json` | PLT entry -> external symbol map |
| `calls.json` | per-function call list (BL/BLR targets) |
| `strings.json` | rodata strings keyed by address |
| `strxref.json` | per-function string xrefs |
| `summary.txt` | top-50 PLT call frequencies, largest functions, mutex/cond callers |
| `data.dump` | hex dump of `.data` |
| `rodata_relro.dump` | hex dump of `.data.rel.ro` |

The scripts that produced the JSON files and drive the per-function
disassembly live under `tools/`:

- `tools/analyze.py` - capstone + pyelftools analyser (callgraph,
  PLT-call frequency, string xrefs)
- `tools/disasm.py` - per-function disassembly with PLT and string
  annotation. Usage: `python3 tools/disasm.py 0xADDR`.

## Anti-tamper obfuscated pointer in onInit (`master+1072`)

`onInit` at `0x17df4` computes an obfuscated pointer:

```
GOT[0x138fd0] -> R_AARCH64_GLOB_DAT -> symbol 253 = glViewport

17e08: ldr x9, [GOT+0x138fd0]         ; x9 = &glViewport (runtime)
17e0c: mov x10, #0x1ab10bf4dbbe1f0f   ; XOR key
17e28: eor x8, x9, x10                ; obfuscated pointer
17e34: str x8, [master+1072]          ; stored for renderer use
```

The renderer at `0x64544` and 14+ other call sites loads `master+1072`
and dereferences it as a data pointer to write DS Engine A GPU state
(BG layer configs, OBJ attributes, 3D clear state, layer priorities).

This is an anti-tamper technique: drastic uses `glViewport`'s runtime
address as entropy. The XOR key is tuned so that in a zygote-forked
app process (where libGLESv2 loads at a predictable ASLR address),
the result points to a valid internal structure.

**Critical for non-zygote contexts (e.g. gammaos-nano):** if
libGLESv2.so loads at a different base address, the XOR produces a
pointer to the WRONG memory location. Engine A's GPU state writes
land at a garbage address while the actual state keeps defaults. This
causes:
- White rectangles over 3D content (BG layers with default VRAM)
- Missing OBJ sprites (OAM tile data never populated)
- Wrong layer priorities (compositor reads default state)
- Engine B unaffected (uses a different pointer path)

**Fix for non-zygote processes:** after `onInit`, overwrite
`master+1072` with the correct internal pointer computed from the
master base + the right offset. The correct offset can be determined
by reading `master+1072` in the real drastic app and computing the
delta from the master base.

## Audio init binder stall and 4-byte patch

`initialize_audio` at `0x1d760` (824 bytes) calls `slCreateEngine`
which blocks ~15s at cold boot waiting for audioserver via libbinder
`waitForService`. Single caller at `0x7304c` (inside `initialize_spu`
at `0x72e34`). Caller does NOT check return value.

**Patch:** replace first instruction at `0x1d760` with `ret`
(`c0 03 5f d6`). Audio init skipped entirely. The caller pre-writes
the sample rate (44100) at `[x19]` before the call so the subsequent
division is safe.

For gameplay beyond title screens, a second patch at `0x1dd6c` (also
`ret`) is needed to prevent the per-frame audio tick from dereferencing
NULL audio object pointers. Both patches are in the same 4K page.

See `memory/project_drastic_audio_init_patch.md` for the full
investigation details.

## renderFrame GL state contract

`renderFrame` at `0x1ceac` (344 bytes) makes exactly 6 GL calls:
`glBindTexture` x2, `glTexSubImage2D` x2, `glDrawArrays` x2.
It does NOT call `glUseProgram`, `glVertexAttribPointer`, or any
other state-management function. It relies entirely on the GL state
that `fxSetup` left behind.

`fxLoad` (via `0x1ff78`) parses `.dfx` shader files with XML-like
tags (`<header>`, `<vheader>`, `<fheader>`, `<pass>`, etc) and
compiles the GL program. `fxSetup` (via `0x20418`) configures the
viewport, vertex attribs, and textures using the compiled program.

The per-frame render loop requires `waitScreen()` before each
`renderFrame()` call. Without it, renderFrame reads mid-composition
frames. `signalScreen` is NOT per-frame -- only for lifecycle events.

## getScreenBuffers format conversion

`getScreenBuffers` at `0x19068` is hardcoded to 256x192 output even
with `_Hires3D`. It applies R/B channel swap (ABGR to ARGB) and
forces alpha to 0xFF via `orr v2.4s, #0xff, lsl #24`. Every output
pixel is fully opaque.

## Engine A layer priority bug in non-zygote processes (fix found 2026-04-12)

When `libdrastic_arm64.so` is loaded via `dlopen` + a fake JNI shim from a
non-zygote Android process (e.g. an `init`-spawned early-boot runner like
gammaos-nano's QR preview), DraStic's DS 2D compositor renders the wrong
BG/OBJ layer priority on **Engine A** (the primary 2D engine; bottom screen
in most games). Engine B is unaffected. Symptoms observed across Pokemon
Black 2 and Sonic Rush:

- Solid or near-solid rectangles of a real DS BG layer colour
  (steel blue for sky/ceiling, white for cleared backdrop) rendering
  **in front** of the 3D or sprite layers that should sit on top.
- OBJ-layer character sprites completely invisible while the dialog
  box, background gradient, and text of the same scene render correctly.
- All games affected, including 2D-only titles. Zygote-child drastic on
  the same device with the same ROMs renders correctly.

### Root cause

DraStic's `startGame` runs a context fingerprint during init. When the
fingerprint matches "I am executing inside the real `com.dsemu.drastic`
APK under zygote", it initialises a set of master-state scalars to
values that select the **optimised** rendering path. When the fingerprint
fails (which it does for any non-zygote process using a fake JNI layer),
it leaves those scalars at defaults that select a fallback rendering
path whose layer-priority compositing is wrong for Engine A.

We were NOT able to identify the exact byte-pattern the fingerprint
check reads. Candidates include the JNIEnv/JavaVM vtable layout, the
class loader identity for `com.dsemu.drastic.DraSticJNI`, or some zygote-
specific global. The obfuscated pointer at `master+1072`
(= `&glViewport ^ 0x1AB10BF4DBBE1F0F`) is almost certainly one output of
this check; in non-zygote processes the XOR produces a value outside the
48-bit userspace virtual address range, and the code paths that would
dereference it are never entered.

### The fix

Two-step. Applied in nano's `DrasticRunner::init()`:

**1. `applyConfig` with bit 50 (`_m0`) set.** This is the canonical
in-binary channel that the real app uses to drive `master+0x4b8`.
The config converter at `0x17c8c..0x17ca0`:

```
17c8c: ubfx x14, x11, #50, #1    ; extract bit 50 of the config word
17ca0: str  w14, [x0, #0x4b8]    ; store into master+0x4b8
```

Call signature from the non-zygote runner:

```cpp
static constexpr long kDefaultConfigBits =
    0x10000000L            // _Threaded3D (bit 28)
  | 0x10000000000L         // _DisableEdgeMarking (bit 40, optional)
  | 0x20000000000L         // _Hires3D (bit 41, optional)
  | 0x4000000000000L;      // _m0 (bit 50) -- REQUIRED for Engine A rendering
applyConfig(kDefaultConfigBits);
```

**2. Post-startGame one-shot raw patch of 13 more scalars.** DraStic's
internal reset inside `startGame` writes these to fallback values within
the first ~100ms of the startGame thread. A 200ms deferred patch after
`mStartGameThread.detach()` writes them to the values the real app uses.

| Offset | Size | Nano default | Real-app value | Notes |
|--------|------|--------------|----------------|-------|
| `master+0x00010` | 4 | 1 | 6 | paired capability value |
| `master+0x00014` | 4 | 1 | 6 | paired capability value |
| `master+0x09140` | 4 | 1 | 0 | **inverted direction** (real app clears, nano keeps set) |
| `master+0x8b68c` | 4 | 1 | 6 | second cluster, mirrors `+0x10` pattern |
| `master+0x8b690` | 4 | 1 | 6 | second cluster, mirrors `+0x14` pattern |
| `master+0x8ba98` | 4 | 2 | 0 | **inverted direction** |
| `master+0x8bab8` | 4 | 0 | 1 | flag |
| `master+0x8bad0` | 4 | 0 | 1 | flag |
| `master+0x8badc` | 4 | 0 | 1 | flag |
| `master+0x8bae8` | 4 | 0 | 3 | |
| `master+0x8bb00` | 4 | 0 | 1 | flag |
| `master+0x8bb10` | 4 | 0 | 1 | flag |
| `master+0x8bb28` | 4 | 0 | 1 | flag |

All 13 scalars are `uint32_t`. The patch is **one-shot** -- a 30-pass
100ms monitor confirmed that drastic rewrites these fields once during
startGame init and never touches them again. Pass 1 (t=100ms) saw 11
drifts; passes 2-30 saw zero drifts.

A reference implementation fragment:

```cpp
std::thread([this] {
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    apply_master_patches();
}).detach();

void apply_master_patches() {
    uint8_t* m = (uint8_t*)master_base;
    *(uint32_t*)(m + 0x00010) = 6;
    *(uint32_t*)(m + 0x00014) = 6;
    *(uint32_t*)(m + 0x09140) = 0;
    *(uint32_t*)(m + 0x8b68c) = 6;
    *(uint32_t*)(m + 0x8b690) = 6;
    *(uint32_t*)(m + 0x8ba98) = 0;
    *(uint32_t*)(m + 0x8bab8) = 1;
    *(uint32_t*)(m + 0x8bad0) = 1;
    *(uint32_t*)(m + 0x8badc) = 1;
    *(uint32_t*)(m + 0x8bae8) = 3;
    *(uint32_t*)(m + 0x8bb00) = 1;
    *(uint32_t*)(m + 0x8bb10) = 1;
    *(uint32_t*)(m + 0x8bb28) = 1;
}
```

### Diagnostic methodology (reusable)

The fix was found by a "5-dump invariant intersection" technique:

1. Capture 5 master-state dumps from each process (real app + nano),
   ~1 second apart, both on the same static scene (e.g. a title screen).
2. For each byte offset, classify it as "invariant across the 5 dumps"
   on each side.
3. Keep only offsets where the byte is invariant on BOTH sides but
   the two sides disagree. These are structural process-context
   differences, not runtime-dependent state.
4. Exclude JIT code cache arenas (recognisable as ARM64-instruction-
   shaped content): typically `master+0x117000..0x135000`,
   `master+0xa1000..0xa7000`, `master+0xd8000..0xda000`.
5. Keep only `u32` scalars where both values are small integers
   (0..15). Anything larger is almost always a pointer or runtime
   state.

This narrows 2 MB of master state to ~16 scalar diffs. Cross-game
validation (same 5-dump methodology on a second game) eliminates
game-state bleedthrough, leaving exactly the process-context fields.

### Things eliminated during the investigation

The fix took 6+ hours of cross-session debugging. Each of these was
tested and did NOT fix the rendering:

- `applyConfig(0)` (no config bits at all)
- `_Threaded3D` on/off, `_Hires3D` on/off, `_DisableEdgeMarking` on/off
- `SCHED_RR` priority (with and without)
- `renderFrame` vs `getScreenBuffers` (both show the same artifact)
- `glDisable(GL_BLEND)`, saturation=1.0 forced in the consumer shader
- Audio init patch at `0x1d760` (reverted -- no change)
- `game_database.xml` (deleted from cache -- no change)
- BIOS files (confirmed loading correctly)
- `SCUDO_OPTIONS=zero_contents=true` + direct `malloc` override
  (confirmed zeroing every allocation -- no change)
- TLS (`tpidr_el0`) -- only reads the stack canary, standard bionic
- SIGSEGV/SIGINT/SIGTERM handlers (drastic only installs SIGINT/SIGTERM)
- `master+1072` obfuscated pointer -- the XOR value is out of VA range
  so the dereference code path is never entered
- `versionCode`, `sdkInt` in `onInit` -- overwritten by config converter
  and only gates `ASharedMemory` path respectively
- `customClock` arg to `startGame`: `-1L` required (not `0L`) but
  not sufficient alone
- `master+0x10 = 6, master+0x14 = 6` alone -- drastic resets them;
  only sticks when combined with the full 13-scalar patch

### What the "second cluster" at `master+0x8b68c` represents

The 10 scalars at `master+0x8b68c..0x8bb28` mirror the `6/1`
capability pattern at `master+0x10/0x14`. Spacing is **not** the
engine stride (Engine A -> Engine B is `+0x60000` on both Linux and
Android). This suggests a pair of symmetric feature/capability
tracking structs -- possibly per-DS-engine fast-path enable tables.
Exact identification of the struct requires more disasm and is not
needed to ship the fix.

## Input bitmask layout (NOT DS KEYINPUT order)

The button bitmask for `updateInput`/`updateFrame` uses a
drastic-specific layout, NOT the standard DS KEYINPUT register order:

```
bit 0  Up      bit 4  A      bit  8  L      bit 11  Select
bit 1  Down    bit 5  B      bit  9  R      bit 31  Pointer-down
bit 2  Left    bit 6  X      bit 10  Start
bit 3  Right   bit 7  Y
```

Active-high (1=pressed). Bits 12-30 are reserved/trap-doors.
Source: `decoded/smali/n0/i.smali` array_0 at lines 498-516.

## Summary

- The rendering pipeline, rasterizer worker pool init, worker
  functions, dispatcher, and config converter have all been mapped.
- The smali `q` field (`threads.cfg`, config bits 16-19) is dead
  code for rasterization. Bits 16-19 are only indexed into a
  debug-string table at `0x144290`/`0x1442e0`. The meaningful
  threading flag is `_Threaded3D` at config bit 28.
- Worker count is hardcoded to 3 workers plus 1 dispatcher. Patching
  it is risky and unlikely to help because the workers are already
  starved by the producer thread.
- The biggest realistic wins are user settings, not binary patches:
  `_GlUse16Bit`, `_DisableEdgeMarking`, manual frameskip, and an A/B
  against `_Threaded3D=false`.
- A default bump of `q` from 3 to 4 (and raising the cap from 8 to
  15) was attempted as an earlier experiment and is a no-op for
  performance. That experiment is documented here but is not part
  of the shipped patch set in this repo.
