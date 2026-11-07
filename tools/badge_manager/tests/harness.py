"""Headless-ish smoke test: build the real App (or one tab) against the fake badge, pump events, exit.

    BADGE_FAKE=1 .venv/bin/python tests/harness.py                # whole app
    BADGE_FAKE=1 .venv/bin/python tests/harness.py tabs.apps AppsTab  # one tab
Exits non-zero on any exception. Window is created but closed after a few seconds.
"""
import os, sys, time, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("BADGE_FAKE", "1")
import importlib
import customtkinter as ctk

errors = []
def report(exc, val, tb):
    errors.append("".join(traceback.format_exception(exc, val, tb)))
ctk.CTk.report_callback_exception = lambda self, *a: report(*a)

def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)

if len(sys.argv) == 3:
    from core.context import AppContext
    root = ctk.CTk(); root.geometry("1200x800")
    logs = []
    ctx = AppContext(root, logs.append, logs.append)
    tab = getattr(importlib.import_module(sys.argv[1]), sys.argv[2])(root, ctx)
    tab.pack(fill="both", expand=True)
    pump(root, 1); tab.on_show(); pump(root, 6)
    print("\n".join(str(l) for l in logs[-25:]))
else:
    import app
    root = app.App()
    pump(root, 1)
    for name in list(root.tabs):
        root.tabview.set(name); root._tab_changed(); pump(root, 3)
root.destroy()
if errors:
    print("UI EXCEPTIONS:\n" + "\n".join(errors)); sys.exit(1)
print("SMOKE OK")
