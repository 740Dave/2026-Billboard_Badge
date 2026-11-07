# SAO Blink

A worked example of driving an ESP32-based SAO from the badge.

- `esp32_sao_blinker/esp32_sao_blinker.ino` runs on the add-on ESP32 (Arduino IDE, "esp32 by
  Espressif Systems" core 3.x). It is an I2C target at `0x42`: write 1 byte to set the mode
  (0 off, 1 on, 2 slow blink, 3 fast blink), read 1 byte to get it. A rising edge on its
  `TRIGGER_PIN` also advances the mode. Defaults suit an ESP32-C3 SuperMini (LED on GPIO8).
  Flash it over the ESP32's own USB while it is **not** plugged into the badge.
- `apps/sao_blink.py` runs on the badge and shows up in the menu as **SAO Blink**.
  **User A** sends the next mode over I2C (the LED bar shows the mode; red = nothing at 0x42),
  **User B** toggles SAO2 GPIO1 (badge GPIO4) as a trigger line.

## Wiring (ESP32-C3 SuperMini defaults)

| Badge SAO header | ESP32-C3 |
|---|---|
| 3V3 | 3V3 |
| GND | GND |
| SDA (badge GPIO17) | GPIO6 |
| SCL (badge GPIO18) | GPIO7 |
| GPIO1 (SAO2 = badge GPIO4) | GPIO5 |

Add 4.7 kOhm pull-ups on SDA and SCL if your board doesn't have them. Change the `#define`s in
the sketch (and `TRIGGER_PIN` in the app) for other boards or headers.

## MicroPython 1.24 note

Shipped badges run MicroPython 1.24. Don't `import lib/sao.py` (`from lib.sao import ...`): it uses
syntax (`stop=True, /`) that 1.24 can't parse. The app builds the bus itself:
`machine.I2C(0, scl=machine.Pin(18), sda=machine.Pin(17), freq=100_000)`.

## Install

```bash
mpremote resume cp user_apps/sao_blink/apps/sao_blink.py :/apps/sao_blink.py + reset
```

or install it from the [Badge Manager](../../tools/badge_manager/README.md) **Apps** tab (local app folders).

Credit: 740Dave
