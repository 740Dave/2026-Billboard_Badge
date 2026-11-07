"""Maker Detector tab: configure and install the BLE "Maker Detector" badge app.

The badge app (maker_detector.py; see core.onboarding.local_app_candidates) counts other badges
nearby over BLE. Each badge reads /data/maker.json =
    {"id": "<6 hex>", "name": "<broadcast text, <=16 ASCII>", "makes": "<local only, <=40>", "broadcast": bool}
and creates "id" itself if missing (migrating /data/nerd.json from the old Nerd Detector).
It advertises (non-connectable, legacy 31-byte adv):
    02 01 06                                  flags
    LL FF  FF FF  'S' 'C' '2' '6'  01  id(3)  name(0-16)   manufacturer-specific AD
On the badge: A = redraw now, B = toggle broadcast.

Badge I/O helpers live in core.onboarding (shared with the first-run wizard); everything runs on
ctx.run_device_task. Installing always validates the import on the badge and HARD resets.
"""

from __future__ import annotations

import hashlib

import customtkinter as ctk

from core import onboarding as ob
from core.theme import COLORS
from tabs.base import BaseTab, Section, confirm, info

SETTINGS_PATH = ob.SETTINGS_PATH
BADGE_APP_PATH = ob.BADGE_APP_PATH
MAX_NAME = ob.MAX_BROADCAST
MAX_MAKES = ob.MAX_MAKES
COMPANY_ID = 0xFFFF
MAGIC = b"SC26"
VERSION = 0x01
SCAN_SECONDS = 10

validate_name = ob.validate_broadcast

EXPLAINER = (
    "How it works: each badge sends a tiny Bluetooth LE advertisement (manufacturer data, company "
    "id 0xFFFF): \"SC26\", version 1, a 3-byte id and optionally your broadcast text (up to 16 ASCII "
    "chars). Badges listen for each other and count distinct makers nearby. Nothing connects, nothing "
    "is stored off the badge. \"What you make\" stays on your badge and is not broadcast.\n\n"
    "Privacy: the id is random (the badge generates it on first run), not derived from you or your "
    "hardware. The text is opt-in: leave it empty to broadcast only the anonymous id, or switch "
    "broadcasting off entirely (also button B on the badge). Anything you broadcast can be read by "
    "any phone or computer nearby."
)


# ====================================================================== pure helpers
def parse_maker_payload(data: bytes) -> dict | None:
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


def app_status(badge, local) -> dict:
    """{"installed": bool, "local": bool, "match": bool|None, "old_installed": bool}"""
    try:
        hashes = badge.file_hashes("/apps")
    except Exception:  # noqa: BLE001
        hashes = {}
    remote = hashes.get(BADGE_APP_PATH)
    try:
        lh = hashlib.sha256(local.read_bytes()).hexdigest() if local else None
    except OSError:
        lh = None
    return {"installed": bool(remote), "local": lh is not None,
            "match": (remote == lh) if (remote and lh) else None,
            "old_installed": ob.OLD_APP_PATH in hashes}


def bleak_available() -> bool:
    try:
        import bleak  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def scan_makers(seconds: float = SCAN_SECONDS) -> list[dict]:
    """Blocking BLE scan (run in ctx.run_task). Returns [{"id","name","rssi","address"}]."""
    import asyncio

    from bleak import BleakScanner

    async def go():
        return await BleakScanner.discover(timeout=seconds, return_adv=True)

    found = {}
    for addr, (dev, adv) in asyncio.run(go()).items():
        payload = (adv.manufacturer_data or {}).get(COMPANY_ID)
        p = parse_maker_payload(payload) if payload else None
        if p:
            found[p["id"]] = {"id": p["id"], "name": p["name"], "rssi": adv.rssi, "address": addr}
    return sorted(found.values(), key=lambda d: -(d["rssi"] or -999))


# ====================================================================== tab
class MakerTab(BaseTab):
    TITLE = "Maker Detector"

    def _local(self):
        return ob.find_local_app(self.ctx.config.get("repo_dir"))

    def build(self) -> None:
        prof = ob.get_profile(self.ctx.config)
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        # ---- settings
        s = Section(scroll, "Badge settings  (/data/maker.json)")
        s.pack(fill="x", padx=6, pady=6)
        row = ctk.CTkFrame(s.body, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text="Broadcast as:", width=110, anchor="w").pack(side="left")
        bt = prof.get("broadcast_text")
        if bt is None:
            bt = ob.default_broadcast(prof.get("name", ""))
        self.name_var = ctk.StringVar(value=bt)
        ctk.CTkEntry(row, textvariable=self.name_var, width=220,
                     placeholder_text="(empty = anonymous id only)").pack(side="left", padx=6)
        self.counter = ctk.CTkLabel(row, text="", width=260, anchor="w")
        self.counter.pack(side="left", padx=6)
        row2 = ctk.CTkFrame(s.body, fg_color="transparent")
        row2.pack(fill="x", pady=2)
        ctk.CTkLabel(row2, text="What you make:", width=110, anchor="w").pack(side="left")
        self.makes_var = ctk.StringVar(value=prof.get("makes", ""))
        ctk.CTkEntry(row2, textvariable=self.makes_var, width=320,
                     placeholder_text="e.g. robots, synths, badges (not broadcast)").pack(side="left", padx=6)
        self.makes_counter = ctk.CTkLabel(row2, text="", width=160, anchor="w")
        self.makes_counter.pack(side="left", padx=6)
        self.name_var.trace_add("write", lambda *_: self._changed())
        self.makes_var.trace_add("write", lambda *_: self._changed())
        self.broadcast_var = ctk.BooleanVar(value=bool(prof.get("detector_enabled", True)))
        ctk.CTkSwitch(s.body, text="Broadcast my presence (Maker Detector on)",
                      variable=self.broadcast_var).pack(anchor="w", pady=4)
        self.id_label = ctk.CTkLabel(s.body, text="Badge id: (not read yet)", anchor="w")
        self.id_label.pack(fill="x")
        btns = ctk.CTkFrame(s.body, fg_color="transparent")
        btns.pack(fill="x", pady=4)
        ctk.CTkButton(btns, text="Read from badge", command=self.read_from_badge).pack(side="left", padx=(0, 6))
        self.write_btn = ctk.CTkButton(btns, text="Write to badge", fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                                       command=self.write_to_badge)
        self.write_btn.pack(side="left", padx=6)
        self.apply_btn = ctk.CTkButton(btns, text="Write settings + install + restart",
                                       fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                                       command=self.write_install_restart)
        self.apply_btn.pack(side="left", padx=6)
        ctk.CTkButton(btns, text="Run first-time setup again",
                      command=self.rerun_setup).pack(side="right", padx=6)

        # ---- app install
        a = Section(scroll, f"App install  ({BADGE_APP_PATH})")
        a.pack(fill="x", padx=6, pady=6)
        self.app_label = ctk.CTkLabel(a.body, text="Status: (not checked)", anchor="w", justify="left")
        self.app_label.pack(fill="x")
        loc = self._local()
        self.local_label = ctk.CTkLabel(
            a.body, anchor="w", justify="left", text_color="gray60",
            text=f"Local file: {loc}" if loc else "Local file not found. Looked in:\n  " +
            "\n  ".join(str(p) for p in ob.local_app_candidates(self.ctx.config.get("repo_dir"))))
        self.local_label.pack(fill="x")
        b2 = ctk.CTkFrame(a.body, fg_color="transparent")
        b2.pack(fill="x", pady=4)
        ctk.CTkButton(b2, text="Check", width=80, command=self.check_app).pack(side="left", padx=(0, 6))
        ctk.CTkButton(b2, text="Install / update app", fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"], command=self.install).pack(side="left", padx=6)
        ctk.CTkLabel(a.body, anchor="w", justify="left", text_color="gray60", wraplength=1000,
                     text="Installing checks that the app loads on the badge (it is removed again if not), "
                          "removes the old Nerd Detector (two detectors would both advertise), keeps your "
                          "badge id, then hard-resets the badge.").pack(fill="x")

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

        self._changed()

    # ------------------------------------------------------------ settings
    def _changed(self) -> None:
        name, makes = self.name_var.get(), self.makes_var.get()
        err = validate_name(name)
        merr = ob.validate_makes(makes)
        self.counter.configure(text=err or f"{len(name)}/{MAX_NAME}  (public)",
                               text_color="#d9534f" if err else "gray60")
        self.makes_counter.configure(text=merr or f"{len(makes)}/{MAX_MAKES}",
                                     text_color="#d9534f" if merr else "gray60")
        state = "disabled" if (err or merr) else "normal"
        self.write_btn.configure(state=state)
        self.apply_btn.configure(state=state)

    def _invalid(self) -> bool:
        return bool(validate_name(self.name_var.get()) or ob.validate_makes(self.makes_var.get()))

    def _show_settings(self, d: dict) -> None:
        if not d:
            return
        if "name" in d:
            self.name_var.set(str(d.get("name") or ""))
        if "makes" in d:
            self.makes_var.set(str(d.get("makes") or ""))
        if "broadcast" in d:
            self.broadcast_var.set(bool(d.get("broadcast")))
        self.id_label.configure(text=f"Badge id: {d.get('id') or '(none yet; the badge creates one)'}")

    def read_from_badge(self) -> None:
        def job():
            d = ob.read_settings(self.ctx.badge)
            if not d.get("id"):
                bid = ob.existing_id(self.ctx.badge, d)
                if bid:
                    d = dict(d, id=bid)
            return d

        def done(d):
            self.ctx.status("Read maker settings" if d else f"No {SETTINGS_PATH} on badge yet")
            self._show_settings(d)
        self.ctx.run_device_task(job, done, title="Reading maker.json")

    def _confirm_write(self, extra: str = "") -> bool:
        name = self.name_var.get()
        if self.broadcast_var.get():
            what = (f"\"{name}\" and a random badge id" if name else "A random badge id (no text)")
            msg = (f"{what} will be broadcast publicly over Bluetooth to anyone nearby "
                   "(any phone or laptop can read it), until you switch broadcasting off.")
        else:
            msg = "Broadcasting will be OFF: the badge will only listen for others."
        return confirm(self, "Write Maker Detector settings", msg + extra + "\n\nContinue?")

    def _save_profile(self) -> dict:
        """Store the form in config "profile" (Tk thread) and return it."""
        p = ob.get_profile(self.ctx.config)
        p.update({"broadcast_text": self.name_var.get(), "makes": ob.clean_makes(self.makes_var.get()),
                  "detector_enabled": bool(self.broadcast_var.get())})
        p.setdefault("name", "")
        p.setdefault("applied_to", [])
        self.ctx.config.set(ob.PROFILE_KEY, p)
        return p

    def write_to_badge(self) -> None:
        if self._invalid() or not self._confirm_write("\n\nThe app re-reads settings on restart."):
            return
        p = self._save_profile()
        name, makes, bc = p["broadcast_text"], p["makes"], p["detector_enabled"]

        def done(d):
            self._show_settings(d)
            self.ctx.events.publish("badge.files_changed", [SETTINGS_PATH])
            self.ctx.status("Maker settings written (the app re-reads them on restart)")
        self.ctx.run_device_task(lambda: ob.write_settings(self.ctx.badge, name, makes, bc), done,
                                 title="Writing maker.json")

    def write_install_restart(self) -> None:
        if self._invalid():
            return
        if self.broadcast_var.get() and not self._local_ok():
            return
        if not self._confirm_write("\n\nThe app will be installed (if on) and the badge hard-reset."):
            return

        def done(res):
            self._show_settings(res.get("settings") or {})
            self.check_app()
        def failed(e):
            info(self, "Maker Detector failed", str(e))
            self.check_app()
        ob.apply_profile_to_badge(self.ctx, self._save_profile(), on_done=done, on_error=failed)

    def rerun_setup(self) -> None:
        def wait(win):
            if win is not None and win.winfo_exists():
                self.after(300, lambda: wait(win))
            else:
                self._reload_profile()
        wait(ob.maybe_run_onboarding(self.winfo_toplevel(), self.ctx, force=True))

    def _reload_profile(self) -> None:
        p = ob.get_profile(self.ctx.config)
        self.name_var.set(p.get("broadcast_text") or "")
        self.makes_var.set(p.get("makes") or "")
        self.broadcast_var.set(bool(p.get("detector_enabled")))

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
        if st.get("old_installed"):
            txt += "\nOld Nerd Detector is on the badge (installing Maker Detector removes it)."
        if not st["local"]:
            txt += "\nLocal app file not found yet."
        self.app_label.configure(text=txt)

    def check_app(self) -> None:
        local = self._local()
        self.ctx.run_device_task(lambda: app_status(self.ctx.badge, local), self._show_status,
                                 title="Checking Maker Detector app")

    def _local_ok(self) -> bool:
        if not self._local():
            info(self, "Maker Detector app missing", "Can't find the local app file. Looked in:\n" +
                 "\n".join(str(p) for p in ob.local_app_candidates(self.ctx.config.get("repo_dir"))))
            return False
        return True

    def install(self) -> None:
        if not self._local_ok():
            return
        if not confirm(self, "Install Maker Detector",
                       "Install / update the Maker Detector app? The old Nerd Detector is removed if present, "
                       "and the badge is hard-reset afterwards.\n\nContinue?"):
            return
        local = self._local()
        badge = self.ctx.badge

        def job():
            try:
                res = ob.install_app(badge, local, log=self.ctx.log)
                return res, app_status(badge, local)
            finally:
                badge.reset()  # HARD reset, also when validation failed (never soft reset)

        def done(r):
            res, st = r
            self._show_status(st)
            self.ctx.events.publish("badge.files_changed", [BADGE_APP_PATH, ob.OLD_APP_PATH])
            msg = "Maker Detector installed, badge restarting"
            if res.get("removed_old"):
                msg += " (old Nerd Detector removed)"
            self.ctx.status(msg)

        def failed(e):
            info(self, "Maker Detector install failed", str(e))
            self.check_app()
        self.ctx.run_device_task(job, done, failed, title="Installing Maker Detector")

    # ------------------------------------------------------------ scan
    def scan(self) -> None:
        self.scan_btn.configure(state="disabled")

        def done(rows):
            self.scan_btn.configure(state="normal")
            lines = [f"{'id':<8}{'rssi':>6}  name"] + [
                f"{r['id']:<8}{r['rssi'] if r['rssi'] is not None else '?':>6}  {r['name'] or '(anonymous)'}"
                for r in rows] if rows else ["No Maker Detector badges found."]
            self.scan_box.configure(state="normal")
            self.scan_box.delete("1.0", "end")
            self.scan_box.insert("1.0", "\n".join(lines))
            self.scan_box.configure(state="disabled")
            self.ctx.status(f"Scan done: {len(rows)} maker(s)")

        self.ctx.run_task(scan_makers, done, lambda _e: self.scan_btn.configure(state="normal"),
                          title="BLE scan")
