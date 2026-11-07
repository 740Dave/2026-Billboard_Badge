"""Settings tab: Wi-Fi network profiles (written to the badge as /data/wifi.json) and general options.

Wi-Fi on the badge comes from Kevin Criqui's fork (kcriqui/2026-Billboard_Badge), whose
lib/wifi.py reads /data/wifi.json: a dict or an ordered LIST of {"ssid", "password"}; the first
visible network wins (SSID match is case-insensitive).

Profile metadata (id, name, ssid, enabled, notes) lives in config key "wifi_profiles".
Passwords live ONLY in the macOS Keychain as "wifi:<profile id>". They are never logged or saved
to config.json.

"User Settings" edits config key "profile" (schema shared with core/onboarding.py):
    {"maker_handle" (<=60), "makes" (<=64), "detector_enabled", "onboarded", "applied_to": [...]}
The broadcast text is derived from the handle (printable ASCII, clipped to 16). Saving keeps unknown
keys plus "onboarded"/"applied_to", drops legacy "name"/"display_name"/"broadcast_text", and
publishes "profile.changed". Writing the badge (/data/owner.json + /data/maker.json) is done only by
core.onboarding.apply_profile_to_badge (lazy import).
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core.config import CONFIG_FILE, KEYRING_SERVICE, get_secret, set_secret
from core.theme import COLORS
from .base import BaseTab, Section, confirm, info

WIFI_JSON = "/data/wifi.json"
WIFI_LIB = "/lib/wifi.py"
WIFI_LIB_REF = "kcriqui/main"
WIFI_LIB_SRC = "firmware/badge/lib/wifi.py"
MASK = "********"


# ------------------------------------------------------------------ pure helpers (testable)
def secret_key(profile_id: str) -> str:
    return f"wifi:{profile_id}"


def new_profile(name: str = "New network", ssid: str = "", enabled: bool = True, notes: str = "") -> dict:
    return {"id": str(uuid.uuid4()), "name": name, "ssid": ssid, "enabled": enabled, "notes": notes}


def build_badge_payload(profiles: list[dict], get_password, masked: bool = False) -> list[dict]:
    """Ordered [{"ssid","password"}] of enabled profiles with a non-empty SSID."""
    out = []
    for p in profiles:
        if not p.get("enabled", True) or not p.get("ssid", "").strip():
            continue
        pwd = get_password(p["id"]) or ""
        out.append({"ssid": p["ssid"].strip(), "password": (MASK if pwd else "") if masked else pwd})
    return out


def parse_badge_file(raw: bytes) -> list[dict]:
    data = json.loads(raw.decode("utf-8"))
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("wifi.json must be an object or a list")
    return [d for d in data if isinstance(d, dict) and d.get("ssid")]


def merge_imported(profiles: list[dict], entries: list[dict], set_password) -> tuple[int, int]:
    """Merge badge entries into profiles by SSID (case-insensitive). Returns (added, updated)."""
    added = updated = 0
    by_ssid = {p.get("ssid", "").lower(): p for p in profiles}
    for e in entries:
        ssid = str(e["ssid"])
        pwd = str(e.get("password", ""))
        p = by_ssid.get(ssid.lower())
        if p is None:
            p = new_profile(name=ssid, ssid=ssid, notes="Imported from badge")
            profiles.append(p)
            by_ssid[ssid.lower()] = p
            added += 1
        else:
            updated += 1
        set_password(p["id"], pwd or None)
    return added, updated


# ------------------------------------------------------------- user profile helpers (testable)
PROFILE_KEY = "profile"
MAX_NAME = 60
MAX_MAKES = 64
MAX_BROADCAST = 16
MAX_HANDLE = MAX_NAME
PROFILE_FORM_KEYS = ("maker_handle", "makes", "detector_enabled")
LEGACY_PROFILE_KEYS = ("name", "display_name", "broadcast_text")
HANDLE_HINT = ("Required. A nickname or handle \u2014 not your legal name; it's shown on your badge and may "
               "be broadcast publicly.")


def is_broadcast_char(c: str) -> bool:
    return " " <= c <= "~"


def clean_broadcast(text: str) -> str:
    """Drop everything that is not printable ASCII (no length cut)."""
    return "".join(c for c in (text or "") if is_broadcast_char(c))


def broadcast_text(handle: str) -> str:
    """What the badge broadcasts for a handle: printable ASCII, clipped to 16 (as lib/presence.py)."""
    return clean_broadcast(str(handle or ""))[:MAX_BROADCAST]


default_broadcast = broadcast_text   # old name


def broadcast_note(handle: str) -> str:
    """Live note: empty when the handle is broadcast exactly as typed."""
    handle = str(handle or "").strip()
    bt = broadcast_text(handle)
    if not handle or bt == handle:
        return ""
    if not bt.strip():
        return "No plain ASCII characters: only the anonymous badge id is broadcast over Bluetooth."
    parts = []
    if clean_broadcast(handle) != handle:
        parts.append("non-ASCII characters removed")
    if len(clean_broadcast(handle)) > MAX_BROADCAST:
        parts.append(f"clipped to {MAX_BROADCAST} characters")
    return f"Broadcast as \"{bt}\" ({', '.join(parts)}); the badge itself shows the full handle."


def load_profile(config) -> dict:
    """Stored profile, with an old "name" read as "maker_handle" (core.config migrates it on load)."""
    p = config.get(PROFILE_KEY) or {}
    if not isinstance(p, dict):
        return {}
    p = dict(p)
    if "name" in p and "maker_handle" not in p:
        p["maker_handle"] = p.pop("name")
    return p


def profile_form(p: dict) -> dict:
    """The editable fields of a stored profile, with defaults filled in."""
    return {"maker_handle": str(p.get("maker_handle") or p.get("name") or ""),
            "makes": str(p.get("makes") or ""),
            "detector_enabled": bool(p.get("detector_enabled", False))}


def effective_form(form: dict) -> dict:
    """What would be stored: stripped."""
    return {"maker_handle": form["maker_handle"].strip(), "makes": form["makes"].strip(),
            "detector_enabled": bool(form["detector_enabled"])}


def validate_form(form: dict) -> dict[str, str]:
    """field -> error message (empty dict = valid). Expects an effective_form()."""
    err = {}
    h = form["maker_handle"]
    if not h:
        err["maker_handle"] = "Maker handle is required"
    elif len(h) > MAX_HANDLE:
        err["maker_handle"] = f"Too long ({len(h)}/{MAX_HANDLE})"
    elif any(not c.isprintable() for c in h):
        err["maker_handle"] = "Contains non-printable characters"
    if len(form["makes"]) > MAX_MAKES:
        err["makes"] = f"Too long ({len(form['makes'])}/{MAX_MAKES})"
    elif any(not c.isprintable() for c in form["makes"]):
        err["makes"] = "Contains non-printable characters"
    return err


def merge_profile(current: dict, form: dict) -> dict:
    """current (as stored, unknown keys kept, legacy keys dropped) updated with the form fields."""
    out = {k: v for k, v in current.items() if k not in LEGACY_PROFILE_KEYS}
    out.update(effective_form(form))
    out.setdefault("applied_to", list(current.get("applied_to") or []))
    return out


# ------------------------------------------------------------------------------- the tab
class SettingsTab(BaseTab):
    TITLE = "Settings"

    # Dialog hooks (tests replace these to avoid modal dialogs).
    def _confirm(self, title, msg):
        return confirm(self, title, msg)

    def _info(self, title, msg):
        info(self, title, msg)

    def _ask_unsaved(self, name):
        return messagebox.askyesnocancel("Unsaved changes", f"Save changes to \"{name}\"?", parent=self)

    # keychain wrappers (module-level names so tests can monkeypatch them)
    @staticmethod
    def _get_pw(pid):
        return get_secret(secret_key(pid))

    @staticmethod
    def _set_pw(pid, pwd):
        return set_secret(secret_key(pid), pwd)

    # ------------------------------------------------------------------ build
    def build(self) -> None:
        self.profiles: list[dict] = [dict(p) for p in self.ctx.config.get("wifi_profiles") or []]
        self.sel: int | None = None
        self.dirty = False
        self._loading = False
        self._last_test = None

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        nav = ctk.CTkFrame(self, width=170)
        nav.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        ctk.CTkLabel(nav, text="Settings", font=ctk.CTkFont(size=15, weight="bold")).pack(
            padx=12, pady=(12, 8), anchor="w")
        self.pages: dict[str, ctk.CTkFrame] = {}
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        for name in ("User Settings", "Wi-Fi networks", "General"):
            b = ctk.CTkButton(nav, text=name, anchor="w", width=150, command=lambda n=name: self.show_page(n))
            b.pack(padx=10, pady=3)
            self.nav_buttons[name] = b

        self.pages["User Settings"] = self._build_user(self)
        self.pages["Wi-Fi networks"] = self._build_wifi(self)
        self.pages["General"] = self._build_general(self)
        self.show_page("User Settings")
        self._refresh_list()
        if self.profiles:
            self.select(0)
        else:
            self._set_detail_state(False)
        self._load_token()
        self.load_user_profile()
        self.ctx.events.subscribe("profile.changed", self._on_profile_event)

    def show_page(self, name: str) -> None:
        for n, page in self.pages.items():
            page.grid_forget()
            self.nav_buttons[n].configure(fg_color=COLORS["row"] if n != name else COLORS["accent"],
                                          hover_color=COLORS["row_hover"] if n != name else COLORS["accent_hover"],
                                          text_color=COLORS["text"] if n != name else COLORS["text_on_accent"])
        self.pages[name].grid(row=0, column=1, sticky="nsew")
        self.current_page = name

    # ====================================================== User Settings page
    def _build_user(self, parent):
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        self.pf_base: dict = {}          # effective form as last loaded/saved
        self._pf_loading = False
        self.pf_dirty = False
        self.pf_errors: dict[str, str] = {}
        self.pf_vars = {"maker_handle": ctk.StringVar(), "makes": ctk.StringVar(),
                        "detector_enabled": ctk.BooleanVar(value=False)}
        muted, danger = COLORS["muted_text"], COLORS["danger_text"]

        s = Section(page, "User Settings")
        s.pack(fill="x", pady=(0, 8))
        d = s.body
        d.grid_columnconfigure(1, weight=1)
        self.pf_entries: dict[str, ctk.CTkEntry] = {}
        self.pf_err_labels: dict[str, ctk.CTkLabel] = {}
        self.pf_counters: dict[str, ctk.CTkLabel] = {}

        def field(row, key, label, hint, counter=False):
            ctk.CTkLabel(d, text=label, anchor="w").grid(row=row, column=0, sticky="w", pady=(6, 0), padx=(0, 10))
            e = ctk.CTkEntry(d, textvariable=self.pf_vars[key])
            e.grid(row=row, column=1, sticky="ew", pady=(6, 0))
            self.pf_entries[key] = e
            if counter:
                c = ctk.CTkLabel(d, text="", width=56, anchor="e", text_color=muted)
                c.grid(row=row, column=2, sticky="e", padx=(6, 0), pady=(6, 0))
                self.pf_counters[key] = c
            err = ctk.CTkLabel(d, text="", text_color=danger, anchor="w", justify="left")
            err.grid(row=row + 1, column=1, sticky="w")
            self.pf_err_labels[key] = err
            if hint:
                ctk.CTkLabel(d, text=hint, text_color=muted, anchor="w", justify="left").grid(
                    row=row + 2, column=1, columnspan=2, sticky="w")

        field(0, "maker_handle", "Maker handle *", HANDLE_HINT, counter=True)
        field(3, "makes", "What I like to make", f"Up to {MAX_MAKES} characters, e.g. 'synths and SAOs'.",
              counter=True)
        self.pf_switch = ctk.CTkSwitch(d, text="Broadcast my presence (Maker Detector)",
                                       variable=self.pf_vars["detector_enabled"], command=self._pf_update)
        self.pf_switch.grid(row=6, column=1, sticky="w", pady=(10, 0))
        ctk.CTkLabel(d, text="On: your badge advertises your handle (plain ASCII, up to 16 characters) to "
                     "nearby badges. Off: it only listens.",
                     text_color=muted, anchor="w", justify="left").grid(row=7, column=1, columnspan=2, sticky="w")
        self.pf_bcast_note = ctk.CTkLabel(d, text="", text_color=COLORS["warning_text"], anchor="w", justify="left")
        self.pf_bcast_note.grid(row=8, column=1, columnspan=2, sticky="w")

        bar = ctk.CTkFrame(d, fg_color="transparent")
        bar.grid(row=14, column=1, columnspan=2, sticky="ew", pady=(14, 0))
        self.pf_save_btn = ctk.CTkButton(bar, text="Save", width=90, command=self.save_user_profile)
        self.pf_save_btn.pack(side="left")
        self.pf_revert_btn = ctk.CTkButton(bar, text="Revert", width=90, fg_color=COLORS["danger"],
                                           hover_color=COLORS["danger_hover"], command=self.revert_user_profile)
        self.pf_revert_btn.pack(side="left", padx=6)
        self.pf_send_btn = ctk.CTkButton(bar, text="Save & send to badge", width=170,
                                         fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"], command=self.save_and_send_profile)
        self.pf_send_btn.pack(side="left", padx=(12, 0))
        self.pf_dirty_label = ctk.CTkLabel(bar, text="", text_color=COLORS["warning_text"])
        self.pf_dirty_label.pack(side="left", padx=10)
        self.pf_action = ctk.CTkLabel(d, text="", anchor="w", justify="left")
        self.pf_action.grid(row=15, column=1, columnspan=2, sticky="w", pady=(6, 0))
        self.pf_info = ctk.CTkLabel(d, text="", text_color=muted, anchor="w", justify="left")
        self.pf_info.grid(row=16, column=1, columnspan=2, sticky="w", pady=(2, 0))

        s = Section(page, "First-time setup")
        s.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(s.body, text="Walk through the welcome wizard again (it also offers your profile to "
                     "the connected badge).", text_color=muted, anchor="w", justify="left").pack(fill="x")
        ctk.CTkButton(s.body, text="Run first-time setup again", width=200, command=self.rerun_setup).pack(anchor="w", pady=(6, 0))

        for k in ("maker_handle", "makes"):
            self.pf_vars[k].trace_add("write", lambda *_: self._pf_update())
        return page

    # ------------------------------------------------------------ form state
    def pf_form(self) -> dict:
        return {k: v.get() for k, v in self.pf_vars.items()}

    def load_user_profile(self) -> None:
        """(Re)load the form from config, discarding unsaved edits."""
        form = profile_form(load_profile(self.ctx.config))
        self._pf_loading = True
        for k, v in self.pf_vars.items():
            v.set(form[k])
        self._pf_loading = False
        self.pf_base = effective_form(form)
        self._pf_update()

    def _pf_update(self) -> None:
        if self._pf_loading:
            return
        form = self.pf_form()
        eff = effective_form(form)
        self.pf_errors = validate_form(eff)
        for k, lbl in self.pf_err_labels.items():
            lbl.configure(text=self.pf_errors.get(k, ""))
        self.pf_counters["makes"].configure(
            text=f"{len(eff['makes'])}/{MAX_MAKES}",
            text_color=COLORS["danger_text"] if len(eff["makes"]) > MAX_MAKES else COLORS["muted_text"])
        n = len(eff["maker_handle"])
        self.pf_counters["maker_handle"].configure(
            text=f"{n}/{MAX_HANDLE}",
            text_color=COLORS["danger_text"] if n > MAX_HANDLE else COLORS["muted_text"])
        note = broadcast_note(eff["maker_handle"])
        if note and not eff["detector_enabled"]:
            note += " (Only if you switch broadcasting on.)"
        self.pf_bcast_note.configure(text=note)
        self.pf_dirty = eff != self.pf_base
        self.pf_save_btn.configure(state="normal" if self.pf_dirty else "disabled")
        self.pf_revert_btn.configure(state="normal" if self.pf_dirty else "disabled")
        self.pf_dirty_label.configure(text="● Unsaved changes" if self.pf_dirty else "")
        self._pf_refresh_info()

    def _pf_refresh_info(self) -> None:
        p = load_profile(self.ctx.config)
        n = len(p.get("applied_to") or [])
        parts = [f"Sent to {n} badge{'s' if n != 1 else ''}" if n else "Not sent to a badge yet",
                 "first-time setup done" if p.get("onboarded") else "first-time setup not done yet"]
        self.pf_info.configure(text="; ".join(parts) + ".")

    def revert_user_profile(self) -> None:
        self.load_user_profile()
        self.pf_action.configure(text="")

    def save_user_profile(self) -> dict | None:
        """Validate and store locally. Returns the saved profile, or None if invalid."""
        eff = effective_form(self.pf_form())
        errs = validate_form(eff)
        if errs:
            self._pf_update()
            self.pf_action.configure(text="Not saved: " + "; ".join(errs.values()),
                                     text_color=COLORS["danger_text"])
            return None
        prof = merge_profile(load_profile(self.ctx.config), self.pf_form())
        self.ctx.config.set(PROFILE_KEY, prof)
        self.load_user_profile()
        self.pf_action.configure(text="Saved on this computer.", text_color=COLORS["text"])
        self.ctx.status("User settings saved")
        self.ctx.events.publish("profile.changed", dict(prof))
        return prof

    def save_and_send_profile(self) -> None:
        prof = self.save_user_profile() if self.pf_dirty else load_profile(self.ctx.config)
        if prof is None:
            return
        if validate_form(effective_form(profile_form(prof))):
            self.pf_action.configure(text="Fix the fields above first.", text_color=COLORS["danger_text"])
            return
        try:
            from core.onboarding import apply_profile_to_badge
        except Exception as e:  # noqa: BLE001  (module missing or mid-edit)
            self.pf_action.configure(text=f"Saved locally; sending is unavailable ({e}).",
                                     text_color=COLORS["warning_text"])
            self._info("Can't send to badge", "The badge setup module (core/onboarding.py) could not be "
                       f"loaded:\n\n{e}\n\nYour settings were saved on this computer.")
            return

        from core import onboarding as ob

        def done(_res):
            self.pf_action.configure(text="Sent to the badge.", text_color=COLORS["success_text"])
            self.ctx.events.publish("profile.changed", load_profile(self.ctx.config))  # applied_to changed

        def failed(e):
            if str(e) == "no port":
                e = "select the badge's port in the top bar first"
            self.pf_action.configure(text=f"Sending failed: {e}", text_color=COLORS["danger_text"])

        local = ob.find_local_app(self.ctx.config.get("repo_dir")) if prof.get("detector_enabled") else None

        def planned(plan):
            # The red button overwrites badge files: show exactly what will be written first.
            if plan.get("error"):
                failed(plan["error"])
                return
            if not self._confirm("Send profile to badge", "This writes to the badge:\n\n"
                                 + ob.describe_plan(plan) + "\n\nContinue?"):
                self.pf_action.configure(text="Not sent; nothing was written. Hard-reset the badge to bring "
                                              "its UI back (top bar).", text_color=COLORS["warning_text"])
                return
            self.pf_action.configure(text="Sending to the badge...", text_color=COLORS["text"])
            apply_profile_to_badge(self.ctx, dict(prof), on_done=done, on_error=failed)

        self.pf_action.configure(text="Checking what's on the badge...", text_color=COLORS["text"])
        self.ctx.run_device_task(lambda: ob.plan_job(self.ctx.badge, dict(prof), local), planned, failed,
                                 title="Checking badge profile")

    def _on_profile_event(self, _payload) -> None:
        try:
            if not self.winfo_exists():
                return
        except Exception:  # noqa: BLE001  (tab destroyed)
            return
        if not self.pf_dirty:
            self.load_user_profile()
        else:
            self._pf_refresh_info()
            self.pf_action.configure(text="The profile was changed elsewhere; Revert to load it.",
                                     text_color=COLORS["warning_text"])

    def rerun_setup(self) -> None:
        if self.pf_dirty and not self._confirm("Unsaved changes", "Discard your unsaved user settings and "
                                               "run first-time setup?"):
            return
        try:
            from core.onboarding import maybe_run_onboarding
        except Exception as e:  # noqa: BLE001
            self._info("Setup unavailable", f"The first-time setup (core/onboarding.py) could not be loaded:\n\n{e}")
            return
        win = maybe_run_onboarding(self.winfo_toplevel(), self.ctx, force=True)
        self.setup_window = win

        def wait():
            try:
                alive = win is not None and win.winfo_exists()
            except Exception:  # noqa: BLE001
                alive = False
            if alive:
                self.after(300, wait)
            else:
                self.load_user_profile()
                self.pf_action.configure(text="")
                self.ctx.events.publish("profile.changed", load_profile(self.ctx.config))
        wait()

    # =========================================================== Wi-Fi page
    def _build_wifi(self, parent):
        page = ctk.CTkFrame(parent, fg_color="transparent")
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(0, weight=1)

        # ---- master
        master = Section(page, "Networks (top = tried first)")
        master.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        self.list_frame = ctk.CTkScrollableFrame(master.body, width=240)
        self.list_frame.pack(fill="both", expand=True)
        btns = ctk.CTkFrame(master.body, fg_color="transparent")
        btns.pack(fill="x", pady=(6, 0))
        for i, (txt, cmd) in enumerate([("Add", self.add_profile), ("Duplicate", self.duplicate_profile),
                                        ("Delete", self.delete_profile), ("Move up", lambda: self.move(-1)),
                                        ("Move down", lambda: self.move(1))]):
            ctk.CTkButton(btns, text=txt, width=80, command=cmd,
                          fg_color=COLORS["danger"] if txt == "Delete" else None,
                          hover_color=COLORS["danger_hover"] if txt == "Delete" else None).grid(
                row=i // 3, column=i % 3, padx=2, pady=2, sticky="ew")

        # ---- detail
        detail = Section(page, "Network details")
        detail.grid(row=0, column=1, sticky="nsew")
        d = detail.body
        d.grid_columnconfigure(1, weight=1)
        self.v_name = ctk.StringVar()
        self.v_ssid = ctk.StringVar()
        self.v_pwd = ctk.StringVar()
        self.v_enabled = ctk.BooleanVar(value=True)
        row = 0
        ctk.CTkLabel(d, text="Profile name").grid(row=row, column=0, sticky="w", pady=4)
        self.e_name = ctk.CTkEntry(d, textvariable=self.v_name)
        self.e_name.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        row += 1
        ctk.CTkLabel(d, text="SSID").grid(row=row, column=0, sticky="w", pady=4)
        self.e_ssid = ctk.CTkEntry(d, textvariable=self.v_ssid, placeholder_text="network name")
        self.e_ssid.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        row += 1
        ctk.CTkLabel(d, text="Password").grid(row=row, column=0, sticky="w", pady=4)
        self.e_pwd = ctk.CTkEntry(d, textvariable=self.v_pwd, show="•")
        self.e_pwd.grid(row=row, column=1, sticky="ew", pady=4)
        self.b_show = ctk.CTkButton(d, text="Show", width=60, command=self.toggle_password)
        self.b_show.grid(row=row, column=2, padx=(4, 0), pady=4)
        row += 1
        self.sw_enabled = ctk.CTkSwitch(d, text="Enabled (written to the badge)", variable=self.v_enabled,
                                        command=self._mark_dirty)
        self.sw_enabled.grid(row=row, column=1, sticky="w", pady=4)
        row += 1
        ctk.CTkLabel(d, text="Notes").grid(row=row, column=0, sticky="nw", pady=4)
        self.t_notes = ctk.CTkTextbox(d, height=90)
        self.t_notes.grid(row=row, column=1, columnspan=2, sticky="ew", pady=4)
        self.t_notes.bind("<<Modified>>", self._notes_modified)
        row += 1
        bar = ctk.CTkFrame(d, fg_color="transparent")
        bar.grid(row=row, column=1, columnspan=2, sticky="ew", pady=(8, 0))
        self.b_save = ctk.CTkButton(bar, text="Save", width=90, command=self.save_profile)
        self.b_save.pack(side="left")
        self.b_revert = ctk.CTkButton(bar, text="Revert", width=90, fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"], command=self.revert)
        self.b_revert.pack(side="left", padx=6)
        self.dirty_label = ctk.CTkLabel(bar, text="", text_color=COLORS["warning_text"])
        self.dirty_label.pack(side="left", padx=8)
        row += 1
        ctk.CTkLabel(d, text="Passwords are kept in the macOS Keychain, never in config.json.",
                     text_color=COLORS["muted_text"], anchor="w").grid(row=row, column=1, columnspan=2, sticky="w", pady=(10, 0))
        for v in (self.v_name, self.v_ssid, self.v_pwd):
            v.trace_add("write", lambda *_: self._mark_dirty())

        # ---- bottom actions
        actions = ctk.CTkFrame(page)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ctk.CTkButton(actions, text="Preview badge file", command=self.preview).pack(side="left", padx=6, pady=8)
        ctk.CTkButton(actions, text="Write to badge", fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"], command=self.write_to_badge).pack(side="left", padx=6)
        ctk.CTkButton(actions, text="Read from badge", command=self.read_from_badge).pack(side="left", padx=6)
        ctk.CTkButton(actions, text="Test on badge", command=self.test_on_badge).pack(
            side="left", padx=6)
        self.action_label = ctk.CTkLabel(actions, text="", anchor="w")
        self.action_label.pack(side="left", padx=10, fill="x", expand=True)
        ctk.CTkLabel(page, text="Wi-Fi support (lib/wifi.py) by Kevin Criqui, from the kcriqui fork.",
                     text_color=COLORS["muted_text"], anchor="w").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        return page

    # -------------------------------------------------------------- master list
    def _refresh_list(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        if not self.profiles:
            ctk.CTkLabel(self.list_frame, text="No networks yet.\nClick Add.", text_color=COLORS["muted_text"]).pack(pady=20)
        accent = COLORS["accent"]
        for i, p in enumerate(self.profiles):
            mark = "\u2713" if p.get("enabled", True) else "  "
            label = f"{mark}  {i + 1}. {p.get('name') or '(unnamed)'}"
            if p.get("ssid"):
                label += f"   ({p['ssid']})"
            ctk.CTkButton(self.list_frame, text=label, anchor="w",
                          fg_color=accent if i == self.sel else "transparent",
                          text_color=None if i == self.sel else COLORS["text"],
                          hover_color=COLORS["accent_hover"] if i == self.sel else COLORS["row_hover"],
                          command=lambda i=i: self.select(i)).pack(fill="x", pady=1)

    def select(self, i: int) -> None:
        if i == self.sel:
            return
        if not self._resolve_unsaved():
            return
        self.sel = i
        self._load_detail()
        self._refresh_list()

    def _resolve_unsaved(self) -> bool:
        """True if it's OK to leave the current profile (saved, discarded, or nothing to do)."""
        if not self.dirty or self.sel is None:
            return True
        ans = self._ask_unsaved(self.profiles[self.sel].get("name"))
        if ans is None:
            return False
        if ans:
            self.save_profile()
        else:
            self.dirty = False
        return True

    def _persist(self) -> None:
        self.ctx.config.set("wifi_profiles", [dict(p) for p in self.profiles])

    def add_profile(self) -> None:
        if not self._resolve_unsaved():
            return
        self.profiles.append(new_profile())
        self._persist()
        self.sel = None
        self.select(len(self.profiles) - 1)
        self.e_name.focus_set()

    def duplicate_profile(self) -> None:
        if self.sel is None or not self._resolve_unsaved():
            return
        src = self.profiles[self.sel]
        p = new_profile(name=f"{src.get('name', '')} copy", ssid=src.get("ssid", ""),
                        enabled=src.get("enabled", True), notes=src.get("notes", ""))
        pwd = self._get_pw(src["id"])
        if pwd:
            self._set_pw(p["id"], pwd)
        self.profiles.insert(self.sel + 1, p)
        self._persist()
        target = self.sel + 1
        self.sel = None
        self.select(target)

    def delete_profile(self) -> None:
        if self.sel is None:
            return
        p = self.profiles[self.sel]
        if not self._confirm("Delete network", f"Delete \"{p.get('name')}\" and its saved password?\n"
                             "(This does not change the badge until you write to it again.)"):
            return
        self._set_pw(p["id"], None)
        del self.profiles[self.sel]
        self._persist()
        self.dirty = False
        nxt = min(self.sel, len(self.profiles) - 1)
        self.sel = None
        if nxt >= 0:
            self.select(nxt)
        else:
            self._clear_detail()
            self._refresh_list()

    def move(self, delta: int) -> None:
        if self.sel is None:
            return
        j = self.sel + delta
        if not 0 <= j < len(self.profiles):
            return
        self.profiles[self.sel], self.profiles[j] = self.profiles[j], self.profiles[self.sel]
        self._persist()  # unsaved detail edits live in the widgets, so they survive the move
        self.sel = j
        self._refresh_list()

    # -------------------------------------------------------------- detail pane
    def _set_detail_state(self, on: bool) -> None:
        st = "normal" if on else "disabled"
        for w in (self.e_name, self.e_ssid, self.e_pwd, self.b_show, self.sw_enabled, self.t_notes,
                  self.b_save, self.b_revert):
            w.configure(state=st)

    def _clear_detail(self) -> None:
        self._loading = True
        for v in (self.v_name, self.v_ssid, self.v_pwd):
            v.set("")
        self.t_notes.configure(state="normal")
        self.t_notes.delete("1.0", "end")
        self._loading = False
        self._set_dirty(False)
        self._set_detail_state(False)

    def _load_detail(self) -> None:
        if self.sel is None:
            self._clear_detail()
            return
        self._set_detail_state(True)
        p = self.profiles[self.sel]
        self._loading = True
        self.v_name.set(p.get("name", ""))
        self.v_ssid.set(p.get("ssid", ""))
        self.v_pwd.set(self._get_pw(p["id"]) or "")
        self.v_enabled.set(bool(p.get("enabled", True)))
        self.t_notes.delete("1.0", "end")
        self.t_notes.insert("1.0", p.get("notes", ""))
        self.t_notes.edit_modified(False)
        self.e_pwd.configure(show="•")
        self.b_show.configure(text="Show")
        self._loading = False
        self._set_dirty(False)

    def _notes_modified(self, _e=None) -> None:
        if self.t_notes.edit_modified():
            self.t_notes.edit_modified(False)
            self._mark_dirty()

    def _mark_dirty(self) -> None:
        if not self._loading and self.sel is not None:
            self._set_dirty(True)

    def _set_dirty(self, d: bool) -> None:
        self.dirty = d
        self.dirty_label.configure(text="\u25cf Unsaved changes" if d else "")

    def toggle_password(self) -> None:
        hidden = self.e_pwd.cget("show") != ""
        self.e_pwd.configure(show="" if hidden else "•")
        self.b_show.configure(text="Hide" if hidden else "Show")

    def save_profile(self) -> None:
        if self.sel is None:
            return
        p = self.profiles[self.sel]
        p["name"] = self.v_name.get().strip() or "(unnamed)"
        p["ssid"] = self.v_ssid.get().strip()
        p["enabled"] = bool(self.v_enabled.get())
        p["notes"] = self.t_notes.get("1.0", "end").rstrip("\n")
        pwd = self.v_pwd.get()
        if not self._set_pw(p["id"], pwd or None):
            self._info("Keychain unavailable", "Could not store the password in the macOS Keychain.")
        self._persist()
        self._set_dirty(False)
        self._refresh_list()
        self.ctx.status(f"Saved Wi-Fi profile {p['name']}")

    def revert(self) -> None:
        if self.sel is None:
            return
        self._load_detail()  # self.profiles always mirrors the saved metadata
        self._refresh_list()

    # ----------------------------------------------------------- badge actions
    def _payload(self, masked: bool = False) -> list[dict]:
        return build_badge_payload(self.profiles, self._get_pw, masked=masked)

    def preview_text(self) -> str:
        return json.dumps(self._payload(masked=True), indent=2)

    def preview(self) -> None:
        if not self._resolve_unsaved():
            return
        win = ctk.CTkToplevel(self)
        win.title(f"Preview of {WIFI_JSON}")
        win.geometry("520x420")
        ctk.CTkLabel(win, text=f"{WIFI_JSON} (passwords masked here; written in plain text on the badge)",
                     anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        box = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Menlo", size=12))
        box.pack(fill="both", expand=True, padx=10, pady=4)
        box.insert("1.0", self.preview_text())
        box.configure(state="disabled")
        ctk.CTkButton(win, text="Close", command=win.destroy).pack(pady=8)
        win.after(100, win.lift)
        self.preview_window = win

    def _set_action(self, text: str) -> None:
        self.action_label.configure(text=text)

    def write_to_badge(self) -> None:
        if not self._resolve_unsaved():
            return
        payload = self._payload()
        if not payload:
            self._info("Nothing to write", "There are no enabled networks with an SSID.")
            return
        names = "\n".join(f"  {i + 1}. {e['ssid']}" for i, e in enumerate(payload))
        if not self._confirm("Write Wi-Fi to badge",
                             f"Write {len(payload)} network(s) to {WIFI_JSON}, tried in this order:\n{names}\n\n"
                             "Note: the badge stores this file (including passwords) in PLAIN TEXT; anyone "
                             "with USB access to the badge can read it."):
            return
        data = (json.dumps(payload) + "\n").encode()
        badge = self.ctx.badge

        def job():
            badge.write_bytes(data, WIFI_JSON)
            return badge.exists(WIFI_LIB)

        def done(has_lib):
            self.ctx.log(f"Wrote {len(payload)} Wi-Fi network(s) to {WIFI_JSON}")  # no passwords
            self._set_action(f"Wrote {len(payload)} network(s) to the badge")
            self.ctx.status("Wi-Fi settings written")
            self.ctx.events.publish("badge.files_changed", [WIFI_JSON])
            if not has_lib:
                self.offer_install_lib()

        self.ctx.run_device_task(job, done, lambda e: self._set_action(f"Write failed: {e}"),
                                 title="Writing Wi-Fi settings")

    def offer_install_lib(self) -> None:
        if not self._confirm("Install Wi-Fi library?",
                             f"The badge has no {WIFI_LIB}, so apps can't use these settings yet.\n\n"
                             f"Install it from {WIFI_LIB_REF}? lib/wifi.py was written by Kevin Criqui "
                             "(kcriqui/2026-Billboard_Badge); thanks Kevin!"):
            return
        sources, badge = self.ctx.sources, self.ctx.badge

        def fetched(src: bytes):
            def job():
                badge.write_bytes(src, WIFI_LIB)

            def done(_):
                self.ctx.log(f"Installed {WIFI_LIB} from {WIFI_LIB_REF} (by Kevin Criqui)")
                self._set_action(f"Installed {WIFI_LIB}")
                self.ctx.events.publish("badge.files_changed", [WIFI_LIB])
            self.ctx.run_device_task(job, done, title="Installing lib/wifi.py")

        self.ctx.run_task(lambda: sources.show_file(WIFI_LIB_REF, WIFI_LIB_SRC), fetched,
                          lambda e: self._info("Could not fetch lib/wifi.py",
                                               f"{e}\n\nSync forks in the Firmware tab and try again."),
                          title="Fetching lib/wifi.py")

    def read_from_badge(self) -> None:
        if not self._resolve_unsaved():
            return
        badge = self.ctx.badge

        def job():
            if not badge.exists(WIFI_JSON):
                return None
            return badge.read_file(WIFI_JSON)

        self.ctx.run_device_task(job, self._imported, lambda e: self._set_action(f"Read failed: {e}"),
                                 title="Reading Wi-Fi settings")

    def _imported(self, raw) -> None:
        if raw is None:
            self._info("No Wi-Fi file", f"The badge has no {WIFI_JSON}.")
            return
        try:
            entries = parse_badge_file(raw)
        except ValueError as e:
            self._info("Unreadable file", f"{WIFI_JSON} is not valid: {e}")
            return
        added, updated = merge_imported(self.profiles, entries, self._set_pw)
        # imported metadata is saved right away (passwords already went to the keychain)
        self._persist()
        msg = f"Imported from badge: {added} new, {updated} updated"
        self.ctx.log(msg)
        self._set_action(msg)
        cur = self.sel
        self.sel = None
        if self.profiles:
            self.select(cur if cur is not None else 0)
        self._refresh_list()

    def test_on_badge(self) -> None:
        from core.fakebadge import FakeBadgeService
        if isinstance(self.ctx.badge, FakeBadgeService):
            self._last_test = "not simulated"
            self._set_action("Wi-Fi test is not simulated on the fake badge (needs real hardware).")
            return
        badge = self.ctx.badge
        code = ("import lib.wifi as w\n"
                "try:\n"
                "    print('WIFI_OK', w.connect())\n"
                "except Exception as e:\n"
                "    print('WIFI_ERR', e)\n")

        def done(out: str):
            lines = [l for l in (out or "").splitlines() if l.startswith(("WIFI_OK", "WIFI_ERR"))]
            last = lines[-1] if lines else (out or "").strip()[-200:] or "no output"
            if last.startswith("WIFI_OK"):
                self._last_test = last[8:].strip()
                self._set_action(f"Badge connected: IP {self._last_test}")
            else:
                self._last_test = last.replace("WIFI_ERR", "").strip()
                self._set_action(f"Wi-Fi test failed: {self._last_test}")

        self._set_action("Testing Wi-Fi on the badge (up to 40 s)...")
        self.ctx.run_device_task(lambda: badge.exec_py(code, timeout=40), done,
                                 lambda e: self._set_action(f"Wi-Fi test failed: {e}"), title="Testing Wi-Fi")

    # ========================================================= General page
    def _build_general(self, parent):
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        cfg = self.ctx.config

        s = Section(page, "Appearance")
        s.pack(fill="x", pady=(0, 8))
        self.appearance = ctk.CTkSegmentedButton(s.body, values=["System", "Light", "Dark"],
                                                 command=self.set_appearance)
        self.appearance.set(cfg.get("appearance") or "Dark")
        self.appearance.pack(anchor="w")

        s = Section(page, "Backups")
        s.pack(fill="x", pady=(0, 8))
        row = ctk.CTkFrame(s.body, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="Backup folder").pack(side="left")
        self.v_backup = ctk.StringVar(value=cfg.get("backup_dir"))
        ctk.CTkEntry(row, textvariable=self.v_backup, state="disabled").pack(side="left", fill="x", expand=True,
                                                                             padx=6)
        ctk.CTkButton(row, text="Choose...", width=90, command=self.choose_backup_dir).pack(side="left", padx=2)
        ctk.CTkButton(row, text="Open in Finder", width=110,
                      command=lambda: self.reveal(self.v_backup.get())).pack(side="left", padx=2)
        self.v_backup_first = ctk.BooleanVar(value=bool(cfg.get("backup_before_push")))
        ctk.CTkSwitch(s.body, text="Back up badge before installing firmware", variable=self.v_backup_first,
                      command=lambda: cfg.set("backup_before_push", bool(self.v_backup_first.get()))).pack(
            anchor="w", pady=(8, 0))

        s = Section(page, "Git cache (official repo + forks)")
        s.pack(fill="x", pady=(0, 8))
        row = ctk.CTkFrame(s.body, fg_color="transparent")
        row.pack(fill="x")
        e = ctk.CTkEntry(row)
        e.insert(0, cfg.get("repo_dir"))
        e.configure(state="disabled")
        e.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Open in Finder", width=110,
                      command=lambda: self.reveal(cfg.get("repo_dir"))).pack(side="left", padx=6)
        ctk.CTkLabel(s.body, text=f"To change it, edit \"repo_dir\" in {CONFIG_FILE} and restart the app.",
                     text_color=COLORS["muted_text"], anchor="w").pack(fill="x", pady=(4, 0))

        s = Section(page, "Local app folders")
        s.pack(fill="x", pady=(0, 8))
        self.dirs_frame = ctk.CTkFrame(s.body, fg_color="transparent")
        self.dirs_frame.pack(fill="x")
        ctk.CTkButton(s.body, text="Add folder...", width=110, command=self.add_app_dir).pack(anchor="w", pady=(6, 0))
        self._refresh_dirs()

        s = Section(page, "GitHub token (optional)")
        s.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(s.body, text="Raises the GitHub API limit (60/hour without) when syncing forks. "
                     "A token with no scopes is enough. Stored in the Keychain.",
                     text_color=COLORS["muted_text"], anchor="w", justify="left").pack(fill="x")
        row = ctk.CTkFrame(s.body, fg_color="transparent")
        row.pack(fill="x", pady=(4, 0))
        self.v_token = ctk.StringVar()
        self.e_token = ctk.CTkEntry(row, textvariable=self.v_token, show="•", width=340,
                                    placeholder_text="ghp_... / github_pat_...")
        self.e_token.pack(side="left")
        ctk.CTkButton(row, text="Save", width=70, command=self.save_token).pack(side="left", padx=4)
        ctk.CTkButton(row, text="Clear", width=70, fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"], command=self.clear_token).pack(side="left")
        self.token_label = ctk.CTkLabel(row, text="", text_color=COLORS["muted_text"])
        self.token_label.pack(side="left", padx=8)

        s = Section(page, "About")
        s.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(s.body, justify="left", anchor="w", text=(
            f"Settings are stored in {CONFIG_FILE}.\n"
            f"Wi-Fi passwords and the GitHub token live in the macOS Keychain under service "
            f"\"{KEYRING_SERVICE}\" (open Keychain Access and search for it), never in config.json.\n"
            f"Writing Wi-Fi settings copies them to {WIFI_JSON} on the badge, where they are plain text.\n"
            "Badge Wi-Fi support (lib/wifi.py) comes from Kevin Criqui's fork, kcriqui/2026-Billboard_Badge."
        )).pack(fill="x")
        ctk.CTkButton(s.body, text="Open config folder", width=140,
                      command=lambda: self.reveal(str(CONFIG_FILE.parent))).pack(anchor="w", pady=(6, 0))
        return page

    def set_appearance(self, mode: str) -> None:
        ctk.set_appearance_mode(mode)
        self.ctx.config.set("appearance", mode)

    def choose_backup_dir(self) -> None:
        d = filedialog.askdirectory(parent=self, initialdir=self.v_backup.get(), title="Backup folder")
        if d:
            self.v_backup.set(d)
            self.ctx.config.set("backup_dir", d)

    def reveal(self, path: str) -> None:
        p = Path(path).expanduser()
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._info("Folder missing", f"{p} does not exist ({e}).")
                return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, str(p)])
        except OSError as e:
            self._info("Could not open", str(e))

    def _refresh_dirs(self) -> None:
        for w in self.dirs_frame.winfo_children():
            w.destroy()
        dirs = list(self.ctx.config.get("local_app_dirs") or [])
        if not dirs:
            ctk.CTkLabel(self.dirs_frame, text="(none)", text_color=COLORS["muted_text"]).pack(anchor="w")
        for d in dirs:
            row = ctk.CTkFrame(self.dirs_frame, fg_color="transparent")
            row.pack(fill="x", pady=1)
            missing = "" if Path(d).exists() else "   (missing)"
            ctk.CTkLabel(row, text=d + missing, anchor="w").pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="Remove", width=70,
                          command=lambda d=d: self.remove_app_dir(d)).pack(side="right")

    def add_app_dir(self, path: str | None = None) -> None:
        d = path or filedialog.askdirectory(parent=self, title="Folder of badge apps")
        if not d:
            return
        dirs = list(self.ctx.config.get("local_app_dirs") or [])
        if d not in dirs:
            dirs.append(d)
            self.ctx.config.set("local_app_dirs", dirs)
        self._refresh_dirs()

    def remove_app_dir(self, d: str) -> None:
        dirs = [x for x in self.ctx.config.get("local_app_dirs") or [] if x != d]
        self.ctx.config.set("local_app_dirs", dirs)
        self._refresh_dirs()

    # ------------------------------------------------------------- GitHub token
    def _load_token(self) -> None:
        def done(tok):
            if tok:
                self.ctx.sources.token = tok
                self.v_token.set(tok)
                self.token_label.configure(text="token set")
            else:
                self.token_label.configure(text="no token")
        self.ctx.run_task(lambda: get_secret("github_token"), done, title="Reading GitHub token")

    def save_token(self) -> None:
        tok = self.v_token.get().strip()
        if not tok:
            self.clear_token()
            return
        if set_secret("github_token", tok):
            self.ctx.sources.token = tok
            self.token_label.configure(text="token saved")
            self.ctx.status("GitHub token saved to Keychain")
        else:
            self._info("Keychain unavailable", "Could not store the token in the macOS Keychain.")

    def clear_token(self) -> None:
        set_secret("github_token", None)
        self.ctx.sources.token = None
        self.v_token.set("")
        self.token_label.configure(text="no token")
