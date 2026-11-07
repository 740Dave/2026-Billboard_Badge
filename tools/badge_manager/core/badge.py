"""BadgeService: every interaction with the physical badge goes through here.

Design rules (learned from the badge firmware and the community forks):
  * The serial port is exclusive. All calls take ``self.lock``; tabs must run them through
    ``ctx.run_device_task`` so they execute one at a time on the device worker thread.
  * Never soft-reset the badge (LVGL does not survive a soft reset). Every mpremote call uses
    ``resume`` (no soft reset on connect), and changes are finished with ``reset()``, which is a
    hard reset (machine.reset()).
  * Never delete anything under /data unless the user explicitly asked for that file.

All methods are blocking and may raise BadgeError. Do NOT call them from the Tk main thread.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
from dataclasses import dataclass
from pathlib import Path

ESPRESSIF_VID = 0x303A


class BadgeError(Exception):
    pass


@dataclass
class PortInfo:
    device: str
    description: str
    vid: int | None
    pid: int | None
    serial_number: str | None

    @property
    def looks_like_badge(self) -> bool:
        return self.vid == ESPRESSIF_VID

    def label(self) -> str:
        tag = "  [badge?]" if self.looks_like_badge else ""
        return f"{self.device}  {self.description}{tag}"


@dataclass
class FileEntry:
    path: str        # absolute path on the badge, e.g. "/apps/gallery.py"
    size: int
    is_dir: bool


def _mpremote_cmd() -> list[str]:
    """Run mpremote with the same interpreter as the app, so the venv's copy is used."""
    return [sys.executable, "-m", "mpremote"]


class BadgeService:
    def __init__(self, port: str | None = None, log=print):
        self.port = port
        self.log = log
        self.lock = threading.RLock()
        # Any mpremote command interrupts main.py (Ctrl-C into raw REPL), freezing the badge's
        # menu/LEDs until a hard reset. Track that so the UI can offer the reset.
        self.ui_interrupted = False
        self.on_interrupt_change = None   # callback(bool), set by AppContext

    # ------------------------------------------------------------------ ports
    @staticmethod
    def list_ports() -> list[PortInfo]:
        from serial.tools import list_ports
        out = []
        for p in list_ports.comports():
            if not (p.device.startswith("/dev/cu.") or p.device.startswith("/dev/tty") or p.device.startswith("COM")):
                continue
            if p.device.startswith("/dev/tty.") and sys.platform == "darwin":
                continue  # macOS: use the /dev/cu.* twin
            out.append(PortInfo(p.device, p.description or "", p.vid, p.pid, p.serial_number))
        out.sort(key=lambda p: (not p.looks_like_badge, p.device))
        return out

    @classmethod
    def autodetect_port(cls) -> str | None:
        ports = [p for p in cls.list_ports() if p.looks_like_badge]
        return ports[0].device if len(ports) == 1 else None

    # ------------------------------------------------------------- low level
    def _run(self, *args: str, timeout: float = 60, check: bool = True) -> subprocess.CompletedProcess:
        if not self.port:
            raise BadgeError("No serial port selected")
        cmd = _mpremote_cmd() + ["connect", self.port, "resume", *args]
        if args and args[0] != "reset":
            self._set_interrupted(True)
        with self.lock:
            self.log(f"$ mpremote {' '.join(args)[:120]}")
            try:
                cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired as e:
                raise BadgeError(f"mpremote timed out after {timeout}s: {' '.join(args)[:80]}") from e
        if check and cp.returncode != 0:
            msg = (cp.stderr or cp.stdout).strip().splitlines()[-1:] or ["unknown error"]
            raise BadgeError(f"mpremote {args[0]} failed: {msg[0]}")
        return cp

    def _set_interrupted(self, value: bool) -> None:
        if value != self.ui_interrupted:
            self.ui_interrupted = value
            if self.on_interrupt_change:
                self.on_interrupt_change(value)

    def exec_py(self, code: str, timeout: float = 60) -> str:
        """Run MicroPython code on the badge and return what it printed."""
        cp = self._run("exec", textwrap.dedent(code), timeout=timeout)
        return cp.stdout

    def exec_json(self, code: str, timeout: float = 60):
        """Run code whose LAST printed line is JSON; return the parsed value."""
        out = self.exec_py(code, timeout=timeout).strip().splitlines()
        if not out:
            raise BadgeError("No output from badge")
        try:
            return json.loads(out[-1])
        except ValueError as e:
            raise BadgeError(f"Unexpected output from badge: {out[-1][:200]}") from e

    # ------------------------------------------------------------ high level
    def ping(self) -> bool:
        try:
            return self.exec_py("print('pong')", timeout=10).strip().endswith("pong")
        except BadgeError:
            return False

    def info(self) -> dict:
        """Firmware version, memory, filesystem and battery/USB voltage."""
        return self.exec_json("""
            import os, gc, sys, json, machine
            gc.collect()
            st = os.statvfs('/')
            try:
                adc = machine.ADC(machine.Pin(14), atten=machine.ADC.ATTN_11DB)
                v = adc.read_uv() * 2 / 1e6
            except Exception:
                v = None
            print(json.dumps({
                'micropython': sys.version, 'machine': os.uname().machine,
                'release': os.uname().release,
                'mem_free': gc.mem_free(), 'mem_alloc': gc.mem_alloc(),
                'fs_total': st[0] * st[2], 'fs_free': st[0] * st[3],
                'vin': v,
            }))
        """)

    def listdir(self, path: str = "/", recursive: bool = False) -> list[FileEntry]:
        data = self.exec_json(f"""
            import os, json
            out = []
            def walk(p, rec):
                for name, typ, *_ in os.ilistdir(p):
                    full = (p.rstrip('/') + '/' + name)
                    isdir = typ == 0x4000
                    size = 0 if isdir else os.stat(full)[6]
                    out.append([full, size, isdir])
                    if isdir and rec:
                        walk(full, rec)
            try:
                walk({path!r}, {recursive!r})
            except OSError:
                pass
            print(json.dumps(out))
        """)
        return [FileEntry(p, s, d) for p, s, d in data]

    def exists(self, path: str) -> bool:
        return bool(self.exec_json(f"""
            import os, json
            try:
                os.stat({path!r}); print(json.dumps(True))
            except OSError:
                print(json.dumps(False))
        """))

    def file_hashes(self, path: str = "/") -> dict[str, str]:
        """{badge_path: sha256 hex} for all files ('' for directories). Same as scripts/update.py."""
        return self.exec_json(f"""
            import os, json, hashlib, binascii
            out = {{}}
            def walk(p):
                for name, typ, *_ in os.ilistdir(p):
                    full = p.rstrip('/') + '/' + name
                    if typ == 0x4000:
                        out[full] = ''
                        walk(full)
                    else:
                        h = hashlib.sha256()
                        with open(full, 'rb') as f:
                            while True:
                                b = f.read(2048)
                                if not b: break
                                h.update(b)
                        out[full] = binascii.hexlify(h.digest()).decode()
            walk({path!r})
            print(json.dumps(out))
        """, timeout=180)

    def read_file(self, remote: str) -> bytes:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "f"
            self._run("fs", "cp", f":{remote}", str(local), timeout=120)
            return local.read_bytes()

    def write_file(self, local: str | Path, remote: str, timeout: float = 120) -> None:
        self.mkdir_p(str(Path(remote).parent))
        self._run("fs", "cp", str(local), f":{remote}", timeout=timeout)

    def write_bytes(self, data: bytes, remote: str) -> None:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / Path(remote).name
            local.write_bytes(data)
            self.write_file(local, remote)

    def mkdir_p(self, remote_dir: str) -> None:
        if remote_dir in ("", "/"):
            return
        self.exec_py(f"""
            import os
            p = ''
            for part in {remote_dir!r}.strip('/').split('/'):
                p += '/' + part
                try:
                    os.mkdir(p)
                except OSError:
                    pass
        """)

    def remove(self, remote: str, recursive: bool = False) -> None:
        if remote.rstrip("/") in ("", "/", "/data", "/lib", "/apps"):
            raise BadgeError(f"Refusing to delete {remote}")
        self._run("fs", "rm", *(["-r"] if recursive else []), f":{remote}")

    def rename(self, src: str, dst: str) -> None:
        self.exec_py(f"import os; os.rename({src!r}, {dst!r})")

    # copy order: libraries first, then apps, then main/boot, data last, so running out of space
    # or an interruption is least likely to leave new apps on old libs.
    @staticmethod
    def _copy_order(path: str) -> tuple:
        for i, prefix in enumerate(("/lib/", "/apps/")):
            if path.startswith(prefix):
                return (i, path)
        if path.startswith("/data/"):
            return (9, path)
        return (5, path)

    def plan_push(self, local_root: str | Path, remote_root: str = "/", delete_extra: bool = False,
                  protect: tuple[str, ...] = ("/data",), only_if_missing: tuple[str, ...] = ("/data",)) -> dict:
        """Work out what push_tree would do, without changing anything on the badge.

        Files under ``only_if_missing`` prefixes are copied only when absent on the badge; existing
        ones that differ are reported in "kept" and left alone (so user data survives updates).
        """
        import hashlib
        local_root = Path(local_root)
        base = remote_root.rstrip("/")
        local, sizes = {}, {}
        for f in sorted(local_root.rglob("*")):
            if "__pycache__" in f.parts or f.name.startswith("."):
                continue
            rp = base + "/" + f.relative_to(local_root).as_posix()
            if f.is_dir():
                local[rp] = ""
            else:
                data = f.read_bytes()
                local[rp] = hashlib.sha256(data).hexdigest()
                sizes[rp] = len(data)
        remote = self.file_hashes(remote_root)
        under = lambda p, prefixes: any(p == x or p.startswith(x.rstrip("/") + "/") for x in prefixes)  # noqa: E731
        copy, kept = [], []
        for p, h in local.items():
            if not h or remote.get(p) == h:
                continue
            if p in remote and under(p, only_if_missing):
                kept.append(p)
            else:
                copy.append(p)
        copy.sort(key=self._copy_order)
        delete = []
        if delete_extra:
            delete = [p for p in sorted(set(remote) - set(local), reverse=True)
                      if not under(p, protect) and "__pycache__" not in p]
        return {"local_root": str(local_root), "copy": copy, "kept": kept, "delete": delete,
                "remote_hashes": remote, "sizes": sizes, "bytes": sum(sizes[p] for p in copy),
                "unchanged": sum(1 for p, h in local.items() if h and remote.get(p) == h)}

    def free_bytes(self) -> int:
        return int(self.exec_json("""
            import os, json
            st = os.statvfs('/')
            print(json.dumps(st[0] * st[3]))
        """))

    def push_tree(self, local_root: str | Path, remote_root: str = "/", progress=None,
                  delete_extra: bool = False, protect: tuple[str, ...] = ("/data",),
                  only_if_missing: tuple[str, ...] = ("/data",), plan: dict | None = None) -> dict:
        """Mirror a local folder onto the badge, copying only files whose hash differs.

        Safe defaults: files under /data are only created, never overwritten (pass
        only_if_missing=() to restore data from a backup); delete_extra never touches ``protect``.
        Raises BadgeError before writing anything if the badge lacks the space.
        Returns {"copied", "deleted", "kept", "unchanged"}.
        """
        local_root = Path(local_root)
        plan = plan or self.plan_push(local_root, remote_root, delete_extra, protect, only_if_missing)
        # Space check: new bytes minus what the overwritten files already occupy (approximate),
        # plus a safety margin for FAT clusters and the badge's own files (app_list, indexes).
        existing = {e.path: e.size for e in self.listdir(remote_root, recursive=True) if not e.is_dir}
        need = sum(plan["sizes"][p] - existing.get(p, 0) for p in plan["copy"])
        free = self.free_bytes()
        if need > free - 64 * 1024:
            raise BadgeError(f"Not enough space on the badge: need ~{need // 1024} KB, "
                             f"{free // 1024} KB free. Remove artwork or apps first.")
        base = remote_root.rstrip("/")
        copied, deleted = [], []
        for i, p in enumerate(plan["copy"]):
            if progress:
                progress(i, len(plan["copy"]), p)
            self.write_file(local_root / p[len(base) + 1:], p)
            copied.append(p)
        for p in plan["delete"]:
            self.remove(p, recursive=plan["remote_hashes"].get(p) == "")
            deleted.append(p)
        if progress:
            progress(len(plan["copy"]), len(plan["copy"]), "done")
        return {"copied": copied, "deleted": deleted, "kept": plan["kept"], "unchanged": plan["unchanged"]}

    def backup(self, dest_dir: str | Path) -> Path:
        """Copy the badge's whole filesystem into dest_dir/<timestamp>/ and return that path."""
        import datetime
        dest = Path(dest_dir) / datetime.datetime.now().strftime("badge-%Y%m%d-%H%M%S")
        dest.mkdir(parents=True, exist_ok=True)
        self._run("fs", "cp", "-r", ":/", str(dest) + "/", timeout=600)
        # mpremote may nest the copy one level down; normalise and verify it's a whole badge.
        if not (dest / "main.py").exists():
            nested = [d for d in dest.iterdir() if d.is_dir() and (d / "main.py").exists()]
            if len(nested) == 1:
                for item in nested[0].iterdir():
                    item.rename(dest / item.name)
                nested[0].rmdir()
        if not (dest / "main.py").exists():
            raise BadgeError(f"Backup in {dest} looks incomplete (no main.py)")
        return dest

    def reset(self) -> None:
        """Hard reset (never soft reset: LVGL breaks)."""
        self._run("reset", check=False, timeout=15)
        self._set_interrupted(False)

    # ------------------------------------------------------- runtime flashing
    def esptool(self, *args: str, timeout: float = 600, line_cb=None) -> int:
        """Run esptool against the badge port (badge must be in bootloader mode for writes)."""
        if not self.port:
            raise BadgeError("No serial port selected")
        cmd = [sys.executable, "-m", "esptool", "--port", self.port, *args]
        with self.lock:
            self.log("$ esptool " + " ".join(args))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            watchdog = threading.Timer(timeout, proc.kill)   # a hung esptool must not hold the port forever
            watchdog.start()
            try:
                for line in proc.stdout:
                    (line_cb or self.log)(line.rstrip())
                rc = proc.wait()
            finally:
                watchdog.cancel()
            if rc < 0:
                raise BadgeError(f"esptool killed after {timeout}s timeout")
            return rc


def have(tool: str) -> bool:
    return shutil.which(tool) is not None
