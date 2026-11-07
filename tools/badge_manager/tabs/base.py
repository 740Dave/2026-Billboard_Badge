"""Base class and small shared widgets for tabs."""

from __future__ import annotations

import customtkinter as ctk


class BaseTab(ctk.CTkFrame):
    """Every tab subclasses this.

    Subclasses set TITLE and implement build(). on_show() is called each time the tab is selected
    (use it for cheap refreshes; never block the UI thread: use ctx.run_task / ctx.run_device_task).
    """

    TITLE = "Tab"

    def __init__(self, master, ctx):
        super().__init__(master, fg_color="transparent")
        self.ctx = ctx
        self.build()

    def build(self) -> None:
        raise NotImplementedError

    def on_show(self) -> None:
        pass


class Section(ctk.CTkFrame):
    """A titled panel."""

    def __init__(self, master, title: str, **kw):
        super().__init__(master, **kw)
        ctk.CTkLabel(self, text=title, font=ctk.CTkFont(size=14, weight="bold"), anchor="w").pack(
            fill="x", padx=10, pady=(8, 4))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=10, pady=(0, 10))


def confirm(parent, title: str, message: str) -> bool:
    """Modal yes/no dialog (customtkinter has none built in)."""
    from tkinter import messagebox
    return messagebox.askyesno(title, message, parent=parent)


def info(parent, title: str, message: str) -> None:
    from tkinter import messagebox
    messagebox.showinfo(title, message, parent=parent)


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
