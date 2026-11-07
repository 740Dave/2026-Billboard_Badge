"""Artwork app: show your own images from /data/artwork on the e-paper panel.

Controls (while the app is running):
    User A  -> next image
    User B  -> previous image
    Menu +/- -> back to the app menu (handled by the app switcher)

Images must be 960x672 (or smaller) palette PNGs, not interlaced.
Use prepare_artwork.py on your computer to convert any image into that format.

The LED bar shows status: blue while an image loads and the panel refreshes,
green when done, red if /data/artwork is empty or an image fails to load.
"""

import os
import time

from apps.base_app import BaseApp
from lib.button import UserA, UserB
from lib.epd import display
from lib.led_bar import leds

ART_DIR = "/data/artwork"
INDEX_FILE = "/data/artwork_index"  # remembers the last image across reboots

# Seconds between automatic advances. 0 turns slideshow mode off.
# E-paper full refreshes take a while and flash the panel, so keep this long.
AUTO_ADVANCE_S = 0

_BLUE = (0, 0, 2)
_GREEN = (0, 1, 0)
_RED = (2, 0, 0)


class ArtworkApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("Artwork")
        self.sleep_ms = 50
        self.files = []
        self.index = self._load_index()
        self.pending = 0          # net next/previous presses not yet drawn
        self.needs_draw = False
        self.last_draw = 0

    # ---- helpers -------------------------------------------------------
    def _scan(self):
        try:
            names = os.listdir(ART_DIR)
        except OSError:
            names = []
        self.files = sorted(n for n in names if n.lower().endswith(".png") and not n.startswith("."))

    def _load_index(self) -> int:
        try:
            with open(INDEX_FILE) as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _save_index(self):
        try:
            with open(INDEX_FILE, "w") as f:
                f.write(str(self.index))
        except OSError:
            pass

    def _draw(self):
        if not self.files:
            print(f"Artwork: no .png files in {ART_DIR}")
            leds.fill(_RED)
            return
        self.index %= len(self.files)
        path = f"{ART_DIR}/{self.files[self.index]}"
        print(f"Artwork: [{self.index + 1}/{len(self.files)}] {path}")
        leds.fill(_BLUE)
        try:
            display.clear()
            display.draw_png(path)
            display.update()
            self._save_index()
        except Exception as ex:  # bad file: say so, keep the app alive
            print(f"Artwork: could not draw {path}: {ex}")
            leds.fill(_RED)
        self.last_draw = time.time()

    # ---- app loop ------------------------------------------------------
    def run(self):
        if self.launched:
            # Fresh entry from the menu: rescan so newly copied files show up.
            self._scan()
            self.pending = 0
            self.needs_draw = True

        if UserA.pressed():
            self.pending += 1
        if UserB.pressed():
            self.pending -= 1

        if AUTO_ADVANCE_S and self.files and time.time() - self.last_draw >= AUTO_ADVANCE_S:
            self.pending += 1

        # Only touch the framebuffer once the previous refresh has finished;
        # a display.update() issued mid-refresh is silently dropped.
        if not display.is_done_updating():
            return

        if self.pending and self.files:
            self.index = (self.index + self.pending) % len(self.files)
            self.pending = 0
            self.needs_draw = True

        if self.needs_draw:
            self.needs_draw = False
            self._draw()
        elif self.files and leds[0] == _BLUE:
            leds.fill(_GREEN)  # refresh finished
