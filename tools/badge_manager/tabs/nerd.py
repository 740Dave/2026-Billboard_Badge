"""Nerd Detector tab: configure and install the BLE "Nerd Detector" badge app.

The badge app (user_apps/nerd_detector/apps/nerd_detector.py) counts other badges nearby over BLE.
Each badge reads /data/nerd.json = {"id": "<6 hex>", "name": "<<=16 ASCII>", "broadcast": bool}
and creates "id" itself if missing. It advertises (non-connectable, legacy 31-byte adv):
    02 01 06                                  flags
    LL FF  FF FF  'S' 'C' '2' '6'  01  id(3)  name(0-16)   manufacturer-specific AD
On the badge: A = redraw now, B = toggle broadcast.

Pure helpers (validate_name, build_settings, parse_nerd_payload, write_settings, app_status,
install_app) take a badge service as argument so they are testable against FakeBadgeService.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import customtkinter as ctk

from tabs.base import BaseTab, Section, confirm, info

SETTINGS_PATH = "/data/nerd.json"
BADGE_APP_PATH = "/apps/nerd_detector.py"
_HERE = Path(__file__).resolve()
LOCAL_APP = next((c for c in (_HERE.parents[3] / "user_apps" / "nerd_detector" / "apps" / "nerd_detector.py",
                               _HERE.parents[2] / "nerd_detector" / "apps" / "nerd_detector.py") if c.exists()),
                 _HERE.parents[3] / "user_apps" / "nerd_detector" / "apps" / "nerd_detector.py")
MAX_NAME = 16
COMPANY_ID = 0xFFFF
MAGIC = b"SC26"
VERSION = 0x01
SCAN_SECONDS = 10

EXPLAINER = (
    "How it works: each badge sends a tiny Bluetooth LE advertisement (manufacturer data, company "
    "id 0xFFFF): \"SC26\", version 1, a 3-byte id and optionally your name (up to 16 ASCII chars). "
    "Badges listen for each other and count distinct ids nearby. Nothing connects, nothing is stored "
    "off the badge.\n\n"
    "Privacy: the id is random (the badge generates it on first run), not derived from you or your "
    "hardware. The name is opt-in: leave it empty to broadcast only the anonymous id, or switch "
    "broadcasting off entirely (also button B on the badge). Anything you broadcast can be read by "
    "any phone or computer nearby."
)


# ====================================================================== pure helpers
def validate_name(name: str) -> str | None:
    """None if OK, else an error message."""
    if len(name) > MAX_NAME:
        return f"Too long ({len(name)}/{MAX_NAME})"
    bad = sorted({c for c in name if not (" " <= c <= "~")})
    if bad:
        return "Only printable ASCII allowed (bad: " + " ".join(repr(c) for c in bad) + ")"
    return None


def parse_settings(raw: bytes | None) -> dict:
    """Badge /data/nerd.json bytes -> dict ({} if missing/invalid)."""
    if not raw:
        return {}
    try:
        d = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def _valid_id(v) -> bool:
    return isinstance(v, str) and len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v)


def build_settings(existing: dict, name: str, broadcast: bool) -> bytes:
    """New nerd.json content. Keeps the badge's own id (and any unknown keys)."""
    err = validate_name(name)
    if err:
        raise ValueError(err)
    out = {k: v for k, v in existing.items() if k not in ("id", "name", "broadcast")}
    if _valid_id(existing.get("id")):
        out["id"] = existing["id"].lower()
    out["name"] = name
    out["broadcast"] = bool(broadcast)
    return json.dumps(out).encode()


def parse_nerd_payload(data: bytes) -> dict | None:
    """Parse manufacturer data *after* the company id (as bleak gives it):
    b"SC26" + version(1) + id(3) + name(0-16). Returns {"version","id","name"} or None."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 8 or bytes(data[:4]) != MAGIC:
        return None
    version = data[4]
    if version != VERSION:
        return None
    raw_name = bytes(data[8:8 + MAX_NAME])
    name = "".join(chr(b) if 0x20 <= b <= 0x7E else "?" for b in raw_name)
    return {"version": version, "id": bytes(data[5:8]).hex(), "name": name}


def read_settings(badge) -> dict:
    try:
        return parse_settings(badge.read_file(SETTINGS_PATH))
    except Exception:  # noqa: BLE001  (missing file)
        return {}


def write_settings(badge, name: str, broadcast: bool) -> dict:
    """Read existing settings (keep id), write new ones. Returns the written dict."""
    data = build_settings(read_settings(badge), name, broadcast)
    badge.write_bytes(data, SETTINGS_PATH)
    return json.loads(data)


def local_app_bytes(path: Path | None = None) -> bytes | None:
    try:
        return (path or LOCAL_APP).read_bytes()
    except OSError:
        return None


def app_status(badge, local: Path | None = None) -> dict:
    """{"installed": bool, "local": bool, "match": bool|None}"""
    try:
        hashes = badge.file_hashes("/apps")
    except Exception:  # noqa: BLE001
        hashes = {}
    remote = hashes.get(BADGE_APP_PATH)
    lb = local_app_bytes(local)
    lh = hashlib.sha256(lb).hexdigest() if lb is not None else None
    return {"installed": bool(remote), "local": lb is not None,
            "match": (remote == lh) if (remote and lh) else None}


def install_app(badge, local: Path | None = None) -> None:
    local = local or LOCAL_APP
    if not local.is_file():
        raise FileNotFoundError(f"Local app not found: {local}")
    badge.write_file(local, BADGE_APP_PATH)


def bleak_available() -> bool:
    try:
        import bleak  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def scan_nerds(seconds: float = SCAN_SECONDS) -> list[dict]:
    """Blocking BLE scan (run in ctx.run_task). Returns [{"id","name","rssi","address"}]."""
    import asyncio

    from bleak import BleakScanner

    async def go():
        return await BleakScanner.discover(timeout=seconds, return_adv=True)

    found = {}
    for addr, (dev, adv) in asyncio.run(go()).items():
        payload = (adv.manufacturer_data or {}).get(COMPANY_ID)
        p = parse_nerd_payload(payload) if payload else None
        if p:
            found[p["id"]] = {"id": p["id"], "name": p["name"], "rssi": adv.rssi, "address": addr}
    return sorted(found.values(), key=lambda d: -(d["rssi"] or -999))


# ====================================================================== tab
class NerdTab(BaseTab):
    TITLE = "Nerd Detector"

    def build(self) -> None:
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # ---- settings
        s = Section(scroll, "Badge settings  (/data/nerd.json)")
        s.pack(fill="x", padx=6, pady=6)
        row = ctk.CTkFrame(s.body, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text="Name:").pack(side="left")
        self.name_var = ctk.StringVar(value=self.ctx.config.get("nerd_name", "") or "")
        self.name_entry = ctk.CTkEntry(row, textvariable=self.name_var, width=220,
                                       placeholder_text="(empty = anonymous id only)")
        self.name_entry.pack(side="left", padx=6)
        self.counter = ctk.CTkLabel(row, text="", width=220, anchor="w")
        self.counter.pack(side="left", padx=6)
        self.name_var.trace_add("write", lambda *_: self._name_changed())
        self.broadcast_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(s.body, text="Broadcast my name/presence", variable=self.broadcast_var).pack(
            anchor="w", pady=4)
        self.id_label = ctk.CTkLabel(s.body, text="Badge id: (not read yet)", anchor="w")
        self.id_label.pack(fill="x")
        btns = ctk.CTkFrame(s.body, fg_color="transparent")
        btns.pack(fill="x", pady=4)
        ctk.CTkButton(btns, text="Read from badge", command=self.read_from_badge).pack(side="left", padx=(0, 6))
        self.write_btn = ctk.CTkButton(btns, text="Write to badge", command=self.write_to_badge)
        self.write_btn.pack(side="left", padx=6)

        # ---- app install
        a = Section(scroll, "App install  (/apps/nerd_detector.py)")
        a.pack(fill="x", padx=6, pady=6)
        self.app_label = ctk.CTkLabel(a.body, text="Status: (not checked)", anchor="w", justify="left")
        self.app_label.pack(fill="x")
        ctk.CTkLabel(a.body, text=f"Local file: {LOCAL_APP}", anchor="w", text_color="gray60").pack(fill="x")
        b2 = ctk.CTkFrame(a.body, fg_color="transparent")
        b2.pack(fill="x", pady=4)
        ctk.CTkButton(b2, text="Check", width=80, command=self.check_app).pack(side="left", padx=(0, 6))
        ctk.CTkButton(b2, text="Install / update app", command=self.install).pack(side="left", padx=6)
        ctk.CTkButton(b2, text="Write settings + install + restart",
                      command=self.write_install_restart).pack(side="left", padx=6)

        # ---- scan
        sc = Section(scroll, "Scan from this Mac (optional)")
        sc.pack(fill="x", padx=6, pady=6)
        if bleak_available():
            self.scan_btn = ctk.CTkButton(sc.body, text=f"Scan ({SCAN_SECONDS} s)", command=self.scan)
            self.scan_btn.pack(anchor="w")
            self.scan_box = ctk.CTkTextbox(sc.body, height=140, font=ctk.CTkFont(family="Menlo", size=12))
            self.scan_box.pack(fill="x", pady=4)
            self.scan_box.configure(state="disabled")
        else:
            self.scan_btn = None
            ctk.CTkLabel(sc.body, anchor="w", justify="left",
                         text="Needs the 'bleak' package. Install it with:\n    .venv/bin/pip install bleak\n"
                              "then restart the manager.").pack(fill="x")

        # ---- explainer
        e = Section(scroll, "Protocol & privacy")
        e.pack(fill="x", padx=6, pady=6)
        ctk.CTkLabel(e.body, text=EXPLAINER, anchor="w", justify="left", wraplength=1000).pack(fill="x")

        self._name_changed()

    # ------------------------------------------------------------ settings
    def _name_changed(self) -> None:
        name = self.name_var.get()
        err = validate_name(name)
        if err:
            self.counter.configure(text=err, text_color="#d9534f")
            self.write_btn.configure(state="disabled")
        else:
            self.counter.configure(text=f"{len(name)}/{MAX_NAME}", text_color="gray60")
            self.write_btn.configure(state="normal")

    def _show_settings(self, d: dict) -> None:
        if "name" in d:
            self.name_var.set(str(d.get("name") or ""))
        if "broadcast" in d:
            self.broadcast_var.set(bool(d.get("broadcast")))
        self.id_label.configure(text=f"Badge id: {d.get('id') or '(none yet; the badge creates one)'}")

    def read_from_badge(self) -> None:
        def done(d):
            if not d:
                self.ctx.status(f"No {SETTINGS_PATH} on badge yet")
            else:
                self.ctx.status("Read nerd settings")
            self._show_settings(d)
        self.ctx.run_device_task(lambda: read_settings(self.ctx.badge), done, title="Reading nerd.json")

    def _confirm_write(self, extra: str = "") -> bool:
        name = self.name_var.get()
        if self.broadcast_var.get():
            what = (f"Your name \"{name}\" and a random badge id" if name else "A random badge id (no name)")
            msg = (f"{what} will be broadcast publicly over Bluetooth to anyone nearby "
                   "(any phone or laptop can read it), until you switch broadcasting off.")
        else:
            msg = "Broadcasting will be OFF: the badge will only listen for others."
        return confirm(self, "Write Nerd Detector settings", msg + extra + "\n\nContinue?")

    def _writer(self):
        """Capture the form on the Tk thread; return a job safe to run on the device worker."""
        name, bc = self.name_var.get(), bool(self.broadcast_var.get())
        self.ctx.config.set("nerd_name", name)
        return lambda: write_settings(self.ctx.badge, name, bc)

    def write_to_badge(self) -> None:
        if validate_name(self.name_var.get()) or not self._confirm_write():
            return

        def done(d):
            self._show_settings(d)
            self.ctx.status("Nerd settings written (the app re-reads them on restart)")
        self.ctx.run_device_task(self._writer(), done, title="Writing nerd.json")

    # ------------------------------------------------------------ app
    def on_show(self) -> None:
        self.check_app()

    def _show_status(self, st: dict) -> None:
        if not st["installed"]:
            txt = "Status: not installed on badge"
        elif st["match"] is True:
            txt = "Status: installed, matches local file"
        elif st["match"] is False:
            txt = "Status: installed, DIFFERENT from local file (update available)"
        else:
            txt = "Status: installed (no local file to compare)"
        if not st["local"]:
            txt += "\nLocal app file not found yet."
        self.app_label.configure(text=txt)

    def check_app(self) -> None:
        self.ctx.run_device_task(lambda: app_status(self.ctx.badge), self._show_status,
                                 title="Checking Nerd Detector app")

    def _local_ok(self) -> bool:
        if not LOCAL_APP.is_file():
            info(self, "Nerd Detector app missing", f"Can't find the local app file:\n{LOCAL_APP}")
            return False
        return True

    def install(self) -> None:
        if not self._local_ok():
            return

        def job():
            install_app(self.ctx.badge)
            return app_status(self.ctx.badge)

        def done(st):
            self._show_status(st)
            self.ctx.events.publish("badge.files_changed", [BADGE_APP_PATH])
            self.ctx.status("Nerd Detector installed; hard reset to load it")
        self.ctx.run_device_task(job, done, title="Installing Nerd Detector")

    def write_install_restart(self) -> None:
        if validate_name(self.name_var.get()) or not self._local_ok():
            return
        if not self._confirm_write("\n\nThe app will be installed and the badge hard-reset."):
            return

        write = self._writer()

        def job():
            d = write()
            install_app(self.ctx.badge)
            st = app_status(self.ctx.badge)
            self.ctx.badge.reset()
            return d, st

        def done(res):
            d, st = res
            self._show_settings(d)
            self._show_status(st)
            self.ctx.events.publish("badge.files_changed", [SETTINGS_PATH, BADGE_APP_PATH])
            self.ctx.status("Nerd Detector installed, badge restarting")
        self.ctx.run_device_task(job, done, title="Nerd Detector install + restart")

    # ------------------------------------------------------------ scan
    def scan(self) -> None:
        self.scan_btn.configure(state="disabled")

        def done(rows):
            self.scan_btn.configure(state="normal")
            lines = [f"{'id':<8}{'rssi':>6}  name"] + [
                f"{r['id']:<8}{r['rssi'] if r['rssi'] is not None else '?':>6}  {r['name'] or '(anonymous)'}"
                for r in rows] if rows else ["No Nerd Detector badges found."]
            self.scan_box.configure(state="normal")
            self.scan_box.delete("1.0", "end")
            self.scan_box.insert("1.0", "\n".join(lines))
            self.scan_box.configure(state="disabled")
            self.ctx.status(f"Scan done: {len(rows)} badge(s)")

        self.ctx.run_task(scan_nerds, done, lambda _e: self.scan_btn.configure(state="normal"),
                          title="BLE scan")
