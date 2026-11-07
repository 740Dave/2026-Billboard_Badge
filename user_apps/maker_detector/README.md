# Maker Detector

A Billboard Badge (Hackaday Supercon 2026) app that finds other badges nearby over
Bluetooth LE and lists their names, with a rough distance for each one.

This is the renamed and hardened version of the earlier "Nerd Detector" app. The radio
format is unchanged, so badges running either version see each other.

## Install

Copy `apps/maker_detector.py` into `/apps/` on the badge and reboot. The app switcher
finds it on its own. That one file is all it needs: the Hackaday logo on its screen is
built into it (no image file to copy).

**Menu position:** the app files are discovered alphabetically. With the stock apps it
goes `gallery`, **`maker_detector`**, `nametag`, `schedule`, so it is **#2** on the LED
menu. By display name it also comes second: Gallery, Maker Detector, Nametag, Sat Talks,
Sun Talks, Workshops.

## Use

| Control | Action |
|---|---|
| User A | redraw the screen now |
| User B | turn broadcasting on/off (saved) |

- **Screen:** the Hackaday "Jolly Wrencher" logo top left, the red MAKER DETECTOR title
  beside it, then your own line ("Broadcasting as: dave  •  makes robots"; the makes
  part is left out when you haven't set one), how many
  other makers are in range, and up to 16 names, each with near / same room / far and the
  RSSI. The footer shows about how many Bluetooth devices were heard in the last scan burst.
- **LED bar:** one LED per badge seen in the last minute (green = near, yellow = same room,
  blue = far). While no badges are found, LED 1 blinks red. The bar turns blue while the
  e-paper refreshes (about 30 s). The screen redraws at most once every 25 s.
- **Advertising** starts at boot, so other badges can see you while you use a different app.
  **Scanning** only runs while this app is open.

## The logo

The 92x92 Jolly Wrencher is stored in `maker_detector.py` as a 1-bit bitmap (1,104 bytes,
hex). After LVGL has drawn the screen into the e-paper framebuffers, the app copies the
logo into the black framebuffer with `framebuf.blit()` and then starts the panel refresh.
This needs no PNG file and no PNG decoding, and uses no LVGL memory. If the firmware has no
`display.black` framebuffer, or drawing fails for any other reason, the screen appears
without the logo and the app keeps running. The source image is in
`assets/jolly_wrencher_src.png`, for reference only; nothing reads it on the badge.

Your own line ("Broadcasting as ...") starts right of the logo, so it has about 800 px:
up to 60 characters at 24 px, otherwise 16 px. Lines over 88 characters are cut off with
"...".

## With or without `lib/presence.py`

The upstream "BLE presence broadcast" firmware change adds `lib/presence.py`, which
advertises the badge in exactly this app's over-the-air format. The app checks for it at
import time (a missing or broken module is fine):

| | `lib/presence.py` installed | not installed (stock firmware) |
|---|---|---|
| who advertises | `lib/presence.py` (the app never advertises, so there is only one advert) | the app itself, from boot |
| badge id | `presence.badge_id()` (it adopts the id from `maker.json` on first use) | `maker.json` `id` |
| User B | `presence.set_broadcast(not presence.enabled())`, saved to `owner.json` by presence | flips the flag, saved to `owner.json` `"broadcast"` (other keys kept) **and** `maker.json` |
| parsing adverts | `presence.parse()`, with the app's own parser as fallback | the app's own parser |

Scanning is always done by the app. Each time the app is opened it re-reads the profile
(and calls `presence.refresh()` when presence is installed), so changes made with the
Badge Manager show up without a reboot.

## Settings

Identity comes from the owner profile first, `/data/owner.json` (written by the Badge
Manager; read through `lib/owner.py` when the firmware has it):

```json
{"maker_handle": "dave", "makes": "robots", "broadcast": true}
```

Anything missing there falls back to the legacy `/data/maker.json`:

```json
{"id": "a1b2c3", "name": "Dave", "makes": "robots", "broadcast": true}
```

| value | from | meaning |
|---|---|---|
| handle | `owner.json` `maker_handle`, else `maker.json` `name` | Shown on your screen; broadcast clipped to 16 printable ASCII characters. Empty = "badge a1b2c3". |
| makes | `owner.json` `makes` (if the key exists, even empty), else `maker.json` `makes` | At most 40 printable ASCII characters. **Shown only on your own screen and never broadcast**, because the 31-byte advert has no room left. |
| broadcast | `owner.json` `broadcast` (if true/false), else `maker.json` `broadcast` (default true) | Toggled with User B. |
| id | `presence.badge_id()`, else `maker.json` `id` | 6 hex characters, random, created on first boot. Identifies your badge to others. |

Invalid or missing values in `maker.json` are fixed and saved again on boot.

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
