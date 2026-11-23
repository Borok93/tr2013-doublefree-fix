"""Patch Tomb Raider (2013) v1.1 build 0.0_32: fix a freeze (double free) and a crash on quit.

Patch 1, double free (freeze):
The game's dlmalloc-style free() (RVA 0x2e73f0) has no double-free check.
When the same chunk is freed twice, it gets inserted into its size bin twice
and ends up linked to itself (next == prev == self). The next bin walk at
RVA 0x2e75b2 then loops forever while holding the allocator lock, and the game
freezes. Fix: at the start of free(), check bit 0 of the next chunk's size field
(PREV_INUSE, i.e. "this chunk is in use"). If it is already clear, the chunk is
already free, so return without doing anything.

Patch 2, crash on quit:
After "Exiting the game." the game calls _exit(0) (RVA 0x3334cc). ExitProcess
then runs DLL shutdown code. There, the Epic Online Services overlay
(EOSOVH, loaded by the game's EOSSDK) releases a D3D11 device through ReShade /
the Steam overlay after the GPU driver has already been torn down, and the
release jumps to freed memory (eip=0xdededede). Fix: call
TerminateProcess(GetCurrentProcess(), 0) instead, using the game's own wrapper
at RVA 0x937270. The game has already saved and shut down its systems by then;
only DLL shutdown code is skipped. If TerminateProcess fails, the original
_exit(0) runs.

Usage:
    python tr2013_doublefree_patch.py apply  [path\\to\\TombRaider.exe]
    python tr2013_doublefree_patch.py revert [path\\to\\TombRaider.exe]
    python tr2013_doublefree_patch.py status [path\\to\\TombRaider.exe]
"""

from __future__ import annotations

import shutil
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXE = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Tomb Raider\TombRaider.exe")

TEXT_RVA = 0x1000
TEXT_RAW = 0x400

# Patch 1: double free in free()
SITE_RVA = 0x2E7424  # mov [ebp-8],eax / cmp edx,[ecx] / jne +0x49
SITE_ORIG = bytes.fromhex("8945f83b117549")
RET_RVA = 0x2E75D2  # pop esi / pop edi / pop ebx / leave-ish / ret 4
NOT_TOP_RVA = 0x2E7474  # original jne target
CONT_RVA = 0x2E742B  # instruction after the original jne
CAVE_RVA = 0x6DB800  # inside a 52-byte int3 run starting at 0x6db7fc
CAVE_LEN = 26

# Patch 2: crash on quit
EXIT_SITE_RVA = 0x3334CC  # call _exit thunk (after push 0)
EXIT_SITE_ORIG = bytes.fromhex("e8437c5f00")
EXIT_THUNK_RVA = 0x92B114  # jmp [_exit]
TERMINATE_WRAPPER_RVA = 0x937270  # cdecl bool f(HANDLE h, UINT code) -> TerminateProcess(h, code)
EXIT_CAVE_RVA = 0x6DB81C  # same int3 run, after patch 1's cave (ends 0x6db830)
EXIT_CAVE_LEN = 17


@dataclass(frozen=True)
class Patch:
    """One site rewrite plus its code cave."""

    name: str
    site_rva: int
    site_orig: bytes
    site_new: bytes
    cave_rva: int
    cave: bytes


def rva_to_off(rva: int) -> int:
    """Convert a .text RVA to a file offset."""
    return rva - TEXT_RVA + TEXT_RAW


def rel32(src_end_rva: int, dst_rva: int) -> bytes:
    """Encode a rel32 displacement from the end of an instruction to a target."""
    return struct.pack("<i", dst_rva - src_end_rva)


def build_cave() -> bytes:
    """Return the double-free code-cave bytes (position-independent, relative jumps only)."""
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
    """Return the replacement bytes for the double-free site (jmp cave + 2 nops)."""
    return b"\xe9" + rel32(SITE_RVA + 5, CAVE_RVA) + b"\x90\x90"


def build_exit_cave() -> bytes:
    """Return the quit code-cave: TerminateProcess(-1, 0), falling back to _exit(0)."""
    c = EXIT_CAVE_RVA
    code = bytearray()
    code += bytes.fromhex("6a00")  # push 0                   exit code
    code += bytes.fromhex("6aff")  # push -1                  GetCurrentProcess()
    code += b"\xe8" + rel32(c + len(code) + 5, TERMINATE_WRAPPER_RVA)  # call wrapper
    code += bytes.fromhex("83c408")  # add esp,8                (only if it failed)
    code += b"\xe9" + rel32(c + len(code) + 5, EXIT_THUNK_RVA)  # jmp _exit (original path)
    assert len(code) == EXIT_CAVE_LEN
    return bytes(code)


def build_exit_site() -> bytes:
    """Return the replacement bytes for the _exit call site (call exit cave)."""
    return b"\xe8" + rel32(EXIT_SITE_RVA + 5, EXIT_CAVE_RVA)


PATCHES = (
    Patch("double-free", SITE_RVA, SITE_ORIG, build_site(), CAVE_RVA, build_cave()),
    Patch("quit-crash", EXIT_SITE_RVA, EXIT_SITE_ORIG, build_exit_site(), EXIT_CAVE_RVA, build_exit_cave()),
)


def patch_status(data: bytes, p: Patch) -> str:
    """Return 'original', 'patched' or 'unknown' for one patch."""
    site = data[rva_to_off(p.site_rva) : rva_to_off(p.site_rva) + len(p.site_orig)]
    cave = data[rva_to_off(p.cave_rva) : rva_to_off(p.cave_rva) + len(p.cave)]
    if site == p.site_orig and cave == b"\xcc" * len(p.cave):
        return "original"
    if site == p.site_new and cave == p.cave:
        return "patched"
    return "unknown"


def status(data: bytes) -> str:
    """Return 'original', 'patched', 'partial' or 'unknown' for the whole exe."""
    states = {patch_status(data, p) for p in PATCHES}
    if "unknown" in states:
        return "unknown"
    if len(states) == 2:
        return "partial"
    return states.pop()


def apply(data: bytes) -> bytes:
    """Return exe bytes with all patches applied; raise ValueError if the exe is not the expected build."""
    if status(data) == "unknown":
        raise ValueError("exe is not the expected build (status=unknown)")
    out = bytearray(data)
    for p in PATCHES:
        if patch_status(data, p) == "original":
            out[rva_to_off(p.site_rva) : rva_to_off(p.site_rva) + len(p.site_new)] = p.site_new
            out[rva_to_off(p.cave_rva) : rva_to_off(p.cave_rva) + len(p.cave)] = p.cave
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
        for p in PATCHES:
            print(f"  {p.name}: {patch_status(data, p)}")
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
