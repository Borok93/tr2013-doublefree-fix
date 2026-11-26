# Tomb Raider (2013) double-free freeze fix + crash-on-quit fix

A small binary patch for **Tomb Raider (2013) / GOTY / Survival Edition (Steam, 32-bit `TombRaider.exe`, build `v1.1 build 0.0_32`)**. It fixes two things:

1. A hard freeze (game stops responding, has to be killed from Task Manager) caused by a double free in the game's memory allocator.
2. A crash when quitting to desktop (access violation at `0xdededede` inside `d3d11.dll`), seen with the Epic overlay, ReShade and the Steam overlay all loaded. See [Crash on quit](#crash-on-quit).

A separate, experimental [load-speed and timer patch](#optional-load-speed-and-timer-patch) is also included.

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

## Crash on quit

The current Steam build ships `EOSSDK-Win32-Shipping.dll`, which loads the Epic Online Services overlay (`EOSOVH-Win32-Shipping.dll`). After the game logs "Exiting the game." it calls `_exit(0)` (RVA `0x3334cc`). `ExitProcess` then runs each DLL's shutdown code. In that step, the Epic overlay's exit handler releases a D3D11 device through ReShade's `dxgi.dll` and the Steam overlay (`gameoverlayrenderer.dll`). The GPU driver has already been unloaded, so the release jumps into freed memory:

```
eip=dededede
d3d11!CContext::EmptyOutAllDDIBindPoints+0x110
...
gameoverlayrenderer+0x604ae
dxgi (ReShade 6.8)
EOSOVH_Win32_Shipping
ucrtbase!_execute_onexit_table
ntdll!LdrShutdownProcess
TombRaider+0x3334d1   ; return from _exit(0)
```

The fix replaces that `_exit(0)` call with `TerminateProcess(GetCurrentProcess(), 0)`. It uses the game's own wrapper at RVA `0x937270`, so no new import or relocation is needed. The game has already saved and shut down its own systems by then; only the third-party DLLs' shutdown code is skipped. If `TerminateProcess` fails, the original `_exit(0)` runs.

```asm
; RVA 0x3334cc:  call cave            (replaces: call _exit thunk at 0x92b114)
; cave at RVA 0x6db81c (same int3 padding):
push 0                 ; exit code
push -1                ; GetCurrentProcess()
call 0x937270          ; TerminateProcess wrapper
add  esp, 8            ; only reached if it failed
jmp  0x92b114          ; original _exit
```

That is 5 bytes changed in place plus 17 bytes of padding.

## Usage

Requires Python 3.9+. Close the game first.

```
python tr2013_doublefree_patch.py status "<path>\TombRaider.exe"
python tr2013_doublefree_patch.py apply  "<path>\TombRaider.exe"
python tr2013_doublefree_patch.py revert "<path>\TombRaider.exe"
```

If you leave out the path, the script uses `C:\Program Files (x86)\Steam\steamapps\common\Tomb Raider\TombRaider.exe`.

- `apply` writes a backup to `TombRaider.exe.orig` and checks every byte it replaces. It refuses to patch any other build. It applies both fixes, and it adds the quit fix to an exe that already has only the freeze fix.
- `status` reports each fix (`double-free`, `quit-crash`) separately.
- `revert` restores the backup.
- Steam "Verify integrity of game files" restores the original exe. Run `apply` again afterwards.

Tested build:

| File | SHA-256 |
|---|---|
| original `TombRaider.exe` | `F36B8DD2BD74D48C14BF910AD9BD4AC9F4024433523FFC7E46D5C85C3DD618F5` |
| patched `TombRaider.exe` (both fixes) | `B61CFAB031BF25EE23ED72F7972F4E4CDB141B851A5F9A3CE7FF66114FC846AF` |

## Limitations

- The patch catches the case seen in the dumps: freeing a chunk that is still free. If the game frees memory, it gets reused, and then the stale pointer is freed again, the check cannot see it.
- The quit fix skips DLL shutdown code, so third-party DLLs (overlays, ReShade) do not get their exit callbacks. Anything they would only write at exit is lost. ReShade saves its settings when they change, so this did not matter in testing.
- It has only been tested with the Steam GOTY build above, with both texture mods enabled and the freezing tomb played through. Other builds (GOG, Epic, the Definitive Edition) are untested, and the script refuses to patch them.

## Optional: load-speed and timer patch

`tr2013_load_patch.py` is a separate, experimental patch. It is independent of the fixes above, and its caves do not overlap theirs. It has not been tested in game yet, so the gains are unmeasured. The game's loader reads one file at a time, so on a hard drive the disk stays the main bottleneck.

What it changes:

| Sites | Change | Why |
|---|---|---|
| 15 load-wait loops (12x `Sleep(1)`, 3x `Sleep(5)`) | -> `Sleep(0)` | While loading, the main thread polls in `Sleep` loops, and without a raised timer resolution each `Sleep` can take up to 15.6 ms |
| RVA `0x139b59`, `0x139b61` | file-system read cap 512 KB -> 1 MB | Fewer, larger reads. Each read is still limited by the free space in the 1 MB ring buffer, so it cannot overflow |
| 4 spin loops (`0x2045c3`, `0x2b50c0`, `0x2b8a57`, `0xda2c0`) | add `pause` (the two scene spinlocks become `xchg` + `pause`) | These loops busy-wait without `pause`. Adding it frees the core's other hardware (SMT) thread and saves power. The registers and flags the game uses after each loop are unchanged |
| entry point RVA `0x91222b` + int3 padding at `0x6defd4`, `0x77cf95` | call `timeBeginPeriod(1)` once at startup | The game never sets the 1 ms timer, so every remaining `Sleep(1)`, including render-thread waits, lasts about 1 ms instead of up to 15.6 ms |

The timer code is position independent: it uses the entry call's return address as a base, so it works with ASLR. It gets `GetModuleHandleA("WINMM.dll")` and `GetProcAddress(..., "timeBeginPeriod")` from the existing import table. Then it jumps on to the original `__security_init_cookie`. In a test harness, `Sleep(1)` went from 15.2 ms to 1.3 ms. The Steam overlay, Bink and EOS can also raise the timer resolution, so the real gain depends on what else runs in the game process. The cost: during load screens, the `Sleep(0)` loops keep one core at 100%.

```
python tr2013_load_patch.py status "<path>\TombRaider.exe"
python tr2013_load_patch.py apply  "<path>\TombRaider.exe"
python tr2013_load_patch.py revert "<path>\TombRaider.exe"
```

- Always pass the path.
- `apply` checks every byte it replaces and refuses unknown builds. It completes a partially patched exe. It saves the previous file as `TombRaider.exe.preload`.
- `revert` writes back the original bytes of these sites only, so the freeze and quit fixes stay.
- `tr2013_doublefree_patch.py revert` restores `TombRaider.exe.orig`, which also removes this patch.

## Tests

```
python -m pytest
```

The tests use a synthetic image and do not need the game.
