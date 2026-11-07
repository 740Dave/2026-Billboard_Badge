"""Artwork tab: manage the images shown by the badge's Artwork app (/data/artwork/*.png).

Left:  thumbnails of what's on the badge (cached locally), with delete / reorder / show next / save copy.
Right: pick images, tweak per-image conversion options with a live 3-colour preview, convert & upload.
Bottom: count / size vs free space, whether apps/artwork.py is installed, restart button.

All badge I/O runs on the device worker (ctx.run_device_task); conversion on ctx.run_task.
"""

from __future__ import annotations

import io
import re
from pathlib import Path
from tkinter import filedialog
import tkinter as tk

import customtkinter as ctk
from PIL import Image

from core import artwork_convert as ac
from core import theme
from core.theme import COLORS
from tabs.base import BaseTab, Section, confirm, info, human_size

ART_DIR = "/data/artwork"
GALLERY_DIR = "/data/gallery"
INDEX_FILE = "/data/artwork_index"
BADGE_APP = "/apps/artwork.py"
LOCAL_APP = Path(__file__).resolve().parents[2] / "artwork_app" / "badge" / "apps" / "artwork.py"

THUMB = (176, 123)          # 960x672 scaled
PREVIEW = (336, 235)
GRID_COLS = 3
_PREFIX = re.compile(r"^(\d{3})_")
_TMP = re.compile(r"^\.mv\d+_")      # temporary names used by apply_order


# ------------------------------------------------------------------ pure helpers (testable)
def art_names(entries) -> list[str]:
    """Same filter/order as the badge app's _scan()."""
    names = [Path(e.path).name for e in entries if not e.is_dir]
    return sorted(n for n in names if n.lower().endswith(".png") and not n.startswith("."))


def leftover_names(entries) -> list[str]:
    """Temp ".mvN_*.png" names left behind by an interrupted reorder (hidden from the badge app)."""
    names = [Path(e.path).name for e in entries if not e.is_dir]
    return sorted(n for n in names if _TMP.match(n) and n.lower().endswith(".png"))


def strip_tmp(name: str) -> str:
    return _TMP.sub("", name)


def strip_prefix(name: str) -> str:
    return _PREFIX.sub("", name)


def ordered_names(names: list[str]) -> list[str]:
    """Give every file a NNN_ prefix (010, 020, ...) in the given order."""
    return [f"{(i + 1) * 10:03d}_{strip_prefix(n)}" for i, n in enumerate(names)]


def live_order(live: list[str], intended: list[str]) -> list[str]:
    """The user's intended order restricted to names that still exist, then any new names."""
    present = set(live)
    out = [n for n in intended if n in present]
    seen = set(out)
    return out + [n for n in live if n not in seen]


def _unique_tmp(taken: set[str], i: int, old: str) -> str:
    while f".mv{i}_{old}" in taken:
        i += 1000
    return f".mv{i}_{old}"


def apply_order(badge, folder: str, new_order: list[str]) -> dict[str, str]:
    """Rename files on the badge so they sort as new_order. Returns {old: new} for changed names.

    Must run on the device worker. Re-lists the folder first, so a stale UI snapshot can't
    rename the wrong files (names that vanished are skipped, new ones go at the end).
    Two passes via dot-names (ignored by the Artwork app) so targets never collide with sources.
    On any failure every file is renamed back to its original name, then the error is re-raised.
    """
    entries = badge.listdir(folder)
    live = art_names(entries)
    taken = {Path(e.path).name for e in entries}
    order = live_order(live, new_order)
    moves = {old: new for old, new in zip(order, ordered_names(order)) if old != new}
    tmp_of: dict[str, str] = {}        # original -> temp name (file currently at temp)
    placed: dict[str, str] = {}        # original -> final name (file currently at final)
    try:
        for i, old in enumerate(moves):
            tmp = _unique_tmp(taken, i, old)
            taken.add(tmp)
            badge.rename(f"{folder}/{old}", f"{folder}/{tmp}")
            tmp_of[old] = tmp
        for old, new in moves.items():
            badge.rename(f"{folder}/{tmp_of[old]}", f"{folder}/{new}")
            placed[old] = new
    except Exception:
        _rollback(badge, folder, tmp_of, placed)
        raise
    return moves


def _rollback(badge, folder: str, tmp_of: dict[str, str], placed: dict[str, str]) -> None:
    """Undo a partial apply_order: finals -> temp (frees the original names), then temp -> original."""
    log = getattr(badge, "log", print)
    for old, new in placed.items():
        try:
            badge.rename(f"{folder}/{new}", f"{folder}/{tmp_of[old]}")
        except Exception as e:  # noqa: BLE001
            log(f"artwork rollback: could not move {new} back: {e}")
    for old, tmp in tmp_of.items():
        try:
            badge.rename(f"{folder}/{tmp}", f"{folder}/{old}")
        except Exception as e:  # noqa: BLE001
            log(f"artwork rollback: could not restore {old} (left as {tmp}; use 'Repair names'): {e}")


def repair_leftovers(badge, folder: str) -> dict[str, str]:
    """Rename every ".mvN_x.png" back to "x.png" (or "x_2.png"... if taken). Device worker only."""
    entries = badge.listdir(folder)
    taken = {Path(e.path).name.lower() for e in entries}
    fixed = {}
    for tmp in leftover_names(entries):
        name = strip_tmp(tmp)
        root, k = name[:-4], 2
        while name.lower() in taken:
            name = f"{root}_{k}.png"
            k += 1
        badge.rename(f"{folder}/{tmp}", f"{folder}/{name}")
        taken.add(name.lower())
        fixed[tmp] = name
    return fixed


def next_upload_name(existing: list[str], stem: str) -> str:
    """Safe name for a new upload; sorts after the existing files if they use NNN_ prefixes."""
    base = ac.safe_name(stem) + ".png"
    taken = {n.lower() for n in existing}
    prefixes = [int(m.group(1)) for n in existing if (m := _PREFIX.match(n))]
    if prefixes:
        nxt = max(prefixes) + 10
        name = f"{min(nxt, 999):03d}_{base}"
    else:
        name = base
    k = 2
    root = name[:-4]
    while name.lower() in taken:
        name = f"{root}_{k}.png"
        k += 1
    return name


def _thumb(data: bytes, size=THUMB) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    img.thumbnail(size, Image.NEAREST if img.width <= 960 else Image.LANCZOS)
    return img


class ArtworkTab(BaseTab):
    TITLE = "Artwork"

    # ================================================================== build
    def build(self) -> None:
        self.folder = ART_DIR
        self.badge_files: list[tuple[str, int]] = []   # (name, size) in sorted order
        self.leftovers: list[tuple[str, int]] = []     # (".mvN_name", size) from an interrupted reorder
        self.thumb_imgs: dict[str, ctk.CTkImage] = {}  # keep references alive
        self.thumb_btns: dict[str, ctk.CTkButton] = {}
        self.selected: str | None = None
        self.sources: list[Path] = []
        self.src_opts: dict[Path, ac.ConvertOptions] = {}
        self.cur_src: Path | None = None
        self.preview_refs: list[ctk.CTkImage] = []
        self._preview_job = None
        self._preview_seq = 0
        self._refreshing = False
        self._refresh_again = False
        self._loaded = False
        self._fs_free = None
        self._app_installed = None
        self._busy_upload = False

        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(0, weight=1)

        # ---------------------------------------------------------- left side
        left = Section(self, "On the badge")
        left.grid(row=0, column=0, sticky="nsew", padx=(10, 5), pady=10)
        top = ctk.CTkFrame(left.body, fg_color="transparent")
        top.pack(fill="x")
        self.folder_seg = ctk.CTkSegmentedButton(
            top, values=["Artwork", "Gallery (read-only)"], command=self._folder_changed)
        self.folder_seg.set("Artwork")
        self.folder_seg.pack(side="left")
        ctk.CTkButton(top, text="Refresh", width=80, command=self.refresh).pack(side="right")

        self.grid_frame = ctk.CTkScrollableFrame(left.body)
        self.grid_frame.pack(fill="both", expand=True, pady=6)
        self.empty_label = ctk.CTkLabel(self.grid_frame, text="Not loaded yet.", text_color=COLORS["muted_text"])
        self.empty_label.grid(row=0, column=0, padx=10, pady=10)

        self.repair_bar = ctk.CTkFrame(left.body, fg_color=COLORS["danger_bg"])
        self.repair_label = ctk.CTkLabel(self.repair_bar, text="", anchor="w", justify="left")
        self.repair_label.pack(side="left", padx=8, pady=4, fill="x", expand=True)
        self.repair_btn = ctk.CTkButton(self.repair_bar, text="Repair names", width=110, command=self._repair)
        self.repair_btn.pack(side="right", padx=6, pady=4)

        self.sel_label = ctk.CTkLabel(left.body, text="Nothing selected", anchor="w")
        self.sel_label.pack(fill="x")
        acts = ctk.CTkFrame(left.body, fg_color="transparent")
        acts.pack(fill="x", pady=(4, 0))
        self.btn_up = ctk.CTkButton(acts, text="Move up", width=80, command=lambda: self._move(-1))
        self.btn_down = ctk.CTkButton(acts, text="Move down", width=90, command=lambda: self._move(+1))
        self.btn_next = ctk.CTkButton(acts, text="Show this next", width=110, command=self._show_next)
        self.btn_save = ctk.CTkButton(acts, text="Save copy…", width=90, command=self._save_copy)
        self.btn_del = ctk.CTkButton(acts, text="Delete", width=70, fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                                     command=self._delete)
        for b in (self.btn_up, self.btn_down, self.btn_next, self.btn_save, self.btn_del):
            b.pack(side="left", padx=(0, 6))
        self._update_actions()

        # --------------------------------------------------------- right side
        right = Section(self, "Add artwork")
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)
        rb = right.body
        pick = ctk.CTkFrame(rb, fg_color="transparent")
        pick.pack(fill="x")
        ctk.CTkButton(pick, text="Choose images…", command=self._choose_files).pack(side="left")
        ctk.CTkButton(pick, text="Add folder…", width=100, command=self._choose_folder).pack(side="left", padx=6)
        ctk.CTkButton(pick, text="Remove", width=70, command=self._remove_source).pack(
            side="left")

        self.src_list = tk.Listbox(rb, height=5, exportselection=False, activestyle="none")
        theme.style_listbox(self.src_list)
        self.src_list.pack(fill="x", pady=6)
        self.src_list.bind("<<ListboxSelect>>", self._source_selected)

        opts = ctk.CTkFrame(rb, fg_color="transparent")
        opts.pack(fill="x")
        self.mode_seg = ctk.CTkSegmentedButton(opts, values=["fit", "fill"], command=lambda _v: self._opts_changed())
        self.mode_seg.set("fit")
        self.mode_seg.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        self.rot_seg = ctk.CTkSegmentedButton(opts, values=["0°", "90°", "180°", "270°"],
                                              command=lambda _v: self._opts_changed())
        self.rot_seg.set("0°")
        self.rot_seg.grid(row=0, column=1, sticky="w", pady=2)
        self.dither_var = tk.BooleanVar(value=True)
        self.red_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(opts, text="Dither", variable=self.dither_var, command=self._opts_changed).grid(
            row=1, column=0, sticky="w", pady=2)
        ctk.CTkCheckBox(opts, text="Use red", variable=self.red_var, command=self._opts_changed).grid(
            row=1, column=1, sticky="w", pady=2)
        ctk.CTkButton(opts, text="Apply options to all", width=140,
                      command=self._apply_all).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)

        prev = ctk.CTkFrame(rb, fg_color="transparent")
        prev.pack(fill="x", pady=6)
        ctk.CTkLabel(prev, text="Original").grid(row=0, column=0)
        ctk.CTkLabel(prev, text="On the badge").grid(row=0, column=1)
        self.prev_orig = ctk.CTkLabel(prev, text="", width=PREVIEW[0] // 2, height=PREVIEW[1] // 2)
        self.prev_res = ctk.CTkLabel(prev, text="", width=PREVIEW[0] // 2, height=PREVIEW[1] // 2)
        self.prev_orig.grid(row=1, column=0, padx=2)
        self.prev_res.grid(row=1, column=1, padx=2)
        self.prev_info = ctk.CTkLabel(rb, text="Choose an image to preview it.", anchor="w", justify="left")
        self.prev_info.pack(fill="x")

        self.upload_btn = ctk.CTkButton(rb, text="Convert & upload", command=self._upload)
        self.upload_btn.pack(fill="x", pady=(8, 2))
        self.progress = ctk.CTkProgressBar(rb)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=2)
        self.progress_label = ctk.CTkLabel(rb, text="", anchor="w")
        self.progress_label.pack(fill="x")

        # ------------------------------------------------------------ bottom
        bottom = ctk.CTkFrame(self)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        self.status_label = ctk.CTkLabel(bottom, text="Badge: not read yet", anchor="w")
        self.status_label.pack(side="left", padx=10, pady=6)
        self.restart_btn = ctk.CTkButton(bottom, text="Restart badge", width=110,
                                         command=self._restart)
        self.restart_btn.pack(side="right", padx=6)
        self.install_btn = ctk.CTkButton(bottom, text="Install Artwork app", width=140, command=self._install_app)
        self.app_label = ctk.CTkLabel(bottom, text="", anchor="e")
        self.app_label.pack(side="right", padx=6)

        self.ctx.events.subscribe("badge.connected", lambda _p: self.refresh())
        self.ctx.events.subscribe("badge.files_changed", self._files_changed)

    # ============================================================ lifecycle
    def on_show(self) -> None:
        if not self._loaded:
            self.refresh()

    def _files_changed(self, paths) -> None:
        paths = paths if isinstance(paths, (list, tuple)) else [paths]
        if not paths or any(isinstance(p, str) and (p.startswith("/data") or p.startswith("/apps"))
                            for p in paths):
            self.refresh()

    @property
    def read_only(self) -> bool:
        return self.folder != ART_DIR

    def _folder_changed(self, value) -> None:
        self.folder = GALLERY_DIR if value.startswith("Gallery") else ART_DIR
        self.selected = None
        self.badge_files = []
        self.leftovers = []
        self._render_grid()
        self.refresh()

    def _cache_dir(self, folder: str | None = None) -> Path:
        d = Path(self.ctx.config.get("artwork_cache")) / (folder or self.folder).strip("/").replace("/", "_")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cache_path(self, name: str, size: int, folder: str | None = None) -> Path:
        return self._cache_dir(folder) / f"{size}__{name}"

    # ============================================================== refresh
    def refresh(self) -> None:
        if self._refreshing:
            self._refresh_again = True
            return
        self._refreshing = True
        self._loaded = True
        folder = self.folder
        badge = self.ctx.badge

        def work():
            entries = badge.listdir(folder)
            sizes = {Path(e.path).name: e.size for e in entries if not e.is_dir}
            names = art_names(entries)
            left = leftover_names(entries)
            try:
                inf = badge.info()
            except Exception:  # noqa: BLE001
                inf = {}
            installed = badge.exists(BADGE_APP)
            return (folder, [(n, sizes.get(n, 0)) for n in names], [(n, sizes.get(n, 0)) for n in left],
                    inf, installed)

        def done(res):
            self._refreshing = False
            folder_, files, left, inf, installed = res
            if folder_ != self.folder:
                self._again()
                return
            self.badge_files = files
            self.leftovers = left
            self._fs_free = inf.get("fs_free")
            self._app_installed = installed
            if self.selected not in {n for n, _ in files}:
                self.selected = None
            self._render_grid()
            self._update_status()
            self._load_thumbs()
            self._again()

        def failed(_e):
            self._refreshing = False
            self.empty_label.configure(text="Could not read the badge (see log).")
            self._again()

        self.ctx.run_device_task(work, done, failed, title="Reading artwork")

    def _again(self) -> None:
        if self._refresh_again:
            self._refresh_again = False
            self.refresh()

    def _load_thumbs(self) -> None:
        """Download (or take from cache) every image, then build thumbnails as they arrive."""
        folder = self.folder
        todo = []
        for name, size in self.badge_files + self.leftovers:
            key = f"{folder}/{name}:{size}"
            if key in self.thumb_imgs:
                continue
            cp = self._cache_path(name, size, folder)
            if cp.exists():
                self._set_thumb(folder, name, size, _thumb(cp.read_bytes()))
            else:
                todo.append((name, size, cp))
        if not todo:
            return
        badge = self.ctx.badge

        def work():
            for i, (name, size, cp) in enumerate(todo):
                data = badge.read_file(f"{folder}/{name}")
                cp.write_bytes(data)
                self.ctx.ui(self._set_thumb, folder, name, size, _thumb(data))
                self.ctx.status(f"Thumbnails {i + 1}/{len(todo)}")
            return len(todo)

        self.ctx.run_device_task(work, lambda n: self.ctx.status(f"Downloaded {n} image(s)"),
                                 title="Downloading thumbnails")

    def _set_thumb(self, folder, name, size, pil) -> None:
        if pil is None:
            return
        self.thumb_imgs[f"{folder}/{name}:{size}"] = ctk.CTkImage(light_image=pil, dark_image=pil, size=pil.size)
        if folder == self.folder and name in self.thumb_btns:
            self.thumb_btns[name].configure(image=self.thumb_imgs[f"{folder}/{name}:{size}"])

    # ================================================================= grid
    def _render_grid(self) -> None:
        for b in self.thumb_btns.values():
            b.destroy()
        self.thumb_btns.clear()
        if not self.badge_files and not self.leftovers:
            self.empty_label.configure(
                text=f"No .png files in {self.folder}." if self._loaded else "Not loaded yet.")
            self.empty_label.grid(row=0, column=0, padx=10, pady=10)
        else:
            self.empty_label.grid_forget()
        for i, (name, size) in enumerate(self.badge_files):
            img = self.thumb_imgs.get(f"{self.folder}/{name}:{size}")
            label = f"{i + 1}. {name if len(name) <= 24 else name[:21] + '…'}\n{human_size(size)}"
            b = ctk.CTkButton(self.grid_frame, text=label, image=img, compound="top",
                              width=THUMB[0] + 8, height=THUMB[1] + 40, fg_color=COLORS["tile"],
                              hover_color=COLORS["tile_hover"], border_width=3, border_color=COLORS["tile"],
                              text_color=COLORS["text"],
                              command=lambda n=name: self._select(n))
            b.grid(row=i // GRID_COLS, column=i % GRID_COLS, padx=4, pady=4)
            self.thumb_btns[name] = b
        n0 = len(self.badge_files)
        for k, (name, size) in enumerate(self.leftovers):
            i, shown = n0 + k, strip_tmp(name)
            img = self.thumb_imgs.get(f"{self.folder}/{name}:{size}")
            label = f"hidden: {shown if len(shown) <= 20 else shown[:17] + '…'}\n{human_size(size)}"
            b = ctk.CTkButton(self.grid_frame, text=label, image=img, compound="top", state="disabled",
                              width=THUMB[0] + 8, height=THUMB[1] + 40, fg_color=COLORS["danger_bg"],
                              border_width=3, border_color=COLORS["danger"],
                              text_color_disabled=COLORS["muted_text"])
            b.grid(row=i // GRID_COLS, column=i % GRID_COLS, padx=4, pady=4)
            self.thumb_btns[name] = b
        if self.leftovers:
            n = len(self.leftovers)
            msg = (f"{n} file(s) were left with temporary names by an interrupted reorder; "
                   "the badge's Artwork app hides them.")
            self.repair_label.configure(text=msg if not self.read_only else msg + " (Switch to Artwork to repair.)")
            self.repair_btn.configure(state="disabled" if self.read_only else "normal")
            self.repair_bar.pack(fill="x", pady=(0, 4), before=self.sel_label)
        else:
            self.repair_bar.pack_forget()
        self._highlight()
        self._update_actions()

    def _select(self, name: str) -> None:
        self.selected = name
        self._highlight()
        self._update_actions()

    def _highlight(self) -> None:
        for n, b in self.thumb_btns.items():
            b.configure(border_color=COLORS["accent_text"] if n == self.selected else COLORS["tile"])
        if self.selected:
            idx = self._names().index(self.selected)
            size = dict(self.badge_files)[self.selected]
            self.sel_label.configure(text=f"Selected: {self.folder}/{self.selected}  "
                                          f"(#{idx + 1} of {len(self.badge_files)}, {human_size(size)})")
        else:
            self.sel_label.configure(text="Nothing selected")

    def _names(self) -> list[str]:
        return [n for n, _ in self.badge_files]

    def _update_actions(self) -> None:
        sel = self.selected is not None
        rw = sel and not self.read_only
        idx = self._names().index(self.selected) if sel else -1
        for b, on in ((self.btn_up, rw and idx > 0), (self.btn_down, rw and 0 <= idx < len(self.badge_files) - 1),
                      (self.btn_next, rw), (self.btn_del, rw), (self.btn_save, sel)):
            b.configure(state="normal" if on else "disabled")

    def _update_status(self) -> None:
        total = sum(s for _, s in self.badge_files)
        where = "artwork" if not self.read_only else "gallery"
        self.status_label.configure(
            text=f"{len(self.badge_files)} {where} image(s), {human_size(total)} on badge  ·  "
                 f"free space {human_size(self._fs_free)}")
        if self._app_installed:
            self.app_label.configure(text="Artwork app installed ✓", text_color=COLORS["success_text"])
            self.install_btn.pack_forget()
        else:
            self.app_label.configure(text="apps/artwork.py not on badge", text_color=COLORS["danger_text"])
            self.install_btn.pack(side="right", padx=6, before=self.app_label)

    # ============================================================== actions
    def _changed(self, paths: list[str], note: str) -> None:
        self.ctx.status(note)
        self.ctx.log(note)
        self.ctx.events.publish("badge.files_changed", paths)  # triggers our refresh too

    def _move(self, delta: int) -> None:
        if not self.selected or self.read_only:
            return
        names = self._names()
        i = names.index(self.selected)
        j = i + delta
        if not 0 <= j < len(names):
            return
        new = names[:]
        new[i], new[j] = new[j], new[i]
        folder, badge = self.folder, self.ctx.badge
        sizes = dict(self.badge_files)

        def work():
            # apply_order re-lists the folder itself; `new` is only the intended order
            return apply_order(badge, folder, new)

        def done(moves):
            # keep thumbnails/cache under the new names so nothing is re-downloaded
            for old, nw in moves.items():
                s = sizes.get(old)
                if s is None:
                    continue
                cp = self._cache_path(old, s, folder)
                if cp.exists():
                    cp.replace(self._cache_path(nw, s, folder))
                img = self.thumb_imgs.pop(f"{folder}/{old}:{s}", None)
                if img:
                    self.thumb_imgs[f"{folder}/{nw}:{s}"] = img
            self.selected = moves.get(self.selected, self.selected)
            self._changed([f"{folder}/{n}" for n in moves.values()], f"Reordered {len(moves)} file(s)")

        def failed(_e):
            # apply_order already rolled back; re-read so the grid matches the badge
            self.refresh()

        self.ctx.run_device_task(work, done, failed, title="Reordering artwork")

    def _repair(self) -> None:
        if self.read_only or not self.leftovers:
            return
        folder, badge = self.folder, self.ctx.badge

        def done(fixed):
            for tmp, name in fixed.items():
                self.ctx.log(f"Repaired {folder}/{tmp} -> {name}")
            self._changed([f"{folder}/{n}" for n in fixed.values()], f"Repaired {len(fixed)} file name(s)")

        self.ctx.run_device_task(lambda: repair_leftovers(badge, folder), done,
                                 lambda _e: self.refresh(), title="Repairing artwork names")

    def _show_next(self) -> None:
        if not self.selected or self.read_only:
            return
        idx = self._names().index(self.selected)
        badge = self.ctx.badge

        def done(_r):
            self._changed([INDEX_FILE], f"Artwork index set to {idx} ({self.selected})")
            self._offer_restart("The Artwork app reads its position when the badge starts. "
                                "Restart the badge now so it opens on this image?")

        self.ctx.run_device_task(lambda: badge.write_bytes(str(idx).encode(), INDEX_FILE), done,
                                 title="Setting next image")

    def _save_copy(self) -> None:
        if not self.selected:
            return
        name, size, folder = self.selected, dict(self.badge_files)[self.selected], self.folder
        dest = filedialog.asksaveasfilename(parent=self, initialfile=name, defaultextension=".png",
                                            filetypes=[("PNG image", "*.png")])
        if not dest:
            return
        cp = self._cache_path(name, size, folder)
        if cp.exists():
            Path(dest).write_bytes(cp.read_bytes())
            self.ctx.status(f"Saved {dest}")
            return
        badge = self.ctx.badge

        def work():
            data = badge.read_file(f"{folder}/{name}")
            Path(dest).write_bytes(data)
            return dest
        self.ctx.run_device_task(work, lambda d: self.ctx.status(f"Saved {d}"), title="Saving copy")

    def _delete(self) -> None:
        if not self.selected or self.read_only:
            return
        path = f"{ART_DIR}/{self.selected}"
        if not confirm(self, "Delete artwork", f"Delete {path} from the badge?\nThis cannot be undone."):
            return
        badge = self.ctx.badge
        self.ctx.run_device_task(lambda: badge.remove(path),
                                 lambda _r: self._changed([path], f"Deleted {path}"), title="Deleting artwork")
        self.selected = None

    def _install_app(self) -> None:
        if not LOCAL_APP.exists():
            info(self, "Artwork app", f"Can't find {LOCAL_APP}")
            return
        badge = self.ctx.badge

        def done(_r):
            self._changed([BADGE_APP], "Installed /apps/artwork.py")
            self._offer_restart("Artwork app installed. Restart the badge so it shows up in the menu?")
        self.ctx.run_device_task(lambda: badge.write_file(LOCAL_APP, BADGE_APP), done, title="Installing Artwork app")

    def _offer_restart(self, msg: str) -> None:
        if confirm(self, "Restart badge", msg + "\n\n(Hard reset; the panel takes ~30 s to redraw.)"):
            self._restart(ask=False)

    def _restart(self, ask: bool = True) -> None:
        if ask and not confirm(self, "Restart badge", "Hard-reset the badge now?"):
            return
        self.ctx.run_device_task(self.ctx.badge.reset, lambda _r: self.ctx.status("Badge restarted"),
                                 title="Restarting badge")

    # =========================================================== sources
    def _add_sources(self, paths) -> None:
        added = 0
        for p in paths:
            p = Path(p)
            if p.is_dir():
                self._add_sources(ac.list_images(p))
                continue
            if p not in self.src_opts:
                self.sources.append(p)
                self.src_opts[p] = self._current_opts()
                self.src_list.insert("end", p.name)
                added += 1
        if added and self.cur_src is None:
            self.src_list.selection_set(0)
            self._source_selected()
        self._update_upload_label()

    def _choose_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self, title="Choose images",
            filetypes=[("Images", " ".join(f"*{e}" for e in sorted(ac.IMAGE_EXTS))), ("All files", "*")])
        if paths:
            self._add_sources(paths)

    def _choose_folder(self) -> None:
        d = filedialog.askdirectory(parent=self, title="Folder of images")
        if d:
            self._add_sources([d])

    def _remove_source(self) -> None:
        sel = self.src_list.curselection()
        if not sel:
            return
        i = sel[0]
        p = self.sources.pop(i)
        self.src_opts.pop(p, None)
        self.src_list.delete(i)
        self.cur_src = None
        if self.sources:
            self.src_list.selection_set(min(i, len(self.sources) - 1))
            self._source_selected()
        else:
            self.prev_orig.configure(image=None)
            self.prev_res.configure(image=None)
            self.preview_refs.clear()
            self.prev_info.configure(text="Choose an image to preview it.")
        self._update_upload_label()

    def _source_selected(self, _e=None) -> None:
        sel = self.src_list.curselection()
        if not sel:
            return
        self.cur_src = self.sources[sel[0]]
        o = self.src_opts[self.cur_src]
        self.mode_seg.set(o.mode)
        self.rot_seg.set(f"{o.rotate}°")
        self.dither_var.set(o.dither)
        self.red_var.set(o.red)
        self._schedule_preview()

    def _current_opts(self) -> ac.ConvertOptions:
        return ac.ConvertOptions(mode=self.mode_seg.get() or "fit", dither=bool(self.dither_var.get()),
                                 red=bool(self.red_var.get()), rotate=int((self.rot_seg.get() or "0°").rstrip("°")))

    def _opts_changed(self) -> None:
        if self.cur_src is not None:
            self.src_opts[self.cur_src] = self._current_opts()
            self._schedule_preview()

    def _apply_all(self) -> None:
        o = self._current_opts()
        for p in self.sources:
            self.src_opts[p] = o.copy()
        self.ctx.status(f"Options applied to {len(self.sources)} image(s)")

    def _update_upload_label(self) -> None:
        n = len(self.sources)
        self.upload_btn.configure(text=f"Convert & upload {n} image(s)" if n else "Convert & upload",
                                  state="normal" if n and not self._busy_upload else "disabled")

    # ============================================================ preview
    def _schedule_preview(self) -> None:
        if self._preview_job:
            self.after_cancel(self._preview_job)
        self._preview_job = self.after(250, self._run_preview)

    def _run_preview(self) -> None:
        self._preview_job = None
        if self.cur_src is None:
            return
        self._preview_seq += 1
        seq, src, opts = self._preview_seq, self.cur_src, self.src_opts[self.cur_src].copy()
        self.prev_info.configure(text="Converting…")

        def work():
            orig = ac.load_source(src)
            res = ac.convert(orig, opts)
            data = ac.encode_png(res)
            size = orig.size
            o = orig.rotate(-opts.rotate, expand=True) if opts.rotate else orig.copy()
            o.thumbnail(PREVIEW, Image.LANCZOS)
            r = res.convert("RGB").resize(PREVIEW, Image.LANCZOS)
            return o, r, len(data), size

        def done(res):
            if seq != self._preview_seq:
                return  # superseded
            o, r, nbytes, size = res
            io_ = ctk.CTkImage(light_image=o, dark_image=o, size=o.size)
            ir = ctk.CTkImage(light_image=r, dark_image=r, size=r.size)
            self.preview_refs = [io_, ir]
            self.prev_orig.configure(image=io_)
            self.prev_res.configure(image=ir)
            self.prev_info.configure(text=f"{src.name}: {size[0]}x{size[1]} → 960x672, "
                                          f"PNG ≈ {human_size(nbytes)}")

        def failed(e):
            if seq == self._preview_seq:
                self.prev_info.configure(text=f"Can't read {src.name}: {e}")
        self.ctx.run_task(work, done, failed, title="Preview")

    # ============================================================= upload
    def _upload(self) -> None:
        if not self.sources or self._busy_upload:
            return
        if self._app_installed is False:
            info(self, "Artwork app missing",
                 "apps/artwork.py is not on the badge. Images will upload, but use "
                 "'Install Artwork app' to view them.")
        jobs = [(p, self.src_opts[p].copy()) for p in self.sources]
        existing = [n for n, _ in self.badge_files] if self.folder == ART_DIR else None
        fs_free = self._fs_free
        badge = self.ctx.badge
        self._busy_upload = True
        self._update_upload_label()
        self.progress.set(0)
        self.progress_label.configure(text="Converting…")

        def convert_all():
            out = []
            for i, (p, o) in enumerate(jobs):
                self.ctx.ui(self._progress, i / (2 * len(jobs)), f"Converting {p.name}")
                out.append((p, ac.convert_to_png(p, o)))
            return out

        def upload(converted):
            total = sum(len(d) for _, d in converted)
            if fs_free is not None and total > fs_free - 20_000:
                if not confirm(self, "Low space", f"The images need {human_size(total)} but the badge has "
                                                  f"{human_size(fs_free)} free. Upload anyway?"):
                    finish(None)
                    return

            def work():
                names = existing if existing is not None else art_names(badge.listdir(ART_DIR))
                names = list(names)
                written = []
                for i, (p, data) in enumerate(converted):
                    name = next_upload_name(names, p.stem)
                    self.ctx.ui(self._progress, 0.5 + i / (2 * len(converted)), f"Uploading {name}")
                    badge.write_bytes(data, f"{ART_DIR}/{name}")
                    names.append(name)
                    written.append(f"{ART_DIR}/{name}")
                return written
            self.ctx.run_device_task(work, finish, lambda _e: finish(None), title="Uploading artwork")

        def finish(written):
            self._busy_upload = False
            if written:
                self._progress(1, f"Uploaded {len(written)} image(s). Re-open the Artwork app on the badge to see them.")
                self.sources.clear()
                self.src_opts.clear()
                self.src_list.delete(0, "end")
                self.cur_src = None
                if self.read_only:
                    self.folder_seg.set("Artwork")
                    self.folder = ART_DIR
                self._changed(written, f"Uploaded {len(written)} image(s) to {ART_DIR}")
            else:
                self._progress(0, "Upload cancelled or failed (see log).")
            self._update_upload_label()

        self.ctx.run_task(convert_all, upload, lambda _e: finish(None), title="Converting artwork")

    def _progress(self, frac: float, text: str) -> None:
        self.progress.set(max(0.0, min(1.0, frac)))
        self.progress_label.configure(text=text)
