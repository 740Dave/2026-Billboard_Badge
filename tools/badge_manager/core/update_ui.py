"""UpdateIndicator: small top-bar widget for the background fork poller (core.updates).

    "Forks checked 3 min ago · 2 updates"   [Check now]

Clicking the text opens a window listing what changed (new forks, updated branches, new/changed
apps) since the app started; opening it marks the updates as seen. Subscribes to "updates.found"
and "updates.checking".
"""

from __future__ import annotations

import time

import customtkinter as ctk

from .updates import update_count

ACCENT = "#c8641e"


def ago(ts: float, now: float | None = None) -> str:
    s = max(0, int((now or time.time()) - ts))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


class UpdateIndicator(ctk.CTkFrame):
    def __init__(self, master, ctx, poller=None, **kw):
        kw.setdefault("fg_color", "transparent")
        super().__init__(master, **kw)
        self.ctx = ctx
        self.poller = poller
        self.history: list[dict] = []     # every non-empty result this session, oldest first
        self.unseen = 0
        self.last: dict | None = None
        self.checking = False
        self._win = None
        self.label = ctk.CTkLabel(self, text="Forks: not checked yet", text_color="gray60", cursor="hand2")
        self.label.pack(side="left", padx=(4, 6))
        self.label.bind("<Button-1>", lambda _e: self.show_details())
        self.btn = ctk.CTkButton(self, text="Check now", width=86, command=self.check_now)
        self.btn.pack(side="left", padx=(0, 4))
        ctx.events.subscribe("updates.found", self._on_found)
        ctx.events.subscribe("updates.checking", self._on_checking)
        self._timer = self.after(60_000, self._minute)

    # -------------------------------------------------------------- events
    def check_now(self) -> None:
        if self.poller is not None and not self.poller.check_now():
            self.ctx.status("Fork check already running")

    def _on_checking(self, on) -> None:
        self.checking = bool(on)
        self.btn.configure(state="disabled" if on else "normal")
        self.refresh()

    def _on_found(self, result) -> None:
        self.last = result
        if not result.get("baseline") and update_count(result):
            self.history.append(result)
            self.unseen += update_count(result)
        self.refresh()

    def _minute(self) -> None:
        self.refresh()
        self._timer = self.after(60_000, self._minute)

    def destroy(self):
        try:
            self.after_cancel(self._timer)
        except Exception:  # noqa: BLE001
            pass
        super().destroy()

    # -------------------------------------------------------------- display
    def text(self) -> tuple[str, str]:
        if self.checking:
            base = "Checking forks..."
        elif self.last is None:
            return "Forks: not checked yet", "gray60"
        else:
            base = f"Forks checked {ago(self.last['checked_at'])}"
        if self.unseen:
            return f"{base} · {self.unseen} update{'s' if self.unseen != 1 else ''}", ACCENT
        if self.last and self.last.get("error") and not self.checking:
            return f"{base} · check incomplete", ACCENT
        if self.last and self.last.get("baseline") and not self.checking:
            return f"{base} · baseline saved", "gray60"
        return f"{base} · no updates" if self.last else base, "gray60"

    def refresh(self) -> None:
        txt, color = self.text()
        self.label.configure(text=txt, text_color=color)

    def show_details(self) -> None:
        if self._win is not None and self._win.winfo_exists():
            self._win.destroy()
        self.unseen = 0
        self.refresh()
        win = self._win = ctk.CTkToplevel(self)
        win.title("Fork updates")
        win.geometry("620x460")
        win.after(50, win.lift)
        box = ctk.CTkTextbox(win, font=ctk.CTkFont(family="Menlo", size=12), wrap="word")
        box.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        box.insert("1.0", self.details_text())
        box.configure(state="disabled")
        ctk.CTkButton(win, text="Close", width=80, command=win.destroy).pack(pady=(0, 10))

    def details_text(self) -> str:
        lines = []
        if self.last:
            t = self.last["totals"]
            lines.append(f"Last check: {time.strftime('%H:%M:%S', time.localtime(self.last['checked_at']))}"
                         f"  ({t['forks']} forks, {t['refs']} branches, {t['apps']} apps)")
            if self.last.get("error"):
                lines.append(f"Warning: {self.last['error']}")
            lines.append("")
        if not self.history:
            lines.append("No updates found since the app started.")
        for res in reversed(self.history):
            lines.append(f"== {time.strftime('%H:%M', time.localtime(res['checked_at']))} ==")
            for f in res["new_forks"]:
                lines.append(f"  new fork      {f['remote']}  ({f['repo']})")
            for r in res["changed_refs"]:
                tag = "new branch   " if r["is_new"] else "updated      "
                fw = "  [firmware]" if r["firmware_changed"] else ""
                lines.append(f"  {tag} {r['name']}{fw}")
                lines.append(f"                {r['subject'][:70]}  - {r['author']}")
            for a in res["new_apps"]:
                lines.append(f"  new app       {a['module']}  {a['title'][:40]}")
                lines.append(f"                in {', '.join(a['refs'][:4])}{' ...' if len(a['refs']) > 4 else ''}")
            for a in res["changed_apps"]:
                lines.append(f"  changed app   {a['module']}  {a['title'][:40]}")
                lines.append(f"                in {', '.join(a['refs'][:4])}{' ...' if len(a['refs']) > 4 else ''}")
            lines.append("")
        return "\n".join(lines)
