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
