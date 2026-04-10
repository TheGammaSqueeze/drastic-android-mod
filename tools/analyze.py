#!/usr/bin/env python3
"""
libdrastic_arm64.so analyzer.

Outputs:
  funcs.json     - all function ranges + size + dynsym name (if any)
  plt.json       - PLT addr -> external symbol
  calls.json     - per-func: list of (callee_addr or PLT name, call_site)
  strings.json   - addr -> string content (rodata)
  strxref.json   - per-func: list of strings referenced
  summary.txt    - human-readable highlights
"""
import json, sys, os, struct, re
from collections import defaultdict
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from capstone import Cs, CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN, CS_OP_IMM, CS_OP_MEM
from capstone.arm64 import ARM64_OP_IMM, ARM64_OP_MEM, ARM64_OP_REG, ARM64_INS_BL, ARM64_INS_B, ARM64_INS_BR, ARM64_INS_BLR, ARM64_INS_RET, ARM64_INS_ADRP, ARM64_INS_ADD, ARM64_INS_LDR, ARM64_INS_LDRB, ARM64_INS_LDRH

LIB = sys.argv[1] if len(sys.argv) > 1 else "libdrastic_arm64.so"
OUT = sys.argv[2] if len(sys.argv) > 2 else "."
os.makedirs(OUT, exist_ok=True)

with open(LIB, "rb") as f:
    elf = ELFFile(f)
    sections = {}
    for s in elf.iter_sections():
        sections[s.name] = {
            "addr": s["sh_addr"],
            "size": s["sh_size"],
            "data": s.data() if s["sh_type"] != "SHT_NOBITS" else b"",
        }

    # Dynamic symbols
    dynsym = {}
    for s in elf.iter_sections():
        if isinstance(s, SymbolTableSection):
            for sym in s.iter_symbols():
                if sym["st_value"] and sym.name:
                    dynsym[sym["st_value"]] = (sym.name, sym["st_size"])

    # Relocations - PLT
    rela_plt = sections.get(".rela.plt")
    plt_sec = sections[".plt"]
    plt_entries = {}  # addr -> name

    # PLT layout: stub at .plt+0x20, then 16 bytes per entry
    # Each PLT entry corresponds to a relocation in .rela.plt
    rela_data = sections[".rela.plt"]["data"]
    relocs = []
    for i in range(0, len(rela_data), 24):
        r_offset, r_info, r_addend = struct.unpack("<QQq", rela_data[i:i+24])
        sym_idx = r_info >> 32
        relocs.append((r_offset, sym_idx))

    # Get dynsym name table
    dynsym_sec = elf.get_section_by_name(".dynsym")
    dynsym_names = []
    for sym in dynsym_sec.iter_symbols():
        dynsym_names.append(sym.name)

    plt_addr_base = plt_sec["addr"]
    # First PLT entry is at +0x20, then 16 bytes each
    for i, (r_off, sym_idx) in enumerate(relocs):
        plt_addr = plt_addr_base + 0x20 + i * 0x10
        plt_entries[plt_addr] = dynsym_names[sym_idx]

    # Disassemble .text
    text_sec = sections[".text"]
    text_addr = text_sec["addr"]
    text_data = text_sec["data"]

    md = Cs(CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN)
    md.detail = True

    # Pass 1: find function boundaries via:
    #  - addresses targeted by BL instructions
    #  - dynsym entries inside .text
    #  - addresses after a ret + nop padding
    # Then split text into functions.
    bl_targets = set()
    branch_targets = set()
    instructions = []
    for insn in md.disasm(text_data, text_addr):
        instructions.append((insn.address, insn.bytes, insn.mnemonic, insn.op_str, insn))
        if insn.id == ARM64_INS_BL:
            for op in insn.operands:
                if op.type == ARM64_OP_IMM:
                    bl_targets.add(op.imm)
        elif insn.id in (ARM64_INS_B,):
            for op in insn.operands:
                if op.type == ARM64_OP_IMM:
                    branch_targets.add(op.imm)

    # Function starts: dynsym entries within text + BL targets within text
    func_starts = set()
    for addr, (name, sz) in dynsym.items():
        if text_addr <= addr < text_addr + len(text_data):
            func_starts.add(addr)
    func_starts.update(t for t in bl_targets if text_addr <= t < text_addr + len(text_data))

    # Also: address right after ret/b (unconditional control flow), if next 4 bytes is a fn prologue (stp x29,x30 or stp ...)
    addr_to_idx = {a: i for i, (a, *_) in enumerate(instructions)}
    for i, (addr, b, mnem, op, insn) in enumerate(instructions):
        if mnem == "ret" or (mnem == "b" and not insn.cc):
            # check next instruction
            if i + 1 < len(instructions):
                next_addr, nb, nmnem, nop, ninsn = instructions[i+1]
                # function prologue patterns
                if nmnem == "stp" and ("x29" in nop or "x30" in nop or "x19" in nop):
                    func_starts.add(next_addr)
                elif nmnem == "sub" and "sp," in nop:
                    func_starts.add(next_addr)

    func_starts.add(text_addr)
    func_starts = sorted(func_starts)

    # Build function ranges
    funcs = []
    for i, start in enumerate(func_starts):
        end = func_starts[i+1] if i+1 < len(func_starts) else text_addr + len(text_data)
        funcs.append({"addr": start, "end": end, "size": end - start,
                      "name": dynsym.get(start, (None,))[0]})

    # Pass 2: per-function call analysis
    func_by_addr = {f["addr"]: f for f in funcs}
    calls_by_func = defaultdict(list)  # func_addr -> [(callee_addr, callsite, callee_kind)]

    # Map address -> function it belongs to
    starts_sorted = sorted(func_by_addr.keys())
    def find_func(addr):
        # binary search
        lo, hi = 0, len(starts_sorted) - 1
        while lo <= hi:
            mid = (lo+hi) // 2
            if starts_sorted[mid] <= addr:
                if mid == len(starts_sorted)-1 or starts_sorted[mid+1] > addr:
                    return starts_sorted[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return None

    for addr, b, mnem, op, insn in instructions:
        if insn.id in (ARM64_INS_BL, ARM64_INS_BLR):
            owner = find_func(addr)
            if owner is None:
                continue
            if insn.id == ARM64_INS_BL:
                target = insn.operands[0].imm
                if target in plt_entries:
                    calls_by_func[owner].append((target, addr, "plt:" + plt_entries[target]))
                elif text_addr <= target < text_addr + len(text_data):
                    calls_by_func[owner].append((target, addr, "func"))
                else:
                    calls_by_func[owner].append((target, addr, "extern"))
            else:  # BLR (indirect call)
                calls_by_func[owner].append((None, addr, "indirect"))

    # Pass 3: extract strings + xrefs to .rodata
    rodata = sections[".rodata"]
    rodata_addr = rodata["addr"]
    rodata_data = rodata["data"]
    strings = {}
    cur = b""
    cur_start = 0
    for i, byte in enumerate(rodata_data):
        if 32 <= byte < 127 or byte in (9, 10, 13):
            if not cur:
                cur_start = rodata_addr + i
            cur += bytes([byte])
        else:
            if len(cur) >= 4:
                strings[cur_start] = cur.decode("ascii", "replace")
            cur = b""
    if len(cur) >= 4:
        strings[cur_start] = cur.decode("ascii", "replace")

    # ADRP + ADD detection for string xrefs (and rodata access in general)
    # Track pairs (adrp, add) per register window.
    str_xref = defaultdict(list)  # str_addr -> [(func_addr, insn_addr)]
    func_strings = defaultdict(set)  # func_addr -> set(str_addr)

    # Per-register pending ADRP value
    reg_adrp = {}
    cur_func = None
    for addr, b, mnem, op, insn in instructions:
        # Reset on function entry
        f_owner = find_func(addr)
        if f_owner != cur_func:
            cur_func = f_owner
            reg_adrp = {}

        if insn.id == ARM64_INS_ADRP:
            ops = insn.operands
            if len(ops) >= 2 and ops[0].type == ARM64_OP_REG and ops[1].type == ARM64_OP_IMM:
                reg = insn.reg_name(ops[0].reg)
                reg_adrp[reg] = ops[1].imm
        elif insn.id == ARM64_INS_ADD:
            ops = insn.operands
            if len(ops) >= 3 and ops[0].type == ARM64_OP_REG and ops[1].type == ARM64_OP_REG and ops[2].type == ARM64_OP_IMM:
                src = insn.reg_name(ops[1].reg)
                dst = insn.reg_name(ops[0].reg)
                if src in reg_adrp:
                    target = reg_adrp[src] + ops[2].imm
                    if target in strings:
                        str_xref[target].append((cur_func, addr))
                        if cur_func is not None:
                            func_strings[cur_func].add(target)
                    # propagate to dst register if adrp+add pattern
                    reg_adrp[dst] = target
        elif insn.id in (ARM64_INS_LDR, ARM64_INS_LDRB, ARM64_INS_LDRH):
            # LDR Xd, [Xn, #imm] - if Xn was just adrp'd, we have a global access
            ops = insn.operands
            if len(ops) >= 2 and ops[1].type == ARM64_OP_MEM:
                base = insn.reg_name(ops[1].mem.base) if ops[1].mem.base else None
                if base in reg_adrp:
                    target = reg_adrp[base] + ops[1].mem.disp
                    if target in strings:
                        str_xref[target].append((cur_func, addr))
                        if cur_func is not None:
                            func_strings[cur_func].add(target)

    # Write outputs
    with open(os.path.join(OUT, "funcs.json"), "w") as f:
        json.dump([{"addr": fn["addr"], "size": fn["size"], "name": fn["name"]}
                   for fn in funcs], f, indent=1)

    with open(os.path.join(OUT, "plt.json"), "w") as f:
        json.dump({hex(k): v for k, v in plt_entries.items()}, f, indent=1)

    with open(os.path.join(OUT, "calls.json"), "w") as f:
        out = {}
        for fn_addr, lst in calls_by_func.items():
            out[hex(fn_addr)] = [{"callee": hex(c[0]) if c[0] else None,
                                  "site": hex(c[1]),
                                  "kind": c[2]} for c in lst]
        json.dump(out, f, indent=1)

    with open(os.path.join(OUT, "strings.json"), "w") as f:
        json.dump({hex(k): v for k, v in strings.items()}, f, indent=1)

    with open(os.path.join(OUT, "strxref.json"), "w") as f:
        out = {}
        for fn_addr, str_set in func_strings.items():
            out[hex(fn_addr)] = [hex(s) for s in sorted(str_set)]
        json.dump(out, f, indent=1)

    # Reverse callgraph: callee -> callers
    callers_of = defaultdict(set)
    for caller, calls in calls_by_func.items():
        for c in calls:
            if c[0] is not None:
                callers_of[c[0]].add(caller)

    # PLT-call frequency by extern symbol
    plt_call_count = defaultdict(int)
    plt_call_callers = defaultdict(set)
    for caller, calls in calls_by_func.items():
        for c in calls:
            if c[2].startswith("plt:"):
                plt_call_count[c[2][4:]] += 1
                plt_call_callers[c[2][4:]].add(caller)

    summary = []
    summary.append(f"Functions:    {len(funcs)}")
    summary.append(f"PLT entries:  {len(plt_entries)}")
    summary.append(f"Strings:      {len(strings)}")
    summary.append("")
    summary.append("=== JNI exported entries ===")
    for fn in funcs:
        if fn["name"] and fn["name"].startswith("Java_com_dsemu"):
            n_calls = len(calls_by_func.get(fn["addr"], []))
            summary.append(f"  {hex(fn['addr'])}  size={fn['size']:>5}  calls={n_calls:>3}  {fn['name']}")
    summary.append("")
    summary.append("=== PLT call frequency (top 50) ===")
    for sym, cnt in sorted(plt_call_count.items(), key=lambda x: -x[1])[:50]:
        n_callers = len(plt_call_callers[sym])
        summary.append(f"  {cnt:>5} calls from {n_callers:>4} fns: {sym}")
    summary.append("")
    summary.append("=== Largest functions (top 30) ===")
    for fn in sorted(funcs, key=lambda f: -f["size"])[:30]:
        n_calls = len(calls_by_func.get(fn["addr"], []))
        summary.append(f"  {hex(fn['addr'])}  size={fn['size']:>6}  calls={n_calls:>4}  {fn['name'] or '<anon>'}")
    summary.append("")
    summary.append("=== Functions calling pthread_create ===")
    for caller in sorted(plt_call_callers.get("pthread_create", set())):
        fn = func_by_addr.get(caller)
        if fn:
            summary.append(f"  {hex(caller)}  size={fn['size']:>5}  {fn['name'] or '<anon>'}")
    summary.append("")
    summary.append("=== Functions calling pthread_cond_wait ===")
    for caller in sorted(plt_call_callers.get("pthread_cond_wait", set())):
        fn = func_by_addr.get(caller)
        if fn:
            summary.append(f"  {hex(caller)}  size={fn['size']:>5}  {fn['name'] or '<anon>'}")
    summary.append("")
    summary.append("=== Functions calling pthread_cond_signal/broadcast ===")
    for plt_sym in ("pthread_cond_signal", "pthread_cond_broadcast"):
        for caller in sorted(plt_call_callers.get(plt_sym, set())):
            fn = func_by_addr.get(caller)
            if fn:
                summary.append(f"  [{plt_sym:25}] {hex(caller)}  size={fn['size']:>5}  {fn['name'] or '<anon>'}")

    with open(os.path.join(OUT, "summary.txt"), "w") as f:
        f.write("\n".join(summary) + "\n")

    print("\n".join(summary))
    print(f"\nWrote artifacts to {OUT}/")
