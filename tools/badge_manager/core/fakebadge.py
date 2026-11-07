"""FakeBadgeService: a simulated badge for testing the GUI without hardware.

Enable with:  BADGE_FAKE=1 .venv/bin/python app.py
The "badge filesystem" is a temp folder seeded from upstream firmware/badge (the upstream remote's main).
Same public methods as BadgeService. exec_py/exec_json only understand nothing: they raise,
except info(), which is simulated. Slight delays mimic the real serial link.
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
                sm = SourceManager(repo_dir)
                sm.export_badge_tree(sm.upstream_ref, self.root)
            except Exception as e:  # noqa: BLE001
                log(f"fake badge: could not seed from repo: {e}")
        (self.root / "data").mkdir(exist_ok=True)
        log(f"fake badge filesystem at {self.root}")

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

    def backup(self, dest_dir):
        import datetime
        dest = Path(dest_dir) / datetime.datetime.now().strftime("badge-%Y%m%d-%H%M%S")
        shutil.copytree(self.root, dest)
        return dest

    def reset(self):
        self._sleep(10)
        self.log("fake badge: hard reset")

    def esptool(self, *args, timeout=600, line_cb=None):
        for line in ("esptool (simulated) " + " ".join(args), "Connecting....", "Done (simulated)"):
            (line_cb or self.log)(line)
            self._sleep(5)
        return 0
