#!/usr/bin/env python3
"""Billboard Badge Manager: a desktop GUI for the Hackaday Supercon 2026 Billboard Badge.

Run:  .venv/bin/python app.py
"""

from __future__ import annotations

import importlib
import sys
import traceback
from pathlib import Path

import customtkinter as ctk

sys.path.insert(0, str(Path(__file__).parent))

from core.context import AppContext  # noqa: E402
from core import theme  # noqa: E402
from core.theme import COLORS  # noqa: E402
from core.updates import UpdatePoller  # noqa: E402
from core.update_ui import UpdateIndicator  # noqa: E402

# (module, class). Order = tab order. A tab that fails to import shows its error instead of
# taking the whole app down, which matters while forks and features change daily.
TABS = [
    ("tabs.device", "DeviceTab"),
    ("tabs.firmware", "FirmwareTab"),
    ("tabs.apps", "AppsTab"),
    ("tabs.artwork", "ArtworkTab"),
    ("tabs.maker", "MakerTab"),
    ("tabs.settings", "SettingsTab"),
]


class ConnectionBar(ctk.CTkFrame):
    def __init__(self, master, ctx: AppContext):
        super().__init__(master)
        self.ctx = ctx
        ctk.CTkLabel(self, text="Badge port:").pack(side="left", padx=(10, 4), pady=8)
        self.port_menu = ctk.CTkOptionMenu(self, values=["(none)"], width=360, command=self._picked)
        self.port_menu.pack(side="left", padx=4)
        ctk.CTkButton(self, text="Rescan", width=70, command=self.rescan).pack(side="left", padx=4)
        ctk.CTkButton(self, text="Connect", width=80, command=self.connect).pack(side="left", padx=4)
        self.reset_btn = ctk.CTkButton(self, text="Hard reset", width=90, command=self.hard_reset)
        self.reset_btn.pack(side="left", padx=4)
        self.state = ctk.CTkLabel(self, text="● not connected", text_color=COLORS["muted_text"])
        self.state.pack(side="left", padx=12)
        self.busy = ctk.CTkProgressBar(self, mode="indeterminate", width=120)
        self.busy_label = ctk.CTkLabel(self, text="")
        self.busy_label.pack(side="right", padx=8)
        self._ports = {}
        ctx.events.subscribe("busy", self._on_busy)
        ctx.events.subscribe("badge.ui_interrupted", self._on_interrupted)
        ctx.events.subscribe("badge.connected", self._on_connected)
        ctx.events.subscribe("badge.disconnected", lambda _: self._set_state("● not connected", COLORS["muted_text"]))
        self.rescan()

    def rescan(self):
        from core.badge import BadgeService
        ports = BadgeService.list_ports()
        self._ports = {p.label(): p.device for p in ports}
        labels = list(self._ports) or ["(no serial ports - plug in the badge)"]
        self.port_menu.configure(values=labels)
        current = self.ctx.badge.port
        auto = BadgeService.autodetect_port()
        pick = next((l for l, d in self._ports.items() if d == current), None) or \
            next((l for l, d in self._ports.items() if d == auto), None) or labels[0]
        self.port_menu.set(pick)
        self._picked(pick, quiet=True)

    def _picked(self, label, quiet=False):
        dev = self._ports.get(label)
        if dev != self.ctx.badge.port:
            if not self.ctx.set_port(dev):   # refused: a badge task is running
                cur = next((l for l, d in self._ports.items() if d == self.ctx.badge.port), None)
                if cur:
                    self.port_menu.set(cur)
                return
            if not quiet:
                self.ctx.log(f"Port set to {dev}")

    def _on_interrupted(self, paused):
        if paused:
            self.reset_btn.configure(text="Hard reset ⚠ badge paused", width=190, fg_color=COLORS["warning"],
                                     hover_color=COLORS["warning_hover"])
        else:
            self.reset_btn.configure(text="Hard reset", width=90, fg_color=COLORS["accent"],
                                     hover_color=COLORS["accent_hover"])

    def connect(self):
        def done(info):
            self.ctx.events.publish("badge.connected", info)
            self.ctx.status("Connected")

        def failed(_e):
            self.ctx.events.publish("badge.disconnected")
        self.ctx.run_device_task(self.ctx.badge.info, done, failed, title="Connecting")

    def hard_reset(self):
        self.ctx.run_device_task(self.ctx.badge.reset, lambda _: self.ctx.status("Badge reset"),
                                 title="Hard reset")

    def _on_connected(self, info):
        v = info.get("vin")
        vtxt = f"  ·  {v:.2f} V" if v else ""
        self._set_state(f"● connected  ·  MicroPython {info.get('release', '?')}{vtxt}", COLORS["success_text"])

    def _set_state(self, text, color):
        self.state.configure(text=text, text_color=color)

    def _on_busy(self, payload):
        busy, title = payload
        if busy:
            self.busy.pack(side="right", padx=4)
            self.busy.start()
            self.busy_label.configure(text=title)
        else:
            self.busy.stop()
            self.busy.pack_forget()
            self.busy_label.configure(text="")


class App(ctk.CTk):
    def __init__(self):
        from core.config import Config
        theme.apply_theme(Config())   # must run before the root window exists
        super().__init__()
        self.title("2026 Hackaday Billboard Badge Manager")
        self.geometry("1280x860")
        self.minsize(1050, 700)

        # log console + status bar (created first so ctx can write to them)
        bottom = ctk.CTkFrame(self)
        self.status_label = ctk.CTkLabel(bottom, text="Ready", anchor="w")
        self.log_box = ctk.CTkTextbox(bottom, height=140, font=ctk.CTkFont(family="Menlo", size=11))
        self.log_box.configure(state="disabled")

        self.ctx = AppContext(self, self._append_log, lambda m: self.status_label.configure(text=m))

        self.conn_bar = ConnectionBar(self, self.ctx)
        self.conn_bar.pack(fill="x", padx=10, pady=(10, 4))

        self.tabview = ctk.CTkTabview(self, command=self._tab_changed)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=4)
        self.tabs = {}
        for mod, cls in TABS:
            self._add_tab(mod, cls)

        # Poll every fork for firmware/app updates: at launch, then every 15 minutes.
        self.updates = UpdatePoller(self.ctx, interval_s=900)
        UpdateIndicator(self.conn_bar, self.ctx, self.updates).pack(side="right", padx=8)
        self.updates.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        bottom.pack(fill="x", padx=10, pady=(0, 10))
        bar = ctk.CTkFrame(bottom, fg_color="transparent")
        bar.pack(fill="x")
        self.status_label.pack(in_=bar, side="left", fill="x", expand=True, padx=8, pady=2)
        self._log_visible = True
        ctk.CTkButton(bar, text="Clear log", width=80,
                      command=self._clear_log).pack(side="right", padx=4, pady=2)
        ctk.CTkButton(bar, text="Hide log", width=80,
                      command=self._toggle_log).pack(side="right", padx=4, pady=2)
        self.log_box.pack(fill="x", padx=4, pady=(0, 4))
        self._tab_changed()

        # First-run wizard (maker handle, what you make, Maker Detector); also offers the profile
        # to badges as they connect.
        from core.onboarding import maybe_run_onboarding
        self.after(400, lambda: maybe_run_onboarding(self, self.ctx))

    def _on_close(self):
        try:
            self.updates.stop()
        finally:
            self.destroy()

    def _add_tab(self, mod, cls):
        try:
            module = importlib.import_module(mod)
            klass = getattr(module, cls)
            title = klass.TITLE
            frame = self.tabview.add(title)
            tab = klass(frame, self.ctx)
            tab.pack(fill="both", expand=True)
            self.tabs[title] = tab
        except Exception:  # noqa: BLE001
            title = cls.replace("Tab", "")
            frame = self.tabview.add(title + " (error)")
            box = ctk.CTkTextbox(frame)
            box.insert("1.0", f"Failed to load {mod}.{cls}:\n\n{traceback.format_exc()}")
            box.pack(fill="both", expand=True)

    def _tab_changed(self):
        tab = self.tabs.get(self.tabview.get())
        if tab:
            tab.on_show()

    def _append_log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg.rstrip("\n") + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _toggle_log(self):
        if self._log_visible:
            self.log_box.pack_forget()
        else:
            self.log_box.pack(fill="x", padx=4, pady=(0, 4))
        self._log_visible = not self._log_visible


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
