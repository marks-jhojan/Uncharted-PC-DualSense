# Upstream drafts for Supermedo/uncharted_pc

Ready to paste. The three PRs are independent of UNDS and apply on v0.3.9 with `git am`
(`patches/v0.3.9/0005`, `0006`, `0007`); they also apply without 0001–0004.

---

## PR 1 — red_zone: reserve thunk memory before the GPU starts (`0005`)

**Problem.** On some launches the red-zone pass cannot get thunk memory near the image and leaves most of
the accesses unpatched (`... 3306 left as they were` against the usual `46`). Faults at those sites later
print `its data may be lost`, and those sessions ended badly after a checkpoint save.

Setup: Windows 11, RTX 3060 Laptop 6 GB, i7 16 threads, 32 GB RAM. U1 (CUSA02320 1.00, plain ELF dump), own
builds of 0.3.8/0.3.9 (red_zone.c, cpu_patch.c and runtime_memory.c unmodified).

| Build | Start-up summary | What happened |
|---|---|---|
| 0.3.8 | 46 left | fine |
| 0.3.8 | 3396 left | (stopped by the old error 1450) |
| 0.3.9 | 3319 left, 50 × "data may be lost" at 0x5811d4 | hang after a checkpoint save |
| 0.3.9 | 3306 left, 51 × at 0x5811d4 | `iSubMesh` assertion loading `chase-3` after a save |
| 0.3.9 | 3317 left, 51 × at 0x5811d4 | read fault at 0x581a90, 2 KiB from the lost data |

The base address can be the same in a good and a bad launch: `red_zone_patch` runs after `bbgpu_init`,
and the GPU driver's allocations sometimes take the ±2 GiB window around the image, so `near_image()` finds
no free 1 MiB block.

**Change.** `red_zone_reserve()`, right after `runtime_image_map` and before `bbgpu_init`, reserves 16 MiB
(MEM_RESERVE only) where the search looked first (64 MiB past the image's end), else right after the image,
else below it; `near_image` commits 1 MiB chunks from it and falls back to the old search only when it is
used up. A log line says how much was used. `BB_RED_ZONE=0` reserves nothing.

**Tested.** Wine, with every free 64 KiB of the image's reach taken after the reservation: 16/16 chunks in
reach (0 without it). Windows 11, U1: four launches in a row with `46 left`, 1 of the 16 MiB used.

---

## PR 2 — probe: Windows fault report names the thread and the caller, writes user/crash.dmp (`0006`)

**Problem.** A crash with a call through a bad pointer printed only registers:
`Fault: execute access to 0000000000000400 at 0000000000000400`, no frames. The rbp chain stops at once when
rbp is not a frame pointer, and the return address at `[rsp]` (the caller) was never read, so it could not
even be told whether it was a guest or a host thread.

**Change.** The report adds the thread (guest thread and fiber names, or "host"), `[rsp]` described as
module+offset / guest offset / red-zone thunk, up to 24 stack slots that point into committed executable
memory, and writes `user/crash.dmp` (indirectly referenced memory and thread info, no full memory: 2–5 MB) from a
helper thread with a 15 s limit, so a lock held by the faulting code costs the dump, never a hung process.
`BB_NO_CRASH_DUMP=1` turns the dump off.

**Tested.** Wine: a call to 0x400 from a thread reports `[rsp] t.exe+0x1486` (the caller) and writes the dump.

---

## PR 3 — run.py: mesh raycaster copies on by default in Uncharted 1 too (`0007`)

U1 stops with the same assertion the fix exists for, right after a checkpoint save:
`ASSERTION: iSubMesh < m_numSubMeshInstances` (GetSubMeshInstance, ndlib/render/ngen/mesh.h:1361), seen
twice on 0.3.8/0.3.9 with *Fixes → Mesh raycaster copies* on Automatic (= off outside U2), once on a clean
red-zone launch. With the fix On, U1 has been played across several sessions and checkpoint saves without
it. Automatic now means on in U1 and U2 (still off in U3, where it costs FPS in Cartagena); explicit On/Off
wins as before. The launcher's text says so. FPS cost in U1 not measured; nothing noticeable on an RTX 3060
Laptop.

---

## Issue (only if it still happens on game version 1.02) — U1 jet-ski: killed by the explosion of your own far-away grenade

Chapter with Elena on the jet-ski, grenade launcher: the player dies right after firing, with full health,
when the grenade explodes far away.

- 12 deaths in 3 sessions: one hit of type 3 for 91 667 (kills at full health), attributed to the player
  as the source, 0.33–0.63 s after firing (the grenade's flight time).
- Not changed by *Exact GPU results* On (3 of 5 runs still died) or *Mesh raycaster copies*; also happens
  with the local game-state callbacks off (`UNDS_STATE=0`, none installed).
- Game version 1.00 (plain ELF dump). [TO FILL: result on 1.02 at the same checkpoint.]
