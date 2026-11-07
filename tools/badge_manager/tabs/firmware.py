"""Firmware tab: track upstream + every community fork, compare one with the badge, install it.

Left: the list of sources (git refs; every GitHub fork is a remote in one local repo).
Right: details of the selected ref, "Compare & install" and an advanced "Runtime" page for
flashing MicroPython .bin images with esptool.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import shutil
import tempfile
import webbrowser
from pathlib import Path

import customtkinter as ctk

from core.badge import BadgeError
from core.theme import COLORS

from .base import BaseTab, Section, confirm, info, human_size

BADGE_PREFIX = "firmware/badge/"
BIN_PREFIX = "firmware/bin/"
UPSTREAM_REF = "origin/main"
FLASH_SIZE = "0x800000"          # 8 MB
RUNTIME_FILES = (("0x0", "bootloader.bin"), ("0x8000", "partition-table.bin"),
                 ("0x10000", "micropython.bin"))
FACTORY_FILE = "supercon26_factory.bin"
PROTECT = ("/data", "/apps", "/lib")   # never deleted by "delete files not in this version"

MONO = ("Menlo", 12)
SEL_COLOR = COLORS["selection"]
ROW_COLOR = COLORS["row"]


def _row_text(sel: bool, dim: bool):
    if sel:
        return COLORS["text_on_accent"]
    return COLORS["muted_text"] if dim else COLORS["text"]

BOOTLOADER_HELP = (
    "Put the badge in BOOTLOADER mode first:\n"
    "  1. Hold the BOOT button.\n"
    "  2. Tap RESET (keep holding BOOT).\n"
    "  3. Release BOOT.\n"
    "The serial port may change name; re-select it in the top bar if needed.\n"
    "When esptool finishes, tap RESET to start the badge normally.")


def _parse_date(s: str) -> _dt.datetime:
    try:
        return _dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return _dt.datetime.fromtimestamp(0, _dt.timezone.utc)


def _fmt_date(s: str) -> str:
    d = _parse_date(s)
    try:
        return d.astimezone().strftime("%a %d %b %H:%M")
    except (ValueError, OSError):
        return s[:16]


def _badge_files(ref) -> list[str]:
    return [t for t in ref.touched if t.startswith(BADGE_PREFIX)]


def _bin_files(ref) -> list[str]:
    return [t for t in ref.touched if t.startswith(BIN_PREFIX)]


def _under(p: str, prefixes) -> bool:
    return any(p == x or p.startswith(x.rstrip("/") + "/") for x in prefixes)


def plan_install(badge, local_root) -> dict:
    """Ask the badge what installing ``local_root`` would do (runs on the device worker).

    Always planned with delete_extra=True (deletion is dropped at install time if not ticked) and
    PROTECT, so /data, /apps and /lib are never deleted. Disabled apps are handled here: if the
    version has apps/x.py and the badge has only /apps/_x.py, the exported file is renamed to
    apps/_x.py in ``local_root`` so the disabled copy is updated in place and stays disabled.
    Extra keys added: "disabled_updated", "disabled_same", "lib_kept", "apps_kept", "data_extra".
    """
    root = Path(local_root)
    plan = badge.plan_push(root, "/", delete_extra=True, protect=PROTECT, only_if_missing=("/data",))
    remote = plan["remote_hashes"]
    copy = list(plan["copy"])
    upd, same = [], []
    apps_dir = root / "apps"
    if apps_dir.is_dir():
        for f in sorted(apps_dir.glob("*.py")):
            if f.name.startswith("_") or not f.is_file():
                continue
            enabled, disabled = f"/apps/{f.name}", f"/apps/_{f.name}"
            if enabled in remote or disabled not in remote:
                continue
            target = apps_dir / ("_" + f.name)
            if enabled in copy:
                copy.remove(enabled)
            plan["sizes"].pop(enabled, None)
            if target.exists():       # the version ships both; its own _x.py is planned already
                f.unlink()
                continue
            f.rename(target)
            data = target.read_bytes()
            plan["sizes"][disabled] = len(data)
            if hashlib.sha256(data).hexdigest() == remote.get(disabled):
                same.append(disabled)
                plan["unchanged"] += 1
            else:
                copy.append(disabled)
                upd.append(disabled)
    copy.sort(key=badge._copy_order)
    plan["copy"] = copy
    plan["bytes"] = sum(plan["sizes"][p] for p in copy)
    plan["disabled_updated"], plan["disabled_same"] = upd, same
    local = {"/" + f.relative_to(root).as_posix() for f in root.rglob("*")}
    extra = sorted(p for p, h in remote.items() if h and p not in local and "__pycache__" not in p)
    plan["lib_kept"] = [p for p in extra if _under(p, ("/lib",))]
    plan["apps_kept"] = [p for p in extra if _under(p, ("/apps",)) and p not in upd and p not in same]
    plan["data_extra"] = [p for p in extra if _under(p, ("/data",))]
    return plan


def install_plan(plan: dict, delete: bool) -> dict:
    """The plan actually handed to push_tree: deletion dropped unless ticked."""
    p = dict(plan)
    p["delete"] = list(plan["delete"]) if delete else []
    return p


def _kb(n: int) -> str:
    return f"{(n + 1023) // 1024} KB"


def confirm_text(name: str, sha: str, plan: dict, delete: bool, backup: bool) -> str:
    copy, upd = plan["copy"], set(plan.get("disabled_updated", ()))
    remote = plan["remote_hashes"]
    n_new = sum(1 for p in copy if p not in remote)
    n_over = len(copy) - n_new
    msg = f"Install {name} @ {sha[:8]} to the badge?\n\n"
    if copy:
        msg += (f"  {len(copy)} file(s) will be written ({_kb(plan['bytes'])}): "
                f"{n_over} overwritten, {n_new} new\n")
    else:
        msg += "  No files need copying (badge already matches)\n"
    if upd:
        msg += f"  {len(upd)} disabled app(s) updated in place and kept disabled\n"
    if plan["kept"]:
        msg += (f"  {len(plan['kept'])} /data file(s) differ from this version and are KEPT "
                "(badge copy is not overwritten)\n")
    if delete:
        msg += (f"  {len(plan['delete'])} file(s)/folder(s) not in this version will be DELETED\n"
                if plan["delete"] else "  Nothing will be deleted\n")
    else:
        msg += "  Nothing will be deleted"
        msg += (f" ({len(plan['delete'])} file(s) not in this version are left alone)\n"
                if plan["delete"] else "\n")
    msg += "  Never deleted: /data, /apps and /lib"
    if plan.get("lib_kept"):
        msg += f" ({len(plan['lib_kept'])} /lib file(s) not in this version kept, may be used by apps)"
    msg += "\n\n"
    msg += ("A full, verified file backup is made first (several minutes with a full gallery); "
            "the install stops if the backup is incomplete.\n" if backup else "No backup (disabled in Settings).\n")
    msg += "The badge is hard-reset afterwards."
    return msg


class FirmwareTab(BaseTab):
    TITLE = "Firmware"

    # ------------------------------------------------------------------ build
    def build(self) -> None:
        self.refs: list = []
        self.selected = None              # Ref
        self.show_unchanged = False
        self.compare = None               # dict with ref name/sha/tmpdir/result
        self._tmpdirs: list[str] = []
        self._loading_refs = False
        self._row_widgets: dict[str, ctk.CTkButton] = {}

        # ---- top bar
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=6, pady=(4, 6))
        self.installed_label = ctk.CTkLabel(top, text="", anchor="w",
                                            font=ctk.CTkFont(size=13, weight="bold"))
        self.installed_label.pack(side="left", padx=10, pady=6, fill="x", expand=True)
        ctk.CTkButton(top, text="Reload list", width=100,
                      command=self.load_refs).pack(side="right", padx=4)
        self.sync_btn = ctk.CTkButton(top, text="Check GitHub for new forks + fetch", width=250,
                                      command=self.sync_sources)
        self.sync_btn.pack(side="right", padx=4)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=0, minsize=380)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # ---- left: sources
        left = Section(body, "Sources")
        left.grid(row=0, column=0, sticky="nsew", padx=(6, 4), pady=0)
        fl = ctk.CTkFrame(left.body, fg_color="transparent")
        fl.pack(fill="x")
        self.filter_var = ctk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.render_list())
        ctk.CTkEntry(fl, textvariable=self.filter_var, placeholder_text="Filter (name, author, subject)")\
            .pack(side="left", fill="x", expand=True)
        self.badge_only_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(left.body, text="Only forks that change badge files", variable=self.badge_only_var,
                        command=self.render_list).pack(anchor="w", pady=(6, 2))
        self.count_label = ctk.CTkLabel(left.body, text="", anchor="w", text_color=COLORS["muted_text"])
        self.count_label.pack(fill="x")
        self.list_frame = ctk.CTkScrollableFrame(left.body, width=350)
        self.list_frame.pack(fill="both", expand=True, pady=(4, 0))

        # ---- right: detail
        right = ctk.CTkFrame(body)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 6))
        head = ctk.CTkFrame(right, fg_color="transparent")
        head.pack(fill="x", padx=10, pady=(8, 0))
        self.title_label = ctk.CTkLabel(head, text="Select a source on the left", anchor="w",
                                        font=ctk.CTkFont(size=16, weight="bold"))
        self.title_label.pack(side="left", fill="x", expand=True)
        self.gh_btn = ctk.CTkButton(head, text="Open on GitHub", width=130, state="disabled",
                                    command=self.open_github)
        self.gh_btn.pack(side="right")
        self.meta_label = ctk.CTkLabel(right, text="", anchor="w", justify="left", text_color=COLORS["muted_text"])
        self.meta_label.pack(fill="x", padx=10)

        self.pages = ctk.CTkTabview(right)
        self.pages.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._build_changes(self.pages.add("Changes"))
        self._build_install(self.pages.add("Compare & install"))
        self._build_runtime(self.pages.add("Runtime (advanced)"))

        self.ctx.events.subscribe("sources.updated", self._on_sources_updated)
        self.ctx.events.subscribe("badge.files_changed", self._on_badge_changed)
        self._update_installed_label()

    def _build_changes(self, page) -> None:
        ctk.CTkLabel(page, text="Commits not in upstream", anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(fill="x")
        self.commits_box = ctk.CTkTextbox(page, height=150, font=ctk.CTkFont(*MONO), wrap="none")
        self.commits_box.pack(fill="both", expand=True, pady=(2, 8))
        ctk.CTkLabel(page, text="Badge files changed vs upstream (firmware/badge)", anchor="w",
                     font=ctk.CTkFont(weight="bold")).pack(fill="x")
        self.files_box = ctk.CTkTextbox(page, height=150, font=ctk.CTkFont(*MONO), wrap="none")
        self.files_box.pack(fill="both", expand=True, pady=(2, 0))
        for box in (self.commits_box, self.files_box):
            box.configure(state="disabled")

    def _build_install(self, page) -> None:
        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.pack(fill="x")
        self.compare_btn = ctk.CTkButton(bar, text="Compare with badge", width=160,
                                         command=self.compare_with_badge, state="disabled")
        self.compare_btn.pack(side="left")
        self.install_btn = ctk.CTkButton(bar, text="Install to badge", width=150, state="disabled",
                                         fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                                         command=self.install)
        self.install_btn.pack(side="left", padx=8)
        self.delete_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(page, variable=self.delete_var, command=self._on_delete_toggled,
                        text="Delete badge files not in this version (always keeps /data, /apps and /lib)")\
            .pack(anchor="w", pady=(8, 2))
        self.compare_label = ctk.CTkLabel(page, text="Compare to see what installing would change.",
                                          anchor="w", justify="left")
        self.compare_label.pack(fill="x", pady=(4, 2))
        self.compare_box = ctk.CTkTextbox(page, font=ctk.CTkFont(*MONO), wrap="none")
        self.compare_box.pack(fill="both", expand=True, pady=(2, 4))
        self.compare_box.configure(state="disabled")
        pf = ctk.CTkFrame(page, fg_color="transparent")
        pf.pack(fill="x")
        self.progress = ctk.CTkProgressBar(pf)
        self.progress.set(0)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.progress_label = ctk.CTkLabel(pf, text="", width=320, anchor="w")
        self.progress_label.pack(side="left")

    def _build_runtime(self, page) -> None:
        warn = ctk.CTkLabel(page, anchor="w", justify="left", text_color=COLORS["warning_text"],
                            text=("ADVANCED: these write the ESP32-S3 flash with esptool. Normal updates only need "
                                  "'Compare & install'.\nFlashing the runtime keeps the badge filesystem; restoring the "
                                  "factory image ERASES it (including /data)."))
        warn.pack(fill="x", pady=(0, 6))
        self.bin_box = ctk.CTkTextbox(page, height=150, font=ctk.CTkFont(*MONO), wrap="none")
        self.bin_box.pack(fill="both", expand=True)
        self.bin_box.configure(state="disabled")
        bar = ctk.CTkFrame(page, fg_color="transparent")
        bar.pack(fill="x", pady=(8, 0))
        self.rt_buttons = [
            ctk.CTkButton(bar, text="Back up full flash (read 8 MB)", command=self.backup_flash),
            ctk.CTkButton(bar, text="Flash MicroPython runtime", fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                          command=self.flash_runtime),
            ctk.CTkButton(bar, text="Restore factory image", fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                          command=self.restore_factory),
        ]
        for b in self.rt_buttons:
            b.pack(side="left", padx=(0, 8))
        self.rt_label = ctk.CTkLabel(page, text="", anchor="w", text_color=COLORS["muted_text"])
        self.rt_label.pack(fill="x", pady=(6, 0))

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _set_text(box, text: str) -> None:
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("1.0", text)
        box.configure(state="disabled")

    def _new_tmpdir(self, prefix: str) -> str:
        d = tempfile.mkdtemp(prefix=prefix)
        self._tmpdirs.append(d)
        # keep the last few (an install might still be using one)
        while len(self._tmpdirs) > 4:
            shutil.rmtree(self._tmpdirs.pop(0), ignore_errors=True)
        return d

    def destroy(self):
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        super().destroy()

    def _ref_by_name(self, name):
        return next((r for r in self.refs if r.name == name), None)

    def _update_installed_label(self) -> None:
        inst = self.ctx.config.get("installed_ref")
        if not inst:
            self.installed_label.configure(text="Currently installed: unknown (not installed with this app yet)")
            return
        txt = f"Currently installed: {inst.get('name')} @ {str(inst.get('sha', ''))[:8]}  ({_fmt_date(inst.get('date', ''))})"
        cur = self._ref_by_name(inst.get("name"))
        if cur and cur.sha != inst.get("sha"):
            txt += "   -  newer commits available"
        if inst.get("installed_at"):
            txt += f"   ·  installed {inst['installed_at']}"
        self.installed_label.configure(text=txt)

    # --------------------------------------------------------------- sources
    def on_show(self) -> None:
        if not self.refs and not self._loading_refs:
            self.load_refs()

    def load_refs(self) -> None:
        if self._loading_refs:
            return
        self._loading_refs = True

        def done(refs):
            self._loading_refs = False
            self._set_refs(refs)
            self.ctx.status(f"{len(refs)} sources loaded")

        def failed(_e):
            self._loading_refs = False
            self.count_label.configure(text="Could not read the repo (see log)")
        self.count_label.configure(text="Loading sources...")
        self.ctx.run_task(self.ctx.sources.refs, done, failed, title="Reading sources")

    def sync_sources(self) -> None:
        src = self.ctx.sources

        def work():
            added = src.sync_remotes()
            self.ctx.log(f"{added} new fork remote(s) added; fetching all ...")
            src.fetch_all()
            return added, src.refs()

        def done(res):
            added, refs = res
            self.sync_btn.configure(state="normal")
            self.ctx.events.publish("sources.updated", refs)
            self.ctx.status(f"Fetched. {added} new fork(s); {len(refs)} sources")

        def failed(e):
            self.sync_btn.configure(state="normal")
            msg = str(e)
            if "403" in msg or "rate limit" in msg.lower():
                self.ctx.status("GitHub API rate limit hit (60/h). Try later or set a token in Settings.")
        self.sync_btn.configure(state="disabled")
        self.ctx.run_task(work, done, failed, title="Checking GitHub + fetching forks")

    def _on_sources_updated(self, refs) -> None:
        if refs:
            self._set_refs(refs)
        else:
            self.load_refs()

    def _set_refs(self, refs) -> None:
        ups = [r for r in refs if r.is_upstream]
        changed = sorted([r for r in refs if not r.is_upstream and r.ahead],
                         key=lambda r: _parse_date(r.date), reverse=True)
        same = sorted([r for r in refs if not r.is_upstream and not r.ahead], key=lambda r: r.name.lower())
        self.refs = ups + changed + same
        self.render_list()
        self._update_installed_label()
        if self.selected:
            new = self._ref_by_name(self.selected.name)
            if new and new.sha != self.selected.sha:
                self.select(new)
            elif new:
                self.selected = new
        elif self.refs:
            self.select(self.refs[0])

    def _matches(self, r) -> bool:
        q = self.filter_var.get().strip().lower()
        if q and q not in f"{r.name} {r.author} {r.subject}".lower():
            return False
        if self.badge_only_var.get() and not r.is_upstream and not _badge_files(r):
            return False
        return True

    def render_list(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        self._row_widgets = {}
        refs = [r for r in self.refs if self._matches(r)]
        active = [r for r in refs if r.is_upstream or r.ahead]
        idle = [r for r in refs if not r.is_upstream and not r.ahead]
        for r in active:
            self._add_row(r)
        if idle:
            arrow = "v" if self.show_unchanged else ">"
            ctk.CTkButton(self.list_frame, text=f"{arrow}  Forks with no new commits ({len(idle)})",
                          anchor="w", fg_color="transparent", text_color=COLORS["muted_text"],
                          hover_color=COLORS["row_hover"], command=self._toggle_unchanged)\
                .pack(fill="x", pady=(8, 2))
            if self.show_unchanged:
                for r in idle:
                    self._add_row(r, dim=True)
        n_changed = sum(1 for r in self.refs if not r.is_upstream and r.ahead)
        n_badge = sum(1 for r in self.refs if not r.is_upstream and _badge_files(r))
        self.count_label.configure(
            text=f"{len(self.refs)} sources · {n_changed} with new commits · {n_badge} touch badge files")

    def _toggle_unchanged(self) -> None:
        self.show_unchanged = not self.show_unchanged
        self.render_list()

    def _add_row(self, r, dim: bool = False) -> None:
        if r.is_upstream:
            tag = "[UPSTREAM]"
        else:
            parts = []
            if _badge_files(r):
                parts.append(f"[badge {len(_badge_files(r))}]")
            if _bin_files(r):
                parts.append("[runtime .bin]")
            if r.ahead and not parts:
                parts.append("(hardware/docs only)")
            tag = " ".join(parts)
        line1 = f"{r.name}   {tag}"
        counts = "" if r.is_upstream else f"+{r.ahead} / -{r.behind}   "
        subj = r.subject if len(r.subject) < 44 else r.subject[:42] + "..."
        line2 = f"{counts}{_fmt_date(r.date)}\n{subj}"
        sel = self.selected is not None and self.selected.name == r.name
        b = ctk.CTkButton(self.list_frame, text=f"{line1}\n{line2}", anchor="w", height=56,
                          fg_color=SEL_COLOR if sel else ROW_COLOR,
                          text_color=_row_text(sel, dim),
                          hover_color=COLORS["row_hover"],
                          command=lambda r=r: self.select(r))
        b.pack(fill="x", pady=2)
        b._dim = dim
        self._row_widgets[r.name] = b

    # ---------------------------------------------------------------- detail
    def select(self, r) -> None:
        prev = self.selected.name if self.selected else None
        self.selected = r
        for name, b in self._row_widgets.items():
            sel = name == r.name
            b.configure(fg_color=SEL_COLOR if sel else ROW_COLOR,
                        text_color=_row_text(sel, getattr(b, "_dim", False)))
        self.title_label.configure(text=r.name + ("   (official upstream)" if r.is_upstream else ""))
        meta = f"{r.sha[:10]}  ·  {_fmt_date(r.date)}  ·  {r.author}  ·  {r.subject}"
        if not r.is_upstream:
            meta += f"\n{r.ahead} commit(s) ahead of upstream, {r.behind} behind"
            if r.ahead:
                meta += (f"  ·  {len(_badge_files(r))} badge file(s), "
                         f"{len(r.touched) - len(_badge_files(r))} other file(s) changed")
        self.meta_label.configure(text=meta)
        self.gh_btn.configure(state="normal")
        self.compare_btn.configure(state="normal")
        if prev != r.name or (self.compare and self.compare["sha"] != r.sha):
            self._clear_compare("Compare to see what installing would change.")
        self._set_text(self.commits_box, "Loading...")
        self._set_text(self.files_box, "")
        self._set_text(self.bin_box, "Loading...")
        src = self.ctx.sources
        name = r.name

        def work():
            commits = src.log_commits(name, n=50, only_unique=not r.is_upstream)
            stat = "" if r.is_upstream else src.git("diff", "--stat=100", f"{UPSTREAM_REF}...{name}",
                                                       "--", BADGE_PREFIX.rstrip("/"))
            bins = self._bin_info(name)
            return commits, stat, bins

        def done(res):
            if not self.selected or self.selected.name != name:
                return
            commits, stat, bins = res
            if commits:
                lines = [f"{c.sha[:8]}  {c.date}  {c.author[:18]:<18}  {c.subject}" for c in commits]
                if r.is_upstream:
                    lines.insert(0, "(upstream: most recent commits)")
            else:
                lines = ["No commits beyond upstream."]
            self._set_text(self.commits_box, "\n".join(lines))
            if r.is_upstream:
                files = "This is the official upstream; installing it resets badge code to the official version."
            elif stat.strip():
                files = stat.replace(BADGE_PREFIX, "/")
                other = [t for t in r.touched if not t.startswith(BADGE_PREFIX)]
                if other:
                    files += "\nOther files changed (not copied to the badge):\n" + \
                        "\n".join("  " + t for t in other[:40])
            else:
                files = "No badge files changed (hardware/docs only, or identical)."
                if r.touched:
                    files += "\n\n" + "\n".join("  " + t for t in r.touched[:40])
            self._set_text(self.files_box, files)
            self._set_text(self.bin_box, bins)
        self.ctx.run_task(work, done, title=f"Loading {name}")

    def _bin_info(self, name: str) -> str:
        src = self.ctx.sources

        def tree(ref):
            out = {}
            for line in src.git("ls-tree", "-l", ref, "--", BIN_PREFIX).splitlines():
                meta, path = line.split("\t", 1)
                _mode, _typ, blob, size = meta.split()
                out[Path(path).name] = (blob, int(size) if size.isdigit() else None)
            return out
        mine = tree(name)
        ups = tree(UPSTREAM_REF) if name != UPSTREAM_REF else mine
        if not mine:
            return f"{name} has no firmware/bin folder."
        lines = [f"Runtime images in {name}:firmware/bin", ""]
        for fn, (blob, size) in sorted(mine.items()):
            note = ""
            if name != UPSTREAM_REF:
                u = ups.get(fn)
                note = "same as upstream" if u and u[0] == blob else ("NEW in this fork" if not u
                                                                      else "DIFFERENT from upstream")
            role = {"bootloader.bin": "@0x0", "partition-table.bin": "@0x8000",
                    "micropython.bin": "@0x10000", FACTORY_FILE: "full image @0x0"}.get(fn, "")
            lines.append(f"  {fn:<26} {human_size(size):>9}  {blob[:8]}  {role:<16} {note}")
        missing = [f for _, f in RUNTIME_FILES if f not in mine]
        if missing:
            lines.append(f"\nMissing for a runtime flash: {', '.join(missing)}")
        return "\n".join(lines)

    def open_github(self) -> None:
        r = self.selected
        if not r:
            return

        def work():
            return self.ctx.sources.remote_repo(r.remote)

        def done(repo):
            if "://" in repo or "/" not in repo:
                self.ctx.status(f"No GitHub URL for remote {r.remote}: {repo}")
                return
            webbrowser.open(f"https://github.com/{repo}/tree/{r.branch}")
        self.ctx.run_task(work, done, title="Opening GitHub")

    # --------------------------------------------------------------- compare
    def _clear_compare(self, msg: str) -> None:
        self.compare = None
        self.install_btn.configure(state="disabled")
        self.compare_label.configure(text=msg)
        self._set_text(self.compare_box, "")

    def compare_with_badge(self, then=None) -> None:
        r = self.selected
        if not r:
            return
        self.compare_btn.configure(state="disabled")
        self.install_btn.configure(state="disabled")
        self.compare_label.configure(text=f"Exporting {r.name} ...")
        tmp = self._new_tmpdir("badge-fw-")
        sha = r.sha

        def export():
            self.ctx.sources.export_badge_tree(sha, tmp)

        def exported(_):
            self.compare_label.configure(text="Reading file hashes from the badge (can take a minute) ...")
            self.ctx.run_device_task(lambda: plan_install(self.ctx.badge, tmp),
                                     compared, failed, title="Hashing badge files")

        def compared(plan):
            self.compare_btn.configure(state="normal")
            self.compare = {"name": r.name, "sha": sha, "date": r.date, "tmpdir": tmp, "plan": plan}
            if not self.selected or self.selected.name != r.name:
                return
            self._show_compare(plan)
            self.install_btn.configure(state="normal")
            self.ctx.status(f"Compared {r.name}: {len(plan['copy'])} file(s) to copy, "
                            f"{len(plan['kept'])} /data file(s) kept")
            if then:
                then()

        def failed(e):
            self.compare_btn.configure(state="normal")
            self.compare_label.configure(text=f"Compare failed: {e}")
        self.ctx.run_task(export, exported, failed, title=f"Exporting {r.name}")

    def _on_delete_toggled(self) -> None:
        if self.compare:
            self._show_compare(self.compare["plan"])

    def _show_compare(self, plan: dict) -> None:
        delete = self.delete_var.get()
        copy, remote = plan["copy"], plan["remote_hashes"]
        upd = set(plan.get("disabled_updated", ()))
        if copy:
            head = f"Installing writes {len(copy)} file(s), {_kb(plan['bytes'])}."
        else:
            head = "The badge already has every file of this version."
        if plan["kept"]:
            head += f"\n{len(plan['kept'])} /data file(s) differ but are KEPT (badge copy wins)."
        if plan["delete"]:
            head += (f"\n{len(plan['delete'])} item(s) not in this version WILL BE DELETED." if delete else
                     f"\n{len(plan['delete'])} item(s) not in this version are left alone (delete not ticked).")
        head += f"\n{plan['unchanged']} file(s) identical."
        self.compare_label.configure(text=head)
        lines = []
        for p in copy:
            if p in upd:
                tag, note = "*", "updated (stays disabled)"
            elif p in remote:
                tag, note = "*", "changed"
            else:
                tag, note = "+", "new"
            lines.append(f"{tag}  {p}    ({note}, {_kb(plan['sizes'].get(p, 0))})")
        if copy:
            lines.append(f"   total to copy: {_kb(plan['bytes'])}")
            lines.append("")
        for p in plan.get("disabled_same", ()):
            lines.append(f"   {p}    (disabled app, already up to date, stays disabled)")
        for p in plan["kept"]:
            lines.append(f"=  {p}    (differs from this version; KEPT, badge copy wins)")
        for p in plan["delete"]:
            lines.append(f"-  {p}    (not in this version; "
                         + ("WILL BE DELETED)" if delete else "deleted only if 'Delete files' is ticked)"))
        for p in plan.get("lib_kept", ()):
            lines.append(f"   {p}    (not in this version; kept (may be used by apps))")
        for p in plan.get("apps_kept", ()):
            lines.append(f"   {p}    (app not in this version; kept)")
        for p in plan.get("data_extra", ()):
            lines.append(f"   {p}    (data, never deleted)")
        if not lines:
            lines = ["Nothing to do."]
        self._set_text(self.compare_box, "\n".join(lines))

    def _on_badge_changed(self, _payload) -> None:
        if self.compare:
            self.compare = None
            self.install_btn.configure(state="disabled")
            self.compare_label.configure(text="Badge files changed since the last compare; compare again.")

    # --------------------------------------------------------------- install
    def install(self) -> None:
        r = self.selected
        if not r:
            return
        if not self.compare or self.compare["sha"] != r.sha:
            self.compare_with_badge(then=self.install)
            return
        c = self.compare
        delete = self.delete_var.get()
        backup = bool(self.ctx.config.get("backup_before_push"))
        msg = confirm_text(c["name"], c["sha"], c["plan"], delete, backup)
        if not confirm(self, "Install firmware", msg):
            return
        self.do_install(delete=delete, backup=backup)

    def do_install(self, delete: bool = False, backup: bool = True) -> None:
        c = dict(self.compare)
        badge = self.ctx.badge
        self.install_btn.configure(state="disabled")
        self.compare_btn.configure(state="disabled")
        self.progress.set(0)
        self.progress_label.configure(text="Starting ...")

        def prog(i, n, path):
            self.ctx.ui(self._set_progress, i, n, path)

        def work():
            bpath = None
            if backup:
                self.ctx.ui(lambda: self.progress_label.configure(text="Backing up badge files ..."))
                bdir = Path(self.ctx.config.get("backup_dir")).expanduser()

                def bprog(done, total, text):
                    line = (f"Backing up badge files: {done * 100 // total}% ({text})" if total
                            else f"Backing up badge files: {text}")
                    self.ctx.ui(lambda: self.progress_label.configure(text=line))
                try:
                    # Verified, file-by-file backup; raises BackupIncomplete if any file is missing,
                    # which aborts the install before anything is written.
                    bpath = badge.backup(bdir, progress=bprog)
                except Exception as e:  # noqa: BLE001
                    raise BadgeError(f"Backup before install failed, nothing was installed. {e}") from e
                self.ctx.log(f"Backup saved to {bpath}")
            plan = install_plan(c["plan"], delete)
            res = badge.push_tree(c["tmpdir"], "/", progress=prog, delete_extra=delete, protect=PROTECT,
                                  only_if_missing=("/data",), plan=plan)
            self.ctx.ui(lambda: self.progress_label.configure(text="Hard-resetting badge ..."))
            badge.reset()
            return res, bpath

        def done(out):
            res, bpath = out
            self.compare_btn.configure(state="normal")
            self.progress.set(1)
            n = len(res["copied"]) + len(res["deleted"])
            self.progress_label.configure(text=f"Done: {len(res['copied'])} copied, {len(res['deleted'])} deleted")
            self.ctx.config.set("installed_ref", {
                "name": c["name"], "sha": c["sha"], "date": c["date"],
                "installed_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M")})
            self._update_installed_label()
            self.ctx.log(f"Installed {c['name']} @ {c['sha'][:8]}: {len(res['copied'])} copied, "
                         f"{len(res['deleted'])} deleted, {res['unchanged']} unchanged, "
                         f"{len(res['kept'])} /data file(s) kept"
                         + (f"; backup in {bpath}" if bpath else ""))
            self.ctx.status(f"Installed {c['name']} ({n} file change(s)); badge reset")
            self.ctx.events.publish("badge.files_changed", res["copied"] + res["deleted"])

        def failed(e):
            self.compare_btn.configure(state="normal")
            self.install_btn.configure(state="normal" if self.compare else "disabled")
            self.progress_label.configure(text=f"Install failed: {str(e)[:60]}")
            self.compare_label.configure(text=f"Install failed: {e}")
            if "space" in str(e).lower():
                info(self, "Not enough space on the badge", f"{e}\n\nNothing was copied.")
        self.ctx.run_device_task(work, done, failed, title=f"Installing {c['name']}")

    def _set_progress(self, i, n, path) -> None:
        self.progress.set(i / n if n else 1)
        self.progress_label.configure(text=f"{i}/{n}  {path}" if path != "done" else f"{n}/{n} copied")

    # --------------------------------------------------------------- runtime
    def _strong_confirm(self, title: str, message: str, word: str = "FLASH") -> bool:
        if not confirm(self, title, message + "\n\n" + BOOTLOADER_HELP + "\n\nContinue?"):
            return False
        dlg = ctk.CTkInputDialog(title=title, text=f"Type {word} to confirm:")
        ans = dlg.get_input()
        return (ans or "").strip().upper() == word

    def _runtime_ref(self) -> str:
        return self.selected.name if self.selected else UPSTREAM_REF

    def _set_rt_busy(self, busy: bool, text: str = "") -> None:
        for b in self.rt_buttons:
            b.configure(state="disabled" if busy else "normal")
        self.rt_label.configure(text=text)

    def _run_esptool(self, args: list[str], title: str, on_ok=None) -> None:
        def work():
            rc = self.ctx.badge.esptool(*args, timeout=900, line_cb=self.ctx.log)
            if rc != 0:
                raise RuntimeError(f"esptool exited with code {rc} (is the badge in bootloader mode?)")
            if "write-flash" in args and hasattr(self.ctx.badge, "wait_for_boot"):
                self.ctx.badge.wait_for_boot()   # esptool hard-resets: let it reboot before re-reads
            return rc

        def done(_rc):
            self._set_rt_busy(False, f"{title}: done")
            self.ctx.status(f"{title}: done")
            if on_ok:
                on_ok()

        def failed(e):
            self._set_rt_busy(False, f"{title} failed: {e}")
        self._set_rt_busy(True, f"{title} ... (see log)")
        self.ctx.run_device_task(work, done, failed, title=title)

    def backup_flash(self) -> None:
        bdir = Path(self.ctx.config.get("backup_dir")).expanduser()
        dest = bdir / _dt.datetime.now().strftime("flash-%Y%m%d-%H%M%S.bin")
        if not confirm(self, "Back up full flash",
                       f"Read the whole 8 MB flash into\n{dest}\n\nThis takes a few minutes and does not "
                       f"change the badge.\n\n{BOOTLOADER_HELP}\n\nContinue?"):
            return
        bdir.mkdir(parents=True, exist_ok=True)
        self._run_esptool(["--chip", "esp32s3", "-b", "460800", "read-flash", "0", FLASH_SIZE, str(dest)],
                          "Flash backup", on_ok=lambda: self.ctx.log(f"Flash image saved to {dest}"))

    def _export_bins(self, ref: str, names: list[str], then) -> None:
        tmp = self._new_tmpdir("badge-bin-")

        def work():
            out = {}
            for n in names:
                data = self.ctx.sources.show_file(ref, BIN_PREFIX + n)
                p = Path(tmp) / n
                p.write_bytes(data)
                out[n] = str(p)
            return out

        def failed(e):
            self._set_rt_busy(False, f"Could not export runtime files from {ref}: {e}")
        self._set_rt_busy(True, f"Exporting {', '.join(names)} from {ref} ...")
        self.ctx.run_task(work, then, failed, title="Exporting runtime images")

    def flash_runtime(self) -> None:
        ref = self._runtime_ref()
        msg = (f"Flash the MicroPython runtime from {ref}:\n"
               + "\n".join(f"  {a}  {f}" for a, f in RUNTIME_FILES)
               + "\n\nThe badge filesystem (apps, /data) is not erased, but a bad runtime can stop the badge "
                 "booting until you flash a good one. Back up the full flash first if unsure.")
        if not self._strong_confirm("Flash MicroPython runtime", msg):
            self.ctx.status("Runtime flash cancelled")
            return

        def go(paths):
            args = ["--chip", "esp32s3", "-b", "460800", "--before", "default-reset", "--after", "hard-reset",
                    "write-flash", "--flash-mode", "dio", "--flash-size", "8MB", "--flash-freq", "80m"]
            for addr, fn in RUNTIME_FILES:
                args += [addr, paths[fn]]
            self._run_esptool(args, "Runtime flash",
                              on_ok=lambda: info(self, "Runtime flashed",
                                                 "Done. Tap RESET on the badge if it does not restart by itself."))
        self._export_bins(ref, [f for _, f in RUNTIME_FILES], go)

    def restore_factory(self) -> None:
        ref = self._runtime_ref()
        msg = (f"Write {FACTORY_FILE} from {ref} over the WHOLE 8 MB flash at 0x0.\n\n"
               "This ERASES everything on the badge: all apps, settings and /data.\n"
               "Back up badge files (Device tab) BEFORE entering bootloader mode if you want to keep them.")
        if not self._strong_confirm("Restore factory image", msg, word="ERASE"):
            self.ctx.status("Factory restore cancelled")
            return

        def go(paths):
            args = ["--chip", "esp32s3", "-b", "460800", "--before", "default-reset", "--after", "hard-reset",
                    "write-flash", "--flash-size", "8MB", "0x0", paths[FACTORY_FILE]]

            def ok():
                self.ctx.config.set("installed_ref", None)
                self._update_installed_label()
                self.ctx.events.publish("badge.files_changed", ["/"])
                info(self, "Factory image restored", "Done. Tap RESET on the badge if it does not restart.")
            self._run_esptool(args, "Factory restore", on_ok=ok)
        self._export_bins(ref, [FACTORY_FILE], go)
