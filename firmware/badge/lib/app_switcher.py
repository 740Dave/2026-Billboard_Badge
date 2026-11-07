"""App Switcher"""

import asyncio as aio  # type: ignore
import json
import os
import sys
import time
import btree

from apps.base_app import BaseApp
from lib.button import MenuMinus, MenuPlus, UserA, UserB, PRESSED_READ, PRESSED_UNREAD, UNPRESSED
from lib.epd import display
from lib.led_bar import leds, NUM_LEDS
import lib.app_grid as app_grid

# MENU_TIMEOUT_MILLISECONDS = 1000


APPMOD_DENYLIST = {
    "__init__",
    "base_app",
    "template",
}

LAST_APP_FILE = "/data/last_app.json"
"""Remembers the last app launched from the menu so it can be resumed at boot"""
BOOT_CONFIG_FILE = "/data/boot.json"
"""Optional. {"resume": false} turns resume off,
{"pinned": {"module": ..., "app": ...}} always boots that app"""

CHORD_MS = 150
RELEASE_QUIET_MS = 80  # after a chord, all buttons must stay up this long
"""Buttons pressed within this many ms of each other count as pressed together"""
CHORD_GRID = "grid"
CHORD_PIN = "pin"


class Chord:
    """Tells single button presses from Menu+ & Menu- together (CHORD_GRID)
    and all four buttons together (CHORD_PIN).

    Presses are held back until it's clear which one it is: until the buttons are
    released or CHORD_MS has passed. Then poll() returns CHORD_GRID, CHORD_PIN or
    the list of buttons that were pressed on their own. After a chord nothing more
    is reported until all buttons are released.

    Outside the menu only Menu buttons start a chord: User A and B belong to the app.
    They are only held back from the app once a Menu button is down."""

    def __init__(self):
        self.buttons = (MenuPlus, MenuMinus, UserA, UserB)
        self.members = []
        self.start = 0
        self.latched = False
        self.released_at = None  # when all buttons were last seen up while latched

    @staticmethod
    def _held(b):
        return b._pressed_state != UNPRESSED

    @staticmethod
    def _onset(b, now):
        return now if b._press_onset_ms is None else b._press_onset_ms

    def _take(self, b):
        self.members.append(b)
        b._pressed_state = PRESSED_READ  # hide the press from everyone else

    def poll(self, in_menu):
        now = time.ticks_ms()
        if self.latched:
            # A chord happened: swallow everything until all buttons have been up for
            # RELEASE_QUIET_MS. lib/button.py doesn't debounce, so contacts bouncing as
            # four fingers lift would otherwise look like a fresh four-button chord.
            any_held = False
            for b in self.buttons:
                if self._held(b):
                    b._pressed_state = PRESSED_READ
                    any_held = True
            if any_held:
                self.released_at = None
            elif self.released_at is None:
                self.released_at = now
            elif time.ticks_diff(now, self.released_at) >= RELEASE_QUIET_MS:
                self.latched = False
                self.released_at = None
            return None

        fresh = [b for b in self.buttons if b._pressed_state == PRESSED_UNREAD]
        if not self.members:
            starters = [b for b in fresh if in_menu or b in (MenuPlus, MenuMinus)]
            if not starters:
                return None
            self.start = self._onset(starters[0], now)
            for b in starters:
                if time.ticks_diff(self.start, self._onset(b, now)) > 0:
                    self.start = self._onset(b, now)
            # Buttons that went down just before (e.g. a User button the app saw) count too
            for b in self.buttons:
                if self._held(b) and abs(time.ticks_diff(self._onset(b, now), self.start)) <= CHORD_MS:
                    if time.ticks_diff(self._onset(b, now), self.start) < 0:
                        self.start = self._onset(b, now)
                    self._take(b)
        else:
            for b in fresh:
                if b not in self.members and time.ticks_diff(self._onset(b, now), self.start) <= CHORD_MS:
                    self._take(b)

        members = self.members
        held = False
        for b in members:
            if self._held(b):
                held = True
        if len(members) == len(self.buttons):
            result = CHORD_PIN
        elif held and time.ticks_diff(now, self.start) < CHORD_MS:
            return None  # wait and see whether more buttons join
        elif MenuPlus in members and MenuMinus in members:
            result = CHORD_GRID
        else:
            result = members
        if result == CHORD_GRID or result == CHORD_PIN:
            self.latched = True       # always wait for a quiet release, even if already up
            self.released_at = None
            print("Buttons pressed together:", result)
        self.members = []
        return result


class AppSwitcher:
    def __init__(self, initial_idx: int = 0) -> None:
        self.apps = []
        self.app_mods = []  # module name of each app, parallel to self.apps
        self.active_idx = initial_idx
        self.selecting_mode = True
        self.selection_idx = initial_idx
        self._chord = Chord()
        self._grid_pending = False  # draw the app grid once the display is free
        self._grid_shown = False
        self._drawn_ms = None  # when the switcher last handed a frame to the display

        # Discover and load apps
        app_mods = []
        for filename, _, _, _ in os.ilistdir("/apps"):  # pyright: ignore[reportAssignmentType]
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            app_modname = filename.split(".")[0]
            app_mods.append(app_modname)
        if app_mods:
            print(f"Found app mods: {app_mods}")
            with open("/data/user_apps.py", "w") as out:
                out.write(f"from apps import {', '.join(app_mods)}\n")

        import data.user_apps as apps  # pyright: ignore[reportMissingImports]
        apps_list = [app for app in dir(apps) if "__" not in app]
        print(f"App module list: {apps_list}")
        for appmod_name in apps_list:
            if appmod_name.startswith("_") or appmod_name in APPMOD_DENYLIST:
                continue
            appmod = getattr(apps, appmod_name)
            try:
                for modattr in appmod.__dict__.values():
                    # Subclasses of an app class count too. _abstract skips the
                    # shared base so only the concrete apps are constructed.
                    if not isinstance(modattr, type) or not issubclass(modattr, BaseApp):
                        continue
                    if modattr is BaseApp or modattr.__dict__.get("_abstract"):
                        continue
                    app = modattr()
                    self.apps.append(app)
                    self.app_mods.append(appmod_name)
                    print(f"Added app: {app.name}")
            except Exception as exc:
                print(f"Failed to load app module: {appmod}")
                sys.print_exception(exc)

        try:
            apps_file = open("/data/app_list", "r+b")
        except OSError:
            apps_file = open("/data/app_list", "w+b")
        # Now open a database itself
        apps_db = btree.open(apps_file)
        for idx, app in enumerate(self.apps):
            apps_db[bytes([idx])] = bytes(app.name, "UTF-8")
        apps_db.flush()
        apps_db.close()
        apps_file.close()

        self._boot()

    @staticmethod
    def _read_json(path):
        """The dict in a JSON file. None if the file is missing, ValueError if it's not a dict."""
        try:
            with open(path) as f:
                data = json.load(f)
        except OSError:
            return None
        if not isinstance(data, dict):
            raise ValueError(path + " is not a JSON object")
        return data

    def _find(self, entry):
        """Index of the app saved as {"module": ..., "app": ...}, or -1"""
        if isinstance(entry, dict):
            for idx, app in enumerate(self.apps):
                if self.app_mods[idx] == entry.get("module") and app.name == entry.get("app"):
                    return idx
        return -1

    def _pinned_idx(self):
        try:
            return self._find((self._read_json(BOOT_CONFIG_FILE) or {}).get("pinned"))
        except Exception:
            return -1

    def _boot(self):
        """Choose what to start with. In order:
        a Menu button held down at boot: the app grid
        the pinned boot app, if any
        the last app launched from the menu, unless "resume" is false
        otherwise the app grid. Any problem falls back to the app grid."""
        if MenuPlus.pin.value() or MenuMinus.pin.value():
            print("Menu button held at boot, showing the app grid")
            # Don't let the held button also move the menu selection
            MenuMinus._pressed_state = PRESSED_READ
            MenuPlus._pressed_state = PRESSED_READ
        else:
            try:
                config = self._read_json(BOOT_CONFIG_FILE) or {}
                choices = [("pinned", config.get("pinned"))]
                if config.get("resume", True) is not False:
                    choices.append(("last", self._read_json(LAST_APP_FILE)))
                for why, entry in choices:
                    idx = self._find(entry)
                    if idx >= 0:
                        print(f"Starting {why} app {self.apps[idx].name}")
                        self.active_idx = idx
                        self.selection_idx = idx
                        self.apps[idx].launched = True
                        self.selecting_mode = False
                        return
                    if entry is not None:
                        print(f"The {why} app {entry} was not found")
            except Exception as exc:
                print("Could not read the boot settings, showing the app grid")
                sys.print_exception(exc)
        self._grid_pending = True

    def _save_last_app(self):
        """Remember the active app. Only writes to flash when it changed."""
        last = {"module": self.app_mods[self.active_idx], "app": self.apps[self.active_idx].name}
        try:
            try:
                with open(LAST_APP_FILE) as f:
                    if json.load(f) == last:
                        return
            except (OSError, ValueError):
                pass
            with open(LAST_APP_FILE, "w") as f:
                json.dump(last, f)
        except Exception as exc:
            print("Could not save last app")
            sys.print_exception(exc)

    def _toggle_pin(self, idx):
        """Make the app the boot app, or stop it being the boot app if it already is.
        Keeps any other settings in boot.json. Returns True if it's pinned now."""
        target = {"module": self.app_mods[idx], "app": self.apps[idx].name}
        try:
            config = self._read_json(BOOT_CONFIG_FILE) or {}
        except Exception:
            print("boot.json was not valid, starting a new one")
            config = {}
        pinned = config.get("pinned") != target
        if pinned:
            config["pinned"] = target
        else:
            config.pop("pinned", None)
        with open(BOOT_CONFIG_FILE, "w") as f:
            json.dump(config, f)
        print("Pinned" if pinned else "Unpinned", "boot app", target)
        return pinned

    async def _flash(self, color):
        """Blink the LED bar, then put back what was there"""
        paused = leds.sequence_paused
        leds.pause()
        saved = [tuple(leds[i]) for i in range(NUM_LEDS)]
        for _ in range(3):
            leds.fill(color)
            await aio.sleep_ms(150)
            leds.fill((0, 0, 0))
            await aio.sleep_ms(100)
        leds.set(saved)
        leds.write()
        if not paused:
            leds.unpause()

    def _display_idle(self):
        """Is the display free for a new frame? A frame handed over while the display
        is still busy with the previous one is lost, so wait for that."""
        if self._drawn_ms is not None and time.ticks_diff(time.ticks_ms(), self._drawn_ms) < 1000:
            return False  # the display task may not have picked up our frame yet
        return display.is_done_updating() and not display._framebuffer_ready.is_set()

    def _enter_menu(self):
        self.selection_idx = self.active_idx
        self.selecting_mode = True
        print("Activated app switching")

    async def _launch(self):
        while not self._display_idle():
            await aio.sleep_ms(50)  # the grid is still being shown
        print(f"Switching to {self.apps[self.selection_idx].name}")
        self.active_idx = self.selection_idx
        self.apps[self.active_idx].launched = True
        self._save_last_app()
        self._grid_pending = False
        self._grid_shown = False
        leds.clear_sequence()
        leds.fill((0, 0, 0))
        leds.write()
        self.selecting_mode = False

    async def _buttons(self):
        in_menu = self.selecting_mode
        pressed = self._chord.poll(in_menu)
        if pressed is None:
            return
        if pressed == CHORD_GRID:
            if not in_menu:
                self._enter_menu()
            self._grid_pending = True
        elif pressed == CHORD_PIN:
            idx = self.selection_idx if in_menu else self.active_idx
            try:
                now_pinned = self._toggle_pin(idx)
            except Exception as exc:
                print("Could not save the boot app")
                sys.print_exception(exc)
                await self._flash((1, 1, 0))
                return
            await self._flash((0, 4, 0) if now_pinned else (4, 0, 0))
            if self._grid_shown:
                self._grid_pending = True  # the BOOT mark moved
        elif not in_menu:
            # Menu+ or Menu- on its own: open the LED menu without moving the selection
            self._enter_menu()
        else:
            launch = False
            for b in pressed:
                if b is MenuPlus:
                    self.selection_idx = (self.selection_idx + 1) % len(self.apps)
                elif b is MenuMinus:
                    self.selection_idx = (self.selection_idx - 1) % len(self.apps)
                else:
                    launch = True
                    continue
                print(f"Selected app {self.selection_idx}: {self.apps[self.selection_idx].name}")
            if launch:
                await self._launch()

    def _draw_grid(self):
        try:
            names = [app.name for app in self.apps]
            app_grid.draw(names, self._pinned_idx(), self.selection_idx)
            self._drawn_ms = time.ticks_ms()
            self._grid_shown = True
        except Exception as exc:
            print("Could not draw the app grid")
            sys.print_exception(exc)

    async def arun(self):
        while True:
            if self.selecting_mode:
               
                ## Display current app from all installed apps
                leds.menu_display(self.selection_idx, len(self.apps))

                if self._grid_pending and self._display_idle():
                    self._grid_pending = False
                    self._draw_grid()
                await self._buttons()
                await aio.sleep_ms(10)
            else:
                ## Menu buttons (and chords) still work while an app runs
                await self._buttons()
                await self.apps[self.active_idx].arun()
                await aio.sleep_ms(0)
                self.apps[self.active_idx].launched = False


switcher = AppSwitcher()
