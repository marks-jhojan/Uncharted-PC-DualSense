# Changelog — Uncharted PC DualSense (UNDS)

## 1.0.0 — 2026-10-26

First public release, for uncharted_pc v0.3.9.

- Port patches 0001–0008 (event bus, trigger thresholds, U1 game state, SDL control channel, red zone
  thunk reservation, fault report with crash dump, mesh raycaster copies on in U1, run.py starts UNDS).
- `UNDS.exe` (no Python needed) starts with the game however it is started, in a minimized console (or
  hidden, logging to `UNDS\logs`), and ends with it; it watches the port's log and warns with a blinking red light bar and a beep. Opened by
  hand it also checks memory, an old game left open and DSX before opening the port's launcher.
- U1 game-state hooks verified on game versions 1.00 and 1.02.
- UNDS Studio (`UNDS Studio.cmd` / `UNDS.exe --studio`): visual tuning in the browser with live triggers,
  a shooting range on the real controller, recoil timeline, haptic voice editor with waveform,
  directional-hit compass, light bar and an editor for every parameter; changes go straight to the
  `.ini` files, validated, with a backup.
- Adaptive triggers per weapon class (U1), mounted weapons (jeep, jet-ski), haptics from rumble plus
  per-shot, directional hit and death voices, light bar with health and damage.
