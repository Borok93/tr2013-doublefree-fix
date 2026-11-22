"""Tests for tr2013_doublefree_patch (synthetic exe image, no game files needed)."""

import pytest

import tr2013_doublefree_patch as p


def make_image() -> bytes:
    size = p.rva_to_off(p.CAVE_RVA) + 0x100
    img = bytearray(size)
    img[p.rva_to_off(p.SITE_RVA) : p.rva_to_off(p.SITE_RVA) + 7] = p.SITE_ORIG
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


def test_apply_roundtrip_status() -> None:
    img = make_image()
    assert p.status(img) == "original"
    patched = p.apply(img)
    assert p.status(patched) == "patched"
    assert len(patched) == len(img)


def test_apply_refuses_unknown_build() -> None:
    img = bytearray(make_image())
    img[p.rva_to_off(p.SITE_RVA)] ^= 0xFF
    with pytest.raises(ValueError):
        p.apply(bytes(img))
    with pytest.raises(ValueError):
        p.apply(p.apply(make_image()))
