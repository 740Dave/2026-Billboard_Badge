# Nerd Detector

Counts other Billboard Badges nearby over Bluetooth LE and shows their names on the e-paper.

- **Install:** copy `apps/nerd_detector.py` to `/apps/` on the badge (or use the Badge Manager's *Nerd Detector* tab), then hard-reset.
  ```bash
  mpremote cp apps/nerd_detector.py :/apps/nerd_detector.py + reset
  ```
- **Name (optional):** `/data/nerd.json` → `{"name": "Your Name", "broadcast": true}` (max 16 plain characters; the badge adds a random `id`).
- **Use:** pick *Nerd Detector* in the LED menu. LEDs show badges heard in the last minute (green near, yellow same room, blue far); the screen lists names (redraws at most every ~25 s). **A** redraws now, **B** turns broadcasting on/off.
- Your badge advertises from boot even while you use other apps; it only *scans* while this app is open.

## Protocol (so other apps can join in)

Legacy non-connectable advert, ≤31 bytes:

| Bytes | Meaning |
|---|---|
| `02 01 06` | Flags |
| `len FF` | Manufacturer-specific data |
| `FF FF` | Company ID 0xFFFF (testing) |
| `53 43 32 36` | Magic `SC26` |
| `01` | Version |
| 3 bytes | Random badge id |
| 0–16 bytes | Name, ASCII |

**Privacy:** the name is broadcast publicly to anyone nearby; leave it empty or press B to stop broadcasting.

Tested on MicroPython 1.24.1 (the shipped badge firmware). By 740Dave, Supercon 2026.
