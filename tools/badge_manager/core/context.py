"""AppContext: the one object every tab receives.

    ctx.config      Config (persistent settings; secrets via core.config.get_secret/set_secret)
    ctx.badge       BadgeService (blocking; only call inside run_device_task)
    ctx.sources     SourceManager (blocking; only call inside run_task)
    ctx.log(msg)    append to the shared log console (thread-safe)
    ctx.status(msg) one-line status in the bottom bar (thread-safe)
    ctx.events      tiny pub/sub; callbacks always run on the Tk main thread

Threading model (Tk is single-threaded):
    ctx.run_task(fn, on_done=None, on_error=None, title="...", quiet=False)
        runs fn() on a background thread; on_done(result) / on_error(exc) run on the Tk thread.
        quiet=True: no busy spinner / status-bar text (for background polling); errors still log.
    ctx.run_device_task(fn, ...)
        same, but on the single DEVICE worker so badge operations never overlap
        (the serial port is exclusive). Also shows a busy indicator.
    ctx.ui(fn, *args)   schedule fn on the Tk thread from any thread (e.g. progress updates).

Standard events (publish with ctx.events.publish(name, payload)):
    "port.changed"      payload: port str | None
    "badge.connected"   payload: info dict (from BadgeService.info)
    "badge.disconnected"
    "badge.files_changed"   payload: list of changed badge paths (after any push/remove)
    "sources.updated"   payload: list[Ref]
    "busy"              payload: (bool busy, str title)
    "updates.checking"  payload: bool. True while the background UpdatePoller (core.updates) checks forks.
    "updates.found"     payload: dict from UpdatePoller after each check:
                        {checked_at: float epoch, baseline: bool (first-ever run, lists empty),
                         totals: {forks, refs, apps}, new_forks: [{remote, repo}],
                         changed_refs: [{name, old, new, is_new, subject, author, date, ahead, behind,
                                         firmware_changed: bool, files: [paths]}],
                         new_apps: [{module, title, refs}], changed_apps: [{module, title, refs}],
                         error: str | None (GitHub API/fetch problem; check still ran what it could)}
    "profile.changed"   payload: config "profile" dict, after it is saved (Settings > User Settings, wizard)
    "badge.ui_interrupted"  payload: bool. True after any badge command (main.py is stopped, the
                        badge menu is frozen) until a hard reset. The top bar offers the reset.
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections import defaultdict

from .badge import BadgeService
from .config import Config
from .sources import SourceManager


class Events:
    def __init__(self, ui_call):
        self._subs = defaultdict(list)
        self._ui = ui_call

    def subscribe(self, name: str, cb) -> None:
        self._subs[name].append(cb)

    def publish(self, name: str, payload=None) -> None:
        for cb in list(self._subs[name]):
            self._ui(cb, payload)


class AppContext:
    def __init__(self, root, log_sink, status_sink):
        self.root = root
        self._ui_q: queue.Queue = queue.Queue()
        root.after(25, self._drain_ui)
        self._log_sink = log_sink
        self._status_sink = status_sink
        self.config = Config()
        self.events = Events(self.ui)
        import os
        if os.environ.get("BADGE_FAKE"):
            from .fakebadge import FakeBadgeService
            self.badge = FakeBadgeService(log=self.log, repo_dir=self.config.get("repo_dir"))
        else:
            self.badge = BadgeService(self.config.get("port"), log=self.log)
        self.sources = SourceManager(self.config.get("repo_dir"), log=self.log)
        self.badge.on_interrupt_change = lambda v: self.events.publish("badge.ui_interrupted", v)
        self._device_q: queue.Queue = queue.Queue()
        self._busy = 0
        threading.Thread(target=self._device_worker, daemon=True, name="device-worker").start()

    # ------------------------------------------------------------ helpers
    def ui(self, fn, *args) -> None:
        """Run fn(*args) on the Tk thread. Safe from any thread: calls are queued and the Tk
        thread drains the queue every 25 ms (Tk itself must only be touched from its own thread)."""
        self._ui_q.put((fn, args))

    def _drain_ui(self) -> None:
        try:
            while True:
                fn, args = self._ui_q.get_nowait()
                try:
                    fn(*args)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        except queue.Empty:
            pass
        try:
            self.root.after(25, self._drain_ui)
        except Exception:  # noqa: BLE001  (window destroyed)
            pass

    def log(self, msg: str) -> None:
        self.ui(self._log_sink, str(msg))

    def status(self, msg: str) -> None:
        self.ui(self._status_sink, str(msg))

    def _set_busy(self, delta: int, title: str) -> None:
        self._busy += delta
        self.events.publish("busy", (self._busy > 0, title))

    # -------------------------------------------------------------- tasks
    def _wrap(self, fn, on_done, on_error, title, quiet=False):
        def run():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                self.log(f"ERROR in {title}: {e}")
                self.log(traceback.format_exc(limit=3))
                if not quiet:
                    self.status(f"{title}: failed - {e}")
                if on_error:
                    self.ui(on_error, e)
            else:
                if on_done:
                    self.ui(on_done, res)
            finally:
                if not quiet:
                    self.ui(self._set_busy, -1, title)
        return run

    def run_task(self, fn, on_done=None, on_error=None, title: str = "task", quiet: bool = False) -> None:
        if not quiet:
            self._set_busy(+1, title)
            self.status(f"{title}...")
        threading.Thread(target=self._wrap(fn, on_done, on_error, title, quiet), daemon=True).start()

    def run_device_task(self, fn, on_done=None, on_error=None, title: str = "badge") -> None:
        if not self.badge.port:
            self.status("Select the badge's serial port first (top bar)")
            if on_error:
                self.ui(on_error, RuntimeError("no port"))
            return
        self._set_busy(+1, title)
        self.status(f"{title}...")
        self._device_q.put(self._wrap(fn, on_done, on_error, title))

    def _device_worker(self) -> None:
        while True:
            job = self._device_q.get()
            job()

    # ---------------------------------------------------------------- port
    def set_port(self, port: str | None) -> bool:
        """Switch serial port. Refused while badge tasks are queued/running (they'd hit the new port)."""
        if port != self.badge.port and self._busy > 0:
            self.status("Wait for the running badge task to finish before changing port")
            return False
        self.badge.port = port
        self.config.set("port", port)
        self.events.publish("port.changed", port)
        return True
