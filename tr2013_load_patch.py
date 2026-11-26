"""Loading-speed patches for Tomb Raider (2013) v1.1 build 0.0_32 (speculative, small gains).

While a level, save or fast travel loads, the main thread waits in polling loops:
Sleep(n), pump one loading-screen frame, check whether the file system is done.
The game never calls timeBeginPeriod, so unless another DLL raised the timer
resolution, each Sleep(1) / Sleep(5) can last ~15.6 ms.

  sleep1-*   12 level-entry / load wait loops: Sleep(1) -> Sleep(0)
  sleep5-*   3 object-load / startup wait loops: Sleep(5) -> Sleep(0)
  readcap-*  file-system read size cap 512 KB -> 1 MB. The read size is already
             bounded by the free space in the 1 MB ring buffer (RVA 0x139b26),
             so the larger cap cannot overflow it; it only allows fewer, larger
             reads when the buffer is empty.
  spin-*     4 busy-wait loops (render pass wait, 2 scene spinlocks, one helper)
             get a `pause` instruction (frees the SMT sibling, saves power).
  timer-*    timeBeginPeriod(1) at startup via two small int3-padding caves,
             so every remaining Sleep(1) lasts ~1 ms instead of up to 15.6 ms.

Sleep(0) still yields to the loader threads (they run at HIGHEST priority);
the cost is one core at 100% during load screens.

Each site is a same-length byte swap. The timer caves use int3 runs that do
not overlap tr2013_doublefree_patch.py's caves (0x6db800..0x6db830), so the
two patchers are independent. Note: that script's `revert`
restores TombRaider.exe.orig and therefore also removes these patches.

Usage:
    python tr2013_load_patch.py apply  [path\\to\\TombRaider.exe]
    python tr2013_load_patch.py revert [path\\to\\TombRaider.exe]
    python tr2013_load_patch.py status [path\\to\\TombRaider.exe]
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXE = Path(r"D:\SteamLibrary\steamapps\common\Tomb Raider\TombRaider.exe")

TEXT_RVA = 0x1000
TEXT_RAW = 0x400


@dataclass(frozen=True)
class Site:
    """One same-length byte swap. orig/new include context bytes for verification."""

    name: str
    rva: int
    orig: bytes
    new: bytes


def _sleep(name: str, rva: int, orig_hex: str) -> Site:
    """Build a Site turning `push N` (6a NN) at the start of orig_hex into `push 0`."""
    orig = bytes.fromhex(orig_hex)
    return Site(name, rva, orig, b"\x6a\x00" + orig[2:])


SITES = (
    # Sleep(1) in level-entry wait loops (esi = Sleep, loaded at 0x344d8b / 0x345096)
    _sleep("sleep1-344df8", 0x344DF8, "6a01ffd6"),
    _sleep("sleep1-344e3e", 0x344E3E, "6a01ffd6"),
    _sleep("sleep1-344e5a", 0x344E5A, "6a01ffd6"),
    _sleep("sleep1-3450a3", 0x3450A3, "6a01c605"),  # mov byte [..],2 then call esi
    _sleep("sleep1-3450f3", 0x3450F3, "6a01c605"),  # mov byte [..],2 then call esi
    _sleep("sleep1-345169", 0x345169, "6a01ffd6"),
    _sleep("sleep1-345191", 0x345191, "6a01ffd6"),
    # Sleep(5) in object-load / startup wait loops (ebx/esi = Sleep)
    _sleep("sleep5-31977d", 0x31977D, "6a05ffd3"),
    _sleep("sleep5-324b85", 0x324B85, "6a05ffd3"),
    _sleep("sleep5-332dc7", 0x332DC7, "6a05ffd6"),
    # FS read cap: cmp ecx,0x80000 / mov ecx,0x80000 -> 0x100000
    Site("readcap-cmp", 0x139B59, bytes.fromhex("81f900000800"), bytes.fromhex("81f900001000")),
    Site("readcap-mov", 0x139B61, bytes.fromhex("b900000800"), bytes.fromhex("b900001000")),
    # Sleep(1) in 5 more load-wait loops (esi = Sleep, or call [Sleep] at 0x347900)
    _sleep("sleep1-340946", 0x340946, "6a01ffd6"),
    _sleep("sleep1-3456c7", 0x3456C7, "6a01ffd6"),
    _sleep("sleep1-3478fe", 0x3478FE, "6a01ff15"),
    _sleep("sleep1-3479f7", 0x3479F7, "6a01ffd6"),
    _sleep("sleep1-347a60", 0x347A60, "6a01ffd6"),
    # Spin loops without `pause`: same exit state, no branches into the ranges.
    # while (!([edi] & ecx)); eax is reloaded at 0x2045cc before use
    Site("spin-2045c3", 0x2045C3, bytes.fromhex("8b0785c174fa"), bytes.fromhex("f390850f74fa")),
    # 0/1 spinlocks (released by xchg [lock],0): lock cmpxchg loop -> xchg + pause.
    # Exit with eax=0 as before; ecx is overwritten right after both loops.
    Site("spin-2b50c0", 0x2B50C0, bytes.fromhex("b90100000033c0f00fb10b85c075f1"),
         bytes.fromhex("b801000000870385c07404f390ebf1")),
    Site("spin-2b8a57", 0x2B8A57, bytes.fromhex("b90100000033c0f00fb10f85c075f1"),
         bytes.fromhex("b801000000870785c07404f390ebf1")),
    # while (*ecx); ret -- uses 4 bytes of int3 padding after the ret
    Site("spin-da2c0", 0x0DA2C0, bytes.fromhex("8b0185c075fac3cccccccc"),
         bytes.fromhex("8b0185c07404f390ebf6c3")),
)


def _disp(target_rva: int, base_rva: int) -> bytes:
    """Signed 32-bit little-endian target_rva - base_rva."""
    return (target_rva - base_rva).to_bytes(4, "little", signed=True)


# timer-*: call timeBeginPeriod(1) once at process start (the game never does).
# The entry point's `call __security_init_cookie` goes to cave A, which runs
# GetModuleHandleA("WINMM.dll") / GetProcAddress(..., "timeBeginPeriod") / call(1)
# across two int3 runs, then tail-jumps to the cookie init. The image is ASLR
# (DYNAMIC_BASE), so the caves are position independent: ebx = the call's
# return address (image + ENTRY_RET), every reference is [ebx + disp32].
ENTRY_CALL = 0x91222B  # e8 50180000  call 0x913a80 (__security_init_cookie)
ENTRY_RET = 0x912230
COOKIE_INIT = 0x913A80
TIMER_A = 0x6DEFD4  # int3 run 0x6defd2..0x6df000 (46 bytes)
TIMER_B = 0x77CF95  # int3 run 0x77cf93..0x77cfc0 (45 bytes)
WINMM_NAME = 0xA0A720  # "WINMM.dll" import-descriptor name in .rdata
IAT_GETMODULEHANDLEA = 0x95540C
IAT_GETPROCADDRESS = 0x955428
TBP_NAME = b"timeBeginPeriod\x00"


def _timer_cave_a() -> bytes:
    code = (
        b"\x53"  # push ebx
        + b"\x8b\x5c\x24\x04"  # mov ebx,[esp+4]
        + b"\x8d\x83" + _disp(WINMM_NAME, ENTRY_RET)  # lea eax,[ebx+"WINMM.dll"]
        + b"\x50"  # push eax
        + b"\xff\x93" + _disp(IAT_GETMODULEHANDLEA, ENTRY_RET)  # call [GetModuleHandleA]
    )
    code += b"\xe9" + _disp(TIMER_B, TIMER_A + len(code) + 5)  # jmp cave B
    return code + TBP_NAME


TBP_NAME_RVA = TIMER_A + len(_timer_cave_a()) - len(TBP_NAME)


def _timer_cave_b() -> bytes:
    code = (
        b"\x8d\x8b" + _disp(TBP_NAME_RVA, ENTRY_RET)  # lea ecx,[ebx+"timeBeginPeriod"]
        + b"\x51\x50"  # push ecx ; push eax
        + b"\xff\x93" + _disp(IAT_GETPROCADDRESS, ENTRY_RET)  # call [GetProcAddress]
        + b"\x85\xc0\x74\x04"  # test eax,eax ; jz +4
        + b"\x6a\x01\xff\xd0"  # push 1 ; call eax   (timeBeginPeriod(1))
        + b"\x5b"  # pop ebx
    )
    return code + b"\xe9" + _disp(COOKIE_INIT, TIMER_B + len(code) + 5)  # jmp cookie init


def _cave(name: str, rva: int, code: bytes) -> Site:
    return Site(name, rva, b"\xcc" * len(code), code)


SITES += (
    Site("timer-entry", ENTRY_CALL, bytes.fromhex("e850180000"),
         b"\xe8" + _disp(TIMER_A, ENTRY_CALL + 5)),
    _cave("timer-cave-a", TIMER_A, _timer_cave_a()),
    _cave("timer-cave-b", TIMER_B, _timer_cave_b()),
)


def rva_to_off(rva: int) -> int:
    """Convert a .text RVA to a file offset."""
    return rva - TEXT_RVA + TEXT_RAW


def site_status(data: bytes, s: Site) -> str:
    """Return 'original', 'patched' or 'unknown' for one site."""
    off = rva_to_off(s.rva)
    cur = data[off : off + len(s.orig)]
    if cur == s.orig:
        return "original"
    if cur == s.new:
        return "patched"
    return "unknown"


def status(data: bytes) -> str:
    """Return 'original', 'patched', 'partial' or 'unknown' for the whole exe."""
    states = {site_status(data, s) for s in SITES}
    if "unknown" in states:
        return "unknown"
    if len(states) == 2:
        return "partial"
    return states.pop()


def _rewrite(data: bytes, to_new: bool) -> bytes:
    """Return data with every site set to new (to_new) or orig bytes; raise ValueError on unknown."""
    if status(data) == "unknown":
        raise ValueError("exe is not the expected build (status=unknown)")
    out = bytearray(data)
    for s in SITES:
        off = rva_to_off(s.rva)
        out[off : off + len(s.orig)] = s.new if to_new else s.orig
    return bytes(out)


def apply(data: bytes) -> bytes:
    """Return exe bytes with all load patches applied."""
    return _rewrite(data, to_new=True)


def revert(data: bytes) -> bytes:
    """Return exe bytes with all load patches removed."""
    return _rewrite(data, to_new=False)


def main(argv: list[str]) -> int:
    """CLI entry point."""
    if len(argv) < 2 or argv[1] not in {"apply", "revert", "status"}:
        print(__doc__)
        return 2
    exe = Path(argv[2]) if len(argv) > 2 else DEFAULT_EXE
    data = exe.read_bytes()
    if argv[1] == "status":
        print(f"{exe}: {status(data)}")
        for s in SITES:
            print(f"  {s.name}: {site_status(data, s)}")
        return 0
    try:
        out = apply(data) if argv[1] == "apply" else revert(data)
    except ValueError as e:
        print(f"{exe}: {e}")
        return 1
    if out == data:
        print(f"{exe}: already {'patched' if argv[1] == 'apply' else 'original'}")
        return 0
    backup = exe.with_name(exe.name + ".preload")
    shutil.copy2(exe, backup)
    exe.write_bytes(out)
    print(f"{argv[1]}: {exe} (previous copy: {backup})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
