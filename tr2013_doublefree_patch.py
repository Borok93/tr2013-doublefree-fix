"""Patch Tomb Raider (2013) v1.1 build 0.0_32 so that free() ignores double frees.

Root cause: the game's dlmalloc-style free() (RVA 0x2e73f0) has no double-free
check. When the same chunk is freed twice, it gets inserted into its size bin
twice and ends up linked to itself (next == prev == self). The next bin walk at
RVA 0x2e75b2 then loops forever while holding the allocator lock, and the game
freezes.

Fix: at the start of free(), check bit 0 of the next chunk's size field
(PREV_INUSE, i.e. "this chunk is in use"). If it is already clear, the chunk is
already free, so return without doing anything.

Usage:
    python tr2013_doublefree_patch.py apply  [path\\to\\TombRaider.exe]
    python tr2013_doublefree_patch.py revert [path\\to\\TombRaider.exe]
    python tr2013_doublefree_patch.py status [path\\to\\TombRaider.exe]
"""

from __future__ import annotations

import shutil
import struct
import sys
from pathlib import Path

DEFAULT_EXE = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Tomb Raider\TombRaider.exe")

TEXT_RVA = 0x1000
TEXT_RAW = 0x400

SITE_RVA = 0x2E7424  # mov [ebp-8],eax / cmp edx,[ecx] / jne +0x49
SITE_ORIG = bytes.fromhex("8945f83b117549")
RET_RVA = 0x2E75D2  # pop esi / pop edi / pop ebx / leave-ish / ret 4
NOT_TOP_RVA = 0x2E7474  # original jne target
CONT_RVA = 0x2E742B  # instruction after the original jne
CAVE_RVA = 0x6DB800  # inside a 52-byte int3 run starting at 0x6db7fc
CAVE_LEN = 26


def rva_to_off(rva: int) -> int:
    """Convert a .text RVA to a file offset."""
    return rva - TEXT_RVA + TEXT_RAW


def rel32(src_end_rva: int, dst_rva: int) -> bytes:
    """Encode a rel32 displacement from the end of an instruction to a target."""
    return struct.pack("<i", dst_rva - src_end_rva)


def build_cave() -> bytes:
    """Return the code-cave bytes (position-independent, relative jumps only)."""
    c = CAVE_RVA
    code = bytearray()
    code += bytes.fromhex("8945f8")  # mov [ebp-8],eax          (original)
    code += bytes.fromhex("f6420401")  # test byte ptr [edx+4],1   next chunk PREV_INUSE
    code += b"\x0f\x84" + rel32(c + len(code) + 6, RET_RVA)  # jz -> return (double free)
    code += bytes.fromhex("3b11")  # cmp edx,[ecx]            (original)
    code += b"\x0f\x85" + rel32(c + len(code) + 6, NOT_TOP_RVA)  # jne (original)
    code += b"\xe9" + rel32(c + len(code) + 5, CONT_RVA)  # jmp back
    assert len(code) == CAVE_LEN
    return bytes(code)


def build_site() -> bytes:
    """Return the replacement bytes for the patch site (jmp cave + 2 nops)."""
    return b"\xe9" + rel32(SITE_RVA + 5, CAVE_RVA) + b"\x90\x90"


def status(data: bytes) -> str:
    """Return 'original', 'patched' or 'unknown' for the given exe bytes."""
    site = data[rva_to_off(SITE_RVA) : rva_to_off(SITE_RVA) + len(SITE_ORIG)]
    cave = data[rva_to_off(CAVE_RVA) : rva_to_off(CAVE_RVA) + CAVE_LEN]
    if site == SITE_ORIG and cave == b"\xcc" * CAVE_LEN:
        return "original"
    if site == build_site() and cave == build_cave():
        return "patched"
    return "unknown"


def apply(data: bytes) -> bytes:
    """Return patched exe bytes; raise ValueError if the exe is not the expected build."""
    if status(data) != "original":
        raise ValueError(f"exe is not an unpatched copy of the expected build (status={status(data)})")
    out = bytearray(data)
    out[rva_to_off(SITE_RVA) : rva_to_off(SITE_RVA) + 7] = build_site()
    out[rva_to_off(CAVE_RVA) : rva_to_off(CAVE_RVA) + CAVE_LEN] = build_cave()
    return bytes(out)


def main(argv: list[str]) -> int:
    """CLI entry point."""
    if len(argv) < 2 or argv[1] not in {"apply", "revert", "status"}:
        print(__doc__)
        return 2
    exe = Path(argv[2]) if len(argv) > 2 else DEFAULT_EXE
    backup = exe.with_name(exe.name + ".orig")
    data = exe.read_bytes()
    state = status(data)
    if argv[1] == "status":
        print(f"{exe}: {state}")
        return 0
    if argv[1] == "apply":
        if state == "patched":
            print("already patched")
            return 0
        patched = apply(data)
        if not backup.exists():
            shutil.copy2(exe, backup)
        exe.write_bytes(patched)
        print(f"patched {exe} (backup: {backup})")
        return 0
    if not backup.exists():
        print(f"no backup at {backup}")
        return 1
    shutil.copy2(backup, exe)
    print(f"restored {exe} from {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
