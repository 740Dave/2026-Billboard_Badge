"""Apps tab: see, enable/disable, remove and install badge apps from every fork and local folders.

Badge facts this relies on (see firmware/badge/lib/app_switcher.py):
  * Every /apps/*.py whose name does not start with "_" is imported at boot (except base_app,
    template, __init__) and every BaseApp subclass in it becomes an LED-menu entry.
  * So disabling an app non-destructively = rename /apps/foo.py -> /apps/_foo.py (and back).
  * Menu order is alphabetical by module name, then class order inside the module.
  * Changes take effect after a HARD reset (never soft reset: LVGL does not survive it).

The pure helpers at module level (scan_badge, set_enabled, remove_app, build_install_plan,
execute_plan, menu_positions) take a badge service / source trees as arguments so they can be
tested against FakeBadgeService without the UI.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from core.config import HOME
from core.sources import BADGE_SUBDIR, parse_app

from .base import BaseTab, Section, human_size

REMOVED_DIR = HOME / "removed_apps"
CORE_APP_FILES = {"__init__.py", "base_app.py", "template.py"}
MENU_DENY = {"__init__", "base_app", "template"}
# Firmware-level files an app install must never overwrite (they come with a firmware build).
CORE_FILES = {"lib/epd.py", "lib/button.py", "lib/led_bar.py", "lib/app_switcher.py", "main.py",
              "apps/base_app.py"}
NUM_LEDS = 8


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def app_deps(src: str) -> list[str]:
    """Other /apps modules an app imports (e.g. shared helpers), besides base_app."""
    mods = set(re.findall(r"^\s*(?:from|import)\s+apps\.(\w+)", src, re.M))
    for group in re.findall(r"^\s*from\s+apps\s+import\s+\(?([\w ,]+)\)?", src, re.M):
        mods |= {g.strip().split(" as ")[0] for g in group.split(",") if g.strip()}
    return sorted(f"apps/{m}.py" for m in mods - {"base_app", "__init__"})


def concrete_classes(src: str, classes: list[str]) -> list[str]:
    """Classes the app switcher instantiates: skips ones whose own body sets _abstract = True."""
    out = []
    for c in classes:
        m = re.search(rf"^class\s+{c}\b.*?(?=^\S|\Z)", src, re.M | re.S)
        if m and re.search(r"^\s+_abstract\s*=\s*True", m.group(0), re.M):
            continue
        out.append(c)
    return out


# ====================================================================== source trees
class SourceTree:
    """Files of one source (git commit or local folder), keyed by badge-relative path."""
    label = "?"

    def files(self) -> set[str]:
        raise NotImplementedError

    def read(self, rel: str) -> bytes | None:
        raise NotImplementedError


class GitTree(SourceTree):
    def __init__(self, sources, sha: str, label: str, tree: dict[str, str], blob_cache: dict):
        self.sources, self.sha, self.label, self.tree, self.blobs = sources, sha, label, tree, blob_cache

    @staticmethod
    def load_tree(sources, sha: str) -> dict[str, str]:
        out = {}
        txt = sources.git("ls-tree", "-r", sha, "--", BADGE_SUBDIR)
        for line in txt.splitlines():
            meta, _, path = line.partition("\t")
            parts = meta.split()
            if len(parts) == 3 and parts[1] == "blob" and path.startswith(BADGE_SUBDIR + "/"):
                out[path[len(BADGE_SUBDIR) + 1:]] = parts[2]
        return out

    def files(self):
        return set(self.tree)

    def read(self, rel):
        blob = self.tree.get(rel)
        if blob is None:
            return None
        if blob not in self.blobs:
            self.blobs[blob] = self.sources.git("cat-file", "blob", blob, binary=True)
        return self.blobs[blob]


class LocalTree(SourceTree):
    def __init__(self, root: Path, label: str):
        self.root, self.label = Path(root), label

    def files(self):
        return {f.relative_to(self.root).as_posix() for f in self.root.rglob("*")
                if f.is_file() and "__pycache__" not in f.parts and not f.name.startswith(".")}

    def read(self, rel):
        p = self.root / rel
        return p.read_bytes() if p.is_file() else None


def local_tree_for(app_file: Path, label_prefix: str = "local") -> tuple[LocalTree, str]:
    """A local app folder named 'apps' is treated as a badge root's apps/ (libs in ../lib)."""
    d = app_file.parent
    if d.name == "apps":
        return LocalTree(d.parent, f"{label_prefix}:{_short(d)}"), f"apps/{app_file.name}"
    return LocalTree(d, f"{label_prefix}:{_short(d)}"), app_file.name


def _short(p: Path) -> str:
    try:
        return "~/" + str(Path(p).resolve().relative_to(Path.home()))
    except ValueError:
        return str(p)


# ====================================================================== catalog
@dataclass
class Provider:
    label: str          # "kcriqui/main" or "local:~/..."
    date: str           # ISO date (commit date or file mtime)
    tree: SourceTree
    app_rel: str        # path of the app inside tree, e.g. "apps/xkcd.py"
    is_git: bool = True


@dataclass
class CatalogEntry:
    key: str            # sha256 of the app file
    module: str
    title: str
    classes: list[str]
    lib_deps: list[str]
    data_refs: list[str]
    app_deps: list[str]
    size: int
    src: bytes
    providers: list[Provider] = field(default_factory=list)

    @property
    def newest(self) -> str:
        return self.providers[0].date if self.providers else ""


def _ts(iso: str) -> float:
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return 0.0


def add_to_catalog(catalog: dict[str, CatalogEntry], data: bytes, module: str, prov: Provider) -> None:
    key = sha256(data)
    e = catalog.get(key)
    if e is None:
        text = data.decode(errors="replace")
        info = parse_app(text, ref=prov.label, path=prov.app_rel)
        e = catalog[key] = CatalogEntry(key, module, info.title, info.classes, info.lib_deps,
                                        info.data_refs, app_deps(text), len(data), data)
    if all(p.label != prov.label for p in e.providers):
        e.providers.append(prov)
        e.providers.sort(key=lambda p: _ts(p.date), reverse=True)


def is_app_file(rel: str) -> bool:
    name = rel[len("apps/"):] if rel.startswith("apps/") else None
    return bool(name) and "/" not in name and name.endswith(".py") and not name.startswith("_") \
        and name not in CORE_APP_FILES


# ====================================================================== badge state
@dataclass
class BadgeApp:
    path: str           # "/apps/_xkcd.py"
    module: str         # "xkcd" (without the disabling underscore)
    enabled: bool
    core: bool
    size: int
    hash: str
    title: str = ""
    classes: list[str] = field(default_factory=list)
    lib_deps: list[str] = field(default_factory=list)
    data_refs: list[str] = field(default_factory=list)
    menu: str = ""      # "#3" / "#3-4" / ""


@dataclass
class BadgeState:
    apps: list[BadgeApp]
    hashes: dict[str, str]       # "/apps/x.py" / "/lib/y.py" -> sha256
    paths: set[str]              # every known badge path (apps, lib, data, root entries)
    menu_count: int = 0

    def app(self, path: str) -> BadgeApp | None:
        return next((a for a in self.apps if a.path == path), None)


def menu_positions(apps: list[BadgeApp]) -> int:
    """Fill BadgeApp.menu for enabled apps (alphabetical module order, then class order)."""
    pos = 0
    for a in sorted(apps, key=lambda a: Path(a.path).stem):
        a.menu = ""
        if not a.enabled or a.core or Path(a.path).stem in MENU_DENY:
            continue
        n = len(a.classes)
        if n == 0:
            a.menu = "none"
            continue
        a.menu = f"#{pos + 1}" if n == 1 else f"#{pos + 1}-{pos + n}"
        pos += n
    return pos


def scan_badge(badge, content_cache: dict[str, bytes]) -> BadgeState:
    """Blocking (device thread). content_cache maps sha256 -> file bytes to avoid re-reading."""
    entries = badge.listdir("/apps")
    hashes: dict[str, str] = {}
    for d in ("/apps", "/lib"):
        try:
            hashes.update(badge.file_hashes(d))
        except Exception:  # noqa: BLE001 - folder may not exist
            pass
    paths = set(hashes)
    for d, rec in (("/data", True), ("/", False)):
        try:
            paths |= {e.path for e in badge.listdir(d, recursive=rec)}
        except Exception:  # noqa: BLE001
            pass
    apps = []
    for e in entries:
        name = Path(e.path).name
        if e.is_dir or not name.endswith(".py"):
            continue
        core = name in CORE_APP_FILES
        enabled = not name.startswith("_")
        module = Path(name).stem.lstrip("_") if not core else Path(name).stem
        h = hashes.get(e.path, "")
        a = BadgeApp(e.path, module, enabled, core, e.size, h)
        if not core:
            data = content_cache.get(h) if h else None
            if data is None:
                data = badge.read_file(e.path)
                h = a.hash = h or sha256(data)
                content_cache[h] = data
            info = parse_app(data.decode(errors="replace"), ref="badge", path=e.path)
            a.title, a.lib_deps, a.data_refs = info.title, info.lib_deps, info.data_refs
            a.classes = concrete_classes(data.decode(errors="replace"), info.classes)
        apps.append(a)
    st = BadgeState(apps, hashes, paths)
    st.menu_count = menu_positions(apps)
    apps.sort(key=lambda a: (a.core, Path(a.path).stem.lstrip("_")))
    return st


def set_enabled(badge, app_path: str, enable: bool) -> str:
    """Rename /apps/x.py <-> /apps/_x.py. Returns the new path."""
    p = Path(app_path)
    if p.name in CORE_APP_FILES:
        raise ValueError(f"{p.name} is a core file")
    name = p.name.lstrip("_") if enable else "_" + p.name
    if name == p.name:
        return app_path
    dst = f"/apps/{name}"
    if badge.exists(dst):
        raise ValueError(f"{dst} already exists on the badge; remove one of the copies first")
    badge.rename(app_path, dst)
    return dst


def backup_local(data: bytes, badge_path: str, backup_dir: Path = REMOVED_DIR) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{stamp}-{Path(badge_path).name}"
    dest.write_bytes(data)
    return dest


def remove_app(badge, app_path: str, keep_copy: bool, backup_dir: Path = REMOVED_DIR) -> Path | None:
    if Path(app_path).name in CORE_APP_FILES:
        raise ValueError(f"{app_path} is a core file and can't be removed")
    saved = backup_local(badge.read_file(app_path), app_path, backup_dir) if keep_copy else None
    badge.remove(app_path)
    return saved


# ====================================================================== install plan
@dataclass
class PlanItem:
    badge_path: str
    data: bytes
    why: str


@dataclass
class InstallPlan:
    module: str
    source: str
    copy: list[PlanItem] = field(default_factory=list)          # will be written
    optional: list[PlanItem] = field(default_factory=list)      # differing non-core files (opt-in)
    backups: list[str] = field(default_factory=list)            # badge files saved locally before overwrite
    core_conflicts: list[str] = field(default_factory=list)     # needs firmware from <source>
    unresolved: list[str] = field(default_factory=list)         # deps found nowhere
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        L = [f"Install '{self.module}' from {self.source}", ""]
        if self.copy:
            L.append("Will copy to the badge:")
            L += [f"  + {i.badge_path}  ({human_size(len(i.data))}; {i.why})" for i in self.copy]
        else:
            L.append("Nothing new to copy.")
        if self.backups:
            L += ["", "Overwrites (old version saved to ~/.badge_manager/removed_apps first):"]
            L += [f"  ! {p}" for p in self.backups]
        if self.optional:
            L += ["", "Differ from the badge's version, NOT overwritten unless ticked below:"]
            L += [f"  ~ {i.badge_path}  ({i.why})" for i in self.optional]
        if self.core_conflicts:
            L += ["", f"Core firmware files differ; NOT overwritten. Needs firmware from {self.source}; "
                      "see the Firmware tab:"]
            L += [f"  x {p}" for p in self.core_conflicts]
        if self.unresolved:
            L += ["", "Referenced but not found in the source or on the badge (may be created at runtime):"]
            L += [f"  ? {p}" for p in self.unresolved]
        if self.notes:
            L += [""] + [f"Note: {n}" for n in self.notes]
        return "\n".join(L)


def build_install_plan(module: str, app_data: bytes, tree: SourceTree | None, source_label: str,
                       state: BadgeState) -> InstallPlan:
    """Work out what to copy. Blocking (may read git blobs); run in ctx.run_task."""
    plan = InstallPlan(module, source_label)
    if not re.fullmatch(r"[A-Za-z_]\w*", module):
        plan.notes.append(f"'{module}' is not a valid Python module name; the app switcher can't import it.")
    dest = f"/apps/{module}.py"
    old = state.hashes.get(dest)
    new_hash = sha256(app_data)
    if old != new_hash:
        plan.copy.append(PlanItem(dest, app_data, "the app"))
        if old:
            plan.backups.append(dest)
    if f"/apps/_{module}.py" in state.paths:
        plan.notes.append(f"A disabled copy /apps/_{module}.py is on the badge; it is left as is.")
    if module.startswith("_"):
        plan.notes.append("The file name starts with '_', so the app will be installed DISABLED.")

    tree_files = tree.files() if tree else set()
    seen: set[str] = set()
    todo = [(rel, "imported by the app") for rel in parse_app(app_data.decode(errors="replace"), "", "").lib_deps]
    todo += [(rel, "imported by the app") for rel in app_deps(app_data.decode(errors="replace"))]
    while todo:
        rel, why = todo.pop(0)
        if rel in seen:
            continue
        seen.add(rel)
        # a lib package (lib/foo/__init__.py ...) instead of lib/foo.py
        rels = [rel]
        if rel not in tree_files and "/" + rel not in state.paths:
            pkg = rel[:-3] + "/"
            members = sorted(f for f in tree_files if f.startswith(pkg))
            if members:
                rels = members
        for r in rels:
            bp = "/" + r
            data = tree.read(r) if tree and r in tree_files else None
            on_badge = state.hashes.get(bp) or ("x" if bp in state.paths else None)
            if data is None:
                if not on_badge:
                    plan.unresolved.append(bp)
                continue
            if r.endswith(".py"):
                text = data.decode(errors="replace")
                for dep in parse_app(text, "", "").lib_deps + app_deps(text):
                    todo.append((dep, f"needed by {r}"))
            if not on_badge:
                if r in CORE_FILES:
                    plan.core_conflicts.append(bp + " (missing on badge)")
                else:
                    plan.copy.append(PlanItem(bp, data, why))
            elif on_badge != sha256(data):
                if r in CORE_FILES:
                    plan.core_conflicts.append(bp)
                else:
                    plan.optional.append(PlanItem(bp, data, f"{why}; badge has a different version"))

    for ref in parse_app(app_data.decode(errors="replace"), "", "").data_refs:
        rel = ref.lstrip("/")
        if ref in state.paths:
            continue
        if rel in tree_files:
            plan.copy.append(PlanItem(ref, tree.read(rel), "data file"))
            continue
        prefix = rel.rstrip("/") + "/"
        members = sorted(f for f in tree_files if f.startswith(prefix))
        for m in members:
            if "/" + m not in state.paths:
                plan.copy.append(PlanItem("/" + m, tree.read(m), f"data under {ref}"))
        if not members and ref.rstrip("/") not in state.paths:
            plan.unresolved.append(ref)
    return plan


def execute_plan(badge, plan: InstallPlan, include_optional: bool = False,
                 backup_dir: Path = REMOVED_DIR, progress=None) -> list[str]:
    """Blocking (device thread). Returns the badge paths written."""
    items = plan.copy + (plan.optional if include_optional else [])
    to_backup = set(plan.backups) | ({i.badge_path for i in plan.optional} if include_optional else set())
    for p in sorted(to_backup):
        try:
            backup_local(badge.read_file(p), p, backup_dir)
        except Exception:  # noqa: BLE001 - nothing to back up
            pass
    written = []
    for n, item in enumerate(items):
        if progress:
            progress(n, len(items), item.badge_path)
        badge.write_bytes(item.data, item.badge_path)
        written.append(item.badge_path)
    return written


# ====================================================================== small windows
def _style_tree(widget: ttk.Treeview) -> None:
    dark = ctk.get_appearance_mode() == "Dark"
    st = ttk.Style(widget)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    bg, fg, sel = ("#2b2b2b", "#dcdcdc", "#1f6aa5") if dark else ("#ffffff", "#1a1a1a", "#3b8ed0")
    st.configure("Apps.Treeview", background=bg, fieldbackground=bg, foreground=fg, rowheight=24,
                 borderwidth=0)
    st.map("Apps.Treeview", background=[("selected", sel)], foreground=[("selected", "white")])
    st.configure("Apps.Treeview.Heading", background="#3a3a3a" if dark else "#e5e5e5",
                 foreground=fg, relief="flat")
    widget.configure(style="Apps.Treeview")
    widget.tag_configure("muted", foreground="gray55")
    widget.tag_configure("warn", foreground="#d08a1f")
    widget.tag_configure("ok", foreground="#3ba55d")


def show_text_window(parent, title: str, text: str) -> ctk.CTkToplevel:
    win = ctk.CTkToplevel(parent)
    win.title(title)
    win.geometry("820x640")
    box = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Menlo", size=12), wrap="none")
    box.pack(fill="both", expand=True, padx=8, pady=8)
    box.insert("1.0", text)
    box.configure(state="disabled")
    win.after(100, win.lift)
    return win


class PlanDialog(ctk.CTkToplevel):
    def __init__(self, parent, plan: InstallPlan, on_ok):
        super().__init__(parent)
        self.title(f"Install {plan.module}")
        self.geometry("760x520")
        self.on_ok = on_ok
        box = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Menlo", size=12), wrap="word")
        box.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        box.insert("1.0", plan.summary())
        box.configure(state="disabled")
        self.opt = tk.BooleanVar(value=False)
        if plan.optional:
            ctk.CTkCheckBox(self, variable=self.opt,
                            text=f"Also overwrite the {len(plan.optional)} differing non-core file(s) "
                                 "(old versions are saved locally)").pack(anchor="w", padx=10, pady=4)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(4, 10))
        ctk.CTkButton(row, text="Cancel", fg_color="gray40", width=100,
                      command=self.destroy).pack(side="right", padx=4)
        ok = ctk.CTkButton(row, text="Install", width=120, command=self._ok)
        ok.pack(side="right", padx=4)
        if not plan.copy and not plan.optional:
            ok.configure(state="disabled")
        self.after(100, self._modal)

    def _modal(self):
        try:
            self.lift()
            self.grab_set()
        except tk.TclError:
            pass

    def _ok(self):
        opt = self.opt.get()
        self.destroy()
        self.on_ok(opt)


# ====================================================================== the tab
class AppsTab(BaseTab):
    TITLE = "Apps"

    def build(self) -> None:
        self.state: BadgeState | None = None
        self.catalog: dict[str, CatalogEntry] = {}
        self._content: dict[str, bytes] = {}        # sha256 -> bytes (badge + catalog files)
        self._trees: dict[str, dict[str, str]] = {}  # commit sha -> {rel: blob}
        self._blobs: dict[str, bytes] = {}           # git blob -> bytes
        self._badge_busy = self._badge_again = False
        self._cat_busy = self._cat_again = False
        self._cat_refs = None
        self._dirty = False
        self._shown = False
        self._cat_rows: dict[str, CatalogEntry] = {}

        # ---- toolbar
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=4, pady=(4, 6))
        ctk.CTkButton(bar, text="Refresh badge", width=120, command=self.refresh_badge).pack(side="left", padx=4)
        ctk.CTkButton(bar, text="Rescan sources", width=120, command=self.refresh_catalog).pack(side="left", padx=4)
        ctk.CTkButton(bar, text="Install from file…", width=140, command=self.install_from_file).pack(side="left", padx=4)
        self.apply_btn = ctk.CTkButton(bar, text="Apply & restart badge", width=170, fg_color="gray40",
                                       command=self.apply_restart)
        self.apply_btn.pack(side="right", padx=4)
        self.apply_note = ctk.CTkLabel(bar, text="New/enabled apps appear in the LED menu after a restart.",
                                       text_color="gray60")
        self.apply_note.pack(side="right", padx=8)

        panes = ctk.CTkFrame(self, fg_color="transparent")
        panes.pack(fill="both", expand=True)
        panes.grid_columnconfigure(0, weight=5, uniform="p")
        panes.grid_columnconfigure(1, weight=6, uniform="p")
        panes.grid_rowconfigure(0, weight=1)

        # ---- left: on the badge
        left = Section(panes, "On the badge")
        left.grid(row=0, column=0, sticky="nsew", padx=(4, 4))
        self.badge_info = ctk.CTkLabel(left.body, text="Not scanned yet", anchor="w", text_color="gray60")
        self.badge_info.pack(fill="x")
        tf = tk.Frame(left.body)
        tf.pack(fill="both", expand=True, pady=4)
        self.btree = ttk.Treeview(tf, columns=("menu", "module", "title", "state", "size"), show="headings",
                                  selectmode="browse")
        for col, text, w, st in (("menu", "LED", 55, False), ("module", "Module", 120, False),
                                 ("title", "Title", 200, True), ("state", "State", 80, False),
                                 ("size", "Size", 70, False)):
            self.btree.heading(col, text=text, anchor="w")
            self.btree.column(col, width=w, stretch=st, anchor="w")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.btree.yview)
        self.btree.configure(yscrollcommand=sb.set)
        self.btree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        _style_tree(self.btree)
        self.btree.bind("<<TreeviewSelect>>", lambda e: self._badge_selected())
        self.btree.bind("<Double-1>", lambda e: self.open_badge_source())
        self.badge_detail = ctk.CTkLabel(left.body, text="", anchor="w", justify="left", wraplength=460)
        self.badge_detail.pack(fill="x", pady=2)
        row = ctk.CTkFrame(left.body, fg_color="transparent")
        row.pack(fill="x", pady=(4, 0))
        self.toggle_btn = ctk.CTkButton(row, text="Disable", width=90, command=self.toggle_selected, state="disabled")
        self.toggle_btn.pack(side="left", padx=(0, 4))
        self.remove_btn = ctk.CTkButton(row, text="Remove…", width=90, fg_color="#a33", hover_color="#822",
                                        command=self.remove_selected, state="disabled")
        self.remove_btn.pack(side="left", padx=4)
        self.bsrc_btn = ctk.CTkButton(row, text="Open source", width=100, command=self.open_badge_source,
                                      state="disabled")
        self.bsrc_btn.pack(side="left", padx=4)

        # ---- right: available
        right = Section(panes, "Available (all forks/branches + local folders)")
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 4))
        frow = ctk.CTkFrame(right.body, fg_color="transparent")
        frow.pack(fill="x")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self._fill_catalog())
        ctk.CTkEntry(frow, textvariable=self.search, placeholder_text="Filter: name, title, fork…",
                     width=240).pack(side="left")
        self.hide_same = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(frow, text="Hide apps identical to the badge", variable=self.hide_same,
                        command=self._fill_catalog).pack(side="left", padx=10)
        prow = ctk.CTkFrame(right.body, fg_color="transparent")
        prow.pack(fill="x", pady=(4, 0))
        self.cat_progress = ctk.CTkProgressBar(prow, height=8)
        self.cat_progress.set(0)
        self.cat_progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.cat_label = ctk.CTkLabel(prow, text="Sources not scanned yet", text_color="gray60", width=260,
                                      anchor="e")
        self.cat_label.pack(side="right")
        tf2 = tk.Frame(right.body)
        tf2.pack(fill="both", expand=True, pady=4)
        self.ctree = ttk.Treeview(tf2, columns=("module", "title", "status", "sources", "date"),
                                  show="headings", selectmode="browse")
        for col, text, w, st in (("module", "Module", 110, False), ("title", "Title", 190, True),
                                 ("status", "On badge", 120, False), ("sources", "From", 190, True),
                                 ("date", "Newest", 85, False)):
            self.ctree.heading(col, text=text, anchor="w")
            self.ctree.column(col, width=w, stretch=st, anchor="w")
        sb2 = ttk.Scrollbar(tf2, orient="vertical", command=self.ctree.yview)
        self.ctree.configure(yscrollcommand=sb2.set)
        self.ctree.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")
        _style_tree(self.ctree)
        self.ctree.bind("<<TreeviewSelect>>", lambda e: self._catalog_selected())
        self.ctree.bind("<Double-1>", lambda e: self.open_catalog_source())
        self.cat_detail = ctk.CTkTextbox(right.body, height=150, wrap="word",
                                         font=ctk.CTkFont(family="Menlo", size=11))
        self.cat_detail.pack(fill="x", pady=2)
        self.cat_detail.configure(state="disabled")
        irow = ctk.CTkFrame(right.body, fg_color="transparent")
        irow.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(irow, text="Install from:").pack(side="left")
        self.prov_menu = ctk.CTkOptionMenu(irow, values=["-"], width=260)
        self.prov_menu.pack(side="left", padx=6)
        self.install_btn = ctk.CTkButton(irow, text="Install…", width=100, command=self.install_selected,
                                         state="disabled")
        self.install_btn.pack(side="left", padx=4)
        self.csrc_btn = ctk.CTkButton(irow, text="Open source", width=100, command=self.open_catalog_source,
                                      state="disabled")
        self.csrc_btn.pack(side="left", padx=4)

        ev = self.ctx.events
        ev.subscribe("badge.files_changed", lambda _p: self.refresh_badge())
        ev.subscribe("badge.connected", lambda _p: self.refresh_badge())
        ev.subscribe("badge.disconnected", lambda _p: self._badge_unavailable("Badge disconnected"))
        ev.subscribe("port.changed", lambda _p: self.refresh_badge())
        ev.subscribe("sources.updated", self._on_sources_updated)

    def on_show(self) -> None:
        if not self._shown:
            self._shown = True
            self.refresh_badge()
            self.refresh_catalog()

    # ================================================================ badge side
    def refresh_badge(self) -> None:
        if self._badge_busy:
            self._badge_again = True
            return
        self._badge_busy = True
        self.badge_info.configure(text="Reading /apps from the badge…")
        badge, cache = self.ctx.badge, self._content
        self.ctx.run_device_task(lambda: scan_badge(badge, cache), self._badge_done, self._badge_failed,
                                 title="Reading badge apps")

    def _badge_done(self, state: BadgeState) -> None:
        self._badge_busy = False
        self.state = state
        self._fill_badge()
        self._fill_catalog()
        if self._badge_again:
            self._badge_again = False
            self.refresh_badge()

    def _badge_failed(self, exc) -> None:
        self._badge_busy = self._badge_again = False
        self._badge_unavailable(f"Couldn't read the badge: {exc}")

    def _badge_unavailable(self, msg: str) -> None:
        self.state = None
        self.btree.delete(*self.btree.get_children())
        self.badge_info.configure(text=msg)
        self._badge_selected()
        self._fill_catalog()

    def _fill_badge(self) -> None:
        sel = self._selected_badge_app()
        keep = sel.module if sel else None
        self.btree.delete(*self.btree.get_children())
        st = self.state
        if not st:
            return
        for a in st.apps:
            state = "core" if a.core else ("enabled" if a.enabled else "disabled")
            tags = ("muted",) if a.core or not a.enabled else ()
            title = a.title if not a.core else "(system file)"
            self.btree.insert("", "end", iid=a.path, tags=tags,
                              values=(a.menu, a.module, title, state, human_size(a.size)))
            if keep and a.module == keep and not a.core:
                self.btree.selection_set(a.path)
        n_en = sum(1 for a in st.apps if a.enabled and not a.core)
        n_dis = sum(1 for a in st.apps if not a.enabled and not a.core)
        extra = f"  (more than {NUM_LEDS} LEDs: the menu wraps)" if st.menu_count > NUM_LEDS else ""
        self.badge_info.configure(text=f"{n_en} enabled, {n_dis} disabled · {st.menu_count} menu entries{extra}")
        self._badge_selected()

    def _selected_badge_app(self) -> BadgeApp | None:
        sel = self.btree.selection()
        return self.state.app(sel[0]) if sel and self.state else None

    def _badge_selected(self) -> None:
        a = self._selected_badge_app()
        usable = a is not None and not a.core
        for b in (self.toggle_btn, self.remove_btn):
            b.configure(state="normal" if usable else "disabled")
        self.bsrc_btn.configure(state="normal" if a else "disabled")
        if not a:
            self.badge_detail.configure(text="")
            return
        self.toggle_btn.configure(text="Disable" if a.enabled else "Enable")
        if a.core:
            self.badge_detail.configure(text=f"{a.path}: required by the firmware; can't be changed here.")
            return
        lines = [f"{a.path}  ·  classes: {', '.join(a.classes) or 'none found'}"]
        missing = [d for d in a.lib_deps if "/" + d not in self.state.paths]
        if a.lib_deps:
            lines.append("libs: " + ", ".join(a.lib_deps) + (f"  (MISSING: {', '.join(missing)})" if missing else ""))
        miss_data = [d for d in a.data_refs if d not in self.state.paths]
        if miss_data:
            lines.append("data not on badge: " + ", ".join(miss_data))
        if not a.enabled:
            lines.append(f"Disabled (file starts with '_'); Enable renames it to {a.module}.py.")
        self.badge_detail.configure(text="\n".join(lines))

    def toggle_selected(self) -> None:
        a = self._selected_badge_app()
        if not a or a.core:
            return
        badge, enable = self.ctx.badge, not a.enabled

        def done(new_path):
            self._changed([a.path, new_path], f"{'Enabled' if enable else 'Disabled'} {a.module}")
        self.ctx.run_device_task(lambda: set_enabled(badge, a.path, enable), done,
                                 lambda e: messagebox.showerror("Apps", str(e), parent=self),
                                 title=f"{'Enabling' if enable else 'Disabling'} {a.module}")

    def remove_selected(self) -> None:
        a = self._selected_badge_app()
        if not a or a.core:
            return
        ans = messagebox.askyesnocancel(
            "Remove app",
            f"Remove {a.path} from the badge?\n\n"
            f"Save a copy to {_short(REMOVED_DIR)} first?\n"
            "  Yes = save a copy, then remove\n  No = remove without a copy\n  Cancel = keep it\n\n"
            "(Tip: Disable keeps the file but hides it from the menu. Libraries and data files the app "
            "used are left on the badge.)", parent=self)
        if ans is None:
            return
        badge = self.ctx.badge

        def done(saved):
            if saved:
                self.ctx.log(f"Saved a copy of {a.path} to {saved}")
            self._changed([a.path], f"Removed {a.path}")
        self.ctx.run_device_task(lambda: remove_app(badge, a.path, keep_copy=bool(ans)), done,
                                 title=f"Removing {a.module}")

    def open_badge_source(self) -> None:
        a = self._selected_badge_app()
        if not a:
            return
        data = self._content.get(a.hash)
        if data is not None:
            show_text_window(self, f"badge:{a.path}", data.decode(errors="replace"))
            return
        badge = self.ctx.badge
        self.ctx.run_device_task(lambda: badge.read_file(a.path),
                                 lambda d: show_text_window(self, f"badge:{a.path}", d.decode(errors="replace")),
                                 title=f"Reading {a.path}")

    def _changed(self, paths: list[str], msg: str) -> None:
        self._dirty = True
        self.apply_btn.configure(fg_color=("#3b8ed0", "#1f6aa5"))
        self.apply_note.configure(text="Changes pending: restart the badge to update the LED menu.",
                                  text_color="#d08a1f")
        self.ctx.status(msg)
        self.ctx.log(msg)
        self.ctx.events.publish("badge.files_changed", paths)

    def apply_restart(self) -> None:
        def done(_):
            self._dirty = False
            self.apply_btn.configure(fg_color="gray40")
            self.apply_note.configure(text="Badge restarted; new apps are in the LED menu once it boots.",
                                      text_color="gray60")
            self.ctx.status("Badge restarting (hard reset)")
        self.ctx.run_device_task(self.ctx.badge.reset, done, title="Hard reset")

    # ================================================================ catalog side
    def _on_sources_updated(self, refs) -> None:
        self._cat_refs = refs
        self.refresh_catalog()

    def refresh_catalog(self) -> None:
        if self._cat_busy:
            self._cat_again = True
            return
        self._cat_busy = True
        refs, self._cat_refs = self._cat_refs, None
        local_dirs = list(self.ctx.config.get("local_app_dirs") or [])
        self.ctx.run_task(lambda: self._scan_catalog(refs, local_dirs), self._catalog_done, self._catalog_failed,
                          title="Scanning apps in forks")

    def _progress(self, i: int, n: int, text: str) -> None:
        self.cat_progress.set(i / n if n else 1)
        self.cat_label.configure(text=text)

    def _scan_catalog(self, refs, local_dirs) -> tuple[dict, list[str]]:
        """Background thread. Uses per-commit caches so rescans are cheap."""
        src = self.ctx.sources
        problems = []
        catalog: dict[str, CatalogEntry] = {}
        try:
            if refs is None:
                refs = src.refs(with_changes=False)
        except Exception as e:  # noqa: BLE001
            problems.append(f"git repo: {e}")
            refs = []
        n = len(refs) + len(local_dirs)
        for i, ref in enumerate(refs):
            self.ctx.ui(self._progress, i, n, f"{ref.name}")
            try:
                tree = self._trees.get(ref.sha)
                if tree is None:
                    tree = self._trees[ref.sha] = GitTree.load_tree(src, ref.sha)
                gt = GitTree(src, ref.sha, ref.name, tree, self._blobs)
                for rel in sorted(tree):
                    if is_app_file(rel):
                        data = gt.read(rel)
                        self._content.setdefault(sha256(data), data)
                        add_to_catalog(catalog, data, Path(rel).stem,
                                       Provider(ref.name, ref.date, gt, rel))
            except Exception as e:  # noqa: BLE001
                problems.append(f"{ref.name}: {e}")
        for j, d in enumerate(local_dirs):
            self.ctx.ui(self._progress, len(refs) + j, n, f"local {Path(d).name}")
            p = Path(d).expanduser()
            if not p.is_dir():
                continue
            for f in sorted(p.glob("*.py")):
                if f.name.startswith("_") or f.name in CORE_APP_FILES:
                    continue
                tree, rel = local_tree_for(f)
                data = f.read_bytes()
                mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime).astimezone().isoformat(timespec="seconds")
                add_to_catalog(catalog, data, f.stem, Provider(tree.label, mtime, tree, rel, is_git=False))
        self.ctx.ui(self._progress, n, n, "")
        return catalog, problems

    def _catalog_done(self, res) -> None:
        self._cat_busy = False
        self.catalog, problems = res
        for p in problems:
            self.ctx.log(f"Apps catalog: {p}")
        mods = {e.module for e in self.catalog.values()}
        self.cat_label.configure(text=f"{len(mods)} apps, {len(self.catalog)} versions"
                                      + (f" · {len(problems)} problem(s), see log" if problems else ""))
        self._fill_catalog()
        if self._cat_again:
            self._cat_again = False
            self.refresh_catalog()

    def _catalog_failed(self, exc) -> None:
        self._cat_busy = self._cat_again = False
        self.cat_label.configure(text=f"Scan failed: {exc}")

    def entry_status(self, e: CatalogEntry) -> tuple[str, str]:
        """(text, tag) for how this version relates to what's on the badge."""
        st = self.state
        if not st:
            return "?", ""
        if st.hashes.get(f"/apps/{e.module}.py") == e.key:
            return "installed", "ok"
        if st.hashes.get(f"/apps/_{e.module}.py") == e.key:
            return "installed (disabled)", "ok"
        if f"/apps/{e.module}.py" in st.paths or f"/apps/_{e.module}.py" in st.paths:
            return "other version", "warn"
        return "", ""

    def missing_deps(self, e: CatalogEntry) -> list[str]:
        if not self.state:
            return []
        return [d for d in e.lib_deps + e.app_deps if "/" + d not in self.state.paths] + \
               [d for d in e.data_refs if d not in self.state.paths]

    def _fill_catalog(self) -> None:
        sel = self.ctree.selection()
        keep = sel[0] if sel else None
        self.ctree.delete(*self.ctree.get_children())
        self._cat_rows = {}
        q = self.search.get().strip().lower()
        entries = sorted(self.catalog.values(), key=lambda e: _ts(e.newest), reverse=True)
        entries.sort(key=lambda e: e.module.lower())  # stable: newest version first per module
        for e in entries:
            status, tag = self.entry_status(e)
            if self.hide_same.get() and status.startswith("installed"):
                continue
            hay = " ".join([e.module, e.title, *e.classes, *(p.label for p in e.providers)]).lower()
            if q and q not in hay:
                continue
            labels = [p.label for p in e.providers]
            src = ", ".join(labels[:2]) + (f" +{len(labels) - 2}" if len(labels) > 2 else "")
            if not status and self.missing_deps(e):
                status = "needs deps"
            self.ctree.insert("", "end", iid=e.key, tags=(tag,) if tag else (),
                              values=(e.module, e.title, status, src, e.newest[:10]))
            self._cat_rows[e.key] = e
        if keep in self._cat_rows:
            self.ctree.selection_set(keep)
        self._catalog_selected()

    def _selected_entry(self) -> CatalogEntry | None:
        sel = self.ctree.selection()
        return self._cat_rows.get(sel[0]) if sel else None

    def _catalog_selected(self) -> None:
        e = self._selected_entry()
        self.install_btn.configure(state="normal" if e else "disabled")
        self.csrc_btn.configure(state="normal" if e else "disabled")
        self.cat_detail.configure(state="normal")
        self.cat_detail.delete("1.0", "end")
        if not e:
            self.prov_menu.configure(values=["-"])
            self.prov_menu.set("-")
            self.cat_detail.configure(state="disabled")
            return
        labels = [f"{p.label}  ({p.date[:10]})" for p in e.providers]
        cur = self.prov_menu.get()
        self.prov_menu.configure(values=labels)
        self.prov_menu.set(cur if cur in labels else labels[0])
        miss = set(self.missing_deps(e))
        others = sorted({o.key for o in self.catalog.values() if o.module == e.module} - {e.key})
        status, _ = self.entry_status(e)

        def mark(items):
            return ", ".join(f"{d} (missing)" if (d in miss or "/" + d in miss) else d for d in items) or "-"
        lines = [
            f"{e.module}.py  ·  {human_size(e.size)}  ·  sha256 {e.key[:12]}  ·  {status or 'not on badge'}",
            f"Title:   {e.title or '-'}",
            f"Classes: {', '.join(e.classes) or 'none found'}",
            f"Libs:    {mark(e.lib_deps + e.app_deps)}",
            f"Data:    {mark(e.data_refs)}",
            f"Other versions of {e.module}: {len(others)}" if others else "",
            f"Provided by ({len(e.providers)}, newest first):",
        ] + [f"  {p.date[:16].replace('T', ' ')}  {p.label}" for p in e.providers]
        if not self.state:
            lines.insert(1, "(badge not read; dependency status unknown)")
        self.cat_detail.insert("1.0", "\n".join(l for l in lines if l))
        self.cat_detail.configure(state="disabled")

    def _selected_provider(self, e: CatalogEntry) -> Provider:
        cur = self.prov_menu.get()
        return next((p for p in e.providers if f"{p.label}  ({p.date[:10]})" == cur), e.providers[0])

    def open_catalog_source(self) -> None:
        e = self._selected_entry()
        if e:
            show_text_window(self, f"{self._selected_provider(e).label}:{e.module}.py",
                             e.src.decode(errors="replace"))

    # ================================================================ install
    def install_selected(self) -> None:
        e = self._selected_entry()
        if not e:
            return
        p = self._selected_provider(e)
        self._plan_and_install(e.module, e.src, p.tree, p.label)

    def install_from_file(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="Install a badge app",
                                          filetypes=[("MicroPython app", "*.py"), ("All files", "*")])
        if not path:
            return
        f = Path(path)
        tree, _rel = local_tree_for(f, "file")
        self._plan_and_install(f.stem, f.read_bytes(), tree, f"file:{_short(f)}")

    def _plan_and_install(self, module: str, data: bytes, tree: SourceTree | None, label: str) -> None:
        if not self.state:
            messagebox.showinfo("Apps", "Read the badge first (connect it, then Refresh badge).", parent=self)
            return
        state = self.state
        self.ctx.run_task(lambda: build_install_plan(module, data, tree, label, state),
                          lambda plan: PlanDialog(self, plan, lambda opt: self._execute(plan, opt)),
                          title=f"Planning install of {module}")

    def _execute(self, plan: InstallPlan, include_optional: bool) -> None:
        badge = self.ctx.badge

        def prog(i, n, path):
            self.ctx.status(f"Installing {plan.module}: {i + 1}/{n} {path}")

        def done(written):
            self._changed(written, f"Installed {plan.module} ({len(written)} file(s))")
        self.ctx.run_device_task(lambda: execute_plan(badge, plan, include_optional, progress=prog), done,
                                 lambda e: messagebox.showerror("Install failed", str(e), parent=self),
                                 title=f"Installing {plan.module}")

