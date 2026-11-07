"""App-wide look: appearance mode, the customtkinter color theme and a shared palette.

    from core import theme
    theme.apply_theme(config)          # once, BEFORE any widget is created
    ctk.CTkButton(..., fg_color=theme.COLORS["danger"], hover_color=theme.COLORS["danger_hover"])
    theme.style_treeview(tree, "Apps.Treeview")   # ttk widgets follow light/dark and re-style on change

Every COLORS value is a (light, dark) tuple, which customtkinter widgets accept directly. For plain
tk/ttk widgets use theme.pick(COLORS[...]) to get the string for the current appearance mode.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

import customtkinter as ctk

THEME_FILE = Path(__file__).with_name("theme_dark.json")
DEFAULT_APPEARANCE = "Dark"

# Hackaday-ish burnt orange/red accent on a near-black charcoal base.
COLORS: dict[str, tuple[str, str]] = {
    # surfaces
    "bg":            ("gray95", "#121214"),   # window
    "surface":       ("gray90", "#1c1c1f"),   # frames / panels
    "surface_alt":   ("gray85", "#242428"),   # nested panels, headings
    "field":         ("#ffffff", "#161618"),  # text boxes, lists, trees
    "border":        ("gray65", "#34343a"),
    # text
    "text":          ("gray14", "#e6e6e9"),
    "muted_text":    ("gray40", "#9a9aa3"),
    "text_on_accent": ("#ffffff", "#ffffff"),
    "disabled":       ("gray80", "#2c2c31"),   # fill of ANY disabled button (blue, red, ...)
    "disabled_text":  ("gray55", "#6e6e76"),
    # accent
    "accent":        ("#1f6feb", "#1f6feb"),
    "accent_hover":  ("#1858c4", "#1858c4"),
    "accent_text":   ("#1f6feb", "#58a6ff"),  # accent used as text on the surface
    "selection":     ("#1f6feb", "#1858c4"),  # selected rows / tiles
    # semantic
    "danger":        ("#b42318", "#b42318"),  # destructive buttons
    "danger_hover":  ("#8f1c13", "#8f1c13"),
    "danger_text":   ("#b42318", "#ff7a6b"),
    "danger_bg":     ("#f6dddd", "#3a1d1d"),  # banners / tiles that flag a problem
    "warning":       ("#b45309", "#b7791f"),  # attention buttons (e.g. "badge paused")
    "warning_hover": ("#92400e", "#946116"),
    "warning_text":  ("#a35d00", "#f0b050"),
    "success":       ("#2f7d4a", "#2f7d4a"),  # positive buttons
    "success_hover": ("#25633a", "#25633a"),
    "success_text":  ("#1f7a3d", "#4cc27a"),
    # neutral / secondary buttons (was "gray40")
    "neutral":       ("#6b6b73", "#3a3a41"),
    "neutral_hover": ("#55555c", "#4a4a52"),
    # list rows built from buttons
    "row":           ("gray86", "#232327"),
    "row_hover":     ("gray78", "#2e2e33"),
    "tile":          ("gray80", "#26262b"),
    "tile_hover":    ("gray72", "#33333a"),
}


def is_dark() -> bool:
    return ctk.get_appearance_mode() == "Dark"


def pick(color) -> str:
    """Resolve a (light, dark) tuple to the string for the current appearance mode."""
    if isinstance(color, (tuple, list)):
        return color[1] if is_dark() else color[0]
    return color


def apply_theme(config=None) -> None:
    """Set appearance mode from config (default Dark) and load the color theme.
    Call before creating widgets: the color theme only affects widgets created afterwards."""
    mode = (config.get("appearance") if config is not None else None) or DEFAULT_APPEARANCE
    if mode not in ("System", "Light", "Dark"):
        mode = DEFAULT_APPEARANCE
    try:
        ctk.set_default_color_theme(str(THEME_FILE))
    except Exception:  # noqa: BLE001  (missing/broken file: keep customtkinter's default theme)
        pass
    ctk.set_appearance_mode(mode)
    _patch_disabled_buttons()


def _patch_disabled_buttons() -> None:
    """customtkinter keeps a button's fill color when it's disabled, so a disabled red/blue button
    looks clickable. Draw every disabled CTkButton grey instead; its real color comes back
    when it's enabled again. Applies to all buttons app-wide."""
    cls = ctk.CTkButton
    if getattr(cls, "_bm_disabled_patch", False):
        return
    orig_draw = cls._draw

    def _draw(self, no_color_updates=False):
        if getattr(self, "_state", "normal") == tk.DISABLED:
            saved_fg, saved_txt = self._fg_color, getattr(self, "_text_color_disabled", None)
            self._fg_color = COLORS["disabled"]
            if saved_txt is not None:
                self._text_color_disabled = COLORS["disabled_text"]
            try:
                return orig_draw(self, no_color_updates)
            finally:
                self._fg_color = saved_fg
                if saved_txt is not None:
                    self._text_color_disabled = saved_txt
        return orig_draw(self, no_color_updates)

    cls._draw = _draw
    cls._bm_disabled_patch = True


# ------------------------------------------------------------------ ttk / tk widgets
def _on_mode_change(widget, fn) -> None:
    """Call fn() now and whenever the appearance mode changes, until widget is destroyed."""
    def cb(_mode=None):
        try:
            if not widget.winfo_exists():
                raise tk.TclError
            fn()
        except tk.TclError:
            ctk.AppearanceModeTracker.remove(cb)
    ctk.AppearanceModeTracker.add(cb, widget)
    fn()


def _style_ttk(widget, style_name: str, rowheight: int) -> None:
    st = ttk.Style(widget)
    try:
        if st.theme_use() != "clam":
            st.theme_use("clam")  # the macOS "aqua" theme ignores most colors
    except tk.TclError:
        pass
    bg, fg = pick(COLORS["field"]), pick(COLORS["text"])
    sel, head = pick(COLORS["selection"]), pick(COLORS["surface_alt"])
    border = pick(COLORS["border"])
    st.configure(style_name, background=bg, fieldbackground=bg, foreground=fg, rowheight=rowheight,
                 borderwidth=0, bordercolor=border, lightcolor=bg, darkcolor=bg)
    st.map(style_name, background=[("selected", sel)], foreground=[("selected", "#ffffff")])
    st.configure(f"{style_name}.Heading", background=head, foreground=fg, relief="flat",
                 bordercolor=border, lightcolor=head, darkcolor=head)
    st.map(f"{style_name}.Heading", background=[("active", pick(COLORS["row_hover"]))])
    thumb, trough = pick(COLORS["neutral"]), bg
    st.configure("Vertical.TScrollbar", background=thumb, troughcolor=trough, bordercolor=trough,
                 lightcolor=thumb, darkcolor=thumb, arrowcolor=fg, relief="flat")
    st.map("Vertical.TScrollbar", background=[("active", pick(COLORS["neutral_hover"]))])


def style_treeview(tree: ttk.Treeview, style_name: str = "Badge.Treeview", rowheight: int = 22) -> None:
    """Style a ttk.Treeview (plus its scrollbars, row tags and tk.Frame parent) for the current
    appearance mode, and keep it in sync when the mode changes."""
    def restyle():
        _style_ttk(tree, style_name, rowheight)
        tree.configure(style=style_name)
        tree.tag_configure("muted", foreground=pick(COLORS["muted_text"]))
        tree.tag_configure("warn", foreground=pick(COLORS["warning_text"]))
        tree.tag_configure("ok", foreground=pick(COLORS["success_text"]))
        tree.tag_configure("danger", foreground=pick(COLORS["danger_text"]))
        parent = tree.master
        if isinstance(parent, tk.Frame) and not isinstance(parent, ctk.CTkBaseClass):
            parent.configure(bg=pick(COLORS["field"]))
    _on_mode_change(tree, restyle)


def style_listbox(lb: tk.Listbox) -> None:
    """Style a plain tk.Listbox for the current appearance mode and follow mode changes."""
    def restyle():
        lb.configure(bg=pick(COLORS["field"]), fg=pick(COLORS["text"]),
                     selectbackground=pick(COLORS["selection"]), selectforeground="#ffffff",
                     highlightthickness=1, highlightbackground=pick(COLORS["border"]),
                     highlightcolor=pick(COLORS["accent"]), borderwidth=0, relief="flat")
    _on_mode_change(lb, restyle)
