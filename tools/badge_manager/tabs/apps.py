"""Apps tab: see, enable/disable, remove and install badge apps from every fork and local folders.

Badge facts this relies on (see firmware/badge/lib/app_switcher.py):
  * Every /apps/*.py whose name does not start with "_" is imported at boot (except base_app,
    template, __init__) and every BaseApp subclass in it becomes an LED-menu entry.
  * So disabling an app non-destructively = rename /apps/foo.py -> /apps/_foo.py (and back).
  * Menu order comes from dir() of a generated module, so it is effectively arbitrary; the switcher
    writes the real order at every boot to /data/app_list (btree: bytes([slot]) -> app .name).
    Read with exec_py (read_app_list); missing -> alphabetical estimate marked "≈".
  * 8 LEDs per page: slots 1-8 orange, 9-16 cyan, 17-24 magenta.
  * Changes take effect after a HARD reset (never soft reset: LVGL does not survive it).

"Start at boot" (resume-last-app firmware change, lib/app_switcher.py):
  * /data/last_app.json {"module": "<file stem>", "app": "<BaseApp .name>"}: written by the badge
    when an app is launched from the menu; at boot that app starts instead of the menu.
  * /data/boot.json {"resume": false} turns that off (missing file / key = on). Read at boot.
    Writing either file goes through mpremote, which pauses the badge UI, so every boot-setting
    write ends with a hard reset (in a finally). Menu+/Menu- held at boot = menu.
  * Supported if /lib/app_switcher.py on the badge mentions "last_app.json".

The pure helpers at module level (scan_badge, set_enabled, remove_app, build_install_plan,
execute_plan, menu_positions, read_app_list, parse_app_list, read_boot_state, write_boot_resume,
write_last_app, write_boot_and_reset) take a badge service / source trees as arguments so they can be
tested against FakeBadgeService without the UI.
"""

from __future__ import annotations

import ast
import datetime
import gc
import hashlib
import json
import re
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from core.config import HOME
from core.sources import BADGE_SUBDIR, parse_app

from core import theme
from core.theme import COLORS

from .base import BaseTab, Section, human_size

REMOVED_DIR = HOME / "removed_apps"
CORE_APP_FILES = {"__init__.py", "base_app.py", "template.py"}
MENU_DENY = {"__init__", "base_app", "template"}
# Firmware-level files an app install must never overwrite (they come with a firmware build).
CORE_FILES = {"lib/epd.py", "lib/button.py", "lib/led_bar.py", "lib/app_switcher.py", "main.py",
              "apps/base_app.py"}
NUM_LEDS = 8
BOOT_PATH = "/data/boot.json"
LAST_APP_PATH = "/data/last_app.json"
SWITCHER_PATH = "/lib/app_switcher.py"
RESUME_MARKER = b"last_app.json"


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


_UNK = object()   # "can't tell statically"


def _regex_names(src: str, classes: list[str]) -> list[tuple[str, str, bool]]:
    """Fallback when the source doesn't parse: literal self.name / super().__init__("...") in the
    class body, else the class name. sure=False when the body computes the name."""
    out = []
    for c in classes:
        m = re.search(rf"^class\s+{c}\b.*?(?=^\S|\Z)", src, re.M | re.S)
        body = m.group(0) if m else ""
        lit = re.findall(r"self\.name\s*=\s*([\"'])(.*?)\1\s*$", body, re.M)
        if lit:
            out.append((c, lit[-1][1], True))
            continue
        sup = re.findall(r"super\(\)\.__init__\(\s*(?:name\s*=\s*)?([\"'])(.*?)\1", body)
        computed = bool(re.search(r"self\.name\s*=", body)) or bool(re.search(r"super\(\)\.__init__\(\s*\w", body))
        if sup and not re.search(r"self\.name\s*=", body):
            out.append((c, sup[-1][1], True))
        else:
            out.append((c, c, not computed))
    return out


class _NameSim:
    """Statically run a BaseApp subclass's __init__ chain (classes of the same file only) to find the
    .name an instance gets, e.g. ScheduleApp: self.name = self.day + " Talks" with SatSchedule.day =
    "Sat" -> "Sat Talks". Understands string literals, + concatenation, `x or y`, f-strings, parameter
    defaults, class/instance attributes, super().__init__(...) / Base.__init__(self, ...) and
    BaseApp's own rule (name or class name). Anything else -> _UNK."""

    def __init__(self, tree: ast.Module):
        self.defs = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}

    @staticmethod
    def _bases(cd: ast.ClassDef) -> list[str]:
        return [b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else "?"
                for b in cd.bases]

    def mro(self, cls: str) -> list | None:
        """Local classes, most derived first, ending with "BaseApp" (None if a base is unknown)."""
        out, cur = [], cls
        while True:
            if cur == "BaseApp" and cur not in self.defs:
                return out + ["BaseApp"]
            cd = self.defs.get(cur)
            if cd is None:
                return None
            out.append(cd)
            bases = self._bases(cd)
            if len(bases) != 1:   # multiple inheritance / no base: don't guess
                return None
            cur = bases[0]

    def name(self, cls: str):
        mro = self.mro(cls)
        if not mro:
            return _UNK
        self._mro, self._inst = mro, {}
        try:
            return self._call(0, [], {})
        except RecursionError:
            return _UNK

    def _attr(self, attr: str):
        if attr in self._inst:
            return self._inst[attr]
        for cd in self._mro[:-1]:
            for st in cd.body:
                if isinstance(st, ast.Assign) and any(isinstance(t, ast.Name) and t.id == attr for t in st.targets):
                    return self._eval(st.value, {})
                if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.target.id == attr \
                        and st.value is not None:
                    return self._eval(st.value, {})
        return _UNK

    def _eval(self, e, env: dict):
        if isinstance(e, ast.Constant) and (isinstance(e.value, str) or e.value is None):
            return e.value
        if isinstance(e, ast.Name):
            return env.get(e.id, _UNK)
        if isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name) and e.value.id == "self":
            return self._attr(e.attr)
        if isinstance(e, ast.Attribute) and e.attr == "__name__" and isinstance(e.value, ast.Attribute) \
                and e.value.attr == "__class__" and isinstance(e.value.value, ast.Name) and e.value.value.id == "self":
            return self._mro[0].name
        if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
            a, b = self._eval(e.left, env), self._eval(e.right, env)
            return a + b if isinstance(a, str) and isinstance(b, str) else _UNK
        if isinstance(e, ast.BoolOp) and isinstance(e.op, ast.Or):
            for v in e.values:
                r = self._eval(v, env)
                if r is _UNK:
                    return _UNK
                if r:
                    return r
            return r
        if isinstance(e, ast.JoinedStr):
            parts = []
            for v in e.values:
                r = self._eval(v.value if isinstance(v, ast.FormattedValue) else v, env)
                if not isinstance(r, str) or (isinstance(v, ast.FormattedValue) and (v.conversion != -1 or v.format_spec)):
                    return _UNK
                parts.append(r)
            return "".join(parts)
        return _UNK

    def _init_target(self, call, idx: int):
        """Index in the MRO whose __init__ this call runs, else None. Handles super().__init__(...)
        and Base.__init__(self, ...)."""
        f = call.func
        if not (isinstance(f, ast.Attribute) and f.attr == "__init__"):
            return None
        if isinstance(f.value, ast.Call) and isinstance(f.value.func, ast.Name) and f.value.func.id == "super":
            return idx + 1, call.args
        if isinstance(f.value, ast.Name) and call.args and isinstance(call.args[0], ast.Name) \
                and call.args[0].id == "self":
            for j in range(idx + 1, len(self._mro)):
                n = self._mro[j] if isinstance(self._mro[j], str) else self._mro[j].name
                if n == f.value.id:
                    return j, call.args[1:]
        return None

    def _call(self, idx: int, args: list, kwargs: dict):
        """Run the __init__ that self._mro[idx:] provides with these argument values; return the
        resulting self.name (or _UNK)."""
        for j in range(idx, len(self._mro)):
            if self._mro[j] == "BaseApp":    # BaseApp.__init__(self, name=None): name or class name
                v = args[0] if args else kwargs.get("name")
                return v if isinstance(v, str) and v else self._mro[0].name if v in (None, "") else _UNK
            init = next((s for s in self._mro[j].body if isinstance(s, ast.FunctionDef) and s.name == "__init__"), None)
            if init is not None:
                break
        else:
            return _UNK
        a = init.args
        if a.vararg or a.kwarg or a.posonlyargs or a.kwonlyargs:
            return _UNK
        params = [p.arg for p in a.args[1:]]
        defaults = dict(zip(params[len(params) - len(a.defaults):], a.defaults))
        env = {}
        for i, p in enumerate(params):
            if i < len(args):
                env[p] = args[i]
            elif p in kwargs:
                env[p] = kwargs[p]
            elif p in defaults:
                env[p] = self._eval(defaults[p], {})
            else:
                return _UNK
        name = _UNK
        for st in init.body:
            call = st.value if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) else None
            tgt = self._init_target(call, j) if call is not None else None
            if tgt is not None:
                k, cargs = tgt
                vals = [self._eval(x, env) for x in cargs]
                kw = {x.arg: self._eval(x.value, env) for x in call.keywords if x.arg}
                if len(kw) != len(call.keywords):
                    return _UNK
                name = self._call(k, vals, kw)
                if name is _UNK:
                    return _UNK
                continue
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Attribute) \
                    and isinstance(st.targets[0].value, ast.Name) and st.targets[0].value.id == "self":
                v = self._eval(st.value, env)
                self._inst[st.targets[0].attr] = v
                if st.targets[0].attr == "name":
                    name = v
                continue
            # anything else that might set the name (if/for/try blocks, setattr, helper calls with self)
            for n in ast.walk(st):
                if isinstance(n, ast.Attribute) and n.attr in ("name", "__init__") and isinstance(n.ctx, ast.Store):
                    return _UNK
                if isinstance(n, ast.Attribute) and n.attr == "__init__":
                    return _UNK
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "setattr":
                    return _UNK
        return name if isinstance(name, str) else _UNK


def app_names(src: str, classes: list[str]) -> list[tuple[str, str, bool]]:
    """Best guess of each class's runtime BaseApp .name (what /data/app_list and last_app.json store):
    [(class, name, sure)]. The __init__ chain is followed through base classes of the same file (so a
    base that computes self.name from a subclass attribute gives e.g. "Sat Talks"). sure=False when
    the name can't be worked out exactly; the guess is then the class name or a literal from its body
    (menu_positions() replaces it with the badge's real name when one matches)."""
    # Runs on the device worker: keep the garbage collector off while parsing, so a collection
    # doesn't run Tk objects' __del__ on this thread (that blocks on the Tk main thread).
    was_on = gc.isenabled()
    gc.disable()
    try:
        sim = _NameSim(ast.parse(src))
    except (SyntaxError, ValueError):
        return _regex_names(src, classes)
    finally:
        if was_on:
            gc.enable()
    fallback = dict((c, (n, s)) for c, n, s in _regex_names(src, classes))
    out = []
    for c in classes:
        n = sim.name(c) if c in sim.defs else _UNK
        if isinstance(n, str) and n:
            out.append((c, n, True))
        else:
            out.append((c, fallback[c][0], False))
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
    menu: str = ""      # "3 (orange 3)" / "9-10 (cyan 1-2)" / "≈3 (orange 3)" / "after restart" / ""
    names: list[tuple[str, str, bool]] = field(default_factory=list)   # app_names()


@dataclass
class BadgeState:
    apps: list[BadgeApp]
    hashes: dict[str, str]       # "/apps/x.py" / "/lib/y.py" -> sha256
    paths: set[str]              # every known badge path (apps, lib, data, root entries)
    menu_count: int = 0
    boot: dict | None = None     # read_boot_state()
    app_list: list[str] | None = None   # read_app_list(); None = menu order only estimated

    def app(self, path: str) -> BadgeApp | None:
        return next((a for a in self.apps if a.path == path), None)


LED_PAGES = ("orange", "cyan", "magenta")   # LED-bar colour per page of 8 menu slots
APP_LIST_PATH = "/data/app_list"
APP_LIST_CODE = (
    "try:\n"
    " import btree\n"
    " f=open('/data/app_list','r+b'); db=btree.open(f)\n"
    " print('SLOTS', [(k[0], db[k].decode()) for k in db]); db.close(); f.close()\n"
    "except Exception as e:\n"
    " print('SLOTS NONE', repr(e))\n")


def parse_app_list(out: str | None) -> list[str] | None:
    """exec_py output of APP_LIST_CODE -> app names by 0-based slot, or None (missing / unreadable)."""
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("SLOTS ") or line.startswith("SLOTS NONE"):
            continue
        try:
            pairs = ast.literal_eval(line[len("SLOTS "):])
            slots = {int(k): str(v) for k, v in pairs}
        except (ValueError, SyntaxError, TypeError):
            return None
        return [slots[k] for k in sorted(slots)] if slots else None
    return None


def read_app_list(badge) -> list[str] | None:
    """Blocking. The real LED-menu order the badge's app_switcher wrote at its last boot, or None."""
    try:
        return parse_app_list(badge.exec_py(APP_LIST_CODE, timeout=20))
    except Exception:  # noqa: BLE001  (old firmware / fake badge / read error -> estimate)
        return None


def led_label(slot: int) -> str:
    """1-based menu slot -> "9 (cyan 1)": LED number within its page and the page colour."""
    page, led = divmod(slot - 1, NUM_LEDS)
    colour = LED_PAGES[page] if page < len(LED_PAGES) else f"page {page + 1}"
    return f"{slot} ({colour} {led + 1})"


def _slots_label(slots: list[int], prefix: str = "") -> str:
    if not slots:
        return ""
    if len(slots) > 1 and slots == list(range(slots[0], slots[0] + len(slots))) \
            and (slots[0] - 1) // NUM_LEDS == (slots[-1] - 1) // NUM_LEDS:
        page, led = divmod(slots[0] - 1, NUM_LEDS)
        colour = LED_PAGES[page] if page < len(LED_PAGES) else f"page {page + 1}"
        return f"{prefix}{slots[0]}-{slots[-1]} ({colour} {led + 1}-{led + len(slots)})"
    return ", ".join(prefix + led_label(n) for n in slots)


def _menu_apps(apps: list[BadgeApp]) -> list[BadgeApp]:
    out = []
    for a in apps:
        a.menu = ""
        if not a.enabled or a.core or Path(a.path).stem in MENU_DENY:
            continue
        if not a.classes:
            a.menu = "none"
            continue
        out.append(a)
    return out


def menu_positions(apps: list[BadgeApp], app_list: list[str] | None = None) -> int:
    """Fill BadgeApp.menu for enabled apps. Returns the number of menu entries.

    app_list (read_app_list(): the names the switcher stored at the last boot, by slot) gives the real
    order; apps are matched by their runtime .name (app_names() guesses). Apps with no slot were added
    or enabled since that boot: "after restart". Without app_list the order is only estimated
    (alphabetical module order, then class order) and marked with "≈"."""
    menu = _menu_apps(apps)
    if app_list is None:
        pos = 0
        for a in sorted(menu, key=lambda a: Path(a.path).stem):
            n = len(a.classes)
            a.menu = _slots_label(list(range(pos + 1, pos + n + 1)), "≈")
            pos += n
        return pos
    free = {i: name for i, name in enumerate(app_list)}
    found: dict[tuple[str, int], int] = {}     # (path, class index) -> 1-based slot
    wanted = [(a, i, (a.names[i] if i < len(a.names) else (c, c, False)))
              for a in sorted(menu, key=lambda a: Path(a.path).stem) for i, c in enumerate(a.classes)]
    # pass 1: exact name; pass 2: computed names (e.g. "Sat Talks" for class Talks) by substring
    def norm(x: str) -> str:
        return re.sub(r"[\W_]", "", x).lower()
    for exact in (True, False):
        # most specific guesses first, so class Talks doesn't take "Sun Talks" from class SunTalks
        for a, i, (cls, name, sure) in sorted(wanted, key=lambda w: -len(w[2][1])) if not exact else wanted:
            if (a.path, i) in found or (not exact and sure):
                continue
            for k in sorted(free):
                v = free[k]
                if (v == name) if exact else (norm(name) in norm(v) or norm(cls) in norm(v)):
                    found[(a.path, i)] = k + 1
                    del free[k]
                    break
    # Uncertain guesses that matched a slot: use the badge's real name (still marked unsure, so
    # "Set as boot app" keeps its warning, but it offers the name the badge actually uses).
    for a, i, (cls, name, sure) in wanted:
        k = found.get((a.path, i))
        if k is not None and not sure and i < len(a.names) and app_list[k - 1] != name:
            a.names[i] = (cls, app_list[k - 1], False)
    for a in menu:
        slots = sorted(found[(a.path, i)] for i in range(len(a.classes)) if (a.path, i) in found)
        missing = len(a.classes) - len(slots)
        label = _slots_label(slots)
        if missing:
            label = (label + f"; +{missing} after restart") if label else "after restart"
        a.menu = label
    return len(app_list)


def scan_badge(badge, content_cache: dict[str, bytes], read_slots: bool = True) -> BadgeState:
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
            a.names = app_names(data.decode(errors="replace"), a.classes)
        apps.append(a)
    st = BadgeState(apps, hashes, paths)
    st.app_list = read_app_list(badge) if read_slots else None
    st.menu_count = menu_positions(apps, st.app_list)
    apps.sort(key=lambda a: (a.core, Path(a.path).stem.lstrip("_")))
    return st


def _read_json_file(badge, path: str) -> dict:
    try:
        if not badge.exists(path):
            return {}
        d = json.loads(badge.read_file(path).decode("utf-8"))
    except Exception:  # noqa: BLE001  (missing / invalid)
        return {}
    return d if isinstance(d, dict) else {}


def read_boot_state(badge) -> dict:
    """Blocking. {"supported": bool, "resume": bool, "last": {"module","app"} | None,
    "boot": boot.json dict}."""
    try:
        supported = RESUME_MARKER in badge.read_file(SWITCHER_PATH)
    except Exception:  # noqa: BLE001  (no such file / read error)
        supported = False
    boot = _read_json_file(badge, BOOT_PATH)
    last = _read_json_file(badge, LAST_APP_PATH)
    if not (isinstance(last.get("module"), str) and isinstance(last.get("app"), str)):
        last = None
    return {"supported": supported, "resume": boot.get("resume", True) is not False,
            "last": last, "boot": boot}


def write_boot_resume(badge, resume: bool) -> dict:
    """Read-modify-write /data/boot.json {"resume": bool}, other keys kept. Returns the new dict."""
    d = _read_json_file(badge, BOOT_PATH)
    d["resume"] = bool(resume)
    badge.write_bytes(json.dumps(d).encode(), BOOT_PATH)
    return d


def write_boot_and_reset(badge, write) -> dict:
    """Blocking: write() then read the boot state back, ALWAYS ending with a hard reset (every
    mpremote command pauses the badge UI until one). Returns the state read before the reset."""
    try:
        write()
        return read_boot_state(badge)
    finally:
        badge.reset()


def write_last_app(badge, module: str, app: str) -> dict:
    """Overwrite /data/last_app.json (the app the badge starts at the next boot if resume is on)."""
    d = {"module": module, "app": app}
    badge.write_bytes(json.dumps(d).encode(), LAST_APP_PATH)
    return d


def boot_status_text(boot: dict | None, state: "BadgeState | None" = None) -> str:
    if not boot:
        return "Start at boot: (badge not read)"
    if not boot.get("supported"):
        return "Badge firmware doesn't support resume yet (it always starts in the menu)."
    if not boot.get("resume"):
        return "Starts at boot: menu"
    last = boot.get("last")
    if not last:
        return "Starts at boot: last used app (none saved yet, so the menu)"
    txt = f"Starts at boot: last used app (currently {last['app']}, from {last['module']}.py)"
    if state is not None:
        a = next((x for x in state.apps if x.module == last["module"] and x.enabled and not x.core), None)
        if a is None:
            txt += "; that app is not enabled on the badge, so it will show the menu"
    return txt


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
    # Follows light/dark mode (re-styles itself when the appearance changes); defines the
    # "muted" / "warn" / "ok" row tags.
    theme.style_treeview(widget, "Apps.Treeview", rowheight=24)


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
        ctk.CTkButton(row, text="Cancel", fg_color=COLORS["neutral"], hover_color=COLORS["neutral_hover"], width=100,
                      command=self.destroy).pack(side="right", padx=4)
        ok = ctk.CTkButton(row, text="Install", width=120, fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"], command=self._ok)
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
        ctk.CTkButton(bar, text="Install from file…", width=140, fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                      command=self.install_from_file).pack(side="left", padx=4)
        self.apply_btn = ctk.CTkButton(bar, text="Apply & restart badge", width=170, fg_color=COLORS["neutral"], hover_color=COLORS["neutral_hover"],
                                       command=self.apply_restart)
        self.apply_btn.pack(side="right", padx=4)
        self.apply_note = ctk.CTkLabel(bar, text="New/enabled apps appear in the LED menu after a restart.",
                                       text_color=COLORS["muted_text"])
        self.apply_note.pack(side="right", padx=8)

        self._build_boot()

        panes = ctk.CTkFrame(self, fg_color="transparent")
        panes.pack(fill="both", expand=True)
        panes.grid_columnconfigure(0, weight=5, uniform="p")
        panes.grid_columnconfigure(1, weight=6, uniform="p")
        panes.grid_rowconfigure(0, weight=1)

        # ---- left: on the badge
        left = Section(panes, "On the badge")
        left.grid(row=0, column=0, sticky="nsew", padx=(4, 4))
        self.badge_info = ctk.CTkLabel(left.body, text="Not scanned yet", anchor="w", text_color=COLORS["muted_text"])
        self.badge_info.pack(fill="x")
        tf = tk.Frame(left.body)
        tf.pack(fill="both", expand=True, pady=4)
        self.btree = ttk.Treeview(tf, columns=("menu", "module", "title", "state", "size"), show="headings",
                                  selectmode="browse")
        for col, text, w, st in (("menu", "LED", 120, False), ("module", "Module", 120, False),
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
        self.remove_btn = ctk.CTkButton(row, text="Remove…", width=90, fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                                        command=self.remove_selected, state="disabled")
        self.remove_btn.pack(side="left", padx=4)
        self.bsrc_btn = ctk.CTkButton(row, text="Open source", width=100, command=self.open_badge_source,
                                      state="disabled")
        self.bsrc_btn.pack(side="left", padx=4)
        self.bootapp_menu = ctk.CTkOptionMenu(row, values=["-"], width=150)
        self.bootapp_btn = ctk.CTkButton(row, text="Set as boot app…", width=130, fg_color=COLORS["danger"],
                                         hover_color=COLORS["danger_hover"], command=self.set_boot_app,
                                         state="disabled")
        self.bootapp_btn.pack(side="right", padx=(4, 0))
        self.bootapp_menu.pack(side="right", padx=4)

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
        self.cat_label = ctk.CTkLabel(prow, text="Sources not scanned yet", text_color=COLORS["muted_text"], width=260,
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
        self.install_btn = ctk.CTkButton(irow, text="Install…", width=100, fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                                         command=self.install_selected,
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
        def job():
            st = scan_badge(badge, cache)
            try:
                st.boot = read_boot_state(badge)
            except Exception as e:  # noqa: BLE001  (never let this break the app list)
                self.ctx.log(f"Start at boot: could not read boot settings: {e}")
            return st
        self.ctx.run_device_task(job, self._badge_done, self._badge_failed, title="Reading badge apps")

    def _badge_done(self, state: BadgeState) -> None:
        self._badge_busy = False
        self.state = state
        self._fill_badge()
        self._show_boot(state.boot)
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
        self._show_boot(None)
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
        extra = (f"  (more than {NUM_LEDS} LEDs: pages orange/cyan/magenta)"
                 if st.menu_count > NUM_LEDS else "")
        order = ("order from the badge's last boot" if st.app_list is not None
                 else "≈ estimated order (badge has no /data/app_list)")
        pending = any("after restart" in a.menu for a in st.apps)
        self.badge_info.configure(
            text=f"{n_en} enabled, {n_dis} disabled · {st.menu_count} menu entries{extra} · {order}"
                 + ("; restart to give new apps a slot" if pending else ""))
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
        self._update_bootapp(a)
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

    # ================================================================ start at boot
    def _build_boot(self) -> None:
        sec = Section(self, "Start at boot")
        sec.pack(fill="x", padx=4, pady=(0, 6))
        row = ctk.CTkFrame(sec.body, fg_color="transparent")
        row.pack(fill="x")
        self.boot_label = ctk.CTkLabel(row, text="Start at boot: (badge not read)", anchor="w")
        self.boot_label.pack(side="left")
        self.boot_save_btn = ctk.CTkButton(row, text="Save boot setting", width=150, fg_color=COLORS["danger"],
                                           hover_color=COLORS["danger_hover"], command=self.save_boot_resume,
                                           state="disabled")
        self.boot_save_btn.pack(side="right", padx=4)
        self.boot_var = ctk.BooleanVar(value=True)
        self.boot_switch = ctk.CTkSwitch(row, text="Resume last app at boot", variable=self.boot_var,
                                         command=self._boot_switch_changed, state="disabled")
        self.boot_switch.pack(side="right", padx=8)
        self.boot_note = ctk.CTkLabel(sec.body, text="", anchor="w", justify="left", text_color=COLORS["muted_text"])
        self.boot_note.pack(fill="x")
        self._boot: dict | None = None

    def _show_boot(self, boot: dict | None) -> None:
        self._boot = boot
        self.boot_label.configure(text=boot_status_text(boot, self.state))
        ok = bool(boot and boot.get("supported"))
        self.boot_switch.configure(state="normal" if ok else "disabled")
        if boot:
            self.boot_var.set(bool(boot.get("resume", True)))
        if ok:
            note = ("Hold Menu+ or Menu\u2212 while the badge boots to get the menu anyway. Saving restarts "
                    "the badge (hard reset) so the setting applies.")
        elif boot:
            note = (f"Needs the resume-last-app firmware change ({SWITCHER_PATH} that saves "
                    f"{LAST_APP_PATH}); see the Firmware tab.")
        else:
            note = ""
        self.boot_note.configure(text=note)
        self._boot_switch_changed()
        self._update_bootapp(self._selected_badge_app())

    def _boot_switch_changed(self) -> None:
        b = self._boot
        differs = bool(b and b.get("supported") and bool(self.boot_var.get()) != bool(b.get("resume", True)))
        self.boot_save_btn.configure(state="normal" if differs else "disabled")

    def save_boot_resume(self) -> None:
        b = self._boot
        if not (b and b.get("supported")):
            return
        resume = bool(self.boot_var.get())
        if not messagebox.askyesno(
                "Start at boot",
                f"Overwrite {BOOT_PATH} on the badge with \"resume\": {str(resume).lower()} (other keys kept)?\n\n"
                + ("The badge will start the last app you used." if resume else
                   "The badge will always start in the menu.")
                + "\n\nThe badge will restart (hard reset) to apply it.", parent=self):
            self.boot_var.set(bool(b.get("resume", True)))
            self._boot_switch_changed()
            return
        badge = self.ctx.badge

        def job():
            return write_boot_and_reset(badge, lambda: write_boot_resume(badge, resume))

        def done(boot):
            if self.state is not None:
                self.state.boot = boot
            self._show_boot(boot)
            self._restarted()
            msg = f"Saved {BOOT_PATH}: resume {'on' if resume else 'off'}; badge restarting"
            self.ctx.status(msg)
            self.ctx.log(msg)
        self.ctx.run_device_task(job, done, lambda e: messagebox.showerror("Start at boot", str(e), parent=self),
                                 title="Saving boot setting")

    def _update_bootapp(self, a) -> None:
        if not hasattr(self, "bootapp_btn"):
            return
        names = [n for _c, n, _sure in (a.names if a else [])]
        usable = bool(a and not a.core and a.enabled and names and self._boot and self._boot.get("supported"))
        cur = self.bootapp_menu.get()
        self.bootapp_menu.configure(values=names or ["-"])
        self.bootapp_menu.set(cur if cur in names else (names[0] if names else "-"))
        self.bootapp_menu.configure(state="normal" if usable and len(names) > 1 else "disabled")
        self.bootapp_btn.configure(state="normal" if usable else "disabled")

    def set_boot_app(self) -> None:
        a = self._selected_badge_app()
        b = self._boot
        if not a or a.core or not a.enabled or not (b and b.get("supported")) or not a.names:
            return
        name = self.bootapp_menu.get()
        entry = next((x for x in a.names if x[1] == name), a.names[0])
        name, sure = entry[1], entry[2]
        msg = (f"Overwrite {LAST_APP_PATH} on the badge with\n  module: {a.module}\n  app: {name}\n\n"
               f"The badge will restart (hard reset) and then starts {name}. Launching another app "
               "from the menu replaces it again.")
        if not sure:
            msg += ("\n\nNote: this app sets its name at runtime; if \"" + name + "\" isn't its real name the "
                    "badge simply shows the menu.")
        if not b.get("resume", True):
            msg += "\n\n\"Resume last app at boot\" is OFF, so this has no effect until you switch it on."
        if not messagebox.askyesno("Set as boot app", msg + "\n\nContinue?", parent=self):
            return
        badge, module = self.ctx.badge, a.module

        def job():
            return write_boot_and_reset(badge, lambda: write_last_app(badge, module, name))

        def done(boot):
            if self.state is not None:
                self.state.boot = boot
            self._show_boot(boot)
            self._restarted()
            msg = f"Boot app set to {name} ({module}.py); badge restarting"
            self.ctx.status(msg)
            self.ctx.log(msg)
        self.ctx.run_device_task(job, done, lambda e: messagebox.showerror("Set as boot app", str(e), parent=self),
                                 title="Setting boot app")

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
        self.apply_btn.configure(fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"])
        self.apply_note.configure(text="Changes pending: restart the badge to update the LED menu.",
                                  text_color=COLORS["warning_text"])
        self.ctx.status(msg)
        self.ctx.log(msg)
        self.ctx.events.publish("badge.files_changed", paths)

    def _restarted(self) -> None:
        """After any hard reset: pending app changes are applied by the reboot."""
        self._dirty = False
        self.apply_btn.configure(fg_color=COLORS["neutral"], hover_color=COLORS["neutral_hover"])
        self.apply_note.configure(text="Badge restarted; new apps are in the LED menu once it boots.",
                                  text_color=COLORS["muted_text"])

    def apply_restart(self) -> None:
        def done(_):
            self._restarted()
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

