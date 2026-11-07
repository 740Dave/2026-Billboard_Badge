"""App Switcher"""

import asyncio as aio  # type: ignore
import json
import os
import sys
import btree

from apps.base_app import BaseApp
from lib.button import MenuMinus, MenuPlus, UserA, UserB, PRESSED_READ
from lib.led_bar import leds

# MENU_TIMEOUT_MILLISECONDS = 1000


APPMOD_DENYLIST = {
    "__init__",
    "base_app",
    "template",
}

LAST_APP_FILE = "/data/last_app.json"
"""Remembers the last app launched from the menu so it can be resumed at boot"""
BOOT_CONFIG_FILE = "/data/boot.json"
"""Optional. {"resume": false} always starts in the menu"""


class AppSwitcher:
    def __init__(self, initial_idx: int = 0) -> None:
        self.apps = []
        self.app_mods = []  # module name of each app, parallel to self.apps
        self.active_idx = initial_idx
        self.selecting_mode = True
        self.selection_idx = initial_idx

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

        self._resume_last_app()

    def _resume_last_app(self):
        """Start the last app launched from the menu instead of the menu.
        Hold Menu+ or Menu- during boot to get the menu instead.
        Any problem falls back to the menu."""
        if MenuPlus.pin.value() or MenuMinus.pin.value():
            print("Menu button held at boot, showing menu")
            # Don't let the held button also move the menu selection
            MenuMinus._pressed_state = PRESSED_READ
            MenuPlus._pressed_state = PRESSED_READ
            return
        try:
            try:
                with open(BOOT_CONFIG_FILE) as f:
                    if json.load(f).get("resume", True) is False:
                        return
            except OSError:
                pass  # no boot config, resume is on by default
            try:
                with open(LAST_APP_FILE) as f:
                    last = json.load(f)
            except OSError:
                return  # nothing launched yet
            for idx, app in enumerate(self.apps):
                if self.app_mods[idx] == last["module"] and app.name == last["app"]:
                    print(f"Resuming {app.name}")
                    self.active_idx = idx
                    self.selection_idx = idx
                    app.launched = True
                    self.selecting_mode = False
                    return
            print(f"Last app {last} not found, showing menu")
        except Exception as exc:
            print("Could not resume last app, showing menu")
            sys.print_exception(exc)

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

    async def arun(self):
        while True:
            if self.selecting_mode:
               
                ## Display current app from all installed apps
                leds.menu_display(self.selection_idx, len(self.apps))

                if MenuPlus.pressed():
                    self.selection_idx = (self.selection_idx + 1) % len(self.apps)
                    print(
                        f"Selected app {self.selection_idx}: {self.apps[self.selection_idx].name}"
                    )
                if MenuMinus.pressed():
                    self.selection_idx = (self.selection_idx - 1) % len(self.apps)
                    print(
                        f"Selected app {self.selection_idx}: {self.apps[self.selection_idx].name}"
                    )
                if UserA.pressed() or UserB.pressed():
                    print(f"Switching to {self.apps[self.selection_idx].name}")
                    self.active_idx = self.selection_idx
                    self.apps[self.active_idx].launched = True
                    self._save_last_app()
                    leds.clear_sequence()
                    leds.fill((0, 0, 0))
                    leds.write()
                    self.selecting_mode = False
                await aio.sleep_ms(10)
            else:
                if MenuMinus.pressed() or MenuPlus.pressed():
                    self.selection_idx = self.active_idx
                    self.selecting_mode = True
                    ## Reset pressed state so that button doesn't advance menu, just enters menu state
                    MenuMinus._pressed_state = PRESSED_READ
                    MenuPlus._pressed_state = PRESSED_READ
                    print("Activated app switching")
                await self.apps[self.active_idx].arun()
                await aio.sleep_ms(0)
                self.apps[self.active_idx].launched = False


switcher = AppSwitcher()
