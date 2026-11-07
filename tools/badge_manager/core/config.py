"""Persistent settings (~/.badge_manager/config.json) and secrets (macOS Keychain via keyring)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

HOME = Path.home() / ".badge_manager"
CONFIG_FILE = HOME / "config.json"
KEYRING_SERVICE = "badge-manager"

# This file lives at <repo>/tools/badge_manager/core/config.py. When run from a clone of the
# badge repo (or a fork of it), use that clone as the git cache; otherwise keep one in HOME.
REPO_ROOT = Path(__file__).resolve().parents[3]
_IN_REPO = (REPO_ROOT / ".git").exists() and (REPO_ROOT / "firmware" / "badge").is_dir()
_DEFAULT_REPO = REPO_ROOT if _IN_REPO else HOME / "repo"
# Each user_apps/<name>/apps folder mirrors the badge's /apps (the Apps tab scans *.py there).
_LOCAL_APPS = [d / "apps" if (d / "apps").is_dir() else d
               for d in (REPO_ROOT / "user_apps" / n for n in ("artwork", "sao_blink", "maker_detector", "maker_tag"))]

DEFAULTS = {
    "port": None,                      # last used serial port
    "repo_dir": str(_DEFAULT_REPO),
    "backup_dir": str(HOME / "backups"),
    "artwork_cache": str(HOME / "artwork"),
    "local_app_dirs": [str(d) for d in _LOCAL_APPS if d.is_dir()],
    "appearance": "Dark",              # System / Light / Dark
    "backup_before_push": True,
    "wifi_profiles": [],               # [{"id","name","ssid","enabled","notes"}]; passwords live in keyring
}


class Config:
    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.data = dict(DEFAULTS)
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except ValueError:
                pass
        self._migrate()

    def _migrate(self) -> None:
        """One-time upgrades of settings saved by older versions."""
        if "ui_version" not in self.data:
            # Before ui_version 2 the default appearance was "System"; the app now defaults to Dark.
            had_file = self.path.exists()
            if self.data.get("appearance") == "System":
                self.data["appearance"] = "Dark"
            self.data["ui_version"] = 2
            if had_file:
                try:
                    self.save()
                except OSError:
                    pass
        p = self.data.get("profile")
        if isinstance(p, dict) and "name" in p and "maker_handle" not in p:
            # Profiles before the owner.json change stored the handle as "name". Move it once;
            # every other key (incl. legacy display_name / broadcast_text) is kept as is.
            p = dict(p)
            p["maker_handle"] = p.pop("name")
            self.data["profile"] = p
            if self.path.exists():
                try:
                    self.save()
                except OSError:
                    pass

    def get(self, key, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value) -> None:
        with self._lock:
            self.data[key] = value
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)


# --------------------------------------------------------------- secrets
def get_secret(name: str) -> str | None:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception:
        return None


def set_secret(name: str, value: str | None) -> bool:
    """Store (or with value=None delete) a secret. Returns False if no keyring backend works."""
    try:
        import keyring
        if value is None:
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
        else:
            keyring.set_password(KEYRING_SERVICE, name, value)
        return True
    except Exception:
        return False
