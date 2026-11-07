"""First-run onboarding wizard + applying the user's profile (owner.json, Maker Detector) to a badge.

Public API:
    maybe_run_onboarding(root, ctx, force=False)
        Shows the modal wizard if config "profile" lacks onboarded=True (or force). Always (once per
        ctx) subscribes to "badge.connected" so a newly connected badge gets offered the profile.
    apply_profile_to_badge(ctx, profile, on_done=None, on_error=None)
        Write /data/owner.json (+ keep /data/maker.json in sync), install/update Maker Detector if the
        profile has it on, then HARD reset. Runs on ctx.run_device_task. Reused by tabs/maker.py and
        Settings > User Settings.

Config key "profile":
    {"maker_handle": str (<=60, a handle, not a legal name), "makes": str (<=64),
     "detector_enabled": bool (= broadcast), "onboarded": bool,
     "applied_to": [machine.unique_id hex, ...]}   badges already offered/applied (never re-prompted)
    Older configs stored the handle as "name" (core.config migrates it once). Legacy keys
    "display_name" / "broadcast_text" are ignored and dropped on the next write.

Badge side:
    /data/owner.json  {"maker_handle", "makes", "broadcast", ...other keys preserved}
                      read by lib/owner.py, nametag, Maker Tag, lib/presence.py (PR 2/3)
    /data/maker.json  {"id": "<6 hex>", "name": "<broadcast text>", "makes", "broadcast", ...}
                      legacy file for badges running an older Maker Detector; id is preserved
    Broadcast text = maker_handle reduced to printable ASCII, clipped to 16 (same as lib/presence.py).
The badge app migrates /data/nerd.json itself; we also carry the old id over when writing.

Pure helpers below take a badge service (BadgeService or FakeBadgeService) as argument.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import customtkinter as ctk

PROFILE_KEY = "profile"
OWNER_PATH = "/data/owner.json"
SETTINGS_PATH = "/data/maker.json"
OLD_SETTINGS_PATH = "/data/nerd.json"
BADGE_APP_PATH = "/apps/maker_detector.py"
OLD_APP_PATH = "/apps/nerd_detector.py"
MAX_BROADCAST = 16
MAX_MAKES = 64
MAX_NAME = 60          # max maker handle length
MAX_HANDLE = MAX_NAME
LEGACY_PROFILE_KEYS = ("name", "display_name", "broadcast_text")
HANDLE_HINT = ("a nickname or handle \u2014 not your legal name; it's shown on your badge and may be "
               "broadcast publicly")
VALIDATE_CODE = ("import apps.maker_detector as m; a = m.MakerDetectorApp(); print('IMPORT_OK')")
UID_CODE = "import machine, ubinascii; print('UID=' + ubinascii.hexlify(machine.unique_id()).decode())"

# Local app file: the Desktop working copy first, then the git repo layout.
_DESKTOP_APP = Path(__file__).resolve().parents[2] / "maker_detector" / "apps" / "maker_detector.py"


def local_app_candidates(repo_dir: str | None = None) -> list[Path]:
    # Desktop workspace layout first, then the git repo layout (<repo>/tools/badge_manager/...).
    here = Path(__file__).resolve()
    out = [_DESKTOP_APP]
    if len(here.parents) > 3:
        out.append(here.parents[3] / "user_apps" / "maker_detector" / "apps" / "maker_detector.py")
    if repo_dir:
        out += [Path(repo_dir) / "user_apps" / "maker_detector" / "apps" / "maker_detector.py",
                Path(repo_dir) / "user_apps" / "maker_detector" / "maker_detector.py"]
    return out


def find_local_app(repo_dir: str | None = None) -> Path | None:
    for p in local_app_candidates(repo_dir):
        if p.is_file():
            return p
    return None


def is_fake(badge) -> bool:
    return bool(os.environ.get("BADGE_FAKE")) and type(badge).__name__ == "FakeBadgeService"


# ====================================================================== validation
def clean_broadcast(text: str) -> str:
    """Keep only printable ASCII (no length cut)."""
    return "".join(c for c in (text or "") if " " <= c <= "~")


def validate_broadcast(text: str) -> str | None:
    if len(text) > MAX_BROADCAST:
        return f"Too long ({len(text)}/{MAX_BROADCAST})"
    bad = sorted({c for c in text if not (" " <= c <= "~")})
    if bad:
        return "Only printable ASCII allowed (bad: " + " ".join(repr(c) for c in bad) + ")"
    return None


def clean_makes(text: str) -> str:
    return "".join(c for c in (text or "") if c.isprintable()).strip()


def validate_makes(text: str) -> str | None:
    if len(text) > MAX_MAKES:
        return f"Too long ({len(text)}/{MAX_MAKES})"
    return None


def broadcast_text(handle: str) -> str:
    """What the badge advertises for this handle: printable ASCII only, at most 16 characters
    (identical to lib/presence.py's rule, so desktop and badge agree)."""
    return clean_broadcast(str(handle or ""))[:MAX_BROADCAST]


default_broadcast = broadcast_text   # old name


def broadcast_note(handle: str) -> str:
    """Live hint for the UI: empty when the handle is broadcast exactly as typed."""
    handle = str(handle or "").strip()
    if not handle:
        return ""
    bt = broadcast_text(handle)
    if bt == handle:
        return ""
    if not bt.strip():
        return ("Your handle has no plain ASCII characters, so only the anonymous badge id is "
                "broadcast over Bluetooth.")
    parts = []
    if clean_broadcast(handle) != handle:
        parts.append("non-ASCII characters removed")
    if len(clean_broadcast(handle)) > MAX_BROADCAST:
        parts.append(f"clipped to {MAX_BROADCAST} characters")
    return f"Broadcast over Bluetooth as \"{bt}\" ({', '.join(parts)}); the badge itself shows the full handle."


def migrate_profile(p: dict) -> dict:
    """Old profile -> new schema (in memory): "name" becomes "maker_handle" if there is none."""
    p = dict(p)
    if "name" in p and "maker_handle" not in p:
        p["maker_handle"] = p.pop("name")
    return p


def profile_handle(p: dict) -> str:
    return str((p or {}).get("maker_handle") or (p or {}).get("name") or "").strip()


def drop_legacy(p: dict) -> dict:
    """Profile as written from now on: legacy keys removed, everything else kept."""
    p = migrate_profile(p)
    for k in LEGACY_PROFILE_KEYS:
        p.pop(k, None)
    return p


def get_profile(config) -> dict:
    p = config.get(PROFILE_KEY) or {}
    return migrate_profile(p) if isinstance(p, dict) else {}


def make_profile(maker_handle: str, makes: str, enabled: bool, applied_to=None) -> dict:
    return {"maker_handle": (maker_handle or "").strip(), "makes": clean_makes(makes)[:MAX_MAKES],
            "detector_enabled": bool(enabled), "onboarded": True, "applied_to": list(applied_to or [])}


def mark_applied(config, uid: str | None) -> None:
    """Tk thread only."""
    if not uid:
        return
    p = get_profile(config)
    seen = list(p.get("applied_to") or [])
    if uid not in seen:
        seen.append(uid)
    p["applied_to"] = seen
    config.set(PROFILE_KEY, p)


# ====================================================================== badge helpers
def _valid_id(v) -> bool:
    return isinstance(v, str) and len(v) == 6 and all(c in "0123456789abcdefABCDEF" for c in v)


def _read_json(badge, path: str) -> dict:
    try:
        if not badge.exists(path):
            return {}
        d = json.loads(badge.read_file(path).decode("utf-8"))
    except Exception:  # noqa: BLE001  (missing / invalid)
        return {}
    return d if isinstance(d, dict) else {}


def read_settings(badge) -> dict:
    """/data/maker.json (legacy Maker Detector settings)."""
    return _read_json(badge, SETTINGS_PATH)


def read_owner(badge) -> dict:
    """/data/owner.json ({} if missing/invalid)."""
    return _read_json(badge, OWNER_PATH)


def existing_id(badge, current: dict | None = None) -> str | None:
    """The badge's id from maker.json, else from the old nerd.json."""
    for d in (current if current is not None else read_settings(badge), _read_json(badge, OLD_SETTINGS_PATH)):
        if _valid_id(d.get("id")):
            return d["id"].lower()
    return None


def build_owner(existing: dict, maker_handle: str, makes: str, broadcast: bool) -> bytes:
    """owner.json content: our three keys updated, every other key of the existing file kept."""
    err = validate_makes(makes)
    if err:
        raise ValueError(err)
    out = dict(existing or {})
    out["maker_handle"] = str(maker_handle or "").strip()
    out["makes"] = makes
    out["broadcast"] = bool(broadcast)
    return json.dumps(out).encode()


def build_settings(existing: dict, name: str, makes: str, broadcast: bool, badge_id: str | None,
                   display_name: str | None = None) -> bytes:
    """maker.json content (name = broadcast text). Unknown keys kept. A legacy "display_name" (read
    by older Maker Tag versions) is only kept in sync with the handle if the file already has one."""
    err = validate_broadcast(name) or validate_makes(makes)
    if err:
        raise ValueError(err)
    out = {k: v for k, v in existing.items() if k not in ("id", "name", "makes", "broadcast")}
    if badge_id:
        out["id"] = badge_id
    out["name"] = name
    out["makes"] = makes
    out["broadcast"] = bool(broadcast)
    if "display_name" in out and display_name is not None:
        out["display_name"] = display_name
    return json.dumps(out).encode()


def write_owner(badge, maker_handle: str, makes: str, broadcast: bool) -> dict:
    """Read-modify-write /data/owner.json (other keys preserved)."""
    data = build_owner(read_owner(badge), maker_handle, makes, broadcast)
    badge.write_bytes(data, OWNER_PATH)
    return json.loads(data)


def write_maker_json(badge, maker_handle: str, makes: str, broadcast: bool) -> dict:
    """Keep the legacy maker.json in sync: existing id (maker.json or nerd.json) and unknown keys kept."""
    cur = read_settings(badge)
    data = build_settings(cur, broadcast_text(maker_handle), makes, broadcast, existing_id(badge, cur),
                          display_name=str(maker_handle or "").strip())
    badge.write_bytes(data, SETTINGS_PATH)
    return json.loads(data)


def write_settings(badge, maker_handle: str, makes: str, broadcast: bool, maker: bool = True) -> dict:
    """Write owner.json and (if maker) maker.json. Returns {"owner": dict, "maker": dict | None}."""
    makes = clean_makes(makes)[:MAX_MAKES]
    maker_handle = str(maker_handle or "").strip()
    out = {"owner": write_owner(badge, maker_handle, makes, broadcast), "maker": None}
    if maker:
        out["maker"] = write_maker_json(badge, maker_handle, makes, broadcast)
    return out


def badge_uid(badge) -> str | None:
    """machine.unique_id() as hex, or None if unreadable."""
    try:
        out = badge.exec_py(UID_CODE, timeout=15)
    except Exception:  # noqa: BLE001
        out = ""
    for line in (out or "").splitlines():
        if line.startswith("UID="):
            return line[4:].strip().lower() or None
    if is_fake(badge):
        return "fake" + str(abs(hash(str(getattr(badge, "root", "")))) % 10**8)
    return None


def install_app(badge, local: Path, log=print) -> dict:
    """Write maker_detector.py, validate it imports+constructs on the badge, remove it on failure.
    Then remove the old nerd_detector.py (two detectors would both advertise) and migrate the id.
    Does NOT reset: the caller must hard reset afterwards (always, also on failure)."""
    local = Path(local)
    if not local.is_file():
        raise FileNotFoundError(f"Local app not found: {local}")
    badge.write_file(local, BADGE_APP_PATH)
    try:
        out = badge.exec_py(VALIDATE_CODE, timeout=40)
    except Exception as e:  # noqa: BLE001
        out = f"{e}"
    if "IMPORT_OK" not in (out or ""):
        if is_fake(badge):
            # The fake badge can't run MicroPython: at least check the file compiles here.
            try:
                compile(local.read_bytes(), str(local), "exec")
            except SyntaxError as e:
                out = f"SyntaxError: {e}"
            else:
                log("fake badge: import validation simulated (compile OK)")
                out = "IMPORT_OK"
    if "IMPORT_OK" not in (out or ""):
        try:
            badge.remove(BADGE_APP_PATH)
        except Exception as e:  # noqa: BLE001
            log(f"could not remove broken {BADGE_APP_PATH}: {e}")
        tail = (out or "no output").strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"Maker Detector failed to load on the badge and was removed: {tail[0][:200]}")
    removed_old = False
    if badge.exists(OLD_APP_PATH):
        badge.remove(OLD_APP_PATH)
        removed_old = True
    migrated = False
    if not badge.exists(SETTINGS_PATH):
        old = _read_json(badge, OLD_SETTINGS_PATH)
        if old:
            name = clean_broadcast(str(old.get("name") or ""))[:MAX_BROADCAST]
            data = build_settings({}, name, "", bool(old.get("broadcast", True)), existing_id(badge, {}))
            badge.write_bytes(data, SETTINGS_PATH)
            migrated = True
    return {"removed_old": removed_old, "migrated": migrated}


def app_installed(badge) -> bool:
    try:
        return bool(badge.exists(BADGE_APP_PATH))
    except Exception:  # noqa: BLE001
        return False


def apply_job(badge, profile: dict, local: Path | None, log=print) -> dict:
    """Blocking (device worker). Returns {"uid","action","settings","removed_old","migrated"}.
    settings = {"owner": owner.json dict, "maker": maker.json dict or None}."""
    res = {"uid": badge_uid(badge), "action": "none", "settings": None,
           "removed_old": False, "migrated": False}
    handle, makes = profile_handle(profile), profile.get("makes") or ""
    try:
        if profile.get("detector_enabled"):
            if local is not None:
                res.update(install_app(badge, local, log=log))
                res["action"] = "installed"
            elif app_installed(badge):
                res["action"] = "settings"
            else:
                raise FileNotFoundError("Maker Detector app file not found locally: "
                                        + ", ".join(str(p) for p in local_app_candidates()))
            res["settings"] = write_settings(badge, handle, makes, True)
        else:
            sync_maker = app_installed(badge) or badge.exists(SETTINGS_PATH)
            was_on = bool(read_owner(badge).get("broadcast")) or \
                bool(sync_maker and read_settings(badge).get("broadcast"))
            res["settings"] = write_settings(badge, handle, makes, False, maker=sync_maker)
            res["action"] = "broadcast_off" if was_on else "owner"
            old = _read_json(badge, OLD_SETTINGS_PATH)
            if badge.exists(OLD_APP_PATH) and old.get("broadcast"):
                old["broadcast"] = False
                badge.write_bytes(json.dumps(old).encode(), OLD_SETTINGS_PATH)
                res["action"] = "broadcast_off"
    finally:
        # badge_uid already ran code (badge UI frozen): ALWAYS hard reset, also on failure
        badge.reset()  # HARD reset; never soft reset (LVGL crashes)
    return res


def probe_job(badge) -> dict:
    """Blocking: what's on this badge? {"uid","installed","old_installed","broadcast","owner"}."""
    uid = badge_uid(badge)
    s = read_settings(badge)
    o = read_owner(badge)
    old = _read_json(badge, OLD_SETTINGS_PATH)
    installed, old_installed = app_installed(badge), badge.exists(OLD_APP_PATH)
    # Broadcasting if owner.json says so (lib/presence.py, new detector) or a detector app's own
    # settings file does (older detectors read only maker.json / nerd.json).
    bc = bool(o.get("broadcast")) or bool(installed and s and s.get("broadcast", True)) or \
        bool(old_installed and old.get("broadcast", False))
    return {"uid": uid, "installed": installed, "old_installed": old_installed,
            "broadcast": bc, "owner": o}


# ====================================================================== ctx-level API
def _ask(parent, title: str, message: str) -> bool:
    """Small confirm dialog (patched in tests)."""
    from tkinter import messagebox
    return messagebox.askyesno(title, message, parent=parent)


def apply_profile_to_badge(ctx, profile: dict, on_done=None, on_error=None) -> None:
    """Write owner.json (+ maker.json) and install/configure Maker Detector per profile on ctx's badge
    (device worker), hard reset, record the badge uid in profile["applied_to"].
    on_done(result) / on_error(exc) on Tk thread."""
    profile = dict(profile)
    local = find_local_app(ctx.config.get("repo_dir")) if profile.get("detector_enabled") else None

    def done(res):
        mark_applied(ctx.config, res.get("uid"))
        written = res.get("settings") or {}
        paths = [OWNER_PATH] + ([SETTINGS_PATH] if written.get("maker") else []) + \
            ([BADGE_APP_PATH, OLD_APP_PATH] if res["action"] == "installed" else [])
        ctx.events.publish("badge.files_changed", paths)
        msg = {"installed": "Maker Detector installed and profile written, badge restarting",
               "settings": "Profile and Maker Detector settings written, badge restarting",
               "broadcast_off": "Profile written, broadcasting switched off, badge restarting",
               "owner": "Maker handle written to the badge, badge restarting",
               "none": "Nothing to change on the badge"}[res["action"]]
        if res.get("removed_old"):
            msg += " (old Nerd Detector removed)"
        ctx.status(msg)
        ctx.log(msg)
        if on_done:
            on_done(res)

    ctx.run_device_task(lambda: apply_job(ctx.badge, profile, local, log=ctx.log), done, on_error,
                        title="Writing profile to badge")


class _Applier:
    """Offers the profile once per badge when one connects."""

    def __init__(self, root, ctx):
        self.root, self.ctx = root, ctx
        self.pending = False
        ctx.events.subscribe("badge.connected", lambda _info: self.check())

    def check(self) -> None:
        p = get_profile(self.ctx.config)
        if self.pending or not p.get("onboarded") or not self.ctx.badge.port:
            return
        self.pending = True
        self.ctx.run_device_task(lambda: probe_job(self.ctx.badge), self._probed,
                                 lambda _e: setattr(self, "pending", False), title="Checking badge profile")

    def _probed(self, st: dict) -> None:
        try:
            self._decide(st)
        finally:
            self.pending = False

    def _decide(self, st: dict) -> None:
        p = get_profile(self.ctx.config)
        uid = st.get("uid")
        if uid and uid in (p.get("applied_to") or []):
            return
        port = self.ctx.badge.port
        handle = profile_handle(p)
        owner = st.get("owner") or {}
        if p.get("detector_enabled"):
            text = broadcast_text(handle)
            q = (f"Install Maker Detector and broadcast '{text}' on the badge at {port}?"
                 if text.strip() else f"Install Maker Detector (anonymous id only) on the badge at {port}?")
            if handle:
                q += f"\n\nYour maker handle '{handle}' is also written to the badge (/data/owner.json)."
            q += "\n\nThe badge will hard-reset afterwards."
            if _ask(self.root, "Maker Detector", q):
                apply_profile_to_badge(self.ctx, p)
                return
        elif st.get("broadcast"):
            if _ask(self.root, "Maker Detector",
                    f"The badge at {port} has a detector app that broadcasts over Bluetooth, but your "
                    "profile has the Maker Detector off.\n\nSwitch broadcasting off on this badge? "
                    "(It will hard-reset.)"):
                apply_profile_to_badge(self.ctx, p)
                return
        elif handle and (owner.get("maker_handle") != handle or (owner.get("makes") or "") != (p.get("makes") or "")):
            if _ask(self.root, "Maker handle",
                    f"Write your maker handle '{handle}' to the badge at {port} (/data/owner.json, shown "
                    "by the name tag)? Broadcasting stays off.\n\nThe badge will hard-reset afterwards."):
                apply_profile_to_badge(self.ctx, p)
                return
        if uid:  # declined or nothing to do: remember so we ask only once per badge
            mark_applied(self.ctx.config, uid)
        else:
            self.ctx.log("Maker Detector: could not read the badge's unique id; will ask again next time")


def _applier(root, ctx) -> _Applier:
    a = getattr(ctx, "_maker_applier", None)
    if a is None:
        a = ctx._maker_applier = _Applier(root, ctx)
    return a


def maybe_run_onboarding(root, ctx, force: bool = False):
    """Show the first-run wizard if needed. Returns the wizard window (or None)."""
    applier = _applier(root, ctx)
    p = get_profile(ctx.config)
    if p.get("onboarded") and not force:
        return None
    # A forced re-run forgets which badges were already offered, so the connected one is asked again.
    return OnboardingWizard(root, ctx, on_finish=lambda _prof: applier.check(), reset_applied=force)


# ====================================================================== wizard UI
class OnboardingWizard(ctk.CTkToplevel):
    STEPS = 3

    def __init__(self, root, ctx, on_finish=None, reset_applied=False):
        super().__init__(root)
        self.root, self.ctx, self.on_finish = root, ctx, on_finish
        self.done = False
        prev = get_profile(ctx.config)
        self._applied = [] if reset_applied else (prev.get("applied_to") or [])
        self.title("Welcome to Billboard Badge Manager")
        self.resizable(False, False)
        self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self.skip)
        self.step = 0

        self.name_var = ctk.StringVar(value=profile_handle(prev))   # the maker handle
        self.handle_var = self.name_var
        self.makes_var = ctk.StringVar(value=prev.get("makes", ""))
        self.enabled_var = ctk.StringVar(value="yes" if prev.get("detector_enabled", True) else "no")
        self.hint = None
        self.counter = None

        self.header = ctk.CTkLabel(self, text="", text_color="gray60", anchor="w")
        self.header.pack(fill="x", padx=24, pady=(18, 0))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=24, pady=10)
        nav = ctk.CTkFrame(self, fg_color="transparent")
        nav.pack(fill="x", padx=24, pady=(0, 18))
        self.skip_btn = ctk.CTkButton(nav, text="Skip", width=80, fg_color="transparent", border_width=1,
                                      text_color=("gray20", "gray80"), command=self.skip)
        self.skip_btn.pack(side="left")
        self.next_btn = ctk.CTkButton(nav, text="Next", width=100, command=self.next)
        self.next_btn.pack(side="right")
        self.back_btn = ctk.CTkButton(nav, text="Back", width=80, command=self.back)
        self.back_btn.pack(side="right", padx=8)

        self.bind("<Return>", lambda _e: self.next())
        self.bind("<KP_Enter>", lambda _e: self.next())
        self.bind("<Escape>", lambda _e: self.skip())
        self._show()
        self._center()
        self.after(50, self._grab)

    # ---------------------------------------------------------------- window
    def _center(self) -> None:
        self.update_idletasks()
        w, h = max(self.winfo_reqwidth(), 520), max(self.winfo_reqheight(), 360)
        try:
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            x, y = rx + max(0, (rw - w) // 2), ry + max(0, (rh - h) // 3)
        except Exception:  # noqa: BLE001
            x, y = 200, 200
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _grab(self) -> None:
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
        except Exception:  # noqa: BLE001  (window not viewable yet)
            if not self.done:
                self.after(100, self._grab)
        if self._focus:
            self._focus.focus_set()

    # ---------------------------------------------------------------- steps
    def _clear(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()

    def _title(self, text: str) -> None:
        ctk.CTkLabel(self.body, text=text, font=ctk.CTkFont(size=20, weight="bold"), anchor="w").pack(
            fill="x", pady=(0, 8))

    def _note(self, text: str, **kw) -> ctk.CTkLabel:
        lab = ctk.CTkLabel(self.body, text=text, anchor="w", justify="left", wraplength=470,
                           text_color=kw.pop("color", "gray60"), **kw)
        lab.pack(fill="x", pady=2)
        return lab

    def _show(self) -> None:
        self._clear()
        self._focus = None
        self.header.configure(text=f"Step {self.step + 1} of {self.STEPS}")
        self.back_btn.configure(state="normal" if self.step else "disabled")
        self.next_btn.configure(text="Finish" if self.step == self.STEPS - 1 else "Next")
        self.error = None
        self.hint = None
        getattr(self, f"_step{self.step}")()
        self._validate()
        if self._focus:
            self.after(10, lambda f=self._focus: f.winfo_exists() and f.focus_set())

    def _step0(self) -> None:
        self._title("What's your maker handle?")
        self._note(HANDLE_HINT[0].upper() + HANDLE_HINT[1:] + ".")
        e = ctk.CTkEntry(self.body, textvariable=self.name_var, width=360, placeholder_text="Maker handle")
        e.pack(anchor="w", pady=8)
        self.error = self._note("", color="#d9534f")
        self.hint = self._note("")
        self._focus = e
        self._trace(self.name_var)

    def _step1(self) -> None:
        self._title("What do you like to make?")
        self._note("Anything goes: robots, synths, badges, keyboards, sourdough... (optional)")
        e = ctk.CTkEntry(self.body, textvariable=self.makes_var, width=360,
                         placeholder_text="e.g. robots, synths, badges")
        e.pack(anchor="w", pady=8)
        self.counter = self._note("")
        self._focus = e
        self._trace(self.makes_var)

    def _step2(self) -> None:
        self._title("Turn on the Maker Detector?")
        self._note("Your badge will broadcast a short public text over Bluetooth so nearby badges running "
                   "Maker Detector can count and list you. Nothing connects; it only advertises.")
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        row.pack(fill="x", pady=6)
        ctk.CTkRadioButton(row, text="Yes, turn it on", variable=self.enabled_var, value="yes",
                           command=self._show).pack(side="left", padx=(0, 16))
        ctk.CTkRadioButton(row, text="No thanks", variable=self.enabled_var, value="no",
                           command=self._show).pack(side="left")
        if self.enabled_var.get() == "yes":
            bt = broadcast_text(self.name_var.get().strip())
            self._note(f"Your badge will broadcast: \"{bt}\"" if bt.strip()
                       else "Your badge will broadcast only its anonymous id.", color=None)
            note = broadcast_note(self.name_var.get())
            if note:
                self.hint = self._note(note)
            self._note("This text is PUBLIC: any phone or laptop nearby can read it. It is your maker "
                       "handle (plain ASCII, up to 16 characters). You can switch it off later in the "
                       "Maker Detector tab or in Settings.", color=("#8a5a00", "#e0a040"))
        else:
            self._note("Fine: nothing will be broadcast. You can turn it on later in the Maker Detector tab.")
        self.error = None

    def _trace(self, var) -> None:
        tid = var.trace_add("write", lambda *_: self._validate())
        self.body.winfo_children()[-1].bind("<Destroy>", lambda _e, v=var, t=tid: self._untrace(v, t), add="+")

    @staticmethod
    def _untrace(var, tid) -> None:
        try:
            var.trace_remove("write", tid)
        except Exception:  # noqa: BLE001
            pass

    def _validate(self) -> bool:
        ok, msg = True, ""
        if self.step == 0:
            ok = bool(self.name_var.get().strip())
            if len(self.name_var.get()) > MAX_NAME:
                ok, msg = False, f"Too long ({len(self.name_var.get())}/{MAX_NAME})"
            if self.error is not None and self.error.winfo_exists():
                self.error.configure(text=msg)
            if self.hint is not None and self.hint.winfo_exists():
                self.hint.configure(text=broadcast_note(self.name_var.get()))
        elif self.step == 1:
            err = validate_makes(self.makes_var.get())
            ok = err is None
            if self.counter.winfo_exists():
                self.counter.configure(text=err or f"{len(self.makes_var.get())}/{MAX_MAKES}",
                                       text_color="#d9534f" if err else "gray60")
        self.next_btn.configure(state="normal" if ok else "disabled")
        return ok

    # ---------------------------------------------------------------- nav
    def next(self) -> None:
        if not self._validate():
            return
        if self.step < self.STEPS - 1:
            self.step += 1
            self._show()
        else:
            self._finish()

    def back(self) -> None:
        if self.step:
            self.step -= 1
            self._show()

    def profile(self) -> dict:
        enabled = self.enabled_var.get() == "yes" and self.step == self.STEPS - 1
        makes = self.makes_var.get() if not validate_makes(self.makes_var.get()) else ""
        return make_profile(self.name_var.get()[:MAX_NAME], makes, enabled, self._applied)

    def skip(self) -> None:
        """Save what's filled (detector only if the user reached and accepted step 3)."""
        self._finish()

    def _finish(self) -> None:
        if self.done:
            return
        self.done = True
        # Merge into the existing profile so unknown keys survive; legacy name/display_name/
        # broadcast_text are dropped (the handle is what the badge shows and broadcasts).
        prof = drop_legacy(get_profile(self.ctx.config))
        prof.update(self.profile())
        self.ctx.config.set(PROFILE_KEY, prof)
        try:
            self.ctx.events.publish("profile.changed", prof)
        except Exception:  # noqa: BLE001
            pass
        self.ctx.log(f"Profile saved (Maker Detector {'on' if prof['detector_enabled'] else 'off'})")
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()
        if self.on_finish:
            self.on_finish(prof)
