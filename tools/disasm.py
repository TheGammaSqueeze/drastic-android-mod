#!/usr/bin/env python3
"""
Disassemble a single function or address range from libdrastic_arm64.so,
with symbols, PLT names, and string xrefs annotated inline.

Usage:
  dis.py <hex_addr> [size]
"""
import sys, json
from elftools.elf.elffile import ELFFile
from capstone import Cs, CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN
from capstone.arm64 import (ARM64_OP_IMM, ARM64_OP_REG, ARM64_OP_MEM,
                            ARM64_INS_BL, ARM64_INS_B, ARM64_INS_BLR,
                            ARM64_INS_ADRP, ARM64_INS_ADD, ARM64_INS_LDR,
                            ARM64_INS_LDRB, ARM64_INS_LDRH, ARM64_INS_LDRSW,
                            ARM64_INS_RET)

LIB = "libdrastic_arm64.so"

if len(sys.argv) < 2:
    print(__doc__); sys.exit(1)

target = int(sys.argv[1], 16)
size_override = int(sys.argv[2]) if len(sys.argv) > 2 else None

with open(LIB, "rb") as f:
    elf = ELFFile(f)
    text = elf.get_section_by_name(".text")
    text_addr = text["sh_addr"]
    text_data = text.data()
    rodata = elf.get_section_by_name(".rodata")
    rodata_addr = rodata["sh_addr"]
    rodata_data = rodata.data()

# Load function map and PLT
funcs = json.load(open("funcs.json"))
plt = {int(k, 16): v for k, v in json.load(open("plt.json")).items()}
strings = {int(k, 16): v for k, v in json.load(open("strings.json")).items()}
funcs_by_addr = {f["addr"]: f for f in funcs}
func_starts = sorted(funcs_by_addr.keys())

def find_func(addr):
    lo, hi = 0, len(func_starts)-1
    while lo <= hi:
        mid = (lo+hi)//2
        if func_starts[mid] <= addr:
            if mid == len(func_starts)-1 or func_starts[mid+1] > addr:
                return func_starts[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return None

# Resolve function size
fn = funcs_by_addr.get(target)
if fn is None:
    f_owner = find_func(target)
    fn = funcs_by_addr[f_owner]
    print(f"# Note: 0x{target:x} is inside fn 0x{f_owner:x}")
size = size_override if size_override else fn["size"]
start = fn["addr"]

offset = start - text_addr
data = text_data[offset:offset + size]

md = Cs(CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN)
md.detail = True

# Track ADRP+ADD/LDR for inline rodata annotations
reg_adrp = {}

print(f"# Function 0x{start:x} size={size} name={fn.get('name')}")
print()
for insn in md.disasm(data, start):
    addr = insn.address
    line = f"  {addr:>8x}:  {insn.mnemonic:8} {insn.op_str}"
    annot = ""

    if insn.id == ARM64_INS_BL:
        target_addr = insn.operands[0].imm
        if target_addr in plt:
            annot = f"  ; -> PLT: {plt[target_addr]}"
        else:
            tf = funcs_by_addr.get(target_addr)
            if tf:
                annot = f"  ; -> fn 0x{target_addr:x} ({tf.get('name') or '<anon>'}, {tf['size']}b)"
            else:
                annot = f"  ; -> 0x{target_addr:x}"
    elif insn.id == ARM64_INS_B and not insn.cc:
        target_addr = insn.operands[0].imm
        if target_addr in plt:
            annot = f"  ; tail -> PLT: {plt[target_addr]}"
        elif target_addr in funcs_by_addr:
            tf = funcs_by_addr[target_addr]
            annot = f"  ; tail -> fn 0x{target_addr:x} ({tf.get('name') or '<anon>'}, {tf['size']}b)"
    elif insn.id == ARM64_INS_BLR:
        annot = "  ; indirect"

    # Track adrp+add for symbol resolution
    if insn.id == ARM64_INS_ADRP:
        ops = insn.operands
        if len(ops) >= 2 and ops[0].type == ARM64_OP_REG and ops[1].type == ARM64_OP_IMM:
            reg = insn.reg_name(ops[0].reg)
            reg_adrp[reg] = ops[1].imm
    elif insn.id == ARM64_INS_ADD:
        ops = insn.operands
        if (len(ops) >= 3 and ops[0].type == ARM64_OP_REG and
            ops[1].type == ARM64_OP_REG and ops[2].type == ARM64_OP_IMM):
            src = insn.reg_name(ops[1].reg)
            dst = insn.reg_name(ops[0].reg)
            if src in reg_adrp:
                t = reg_adrp[src] + ops[2].imm
                if t in strings:
                    s = strings[t][:60]
                    annot = f"  ; \"{s}\""
                elif t in funcs_by_addr:
                    annot = f"  ; -> fn 0x{t:x}"
                else:
                    annot = f"  ; ref 0x{t:x}"
                reg_adrp[dst] = t
    elif insn.id in (ARM64_INS_LDR, ARM64_INS_LDRB, ARM64_INS_LDRH, ARM64_INS_LDRSW):
        ops = insn.operands
        if len(ops) >= 2 and ops[1].type == ARM64_OP_MEM and ops[1].mem.base:
            base = insn.reg_name(ops[1].mem.base)
            if base in reg_adrp:
                t = reg_adrp[base] + ops[1].mem.disp
                if t in strings:
                    s = strings[t][:60]
                    annot = f"  ; load \"{s}\""
                elif t in plt:
                    annot = f"  ; load &{plt[t]}"
                else:
                    annot = f"  ; load mem[0x{t:x}]"

    print(line + annot)

    if insn.id == ARM64_INS_RET:
        # check whether more code follows in the function
        if addr + 4 < start + size:
            print("  ; ----")
