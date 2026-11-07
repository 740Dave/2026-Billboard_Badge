"""FakeBadgeService: a simulated badge for testing the GUI without hardware.

Enable with:  BADGE_FAKE=1 .venv/bin/python app.py
The "badge filesystem" is a temp folder seeded from upstream firmware/badge (origin/main).
Same public methods as BadgeService. exec_py only simulates the /data/app_list read (LED-menu
order, rebuilt at every reset()); otherwise it returns "". exec_json raises. info() is simulated. Slight delays mimic the real serial link.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import threading
import time
from pathlib import Path

from .badge import BadgeError, FileEntry, PortInfo


class FakeBadgeService:
    FAKE_PORT = "/dev/cu.FAKE-badge"

    def __init__(self, log=print, repo_dir=None, delay: float = 0.05):
        self.port = self.FAKE_PORT
        self.log = log
        self.lock = threading.RLock()
        self.delay = delay
        self.root = Path(tempfile.mkdtemp(prefix="fakebadge-"))
        if repo_dir:
            from .sources import SourceManager
            try:
                SourceManager(repo_dir).export_badge_tree("origin/main", self.root)
            except Exception as e:  # noqa: BLE001
                log(f"fake badge: could not seed from repo: {e}")
        (self.root / "data").mkdir(exist_ok=True)
        # Simulated /data/app_list (LED-menu order the app switcher writes at boot): names by 0-based
        # slot, or None = file missing. Rebuilt on every reset(); tests may set it directly.
        self.app_list: list[str] | None = None
        self._boot_app_list()
        log(f"fake badge filesystem at {self.root}")

    def _boot_app_list(self) -> None:
        """Emulate the switcher at boot: every enabled app class, in a scrambled (dir()-like) order."""
        try:
            from tabs.apps import CORE_APP_FILES, MENU_DENY, app_names, concrete_classes
            from .sources import parse_app
            names = []
            for f in sorted((self.root / "apps").glob("*.py")):
                if f.name.startswith("_") or f.name in CORE_APP_FILES or f.stem in MENU_DENY:
                    continue
                src = f.read_text(errors="replace")
                classes = concrete_classes(src, parse_app(src, ref="fake", path=str(f)).classes)
                names += [n for _c, n, _s in app_names(src, classes)]
            self.app_list = sorted(names, key=lambda n: hashlib.sha256(n.encode()).hexdigest()) or None
        except Exception as e:  # noqa: BLE001
            self.log(f"fake badge: could not build app_list: {e}")
            self.app_list = None

    # helpers
    def _p(self, remote: str) -> Path:
        return self.root / remote.lstrip("/")

    def _sleep(self, n=1):
        time.sleep(self.delay * n)

    @staticmethod
    def list_ports():
        return [PortInfo(FakeBadgeService.FAKE_PORT, "Simulated badge", 0x303A, 0x4001, "FAKE")]

    @classmethod
    def autodetect_port(cls):
        return cls.FAKE_PORT

    def ping(self):
        return True

    def info(self):
        self._sleep(5)
        used = sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
        return {"micropython": "3.4.0; MicroPython v1.27.0 (simulated)", "machine": "Billboard Badge (fake)",
                "release": "1.27.0", "mem_free": 7_800_000, "mem_alloc": 400_000,
                "fs_total": 5_000_000, "fs_free": 5_000_000 - used, "vin": 4.02}

    def exec_py(self, code, timeout=60):
        self._sleep(2)
        if "/data/app_list" in code and "btree" in code:   # tabs.apps.APP_LIST_CODE
            if self.app_list is None:
                return "SLOTS NONE OSError(2,)\n"
            return f"SLOTS {[(i, n) for i, n in enumerate(self.app_list)]!r}\n"
        self.log("fake badge: exec_py is not simulated")
        return ""

    def exec_json(self, code, timeout=60):
        raise BadgeError("exec_json is not simulated on the fake badge")

    def listdir(self, path="/", recursive=False):
        self._sleep()
        base = self._p(path)
        if not base.exists():
            return []
        it = base.rglob("*") if recursive else base.iterdir()
        out = []
        for f in sorted(it):
            rp = "/" + f.relative_to(self.root).as_posix()
            out.append(FileEntry(rp, 0 if f.is_dir() else f.stat().st_size, f.is_dir()))
        return out

    def exists(self, path):
        return self._p(path).exists()

    def file_hashes(self, path="/"):
        self._sleep(5)
        out = {}
        for f in sorted(self._p(path).rglob("*")):
            rp = "/" + f.relative_to(self.root).as_posix()
            out[rp] = "" if f.is_dir() else hashlib.sha256(f.read_bytes()).hexdigest()
        return out

    def read_file(self, remote):
        self._sleep()
        p = self._p(remote)
        if not p.is_file():
            raise BadgeError(f"no such file {remote}")
        return p.read_bytes()

    def write_file(self, local, remote, timeout=120):
        self._sleep(2)
        p = self._p(remote)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, p)
        self.log(f"fake badge: wrote {remote}")

    def write_bytes(self, data, remote):
        p = self._p(remote)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.log(f"fake badge: wrote {remote}")

    def mkdir_p(self, remote_dir):
        self._p(remote_dir).mkdir(parents=True, exist_ok=True)

    def remove(self, remote, recursive=False):
        if remote.rstrip("/") in ("", "/", "/data", "/lib", "/apps"):
            raise BadgeError(f"Refusing to delete {remote}")
        p = self._p(remote)
        if p.is_dir():
            if not recursive:
                raise BadgeError("directory; use recursive")
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        self.log(f"fake badge: removed {remote}")

    def rename(self, src, dst):
        self._p(src).rename(self._p(dst))

    ui_interrupted = False
    on_interrupt_change = None

    def free_bytes(self):
        return self.info()["fs_free"]

    def plan_push(self, *a, **k):
        from .badge import BadgeService
        return BadgeService.plan_push(self, *a, **k)

    def push_tree(self, *a, **k):
        from .badge import BadgeService
        return BadgeService.push_tree(self, *a, **k)

    _copy_order = staticmethod(lambda p: __import__("core.badge", fromlist=["x"]).BadgeService._copy_order(p))

    # Backups run the real BadgeService.backup() logic on top of a simulated "mpremote fs cp".
    # Tests can set cp_drop = {badge paths}: those are skipped on the next copy attempt only.
    cp_drop: set = set()

    def walk(self, root="/"):
        return self.listdir(root, recursive=True)

    def _run(self, *args, timeout=60, check=True):
        if args[:2] != ("fs", "cp") or not args[-1].endswith("/"):
            raise BadgeError(f"fake badge: mpremote {' '.join(args[:2])} is not simulated")
        dst = Path(args[-1])
        for src in args[2:-1]:
            path = src[1:]
            if path in self.cp_drop:
                self.cp_drop = self.cp_drop - {path}
                raise BadgeError(f"fake badge: simulated copy failure at {path}")
            self._sleep()
            shutil.copyfile(self._p(path), dst / Path(path).name)

    def backup(self, dest_dir, progress=None, cancel=None):
        from .badge import BadgeService
        return BadgeService.backup(self, dest_dir, progress=progress, cancel=cancel)

    def file_sha256(self, path):
        p = self._p(path)
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None

    def reset(self, wait=True):
        self._sleep(10)
        self._boot_app_list()
        self.log("fake badge: hard reset" + (" (waited for boot)" if wait else ""))

    def esptool(self, *args, timeout=600, line_cb=None):
        for line in ("esptool (simulated) " + " ".join(args), "Connecting....", "Done (simulated)"):
            (line_cb or self.log)(line)
            self._sleep(5)
        return 0
