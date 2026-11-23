"""Tests for tr2013_doublefree_patch (synthetic exe image, no game files needed)."""

import pytest

import tr2013_doublefree_patch as p


def make_image() -> bytes:
    size = p.rva_to_off(p.CAVE_RVA) + 0x100
    img = bytearray(size)
    img[p.rva_to_off(p.SITE_RVA) : p.rva_to_off(p.SITE_RVA) + 7] = p.SITE_ORIG
    img[p.rva_to_off(p.EXIT_SITE_RVA) - 2 : p.rva_to_off(p.EXIT_SITE_RVA)] = bytes.fromhex("6a00")
    img[p.rva_to_off(p.EXIT_SITE_RVA) : p.rva_to_off(p.EXIT_SITE_RVA) + 5] = p.EXIT_SITE_ORIG
    img[p.rva_to_off(p.CAVE_RVA) - 4 : p.rva_to_off(p.CAVE_RVA) + 48] = b"\xcc" * 52
    return bytes(img)


def target(code: bytes, at_rva: int, insn_off: int, insn_len: int) -> int:
    disp = int.from_bytes(code[insn_off + insn_len - 4 : insn_off + insn_len], "little", signed=True)
    return at_rva + insn_off + insn_len + disp


def test_cave_jumps_hit_original_targets() -> None:
    cave = p.build_cave()
    assert cave[0:3] == bytes.fromhex("8945f8")
    assert cave[3:7] == bytes.fromhex("f6420401")
    assert target(cave, p.CAVE_RVA, 7, 6) == p.RET_RVA
    assert cave[13:15] == bytes.fromhex("3b11")
    assert target(cave, p.CAVE_RVA, 15, 6) == p.NOT_TOP_RVA
    assert target(cave, p.CAVE_RVA, 21, 5) == p.CONT_RVA


def test_site_jumps_to_cave() -> None:
    site = p.build_site()
    assert len(site) == 7 and site[0] == 0xE9 and site[5:] == b"\x90\x90"
    assert target(site, p.SITE_RVA, 0, 5) == p.CAVE_RVA


def test_original_exit_site_calls_exit_thunk() -> None:
    assert target(p.EXIT_SITE_ORIG, p.EXIT_SITE_RVA, 0, 5) == p.EXIT_THUNK_RVA


def test_exit_cave_terminates_then_falls_back_to_exit() -> None:
    cave = p.build_exit_cave()
    assert cave[0:4] == bytes.fromhex("6a006aff")
    assert cave[4] == 0xE8 and target(cave, p.EXIT_CAVE_RVA, 4, 5) == p.TERMINATE_WRAPPER_RVA
    assert cave[9:12] == bytes.fromhex("83c408")
    assert cave[12] == 0xE9 and target(cave, p.EXIT_CAVE_RVA, 12, 5) == p.EXIT_THUNK_RVA


def test_exit_site_calls_cave() -> None:
    site = p.build_exit_site()
    assert len(site) == 5 and site[0] == 0xE8
    assert target(site, p.EXIT_SITE_RVA, 0, 5) == p.EXIT_CAVE_RVA


def test_caves_do_not_overlap_and_stay_in_int3_run() -> None:
    assert p.CAVE_RVA + p.CAVE_LEN < p.EXIT_CAVE_RVA
    assert p.EXIT_CAVE_RVA + p.EXIT_CAVE_LEN <= 0x6DB830


def test_apply_roundtrip_status() -> None:
    img = make_image()
    assert p.status(img) == "original"
    patched = p.apply(img)
    assert p.status(patched) == "patched"
    assert len(patched) == len(img)
    assert p.apply(patched) == patched


def test_apply_upgrades_exe_with_only_double_free_patch() -> None:
    img = bytearray(make_image())
    img[p.rva_to_off(p.SITE_RVA) : p.rva_to_off(p.SITE_RVA) + 7] = p.build_site()
    img[p.rva_to_off(p.CAVE_RVA) : p.rva_to_off(p.CAVE_RVA) + p.CAVE_LEN] = p.build_cave()
    assert p.status(bytes(img)) == "partial"
    assert p.status(p.apply(bytes(img))) == "patched"


def test_apply_refuses_unknown_build() -> None:
    img = bytearray(make_image())
    img[p.rva_to_off(p.SITE_RVA)] ^= 0xFF
    with pytest.raises(ValueError):
        p.apply(bytes(img))
    img = bytearray(make_image())
    img[p.rva_to_off(p.EXIT_SITE_RVA) + 1] ^= 0xFF
    with pytest.raises(ValueError):
        p.apply(bytes(img))
