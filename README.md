# Tomb Raider (2013) double-free freeze fix

A small binary patch for **Tomb Raider (2013) / GOTY / Survival Edition (Steam, 32-bit `TombRaider.exe`, build `v1.1 build 0.0_32`)**. It stops a hard freeze (game stops responding, has to be killed from Task Manager) caused by a double free in the game's memory allocator.

It was found with texture mods installed via the TR2013 Mod Manager ([TrRebootTools](https://www.nexusmods.com/tombraider2013/mods/112)). The mod that triggered it was [Upscaled and optional regenerated 2K 4K textures](https://www.nexusmods.com/tombraider2013/mods/128) (`x2_textures`, optional `Regenerated addon`). In the Mountain Village challenge tomb near the crashed bomber (`pc-w\ac_challenge_tomb.drm`), the game froze about a minute after loading. The bug itself is in the game's code; the mod data only triggers it.

## Symptoms

- The game freezes in one specific area and never recovers. No crash dialog, no Windows Error Reporting entry, no dump.
- Memory use is normal, well under the 4 GB 32-bit limit.
- A hang dump shows the main thread spinning at `TombRaider.exe+0x2e75b2..0x2e75c1` (`FAILURE_BUCKET_ID: APPLICATION_HANG_BusyHang_cfffffff_TombRaider.exe!Unknown`). Other threads are blocked in `RtlEnterCriticalSection` on the allocator lock.

## Root cause

`TombRaider.exe` uses a dlmalloc-style allocator. Its `free()` (RVA `0x2e73f0`) does not check for double frees. A chunk's "in use" state is stored in bit 0 (`PREV_INUSE`) of the **next** chunk's size field. If the same chunk is freed twice, the second free inserts it into its size-sorted bin while it is already there. The insert loop then finds the chunk itself and links it to itself (`next == prev == self`). The next allocation or free that walks that bin loops forever at RVA `0x2e75b2` while holding the allocator lock, and the whole game freezes.

## The fix

The patch adds a guard at the start of `free()`: if the next chunk's `PREV_INUSE` bit is already clear, the chunk is already free, so return without doing anything.

```asm
; RVA 0x2e7424 (inside free):   jmp cave ; nop ; nop   (replaces: mov [ebp-8],eax / cmp edx,[ecx] / jne +0x49)
; cave at RVA 0x6db800 (unused int3 padding in .text):
mov  [ebp-8], eax          ; original
test byte ptr [edx+4], 1   ; next chunk PREV_INUSE -> is this chunk in use?
jz   0x2e75d2              ; already free -> return from free() (pop esi/edi/ebx, ret 4)
cmp  edx, [ecx]            ; original
jne  0x2e7474              ; original
jmp  0x2e742b              ; resume free()
```

That is 7 bytes changed in place plus 26 bytes written into int3 padding. All jumps are relative, so the patch works with ASLR. No DLL, loader or runtime hook is needed.

## Usage

Requires Python 3.9+. Close the game first.

```
python tr2013_doublefree_patch.py status "<path>\TombRaider.exe"
python tr2013_doublefree_patch.py apply  "<path>\TombRaider.exe"
python tr2013_doublefree_patch.py revert "<path>\TombRaider.exe"
```

If you leave out the path, the script uses `C:\Program Files (x86)\Steam\steamapps\common\Tomb Raider\TombRaider.exe`.

- `apply` writes a backup to `TombRaider.exe.orig` and checks every byte it replaces. It refuses to patch any other build.
- `revert` restores the backup.
- Steam "Verify integrity of game files" restores the original exe. Run `apply` again afterwards.

Tested build:

| File | SHA-256 |
|---|---|
| original `TombRaider.exe` | `F36B8DD2BD74D48C14BF910AD9BD4AC9F4024433523FFC7E46D5C85C3DD618F5` |
| patched `TombRaider.exe` | `E09EFC533E4B4D41C05E57E878C871A5E9BF0A88D35F21A3E259073315D7BBC0` |

## Limitations

- The patch catches the case seen in the dumps: freeing a chunk that is still free. If the game frees memory, it gets reused, and then the stale pointer is freed again, the check cannot see it.
- It has only been tested with the Steam GOTY build above, with both texture mods enabled and the freezing tomb played through. Other builds (GOG, Epic, the Definitive Edition) are untested, and the script refuses to patch them.

## Tests

```
python -m pytest
```

The tests use a synthetic image and do not need the game.
