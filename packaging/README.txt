UNDS 1.0.0 — DualSense for Uncharted: The Nathan Drake Collection (uncharted_pc port v0.3.9)

USE
  1. This folder lives inside the official port release (uncharted-windows\UNDS\), with the UNDS zip
     extracted over the official v0.3.9 one.
  2. Windows: Sound > Playback > Speakers (Wireless Controller) > Configure > Quadraphonic; volume 100 %.
  3. DualSense over USB. DSX closed.
  4. Play as usual (Uncharted.exe or its shortcuts): UNDS starts with the game in a minimized console
     and closes when it ends (window = hidden: no window; log in logs\unds.log). Double-clicking UNDS.exe also works (it checks memory and DSX first).
     auto = no in unds_launcher.ini: the game no longer starts it by itself.

UNDS STUDIO: double-click "UNDS Studio.cmd" (game closed, controller on USB) to tune triggers, haptics
and light bar in your browser, feeling it on the controller (shooting range with R2).

SETTINGS (reloaded on save): unds_triggers.ini (triggers, light bar), unds_haptics.ini (haptics),
unds_launcher.ini (game folder, checks, variables for the port). Comments are in Spanish.

BLINKING RED LIGHT BAR + BEEP: the port reported a problem (the console says which). After a bad
start-up, close the game and start it again before playing on.

OPTIONS: UNDS.exe --help  (--selftest checks the exe). record = yes in unds_launcher.ini saves each
session in recordings\ for bug reports.

Source: source\ (port patches; bb-probe.exe = tag v0.3.9 + those patches) and the UNDS repository.
License GPL-2.0 (LICENSE.txt). No game files included: you need your own copy.
