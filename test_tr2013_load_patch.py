"""Tests for tr2013_load_patch (synthetic exe image, no game files needed)."""

import pytest

import tr2013_load_patch as p


def make_image() -> bytes:
    size = max(p.rva_to_off(s.rva) + len(s.orig) for s in p.SITES) + 0x10
    img = bytearray(size)
    for s in p.SITES:
        img[p.rva_to_off(s.rva) : p.rva_to_off(s.rva) + len(s.orig)] = s.orig
    return bytes(img)


def test_sites_same_length_and_do_not_overlap() -> None:
    spans = sorted((p.rva_to_off(s.rva), p.rva_to_off(s.rva) + len(s.orig)) for s in p.SITES)
    assert all(len(s.orig) == len(s.new) for s in p.SITES)
    assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:]))


def test_sleep_sites_only_change_push_immediate() -> None:
    for s in p.SITES:
        if s.name.startswith("sleep"):
            assert s.orig[0] == 0x6A and s.orig[1] in (1, 5)
            assert s.new == b"\x6a\x00" + s.orig[2:]


def test_readcap_raises_to_1mb() -> None:
    caps = {s.name: s for s in p.SITES if s.name.startswith("readcap")}
    assert int.from_bytes(caps["readcap-cmp"].new[2:], "little") == 0x100000
    assert int.from_bytes(caps["readcap-mov"].new[1:], "little") == 0x100000


def test_spin_sites_add_pause() -> None:
    spins = [s for s in p.SITES if s.name.startswith("spin")]
    assert len(spins) == 4
    assert all(b"\xf3\x90" in s.new and b"\xf3\x90" not in s.orig for s in spins)


def test_timer_caves_fit_int3_runs_and_chain() -> None:
    sites = {s.name: s for s in p.SITES if s.name.startswith("timer")}
    a, b = sites["timer-cave-a"], sites["timer-cave-b"]
    assert 0x6DEFD2 <= a.rva and a.rva + len(a.new) <= 0x6DF000
    assert 0x77CF93 <= b.rva and b.rva + len(b.new) <= 0x77CFC0

    def rel_target(site: p.Site, at: int, size: int) -> int:
        disp = int.from_bytes(site.new[at + 1 : at + 5], "little", signed=True)
        return site.rva + at + size + disp

    entry = sites["timer-entry"]
    assert rel_target(entry, 0, 5) == p.TIMER_A
    assert rel_target(a, len(a.new) - len(p.TBP_NAME) - 5, 5) == p.TIMER_B
    assert rel_target(b, len(b.new) - 5, 5) == p.COOKIE_INIT
    assert a.new.endswith(b"timeBeginPeriod\x00")


def test_apply_revert_roundtrip() -> None:
    img = make_image()
    assert p.status(img) == "original"
    patched = p.apply(img)
    assert p.status(patched) == "patched"
    assert p.apply(patched) == patched
    assert p.revert(patched) == img


def test_partial_is_completed_by_apply() -> None:
    img = bytearray(make_image())
    s = p.SITES[0]
    img[p.rva_to_off(s.rva) : p.rva_to_off(s.rva) + len(s.new)] = s.new
    assert p.status(bytes(img)) == "partial"
    assert p.status(p.apply(bytes(img))) == "patched"


def test_unknown_bytes_refused() -> None:
    img = bytearray(make_image())
    img[p.rva_to_off(p.SITES[-1].rva)] ^= 0xFF
    assert p.status(bytes(img)) == "unknown"
    with pytest.raises(ValueError):
        p.apply(bytes(img))
