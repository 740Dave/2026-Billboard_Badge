# Maker Detector

A Billboard Badge (Hackaday Supercon 2026) app that finds other badges nearby over
Bluetooth LE and lists their names, with a rough distance for each one.

This is the renamed and hardened version of the earlier "Nerd Detector" app. The radio
format is unchanged, so badges running either version see each other.

## Install

Copy `apps/maker_detector.py` into `/apps/` on the badge and reboot. The app switcher
finds it on its own.

**Menu position:** the app files are discovered alphabetically. With the stock apps it
goes `gallery`, **`maker_detector`**, `nametag`, `schedule`, so it is **#2** on the LED
menu. By display name it also comes second: Gallery, Maker Detector, Nametag, Sat Talks,
Sun Talks, Workshops.

## Use

| Control | Action |
|---|---|
| User A | redraw the screen now |
| User B | turn broadcasting on/off (saved) |

- **Screen:** shows your own line ("Broadcasting as: Dave  •  makes robots"), how many
  other makers are in range, and up to 16 names, each with near / same room / far and the
  RSSI. The footer shows about how many Bluetooth devices were heard in the last scan burst.
- **LED bar:** one LED per badge seen in the last minute (green = near, yellow = same room,
  blue = far). While no badges are found, LED 1 blinks red. The bar turns blue while the
  e-paper refreshes (about 30 s). The screen redraws at most once every 25 s.
- **Advertising** starts at boot, so other badges can see you while you use a different app.
  **Scanning** only runs while this app is open.

## Settings: `/data/maker.json`

```json
{"id": "a1b2c3", "name": "Dave", "makes": "robots", "broadcast": true}
```

| key | meaning |
|---|---|
| `id` | 6 hex characters, random, created on first boot. Identifies your badge to others. |
| `name` | Broadcast to other badges. Printable ASCII, at most 16 characters. |
| `makes` | What you like to make. At most 40 printable ASCII characters. **Shown only on your own screen and never broadcast**, because the 31-byte advert has no room left. |
| `broadcast` | `true` / `false`. Toggled with User B. |

Invalid or missing values are fixed and saved again on boot.

**Migration:** if `/data/maker.json` does not exist but `/data/nerd.json` (the old app's
file) does, `id`, `name` and `broadcast` are copied over. Because the `id` stays the same,
other badges keep recognising you. `nerd.json` is not deleted.

## Over-the-air format (unchanged)

```
02 01 06                       flags: LE General Discoverable, no BR/EDR
LL FF  FF FF  'S' 'C' '2' '6'  01  ID ID ID  name...
       company 0xFFFF  magic  ver  3-byte id  0-16 ASCII
```

The advert is at most 31 bytes, non-connectable, and sent every 400 ms.

## Hardening (why the code looks the way it does)

On a second badge, running the old app at a conference (about 54 BLE devices heard in 3 s,
phones rotating random addresses) stopped USB from responding: no REPL output and Ctrl-C was
ignored. The screen still showed "No other badges yet", and the other broadcasting badge was
never detected. The cause has not been confirmed. This version guards against each
plausible cause:

1. **Minimal IRQ handler.** The handler runs once for every advert received. It now does
   only integer indexing on the memoryviews it is given. It does no dict work, no
   `time.time()`, no printing and no BLE calls. Non-matching adverts are rejected early:
   anything longer than 31 bytes or too short is dropped, then the AD structures are walked
   comparing bytes in place to find `FF FF 'S' 'C' '2' '6'`. Only a matching advert
   allocates anything: one small `bytes` pushed onto a queue capped at 32 entries. Names are
   parsed and peers are updated later, in `run()`.
2. **Bounded memory.** The old `noise` dict (every BLE address to a timestamp, filled from
   the IRQ) is replaced by a 1024-bit bitmap of address hashes, cleared each scan burst. It
   gives an approximate distinct-device count and allocates nothing. Peers are capped at 64,
   and the oldest is evicted when the cap is reached.
3. **Lower scan duty.** The scan now listens for 30 ms every 100 ms (was 60 ms every 100 ms),
   in 8 s bursts. A new burst starts only after the IRQ reports scan-done.
4. **Scan watchdog.** If no scan-done arrives within `SCAN_MS + 5 s`, `run()` calls
   `gap_scan(None)` and starts a new burst. On re-entry after being away, a burst that
   should already be over is stopped straight away.
5. **Stopping when the app is left.** The framework (`apps/base_app.py`,
   `lib/app_switcher.py`) has no "app left" hook. The switcher just stops calling `run()`
   for the inactive app. So no new burst is started once you leave, and the current burst
   ends within 8 s.
6. **Everything wrapped.** The IRQ body is wrapped in try/except, and so are scan, drain,
   advertise and config I/O. A bad advert or a BLE error cannot kill the loop.
7. **Yielding.** `run()` always returns quickly. `BaseApp.arun()` then awaits `sleep_ms`
   (200 ms), so the button, display and LED tasks and USB/REPL get CPU time.

### Most likely cause of the freeze

Ctrl-C being ignored means the MicroPython VM was not running bytecode at all. A Python
exception would have been printed, and KeyboardInterrupt is not caught by
`BaseApp.arun()`'s `except Exception`. So the badge was stuck below Python, or the CPU was
starved. The most likely cause is **an advert flood combined with too heavy an IRQ
handler**:

- The old app scanned 60% of the time with no duplicate filtering. In a hall full of phones
  that means hundreds of scan results per second.
- For each result, the old handler allocated memory (`bytes(addr)`, `bytes(adv)`, slices,
  a tuple) and inserted into the `noise` dict. Rotating random addresses kept that dict
  growing until the next prune.
- On the ESP32 port, the NimBLE host task copies every result into a ring buffer inside a
  critical section, then schedules the Python callback. If the callback can't keep up, the
  VM spends nearly all its time in BLE callbacks and garbage collection, and the NimBLE
  tasks keep the CPU busy. The USB/TinyUSB task, which is also where Ctrl-C is detected,
  can then be starved. That matches "no output, Ctrl-C ignored".

**Why the other badge was never seen** fits the same picture. When the ring buffer
overflows, events are dropped, and that can include `_IRQ_SCAN_DONE`. The old app only
started a new scan after that event, or after leaving and returning to the app. Once
scan-done was lost, it never scanned again and the screen stayed at "No other badges yet".
The e-paper keeps its last image even when the badge is hung, so that screen does not
tell us when it froze.

The fixes above (cheap rejection, no allocation for non-matches, lower duty cycle,
bounded state, watchdog re-arm) address both parts. If the freeze still happens with this
version, the next suspect is the NimBLE stack itself, for example a host-task deadlock
while advertising and scanning at the same time. A quick test is to set
`"broadcast": false` and see whether the hang goes away.

## Notes

- Requires a MicroPython 1.24 build with `bluetooth` (NimBLE). It is written for
  MicroPython 1.24: no positional-only parameters, no f-strings, and `ubinascii`.
- The separator dot is U+2022 (bullet), which is part of LVGL's built-in Montserrat
  fonts. Your own line drops to the 16 px font when it is long, so it stays on one row.

By 740Dave, Hackaday Supercon 2026. Written with Claude.
