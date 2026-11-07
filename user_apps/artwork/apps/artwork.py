"""Artwork app: show your own images from /data/artwork on the e-paper panel.

Controls (while the app is running):
    User A  -> next image
    User B  -> previous image
    Menu +/- -> back to the app menu (handled by the app switcher)

With two or more images the app also advances to the next one every
ROTATE_S seconds (5 minutes), counted from when the previous refresh
finished. /data/artwork is rescanned before each automatic advance, so new
uploads join the rotation and deleted files are skipped.

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

# Seconds between automatic advances (only with 2+ images). 0 turns it off.
# E-paper full refreshes take a while and flash the panel, so keep this long.
ROTATE_S = 300

_BLUE = (0, 0, 2)
_GREEN = (0, 1, 0)
_RED = (2, 0, 0)


class ArtworkApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        super().__init__("Artwork")
        self.sleep_ms = 50
        self.files = []
        self.index = self._load_index()
        self.saved_index = self.index  # what is in INDEX_FILE (limits flash writes)
        self.pending = 0          # net next/previous presses not yet drawn
        self.needs_draw = False
        self.drawing = False      # a refresh was started and has not finished yet
        self.draw_ok = False
        self.last_done = time.ticks_ms()  # when the last refresh finished

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
        if self.index == self.saved_index:
            return
        try:
            with open(INDEX_FILE, "w") as f:
                f.write(str(self.index))
            self.saved_index = self.index
        except OSError:
            pass

    def _draw(self):
        self.drawing = True
        self.draw_ok = False
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
            self.draw_ok = True
        except Exception as ex:  # bad file: say so, keep the app alive
            print(f"Artwork: could not draw {path}: {ex}")
            leds.fill(_RED)

    def _rotate(self):
        """Timer fired: rescan and step to the next image (if there are 2+)."""
        current = None
        if self.files:
            current = self.files[self.index % len(self.files)]
        self._scan()
        if len(self.files) < 2:
            return False  # 0 or 1 image: nothing to rotate to, leave the panel alone
        if current in self.files:
            self.index = self.files.index(current) + 1
        else:
            # The shown file was deleted (or none was shown): go to the first
            # file that sorts after it, wrapping to the start.
            self.index = 0
            if current is not None:
                for i, n in enumerate(self.files):
                    if n > current:
                        self.index = i
                        break
        self.index %= len(self.files)
        return True

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

        # Only touch the framebuffer once the previous refresh has finished;
        # a display.update() issued mid-refresh is silently dropped.
        if not display.is_done_updating():
            return

        now = time.ticks_ms()
        if self.drawing:
            # Refresh just finished: the rotation timer counts from here.
            self.drawing = False
            self.last_done = now
            if self.draw_ok:
                leds.fill(_GREEN)

        if self.pending and self.files:
            self.index = (self.index + self.pending) % len(self.files)
            self.pending = 0
            self.needs_draw = True

        if (not self.needs_draw and ROTATE_S
                and time.ticks_diff(now, self.last_done) >= ROTATE_S * 1000):
            self.last_done = now  # restart the timer whether or not we rotate
            if self._rotate():
                self.needs_draw = True

        if self.needs_draw:
            self.needs_draw = False
            self._draw()
