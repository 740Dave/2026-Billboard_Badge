"""Maker Detector tab: broadcast switch, badge profile files, and installing the BLE "Maker Detector" app.

Identity comes from the user profile (Settings > User Settings): the maker handle is shown here
read-only. Applying writes
    /data/owner.json  {"maker_handle", "makes", "broadcast", ...other keys kept}   (lib/owner.py, presence)
    /data/maker.json  {"id", "name": <broadcast text>, "makes", "broadcast"}       (older Maker Detector)
Broadcast text = handle reduced to printable ASCII, clipped to 16 (core.onboarding.broadcast_text).
The badge app advertises (non-connectable, legacy 31-byte adv):
    02 01 06                                  flags
    LL FF  FF FF  'S' 'C' '2' '6'  01  id(3)  name(0-16)   manufacturer-specific AD
On the badge: A = redraw now, B = toggle broadcast.

Badge I/O helpers live in core.onboarding (shared with the first-run wizard); everything runs on
ctx.run_device_task. Installing always validates the import on the badge and HARD resets.

The optional Mac-side BLE scan runs in a child process (core/ble_scan.py): macOS kills a process
that touches CoreBluetooth without Bluetooth permission, so bleak is never imported here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import customtkinter as ctk

from core import onboarding as ob
from core.ble_scan import COMPANY_ID, MAGIC, VERSION, parse_maker_payload  # noqa: F401  (re-exported)
from core.theme import COLORS
from tabs.base import BaseTab, Section, confirm, info

OWNER_PATH = ob.OWNER_PATH
SETTINGS_PATH = ob.SETTINGS_PATH
BADGE_APP_PATH = ob.BADGE_APP_PATH
MAX_NAME = ob.MAX_BROADCAST
MAX_MAKES = ob.MAX_MAKES
SCAN_SECONDS = 10
PROJECT_DIR = Path(__file__).resolve().parents[1]

validate_name = ob.validate_broadcast

BLE_BLOCKED_MSG = ("macOS blocked Bluetooth for this app. Use a badge to scan instead (Maker Detector on "
                   "the badge), or run Badge Manager from Terminal.app and allow Terminal under System "
                   "Settings \u2192 Privacy & Security \u2192 Bluetooth.")

EXPLAINER = (
    "How it works: each badge sends a tiny Bluetooth LE advertisement (manufacturer data, company "
    "id 0xFFFF): \"SC26\", version 1, a 3-byte id and the broadcast text: your maker handle as plain "
    "ASCII, up to 16 characters. Badges listen for each other and count distinct makers nearby. "
    "Nothing connects, nothing is stored off the badge. \"What you make\" stays on your badge and is "
    "not broadcast.\n\n"
    "Privacy: the id is random (the badge generates it on first run), not derived from you or your "
    "hardware. Use a handle, not your legal name. Broadcasting is opt-in: switch it off here, in "
    "Settings, or with button B on the badge. Anything you broadcast can be read by any phone or "
    "computer nearby."
)


# ====================================================================== pure helpers
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
    """Is bleak installed? Checked WITHOUT importing it (importing in the GUI process is unsafe)."""
    try:
        return importlib.util.find_spec("bleak") is not None
    except Exception:  # noqa: BLE001
        return False


def scan_command(seconds: float) -> list[str]:
    """Child process that scans and prints JSON (tests replace this)."""
    return [sys.executable, "-m", "core.ble_scan", str(seconds)]


class ScanBlocked(RuntimeError):
    """macOS (TCC) killed the scanner: no Bluetooth permission."""


def run_scan(seconds: float = SCAN_SECONDS, cmd: list[str] | None = None) -> list[dict]:
    """Blocking (ctx.run_task). Runs the scan in a child process. Raises ScanBlocked if the child
    was killed by a signal / exit 134 / printed nothing, RuntimeError on other failures."""
    cmd = cmd or scan_command(seconds)
    try:
        proc = subprocess.run(cmd, cwd=str(PROJECT_DIR), capture_output=True, text=True,
                              timeout=seconds + 25)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Bluetooth scan did not finish within {seconds + 25:.0f} s") from None
    out = (proc.stdout or "").strip()
    rc = proc.returncode
    # SIGABRT shows up as -6 (killed by signal) or 134 (128 + 6, via a shell); TCC kills leave no output.
    if rc < 0 or rc == 134 or (not out and (rc == 0 or not (proc.stderr or "").strip())):
        raise ScanBlocked(BLE_BLOCKED_MSG)
    line = out.splitlines()[-1] if out else ""
    try:
        data = json.loads(line)
    except ValueError:
        tail = ((proc.stderr or "").strip().splitlines() or [line or f"exit code {rc}"])[-1]
        raise RuntimeError(f"Bluetooth scan failed: {tail[:200]}") from None
    if isinstance(data, dict):
        raise RuntimeError(f"Bluetooth scan failed: {data.get('error', data)}")
    if rc != 0 or not isinstance(data, list):
        raise RuntimeError(f"Bluetooth scan failed (exit code {rc})")
    return data


def scan_makers(seconds: float = SCAN_SECONDS) -> list[dict]:
    """Old name: [{"id","name","rssi","address"}], via the child process."""
    return run_scan(seconds)


def open_user_settings(widget) -> bool:
    """Jump to Settings > User Settings in the main window. False if not possible (e.g. tests)."""
    top = widget.winfo_toplevel()
    tabs = getattr(top, "tabs", None)
    tv = getattr(top, "tabview", None)
    st = tabs.get("Settings") if isinstance(tabs, dict) else None
    if st is None or tv is None:
        return False
    tv.set("Settings")
    try:
        top._tab_changed()
    except Exception:  # noqa: BLE001
        pass
    if hasattr(st, "show_page"):
        st.show_page("User Settings")
    return True


# ====================================================================== tab
class MakerTab(BaseTab):
    TITLE = "Maker Detector"

    def _local(self):
        return ob.find_local_app(self.ctx.config.get("repo_dir"))

    def build(self) -> None:
        prof = ob.get_profile(self.ctx.config)
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True)
        muted = COLORS["muted_text"]

        # ---- profile + broadcast
        s = Section(scroll, "Your badge profile  (/data/owner.json, also /data/maker.json for older badges)")
        s.pack(fill="x", padx=6, pady=6)
        row = ctk.CTkFrame(s.body, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text="Maker handle:", width=110, anchor="w").pack(side="left")
        self.handle_label = ctk.CTkLabel(row, text="", anchor="w", font=ctk.CTkFont(weight="bold"))
        self.handle_label.pack(side="left", padx=6)
        ctk.CTkButton(row, text="Edit in Settings \u2192 User Settings", width=230,
                      command=self.edit_profile).pack(side="left", padx=10)
        row2 = ctk.CTkFrame(s.body, fg_color="transparent")
        row2.pack(fill="x", pady=2)
        ctk.CTkLabel(row2, text="What you make:", width=110, anchor="w").pack(side="left")
        self.makes_label = ctk.CTkLabel(row2, text="", anchor="w")
        self.makes_label.pack(side="left", padx=6)
        self.bcast_label = ctk.CTkLabel(s.body, text="", anchor="w", justify="left", text_color=muted)
        self.bcast_label.pack(fill="x", pady=(2, 0))
        self.note_label = ctk.CTkLabel(s.body, text="", anchor="w", justify="left",
                                       text_color=COLORS["warning_text"])
        self.note_label.pack(fill="x")
        self.broadcast_var = ctk.BooleanVar(value=bool(prof.get("detector_enabled", True)))
        ctk.CTkSwitch(s.body, text="Broadcast my presence (Maker Detector on)", variable=self.broadcast_var,
                      command=self._switch_changed).pack(anchor="w", pady=4)
        self.id_label = ctk.CTkLabel(s.body, text="On the badge: (not read yet)", anchor="w", justify="left")
        self.id_label.pack(fill="x")
        btns = ctk.CTkFrame(s.body, fg_color="transparent")
        btns.pack(fill="x", pady=4)
        ctk.CTkButton(btns, text="Read from badge", command=self.read_from_badge).pack(side="left", padx=(0, 6))
        self.write_btn = ctk.CTkButton(btns, text="Write to badge", fg_color=COLORS["danger"],
                                       hover_color=COLORS["danger_hover"], command=self.write_to_badge)
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
            a.body, anchor="w", justify="left", text_color=muted,
            text=f"Local file: {loc}" if loc else "Local file not found. Looked in:\n  " +
            "\n  ".join(str(p) for p in ob.local_app_candidates(self.ctx.config.get("repo_dir"))))
        self.local_label.pack(fill="x")
        b2 = ctk.CTkFrame(a.body, fg_color="transparent")
        b2.pack(fill="x", pady=4)
        ctk.CTkButton(b2, text="Check", width=80, command=self.check_app).pack(side="left", padx=(0, 6))
        self.install_btn = ctk.CTkButton(b2, text="Install / update app", fg_color=COLORS["danger"],
                                         hover_color=COLORS["danger_hover"], command=self.install)
        self.install_btn.pack(side="left", padx=6)
        ctk.CTkLabel(a.body, anchor="w", justify="left", text_color=muted, wraplength=1000,
                     text="Installing checks that the app loads on the badge (it is removed again if not), "
                          "removes the old Nerd Detector (two detectors would both advertise), keeps your "
                          "badge id, then hard-resets the badge.").pack(fill="x")

        # ---- scan
        sc = Section(scroll, "Scan from this Mac (optional, experimental)")
        sc.pack(fill="x", padx=6, pady=6)
        if bleak_available():
            self.scan_btn = ctk.CTkButton(sc.body, text=f"Scan from this Mac (experimental, {SCAN_SECONDS} s)",
                                          command=self.scan)
            self.scan_btn.pack(anchor="w")
            ctk.CTkLabel(sc.body, anchor="w", justify="left", text_color=muted, wraplength=1000,
                         text="Runs in a separate helper process. macOS may refuse Bluetooth access to apps "
                              "started from a terminal or IDE; a badge running Maker Detector always works.").pack(fill="x")
            self.scan_box = ctk.CTkTextbox(sc.body, height=140, font=ctk.CTkFont(family="Menlo", size=12))
            self.scan_box.pack(fill="x", pady=4)
            self.scan_box.configure(state="disabled")
        else:
            self.scan_btn = None
            self.scan_box = None
            ctk.CTkLabel(sc.body, anchor="w", justify="left",
                         text="Needs the 'bleak' package. Install it with:\n    .venv/bin/pip install bleak\n"
                              "then restart the manager.").pack(fill="x")

        # ---- explainer
        e = Section(scroll, "Protocol & privacy")
        e.pack(fill="x", padx=6, pady=6)
        ctk.CTkLabel(e.body, text=EXPLAINER, anchor="w", justify="left", wraplength=1000).pack(fill="x")

        self.ctx.events.subscribe("profile.changed", lambda _p: self._reload_profile())
        self._reload_profile()

    # ------------------------------------------------------------ profile
    def _handle(self) -> str:
        return ob.profile_handle(ob.get_profile(self.ctx.config))

    def _refresh_labels(self) -> None:
        p = ob.get_profile(self.ctx.config)
        h = ob.profile_handle(p)
        self.handle_label.configure(text=h or "(not set)",
                                    text_color=COLORS["text"] if h else COLORS["danger_text"])
        self.makes_label.configure(text=p.get("makes") or "(not set)")
        bt = ob.broadcast_text(h)
        if not self.broadcast_var.get():
            self.bcast_label.configure(text="Broadcasting is OFF: the badge only listens for others.")
        elif bt.strip():
            self.bcast_label.configure(text=f"Broadcast text (public): \"{bt}\"  + a random badge id")
        else:
            self.bcast_label.configure(text="Broadcast: the anonymous badge id only (no text)")
        self.note_label.configure(text=ob.broadcast_note(h) if self.broadcast_var.get() else "")
        state = "normal" if h else "disabled"
        self.write_btn.configure(state=state)
        self.apply_btn.configure(state=state)

    def _reload_profile(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except Exception:  # noqa: BLE001
            return
        p = ob.get_profile(self.ctx.config)
        self.broadcast_var.set(bool(p.get("detector_enabled", True)))
        self._refresh_labels()

    def _switch_changed(self) -> None:
        self._save_profile()
        self._refresh_labels()

    def _changed(self) -> None:   # old name
        self._refresh_labels()

    def edit_profile(self) -> None:
        if not open_user_settings(self):
            self.ctx.status("Edit your maker handle in Settings \u2192 User Settings")

    def _invalid(self) -> bool:
        return not self._handle()

    def _show_settings(self, d: dict) -> None:
        """d = {"owner": owner.json, "maker": maker.json} as on the badge (either may be empty)."""
        if not d:
            return
        o, m = d.get("owner") or {}, d.get("maker") or {}
        if not o and not m:
            self.id_label.configure(text="On the badge: no owner.json or maker.json yet")
            return
        handle = o.get("maker_handle") if "maker_handle" in o else m.get("display_name") or m.get("name")
        bc = o.get("broadcast") if "broadcast" in o else m.get("broadcast")
        bits = [f"handle \"{handle or ''}\"", "broadcast " + ("on" if bc else "off"),
                f"id {m.get('id') or '(none yet; the badge creates one)'}"]
        if not o:
            bits.append("no owner.json yet (older setup)")
        self.id_label.configure(text="On the badge: " + ", ".join(bits))

    def read_from_badge(self) -> None:
        def job():
            m = ob.read_settings(self.ctx.badge)
            if not m.get("id"):
                bid = ob.existing_id(self.ctx.badge, m)
                if bid:
                    m = dict(m, id=bid)
            return {"owner": ob.read_owner(self.ctx.badge), "maker": m}

        def done(d):
            self.ctx.status("Read badge profile" if (d["owner"] or d["maker"]) else
                            f"No {OWNER_PATH} or {SETTINGS_PATH} on the badge yet")
            self._show_settings(d)
        self.ctx.run_device_task(job, done, title="Reading badge profile")

    def _confirm_write(self, extra: str = "") -> bool:
        bt = ob.broadcast_text(self._handle())
        if self.broadcast_var.get():
            what = (f"\"{bt}\" and a random badge id" if bt.strip() else "A random badge id (no text)")
            msg = (f"{what} will be broadcast publicly over Bluetooth to anyone nearby "
                   "(any phone or laptop can read it), until you switch broadcasting off.")
        else:
            msg = "Broadcasting will be OFF: the badge will only listen for others."
        msg += (f"\n\nThis overwrites your profile in {OWNER_PATH} (other keys kept) and "
                f"{SETTINGS_PATH} on the badge.")
        return confirm(self, "Write badge profile", msg + extra + "\n\nContinue?")

    def _save_profile(self) -> dict:
        """Store the broadcast switch in config "profile" (Tk thread) and return the profile."""
        p = ob.drop_legacy(ob.get_profile(self.ctx.config))
        p["detector_enabled"] = bool(self.broadcast_var.get())
        p.setdefault("maker_handle", "")
        p.setdefault("applied_to", [])
        self.ctx.config.set(ob.PROFILE_KEY, p)
        self.ctx.events.publish("profile.changed", dict(p))
        return p

    def write_to_badge(self) -> None:
        if self._invalid() or not self._confirm_write("\n\nApps re-read it on restart."):
            return
        p = self._save_profile()
        handle, makes, bc = ob.profile_handle(p), p.get("makes") or "", p["detector_enabled"]

        def done(d):
            self._show_settings(d)
            self.ctx.events.publish("badge.files_changed", [OWNER_PATH, SETTINGS_PATH])
            self.ctx.status("Badge profile written (apps re-read it on restart)")
        self.ctx.run_device_task(lambda: ob.write_settings(self.ctx.badge, handle, makes, bc), done,
                                 title="Writing badge profile")

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
    def _scan_text(self, text: str) -> None:
        if self.scan_box is None:
            return
        self.scan_box.configure(state="normal")
        self.scan_box.delete("1.0", "end")
        self.scan_box.insert("1.0", text)
        self.scan_box.configure(state="disabled")

    def scan(self) -> None:
        """Scan in a child process (never bleak in this process: macOS may SIGABRT it)."""
        if self.scan_btn is not None:
            self.scan_btn.configure(state="disabled")
        self._scan_text("Scanning...")

        def done(rows):
            if self.scan_btn is not None:
                self.scan_btn.configure(state="normal")
            lines = [f"{'id':<8}{'rssi':>6}  name"] + [
                f"{r.get('id', '?'):<8}{r['rssi'] if r.get('rssi') is not None else '?':>6}  "
                f"{r.get('name') or '(anonymous)'}" for r in rows] if rows else ["No Maker Detector badges found."]
            self._scan_text("\n".join(lines))
            self.ctx.status(f"Scan done: {len(rows)} maker(s)")

        def failed(e):
            if self.scan_btn is not None:
                self.scan_btn.configure(state="normal")
            self._scan_text(str(e))
            self.ctx.status("Bluetooth blocked by macOS" if isinstance(e, ScanBlocked) else "Bluetooth scan failed")

        self.ctx.run_task(lambda: run_scan(SCAN_SECONDS), done, failed, title="BLE scan")
