"""SAO Blink: drive an ESP32-based SAO board from the badge.

The ESP32 on the SAO runs its own firmware (see ../esp32_sao_blinker/).
The badge only powers it (3V3/GND on the SAO header) and sends it commands:

    User A -> send the next blink mode over I2C (address 0x42)
    User B -> toggle the SAO's GPIO1 line (a simple "trigger" wire)

The LED bar shows the current mode (one lit LED per mode number), or red
if nothing answers at 0x42.
"""

import machine

from apps.base_app import BaseApp
from lib.button import UserA, UserB
from lib.led_bar import leds
# Build the SAO bus here instead of `from lib.sao import i2c`: the badge ships MicroPython 1.24,
# and upstream lib/sao.py uses syntax ("stop=True, /") that 1.24 can't import.
i2c = machine.I2C(0, scl=machine.Pin(18), sda=machine.Pin(17), freq=100_000)
SAO2_GPIO1 = 4

SAO_ADDR = 0x42
NUM_MODES = 4               # 0 off, 1 on, 2 slow blink, 3 fast blink (matches the sketch)
TRIGGER_PIN = SAO2_GPIO1    # GPIO4 = SAO2 header, pin GPIO1. Change if you plug in elsewhere.


class SaoBlinkApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("SAO Blink")
        self.sleep_ms = 50
        self.mode = 0
        self.present = False
        self.trigger = machine.Pin(TRIGGER_PIN, machine.Pin.OUT, value=0)

    def _show(self):
        if not self.present:
            leds.fill((2, 0, 0))
            return
        vals = [(0, 0, 0)] * 8
        for i in range(self.mode + 1):
            vals[i] = (0, 1, 1)
        leds.set(vals)
        leds.write()

    def _send(self):
        try:
            i2c.writeto(SAO_ADDR, bytes([self.mode]))
            self.present = True
        except OSError:  # ENODEV/ETIMEDOUT: nothing acknowledged
            self.present = False
        print(f"SAO Blink: mode {self.mode}, present={self.present}")

    def run(self):
        if self.launched:
            found = i2c.scan()
            print("SAO Blink: I2C devices found:", [hex(a) for a in found])
            self.present = SAO_ADDR in found
            if self.present:
                try:
                    self.mode = i2c.readfrom(SAO_ADDR, 1)[0] % NUM_MODES
                except OSError:
                    pass
            self._show()

        if UserA.pressed():
            self.mode = (self.mode + 1) % NUM_MODES
            self._send()
            self._show()

        if UserB.pressed():
            self.trigger.value(not self.trigger.value())
            print("SAO Blink: trigger line ->", self.trigger.value())
