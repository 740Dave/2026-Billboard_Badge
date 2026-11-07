"""Device tab: the experiment / lab home.

Sections
  * Status       firmware, RAM, filesystem, input voltage (from the "badge.connected" info dict)
  * Files        browse the badge filesystem; download / upload / delete / view
  * Backup       back up the whole badge, restore from a backup folder, recent backups
  * Lab          LED test, I2C/SAO scan, voltage, free-form Python snippet, hard reset

Every badge call runs on the device worker (ctx.run_device_task); UI updates happen in the
on_done callbacks, which run on the Tk thread. Any mpremote call interrupts the badge's own
main loop, so after experiments the user must hard-reset to get the badge UI back.
"""

from __future__ import annotations

import posixpath
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

import customtkinter as ctk

from core import theme
from core.theme import COLORS

from .base import BaseTab, Section, confirm, human_size, info

PROTECTED = ("/", "/apps", "/lib", "/data")
# Files the badge needs to boot. Deleting any of them bricks the UI until firmware is reinstalled.
CORE_FILES = ("/main.py", "/boot.py", "/lib/epd.py", "/lib/button.py", "/lib/led_bar.py",
              "/lib/app_switcher.py", "/lib/large_png.py", "/lib/board.py", "/lib/base_app.py",
              "/apps/base_app.py", "/apps/__init__.py", "/lib/__init__.py")
WIFI_NOTE = ("Note: backups include /data/wifi.json in plain text (your Wi-Fi passwords). "
             "Don't share backup folders.")
TEXT_EXT = (".py", ".json", ".csv", ".txt", ".md", ".cfg", ".ini", ".toml")

LED_TEST = """
import time
try:
    import neopixel
except ImportError:
    from machine import neopixel  # noqa
from machine import Pin
n = 8
np = neopixel.NeoPixel(Pin(38), n)
B = 24  # low brightness (0-255)
cols = [(B,0,0),(0,B,0),(0,0,B),(B,B,0),(0,B,B),(B,0,B),(B,B,B)]
for c in cols:
    for i in range(n):
        np[i] = c
    np.write()
    time.sleep(0.35)
for step in range(n * 2):
    for i in range(n):
        np[i] = cols[(i + step) % len(cols)]
    np.write()
    time.sleep(0.12)
for i in range(n):
    np[i] = (0, 0, 0)
np.write()
print('LED test done: cycled', len(cols), 'colours on', n, 'LEDs (GPIO38)')
"""

# Built directly: upstream lib/sao.py uses syntax MicroPython 1.24 (shipping badges) rejects.
I2C_SCAN = """
import machine
i2c = machine.I2C(0, scl=machine.Pin(18), sda=machine.Pin(17), freq=100000)
found = i2c.scan()
print('SAO I2C (SDA=17, SCL=18):', [hex(a) for a in found] if found else 'no devices found')
"""

VOLTAGE = """
import machine
try:
    adc = machine.ADC(machine.Pin(14), atten=machine.ADC.ATTN_11DB)
    v = adc.read_uv() * 2 / 1e6
    print('VIN = %.2f V  (%s)' % (v, 'USB power' if v > 4.5 else 'battery'))
except Exception as e:
    print('voltage read failed:', e)
"""

SNIPPET_DEFAULT = """import os, gc
gc.collect()
print('free RAM:', gc.mem_free())
print(os.listdir('/'))
"""


def describe_vin(v) -> str:
    if v is None:
        return "unknown"
    if v >= 4.5:
        return f"{v:.2f} V  (USB power)"
    if v >= 3.2:
        return f"{v:.2f} V  (battery)"
    return f"{v:.2f} V  (low / not measured)"


class DeviceTab(BaseTab):
    TITLE = "Device"

    # ================================================================ build
    def build(self) -> None:
        self.connected = False
        self.entries: dict[str, object] = {}   # path -> FileEntry
        self.ui_interrupted = False
        self.last_backup: Path | None = None

        self.grid_columnconfigure(0, weight=2, uniform="col")
        self.grid_columnconfigure(1, weight=3, uniform="col")
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_status(Section(self, "Badge status"))
        self.status_sec.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        self._build_backup(Section(self, "Backup & restore"))
        self.backup_sec.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        self._build_files(Section(self, "Badge files"))
        self.files_sec.grid(row=0, column=1, rowspan=2, sticky="nsew", pady=(0, 6))
        self._build_lab(Section(self, "Lab / quick experiments"))
        self.lab_sec.grid(row=2, column=0, columnspan=2, sticky="nsew")

        ev = self.ctx.events
        ev.subscribe("badge.connected", self._on_connected)
        ev.subscribe("badge.disconnected", self._on_disconnected)
        ev.subscribe("port.changed", lambda p: None if p else self._on_disconnected(None))
        ev.subscribe("badge.files_changed", lambda _p: self.refresh_files() if self.connected else None)
        ev.subscribe("busy", self._on_busy)
        ev.subscribe("badge.ui_interrupted", lambda v: self._set_interrupted(bool(v)))
        self._show_not_connected()
        self._load_backups()

    # ---------------------------------------------------------- status card
    def _build_status(self, sec: Section) -> None:
        self.status_sec = sec
        b = sec.body
        self.status_msg = ctk.CTkLabel(b, text="", justify="left", anchor="w", wraplength=380)
        self.status_msg.pack(fill="x")
        self.status_grid = ctk.CTkFrame(b, fg_color="transparent")
        self.status_grid.grid_columnconfigure(1, weight=1)
        self.st_labels = {}
        for r, (key, title) in enumerate([("fw", "Firmware"), ("machine", "Board"), ("ram", "RAM"),
                                          ("fs", "Storage"), ("vin", "Input voltage")]):
            ctk.CTkLabel(self.status_grid, text=title + ":", anchor="w",
                         font=ctk.CTkFont(weight="bold")).grid(row=r * 2 if key != "fs" else r * 2, column=0,
                                                              sticky="nw", padx=(0, 8))
            lbl = ctk.CTkLabel(self.status_grid, text="-", anchor="w", justify="left", wraplength=280)
            lbl.grid(row=r * 2, column=1, sticky="w")
            self.st_labels[key] = lbl
            if key == "fs":
                self.fs_bar = ctk.CTkProgressBar(self.status_grid, height=10)
                self.fs_bar.set(0)
                self.fs_bar.grid(row=r * 2 + 1, column=1, sticky="ew", pady=(0, 4))
        row = ctk.CTkFrame(b, fg_color="transparent")
        row.pack(fill="x", side="bottom", pady=(6, 0))
        self.refresh_btn = ctk.CTkButton(row, text="Refresh", width=90, command=self.refresh_info)
        self.refresh_btn.pack(side="left")

    def _show_not_connected(self) -> None:
        self.status_grid.pack_forget()
        self.status_msg.configure(text=(
            "Not connected.\n\n"
            "1. Plug the badge in with a USB-C DATA cable (charge-only cables won't work).\n"
            "2. The port appears as /dev/cu.usbmodem*.\n"
            "3. Select it in the top bar and click Connect."))

    def _show_info(self, d: dict) -> None:
        self.status_msg.configure(text="")
        self.status_grid.pack(fill="x", before=self.refresh_btn.master)
        fw = d.get("release") or "?"
        mp = d.get("micropython") or ""
        self.st_labels["fw"].configure(text=f"MicroPython {fw}" + (f"\n{mp}" if mp else ""))
        self.st_labels["machine"].configure(text=str(d.get("machine") or "?"))
        free, alloc = d.get("mem_free"), d.get("mem_alloc")
        total = (free or 0) + (alloc or 0)
        self.st_labels["ram"].configure(text=f"{human_size(free)} free of {human_size(total)}")
        tot, ff = d.get("fs_total"), d.get("fs_free")
        if tot:
            used = tot - (ff or 0)
            self.fs_bar.set(max(0.0, min(1.0, used / tot)))
            self.st_labels["fs"].configure(
                text=f"{human_size(used)} used / {human_size(tot)} ({human_size(ff)} free, {used * 100 // tot}%)")
        else:
            self.fs_bar.set(0)
            self.st_labels["fs"].configure(text="?")
        self.st_labels["vin"].configure(text=describe_vin(d.get("vin")))

    def _on_connected(self, d) -> None:
        self.connected = True
        if isinstance(d, dict):
            self._show_info(d)
        self.refresh_files()

    def _on_disconnected(self, _p) -> None:
        self.connected = False
        self._show_not_connected()
        self._clear_tree()

    def refresh_info(self) -> None:
        def done(d):
            self.connected = True
            self._show_info(d)
            self.ctx.status("Badge info refreshed")
        self.ctx.run_device_task(self.ctx.badge.info, done, title="Reading badge info")

    def _on_busy(self, payload) -> None:
        busy = bool(payload and payload[0])
        try:
            self.refresh_btn.configure(state="disabled" if busy else "normal")
        except tk.TclError:
            pass

    # --------------------------------------------------------- file browser
    def _build_files(self, sec: Section) -> None:
        self.files_sec = sec
        b = sec.body
        bar = ctk.CTkFrame(b, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 4))
        for text, cmd, w in [("Refresh", self.refresh_files, 70), ("View", self.view_selected, 60),
                             ("Download…", self.download_selected, 90)]:
            ctk.CTkButton(bar, text=text, width=w, command=cmd).pack(side="left", padx=(0, 4))
        ctk.CTkButton(bar, text="Upload…", width=80, fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                      command=self.upload_to_selected).pack(side="left", padx=(0, 4))
        ctk.CTkButton(bar, text="Delete…", width=70, fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                      command=self.delete_selected).pack(side="right")

        wrap = tk.Frame(b)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("size",), selectmode="extended", style="Badge.Treeview")
        self.tree.heading("#0", text="Path", anchor="w")
        self.tree.heading("size", text="Size", anchor="e")
        self.tree.column("#0", width=300, stretch=True)
        self.tree.column("size", width=80, anchor="e", stretch=False)
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ysb.pack(side="right", fill="y")
        theme.style_treeview(self.tree, "Badge.Treeview", rowheight=22)
        self.tree.bind("<Double-1>", lambda _e: self.view_selected())
        self.files_info = ctk.CTkLabel(b, text="", anchor="w")
        self.files_info.pack(fill="x", pady=(4, 0))

    def _clear_tree(self) -> None:
        self.entries = {}
        self.tree.delete(*self.tree.get_children())
        self.files_info.configure(text="Not connected" if not self.connected else "")

    def refresh_files(self) -> None:
        self.ctx.run_device_task(lambda: self.ctx.badge.listdir("/", recursive=True), self._fill_tree,
                                 title="Listing badge files")

    def _fill_tree(self, entries) -> None:
        opened = {i for i in self._all_items() if self.tree.item(i, "open")}
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        self.entries = {e.path: e for e in entries}
        # directories first, then files, alphabetically, inserted parent-before-child
        for e in sorted(entries, key=lambda e: (e.path.count("/"), not e.is_dir, e.path.lower())):
            parent = posixpath.dirname(e.path)
            parent = "" if parent == "/" else parent
            if parent and not self.tree.exists(parent):
                self.tree.insert("", "end", iid=parent, text=posixpath.basename(parent) + "/", values=("",))
            name = posixpath.basename(e.path) + ("/" if e.is_dir else "")
            self.tree.insert(parent, "end", iid=e.path, text=name,
                             values=("" if e.is_dir else human_size(e.size),),
                             open=e.path in opened or (not opened and e.path in ("/apps",)))
        # re-sort children so dirs come first even when they were created on demand
        for item in [""] + self._all_items():
            kids = list(self.tree.get_children(item))
            kids.sort(key=lambda k: (not self._is_dir(k), k.lower()))
            for i, k in enumerate(kids):
                self.tree.move(k, item, i)
        keep = [s for s in selected if self.tree.exists(s)]
        if keep:
            self.tree.selection_set(keep)
        nfiles = sum(1 for e in entries if not e.is_dir)
        total = sum(e.size for e in entries if not e.is_dir)
        self.files_info.configure(text=f"{nfiles} files, {human_size(total)}  (double-click a text file to view)")
        self.ctx.status(f"Badge: {nfiles} files")

    def _all_items(self, parent="") -> list[str]:
        out = []
        for k in self.tree.get_children(parent):
            out.append(k)
            out.extend(self._all_items(k))
        return out

    def _is_dir(self, path: str) -> bool:
        e = self.entries.get(path)
        return e.is_dir if e is not None else True

    def _selection(self) -> list[str]:
        return list(self.tree.selection())

    def _need_selection(self) -> list[str] | None:
        sel = self._selection()
        if not sel:
            info(self, "Nothing selected", "Select one or more files or folders in the list first.")
            return None
        return sel

    # ---- view
    def view_selected(self, path: str | None = None) -> None:
        if path is None:
            sel = self._selection()
            if len(sel) != 1:
                return
            path = sel[0]
        if self._is_dir(path):
            return
        if not path.lower().endswith(TEXT_EXT):
            info(self, "Not a text file", f"{path} doesn't look like a text file.\nUse Download instead.")
            return
        self.ctx.run_device_task(lambda: self.ctx.badge.read_file(path),
                                 lambda data: self._open_viewer(path, data), title=f"Reading {path}")

    def _open_viewer(self, path: str, data: bytes):
        win = ctk.CTkToplevel(self)
        win.title(f"{path} (read-only, on badge)")
        win.geometry("760x560")
        box = ctk.CTkTextbox(win, wrap="none", font=ctk.CTkFont(family="Menlo", size=12))
        box.pack(fill="both", expand=True, padx=8, pady=8)
        box.insert("1.0", data.decode("utf-8", errors="replace"))
        box.configure(state="disabled")
        ctk.CTkLabel(win, text=f"{human_size(len(data))}  -  read-only view; download to edit",
                     anchor="w").pack(fill="x", padx=8, pady=(0, 8))
        win.after(100, win.lift)
        return win

    # ---- download
    def _files_under(self, paths: list[str]) -> list[tuple[str, str]]:
        """[(badge_path, relative local path)] for files in the selection (folders expanded)."""
        out, seen = [], set()
        for p in paths:
            if self._is_dir(p):
                base = posixpath.dirname(p.rstrip("/"))
                for fp, e in sorted(self.entries.items()):
                    if not e.is_dir and fp.startswith(p.rstrip("/") + "/") and fp not in seen:
                        seen.add(fp)
                        out.append((fp, posixpath.relpath(fp, base or "/")))
            elif p not in seen:
                seen.add(p)
                out.append((p, posixpath.basename(p)))
        return out

    def download_selected(self, paths: list[str] | None = None, dest: str | None = None) -> None:
        paths = paths or self._need_selection()
        if not paths:
            return
        files = self._files_under(paths)
        if not files:
            info(self, "Nothing to download", "The selection contains no files.")
            return
        dest = dest or filedialog.askdirectory(parent=self, title="Download to folder",
                                               initialdir=str(Path.home() / "Downloads"))
        if not dest:
            return
        badge = self.ctx.badge

        def work():
            out = []
            for i, (remote, rel) in enumerate(files):
                self.ctx.status(f"Downloading {i + 1}/{len(files)}: {remote}")
                local = Path(dest) / rel
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(badge.read_file(remote))
                out.append(local)
            return out

        def done(out):
            self.ctx.log(f"Downloaded {len(out)} file(s) to {dest}")
            self.ctx.status(f"Downloaded {len(out)} file(s) to {dest}")
        self.ctx.run_device_task(work, done, title="Downloading")

    # ---- upload
    def _target_dir(self) -> str:
        sel = self._selection()
        if not sel:
            return "/"
        p = sel[0]
        return p if self._is_dir(p) else (posixpath.dirname(p) or "/")

    def upload_to_selected(self, local_files: list[str] | None = None, target: str | None = None) -> None:
        target = target or self._target_dir()
        if local_files is None:
            local_files = list(filedialog.askopenfilenames(parent=self, title=f"Upload to {target} on the badge"))
        if not local_files:
            return
        remotes = [(f, posixpath.join(target, Path(f).name)) for f in local_files]
        clobber = [r for _, r in remotes if r in self.entries]
        if clobber and not confirm(self, "Overwrite?", "These files already exist on the badge and will be "
                                   "replaced:\n\n" + "\n".join(clobber[:15])):
            return
        badge = self.ctx.badge

        def work():
            for i, (local, remote) in enumerate(remotes):
                self.ctx.status(f"Uploading {i + 1}/{len(remotes)}: {remote}")
                badge.write_file(local, remote)
            return [r for _, r in remotes]

        def done(changed):
            self.ctx.log(f"Uploaded {len(changed)} file(s) to {target}")
            self.ctx.events.publish("badge.files_changed", changed)
        self.ctx.run_device_task(work, done, title="Uploading")

    # ---- delete
    def delete_selected(self, paths: list[str] | None = None, ask: bool = True) -> None:
        paths = paths or self._need_selection()
        if not paths:
            return
        refused = [p for p in paths if p.rstrip("/") in PROTECTED or p in ("", "/")]
        if refused:
            info(self, "Protected", "These folders are part of the badge firmware and can't be deleted "
                 "here:\n\n" + "\n".join(refused))
            return
        core = sorted({c for p in paths for c in CORE_FILES
                       if c == p.rstrip("/") or c.startswith(p.rstrip("/") + "/")})
        if core:
            info(self, "Core badge files", "These files are needed for the badge to boot; deleting them "
                 "would leave it unable to start:\n\n" + "\n".join(core) +
                 "\n\nNothing was deleted. To replace or repair them, reinstall via the Firmware tab.")
            return
        # drop children of selected folders (they go with the folder)
        paths = [p for p in paths if not any(p != q and p.startswith(q + "/") for q in paths)]
        data = [p for p in paths if p.startswith("/data/")]
        if ask:
            lines = [p + ("/  (folder and everything in it)" if self._is_dir(p) else "") for p in paths]
            msg = "Delete from the badge?\n\n" + "\n".join(lines[:20]) + ("\n…" if len(lines) > 20 else "")
            if data:
                msg += ("\n\nWARNING: items under /data hold your settings, saved artwork and app data. "
                        "They are NOT restored by firmware updates. Back up first if unsure.")
            msg += "\n\nThis cannot be undone."
            if not confirm(self, "Delete files", msg):
                return
        badge = self.ctx.badge

        def work():
            for p in paths:
                badge.remove(p, recursive=self._is_dir(p))
            return paths

        def done(removed):
            self.ctx.log(f"Deleted {', '.join(removed)}")
            self.ctx.events.publish("badge.files_changed", removed)
        self.ctx.run_device_task(work, done, title="Deleting")

    # ------------------------------------------------------ backup & restore
    def _build_backup(self, sec: Section) -> None:
        self.backup_sec = sec
        b = sec.body
        row = ctk.CTkFrame(b, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkButton(row, text="Back up entire badge", command=self.backup_now).pack(side="left", padx=(0, 6))
        ctk.CTkButton(row, text="Restore from backup…", fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                      command=self.restore_from).pack(side="left")
        ctk.CTkLabel(b, text=WIFI_NOTE, anchor="w", justify="left", wraplength=380,
                     text_color=COLORS["warning_text"]).pack(fill="x", pady=(4, 0))
        self.backup_msg = ctk.CTkLabel(b, text="", anchor="w", justify="left", wraplength=380)
        self.backup_msg.pack(fill="x", pady=(6, 0))
        self.restore_bar = ctk.CTkProgressBar(b, height=8)
        self.restore_bar.set(0)
        hdr = ctk.CTkFrame(b, fg_color="transparent")
        hdr.pack(fill="x", pady=(6, 2))
        ctk.CTkLabel(hdr, text="Recent backups", anchor="w", font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(hdr, text="Open folder", width=90,
                      command=lambda: self.open_in_finder(self.backup_dir)).pack(side="right")
        self.backup_list = ctk.CTkScrollableFrame(b, height=90)
        self.backup_list.pack(fill="both", expand=True)

    @property
    def backup_dir(self) -> Path:
        return Path(self.ctx.config.get("backup_dir")).expanduser()

    def recent_backups(self, n: int = 8) -> list[Path]:
        d = self.backup_dir
        if not d.is_dir():
            return []
        return sorted((p for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=lambda p: p.name, reverse=True)[:n]

    def _load_backups(self) -> None:
        for w in self.backup_list.winfo_children():
            w.destroy()
        items = self.recent_backups()
        if not items:
            ctk.CTkLabel(self.backup_list, text=f"No backups yet in {self.backup_dir}", anchor="w").pack(fill="x")
        for p in items:
            r = ctk.CTkFrame(self.backup_list, fg_color="transparent")
            r.pack(fill="x")
            ctk.CTkLabel(r, text=p.name, anchor="w").pack(side="left")
            ctk.CTkButton(r, text="Restore", width=60, height=22, fg_color=COLORS["danger"], hover_color=COLORS["danger_hover"],
                          command=lambda p=p: self.restore_from(str(p))).pack(side="right", padx=2)
            ctk.CTkButton(r, text="Show", width=50, height=22,
                          command=lambda p=p: self.open_in_finder(p)).pack(side="right", padx=2)

    def open_in_finder(self, path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True) if not path.exists() else None
        cmd = ["open", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path)]
        try:
            subprocess.Popen(cmd)
        except OSError as e:
            self.ctx.log(f"Could not open {path}: {e}")

    def backup_now(self) -> None:
        dest = self.backup_dir
        self.backup_msg.configure(text="Backing up… (a full copy can take a minute or two)")

        def done(path):
            self.last_backup = Path(path)
            self.backup_msg.configure(text=f"Backed up to:\n{path}\n{WIFI_NOTE}")
            self.ctx.log(f"Backup saved to {path}")
            self.ctx.status("Backup complete")
            self._load_backups()
            self._remind_reset("Backup finished.")

        def fail(e):
            self.backup_msg.configure(text=f"Backup failed: {e}")
            self._remind_reset("Backup failed.")
        self.ctx.run_device_task(lambda: self.ctx.badge.backup(dest), done, fail, title="Backing up badge")

    def restore_from(self, folder: str | None = None, ask: bool = True) -> None:
        """Restore everything from a backup, including /data (existing /data files are overwritten
        with the backup's versions). Previews with plan_push first and asks for confirmation."""
        folder = folder or filedialog.askdirectory(parent=self, title="Choose a backup folder to restore",
                                                   initialdir=str(self.backup_dir))
        if not folder:
            return
        folder = Path(folder)
        if not folder.is_dir() or not any(folder.iterdir()):
            info(self, "Empty folder", f"{folder} is empty or missing.")
            return
        if not (folder / "main.py").exists() and not (folder / "apps").exists() and ask:
            if not confirm(self, "Not a badge backup?", f"{folder} has no main.py or apps/ folder.\n"
                           "Copy its contents onto the badge root anyway?"):
                return
        badge = self.ctx.badge
        self.backup_msg.configure(text=f"Comparing {folder.name} with the badge…")

        def planned(plan):
            copy = plan["copy"]
            remote = plan["remote_hashes"]
            data_over = [p for p in copy if p.startswith("/data/") and p in remote]
            if not copy:
                self.backup_msg.configure(text=f"Badge already matches {folder.name} "
                                          f"({plan['unchanged']} files unchanged). Nothing to restore.")
                self._remind_reset("Restore check finished.")
                return
            if ask:
                msg = (f"Restore from\n{folder}\n\n"
                       f"  {len(copy)} file(s) to write ({human_size(plan['bytes'])})\n"
                       f"  {len(data_over)} existing /data file(s) will be OVERWRITTEN with the backup's "
                       f"versions (settings, artwork, app data)\n"
                       f"  {plan['unchanged']} file(s) already identical\n")
                if data_over:
                    msg += "\n" + "\n".join(data_over[:10]) + ("\n…" if len(data_over) > 10 else "") + "\n"
                msg += ("\nNothing on the badge is deleted. The badge is hard-reset afterwards.\n\n"
                        "Restore now?")
                if not confirm(self, "Restore backup", msg):
                    self.backup_msg.configure(text="Restore cancelled; nothing was changed.")
                    self._remind_reset("Restore cancelled.")
                    return
            self._run_restore(folder, plan)

        def plan_fail(e):
            self.backup_msg.configure(text=f"Could not compare backup with badge: {e}")
        self.ctx.run_device_task(lambda: badge.plan_push(folder, "/", only_if_missing=()), planned, plan_fail,
                                 title="Checking backup")

    def _run_restore(self, folder: Path, plan: dict) -> None:
        self.restore_bar.pack(fill="x", pady=(4, 0), after=self.backup_msg)
        self.restore_bar.set(0)
        badge = self.ctx.badge

        def progress(i, n, path):
            def upd():
                self.restore_bar.set(1.0 if n == 0 else i / n)
                self.backup_msg.configure(text=f"Restoring {min(i + 1, n)}/{n}: {path}" if path != "done"
                                          else "Restore copied; resetting badge…")
            self.ctx.ui(upd)

        def work():
            res = badge.push_tree(folder, "/", progress=progress, delete_extra=False,
                                  only_if_missing=(), plan=plan)
            badge.reset()
            return res

        def done(res):
            self.restore_bar.pack_forget()
            self.backup_msg.configure(text=f"Restored from {folder.name}: {len(res['copied'])} copied, "
                                      f"{res['unchanged']} unchanged. Badge reset.")
            self.ctx.log(f"Restore from {folder}: copied {len(res['copied'])}, unchanged {res['unchanged']}")
            self._set_interrupted(False)
            self.ctx.events.publish("badge.files_changed", res["copied"])

        def fail(e):
            self.restore_bar.pack_forget()
            text = str(e)
            if "Not enough space" in text:
                self.backup_msg.configure(text=f"Restore NOT started: {text}\nNothing was written.")
                info(self, "Not enough space on the badge",
                     f"{text}\n\nNothing was written to the badge.")
            else:
                self.backup_msg.configure(text=f"Restore failed: {text}\n"
                                          "The badge may be partly restored; try again.")
            self._remind_reset("Restore failed.")
        self.ctx.run_device_task(work, done, fail, title="Restoring backup")

    # ------------------------------------------------------------------ lab
    def _build_lab(self, sec: Section) -> None:
        self.lab_sec = sec
        b = sec.body
        b.grid_columnconfigure(0, weight=1)
        b.grid_columnconfigure(1, weight=1)
        b.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(b, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew")
        for text, cmd in [("LED test", lambda: self.run_lab("LED test", LED_TEST)),
                          ("I2C / SAO scan", lambda: self.run_lab("I2C scan", I2C_SCAN)),
                          ("Battery / USB voltage", self.read_voltage)]:
            ctk.CTkButton(top, text=text, width=150, command=cmd).pack(side="left", padx=(0, 6))
        self.reset_btn = ctk.CTkButton(top, text="Restore badge UI (hard reset)",
                                       font=ctk.CTkFont(weight="bold"),
                                       command=self.hard_reset)
        self.reset_btn.pack(side="right")

        self.lab_warn = ctk.CTkLabel(
            b, anchor="w", justify="left", text_color=COLORS["warning_text"],
            text="Experiments stop the badge's own screen/UI loop. Click \"Restore badge UI\" when you're done.")
        self.lab_warn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 4))

        left = ctk.CTkFrame(b, fg_color="transparent")
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 6))
        hdr = ctk.CTkFrame(left, fg_color="transparent")
        hdr.pack(fill="x")
        ctk.CTkLabel(hdr, text="Python snippet (runs on the badge)", anchor="w").pack(side="left")
        ctk.CTkButton(hdr, text="Run ▶", width=70, command=self.run_snippet).pack(side="right")
        self.snippet = ctk.CTkTextbox(left, height=110, font=ctk.CTkFont(family="Menlo", size=12))
        self.snippet.pack(fill="both", expand=True)
        self.snippet.insert("1.0", SNIPPET_DEFAULT)

        right = ctk.CTkFrame(b, fg_color="transparent")
        right.grid(row=2, column=1, sticky="nsew")
        hdr2 = ctk.CTkFrame(right, fg_color="transparent")
        hdr2.pack(fill="x")
        ctk.CTkLabel(hdr2, text="Output", anchor="w").pack(side="left")
        ctk.CTkButton(hdr2, text="Clear", width=60,
                      command=lambda: self._set_output("")).pack(side="right")
        self.output = ctk.CTkTextbox(right, height=110, font=ctk.CTkFont(family="Menlo", size=12))
        self.output.pack(fill="both", expand=True)
        self.output.configure(state="disabled")

    def _set_output(self, text: str, append: bool = False) -> None:
        self.output.configure(state="normal")
        if not append:
            self.output.delete("1.0", "end")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def _set_interrupted(self, flag: bool) -> None:
        # The top bar shows the global "Hard reset" reminder; here we only tint the Lab hint.
        self.ui_interrupted = flag
        self.lab_warn.configure(text_color=COLORS["danger_text"] if flag else COLORS["warning_text"])

    def _remind_reset(self, what: str) -> None:
        self._set_interrupted(True)
        self.ctx.status(f"{what} Hard-reset the badge to bring its UI back.")

    def run_lab(self, name: str, code: str, timeout: float = 60) -> None:
        self._set_output(f">>> {name}\n")

        def done(out):
            out = (out or "").rstrip()
            text = out if out else "(no output)"
            self._set_output(text + "\n", append=True)
            for line in text.splitlines():
                self.ctx.log(f"[{name}] {line}")
            self._remind_reset(f"{name} finished.")

        def fail(e):
            self._set_output(f"ERROR: {e}\n", append=True)
            self._remind_reset(f"{name} failed.")
        self.ctx.run_device_task(lambda: self.ctx.badge.exec_py(code, timeout=timeout), done, fail, title=name)

    def read_voltage(self) -> None:
        self._set_output(">>> Battery / USB voltage\n")

        def work():
            try:
                return ("info", self.ctx.badge.info())
            except Exception:  # noqa: BLE001  fall back to a raw ADC read
                return ("raw", self.ctx.badge.exec_py(VOLTAGE, timeout=20))

        def done(res):
            kind, val = res
            if kind == "info":
                text = f"VIN = {describe_vin(val.get('vin'))}"
                self._show_info(val)
            else:
                text = (val or "").strip() or "(no output)"
            self._set_output(text + "\n  (~5 V = USB, 3.3-4.2 V = battery)\n", append=True)
            self.ctx.log(f"[voltage] {text}")
            self._remind_reset("Voltage read.")

        def fail(e):
            self._set_output(f"ERROR: {e}\n", append=True)
        self.ctx.run_device_task(work, done, fail, title="Reading voltage")

    def run_snippet(self, code: str | None = None, ask: bool = True) -> None:
        code = code if code is not None else self.snippet.get("1.0", "end")
        if not code.strip():
            return
        if ask and not self.ui_interrupted and not confirm(
                self, "Run on badge",
                "Running code stops the badge's own UI until you hard-reset it "
                "(\"Restore badge UI\" button).\n\nLong-running code (infinite loops) blocks the serial "
                "link until it finishes or times out (60 s).\n\nRun it now?"):
            return
        self.run_lab("snippet", code)

    def hard_reset(self) -> None:
        def done(_):
            self._set_interrupted(False)
            self.ctx.status("Badge hard-reset: its UI is restarting (takes a few seconds)")
            self._set_output("Badge hard-reset; UI restarting.\n", append=True)
        self.ctx.run_device_task(self.ctx.badge.reset, done, title="Hard reset")

    # ------------------------------------------------------------- lifecycle
    def on_show(self) -> None:
        self._load_backups()
