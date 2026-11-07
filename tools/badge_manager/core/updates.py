"""UpdatePoller: check every fork for firmware/app updates at startup and every 15 minutes.

    poller = UpdatePoller(ctx)        # after the tabs exist (they subscribe to "sources.updated")
    poller.start()                    # first check ~2 s later, then every interval_s
    poller.check_now()                # manual trigger (returns False if a check is already running)
    poller.stop()                     # on window close

Each check runs on a plain background thread (ctx.run_task(quiet=True)), never the device worker:
it only touches the local git cache and the GitHub API, never the badge.

    1. sync_remotes()  add a remote for any new fork (GitHub API; token from keyring, else
                       `gh auth token`, else unauthenticated; API errors are logged once and the
                       check carries on with the fetch)
    2. fetch_all()
    3. refs()          with ahead/behind/touched info
    4. apps per ref    (apps_in_ref + blob ids, cached per commit sha so unchanged refs are free)
    5. diff against the snapshot in ~/.badge_manager/update_state.json, save the new snapshot

Then on the Tk thread: publish "sources.updated" (list[Ref], only when some ref moved, or on the
first-ever run) and "updates.found" (dict, see core/context.py), and log a one-line summary.

Concurrency: SourceManager.lock (an RLock) guards sync_remotes/fetch_all. The poller takes it
non-blocking for the whole check, so if a manual fetch from the Firmware tab is running the poll is
skipped (not queued), and a manual fetch started during a poll simply waits for it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
from pathlib import Path

from .config import HOME
from .sources import BADGE_SUBDIR

STATE_FILE = HOME / "update_state.json"
STATE_VERSION = 1
APPS_DIR = f"{BADGE_SUBDIR}/apps"


class UpdatePoller:
    def __init__(self, ctx, interval_s: float = 900, first_delay_s: float = 2.0,
                 state_file: str | Path = STATE_FILE):
        self.ctx = ctx
        self.interval_s = interval_s
        self.first_delay_s = first_delay_s
        self.state_file = Path(state_file)
        self._after_id = None
        self._running = False          # a check is in flight (Tk-thread flag)
        self._stopped = True
        self._gh_tried = False
        self._last_error = None        # last error message logged (log each distinct one once)
        self._app_cache: dict[str, dict] = {}   # commit sha -> {path: {module, title, blob}}
        self.last_result: dict | None = None
        self.last_checked: float | None = None

    # ------------------------------------------------------------ scheduling (Tk thread)
    def start(self) -> None:
        self._stopped = False
        self._schedule(self.first_delay_s)

    def stop(self) -> None:
        self._stopped = True
        if self._after_id is not None:
            try:
                self.ctx.root.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001  (window already gone)
                pass
            self._after_id = None

    @property
    def running(self) -> bool:
        return self._running

    def _schedule(self, delay_s: float) -> None:
        if self._stopped:
            return
        if self._after_id is not None:
            try:
                self.ctx.root.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001
                pass
        self._after_id = self.ctx.root.after(int(delay_s * 1000), self._tick)

    def _tick(self) -> None:
        self._after_id = None
        self.check_now()
        self._schedule(self.interval_s)

    def check_now(self) -> bool:
        """Start a check unless one is already running. Call on the Tk thread. Returns True if started."""
        if self._running:
            return False
        self._running = True
        self.ctx.events.publish("updates.checking", True)
        self.ctx.run_task(self._check, self._done, self._failed, title="Checking forks for updates",
                          quiet=True)
        return True

    def _done(self, res) -> None:
        self._running = False
        self.ctx.events.publish("updates.checking", False)
        if res is None:                                  # skipped: manual fetch in progress
            return
        result, refs, publish_refs = res
        self.last_result, self.last_checked = result, result["checked_at"]
        if publish_refs:
            self.ctx.events.publish("sources.updated", refs)
        self.ctx.events.publish("updates.found", result)
        self.ctx.log(summary_line(result))

    def _failed(self, e) -> None:
        self._running = False
        self.ctx.events.publish("updates.checking", False)
        result = empty_result(time.time(), error=str(e))
        self.last_result, self.last_checked = result, result["checked_at"]
        self.ctx.events.publish("updates.found", result)

    # ------------------------------------------------------------ the check (background thread)
    def _check(self):
        src = self.ctx.sources
        if not src.lock.acquire(blocking=False):
            self.ctx.log("Update check skipped: a fetch is already running")
            return None
        try:
            return self._check_locked(src)
        finally:
            src.lock.release()

    def _check_locked(self, src):
        errors = []
        src.ensure_repo()
        self._ensure_token(src)
        try:
            src.sync_remotes()
        except Exception as e:  # noqa: BLE001  (rate limit, offline, odd API reply)
            errors.append(self._describe_api_error(e))
        try:
            src.fetch_all()
        except Exception as e:  # noqa: BLE001  (some forks deleted/private: git still fetches the rest)
            errors.append(f"git fetch: {str(e)[:200]}")
        refs = src.refs()
        remotes = {}
        for r in src.git("remote").split():
            remotes[r] = src.remote_repo(r)
        apps = self._apps_by_ref(src, refs)

        prev = self._load_state()
        now = time.time()
        error = "; ".join(errors) or None
        result, state = diff_snapshot(prev, remotes, refs, apps, now, error,
                                      changed_files=lambda a, b: changed_files(src, a, b))
        state["app_cache"] = {r.sha: self._app_cache[r.sha] for r in refs if r.sha in self._app_cache}
        self._save_state(state)
        publish_refs = result["baseline"] or bool(result["changed_refs"])

        if error and error != self._last_error:
            self.ctx.log(f"Update check: {error}")
        self._last_error = error
        return result, refs, publish_refs

    # ------------------------------------------------------------ helpers
    def _ensure_token(self, src) -> None:
        if src.token:
            return
        try:
            from .config import get_secret
            tok = get_secret("github_token")
        except Exception:  # noqa: BLE001
            tok = None
        if not tok and not self._gh_tried:
            self._gh_tried = True
            tok = gh_auth_token()
            self._gh_token = tok
        elif not tok:
            tok = getattr(self, "_gh_token", None)
        if tok:
            src.token = tok

    @staticmethod
    def _describe_api_error(e) -> str:
        if isinstance(e, urllib.error.HTTPError):
            if e.code in (403, 429):
                reset = e.headers.get("X-RateLimit-Reset") if e.headers else None
                when = f" until {time.strftime('%H:%M', time.localtime(int(reset)))}" if reset else ""
                return f"GitHub API rate limit{when} (new forks not checked; existing ones fetched)"
            return f"GitHub API HTTP {e.code} (new forks not checked)"
        if isinstance(e, urllib.error.URLError):
            return f"GitHub API unreachable: {e.reason} (new forks not checked)"
        return f"GitHub API: {e} (new forks not checked)"

    def _apps_by_ref(self, src, refs) -> dict[str, dict]:
        """ref name -> {path: {module, title, blob}}. Cached per commit sha (memory + state file)."""
        if not self._app_cache:
            self._app_cache = dict(self._load_state().get("app_cache") or {})
        out = {}
        for r in refs:
            apps = self._app_cache.get(r.sha)
            if apps is None:
                try:
                    blobs = {}
                    for line in src.git("ls-tree", "-r", r.sha, "--", APPS_DIR).splitlines():
                        meta, path = line.split("\t", 1)
                        blobs[path] = meta.split()[2]
                    apps = {a.path: {"module": a.module, "title": a.title, "blob": blobs.get(a.path, "")}
                            for a in src.apps_in_ref(r.sha)}
                except Exception:  # noqa: BLE001  (ref with no apps dir, odd tree, ...)
                    apps = {}
                self._app_cache[r.sha] = apps
            out[r.name] = apps
        return out

    def _load_state(self) -> dict:
        try:
            st = json.loads(self.state_file.read_text())
            return st if st.get("version") == STATE_VERSION else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(self.state_file)


# ---------------------------------------------------------------- pure functions
def gh_auth_token() -> str | None:
    gh = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")
                                     if Path(p).exists()), None)
    if not gh:
        return None
    try:
        cp = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=10)
        tok = cp.stdout.strip()
        return tok if cp.returncode == 0 and tok else None
    except (OSError, subprocess.SubprocessError):
        return None


def changed_files(src, old: str, new: str) -> list[str] | None:
    """Files changed between two commits, or None if the old one is gone (force-push + prune)."""
    out = src.git("diff", "--name-only", old, new, check=False, timeout=60)
    if not out and src.git("cat-file", "-t", old, check=False).strip() != "commit":
        return None
    return out.split()


def empty_result(now: float, error: str | None = None, baseline: bool = False) -> dict:
    return {"checked_at": now, "baseline": baseline, "totals": {"forks": 0, "refs": 0, "apps": 0},
            "new_forks": [], "changed_refs": [], "new_apps": [], "changed_apps": [], "error": error}


def diff_snapshot(prev: dict, remotes: dict, refs, apps: dict, now: float, error, changed_files):
    """Compare the current view with the previous snapshot. Returns (updates.found payload, new state)."""
    # app module -> {blob ids}, titles, which refs carry each blob
    mod_blobs: dict[str, set] = {}
    mod_title: dict[str, str] = {}
    blob_refs: dict[tuple, list] = {}
    for ref_name, files in apps.items():
        for info in files.values():
            m = info["module"]
            mod_blobs.setdefault(m, set()).add(info["blob"])
            if info["title"] and (m not in mod_title or ref_name == "origin/main"):
                mod_title[m] = info["title"]
            blob_refs.setdefault((m, info["blob"]), []).append(ref_name)

    state = {"version": STATE_VERSION, "checked_at": now, "remotes": sorted(remotes),
             "refs": {r.name: r.sha for r in refs},
             "apps": {m: sorted(b) for m, b in mod_blobs.items()}}
    res = empty_result(now, error, baseline=not prev)
    res["totals"] = {"forks": len([r for r in remotes if r != "origin"]), "refs": len(refs),
                     "apps": len(mod_blobs)}
    if not prev:
        return res, state

    old_remotes = set(prev.get("remotes") or [])
    res["new_forks"] = [{"remote": r, "repo": remotes[r]} for r in sorted(remotes) if r not in old_remotes]

    old_refs = prev.get("refs") or {}
    for r in refs:
        old = old_refs.get(r.name)
        if old == r.sha:
            continue
        files = changed_files(old, r.sha) if old else None
        if files is None:
            files = list(r.touched)
        res["changed_refs"].append({
            "name": r.name, "old": old, "new": r.sha, "is_new": old is None,
            "subject": r.subject, "author": r.author, "date": r.date,
            "ahead": r.ahead, "behind": r.behind,
            "firmware_changed": any(f.startswith(BADGE_SUBDIR + "/") for f in files),
            "files": files[:200]})

    old_apps = {m: set(b) for m, b in (prev.get("apps") or {}).items()}
    for m in sorted(mod_blobs):
        if m not in old_apps:
            refs_with = sorted({n for b in mod_blobs[m] for n in blob_refs[(m, b)]})
            res["new_apps"].append({"module": m, "title": mod_title.get(m, ""), "refs": refs_with})
        else:
            fresh = mod_blobs[m] - old_apps[m]
            if fresh:
                refs_with = sorted({n for b in fresh for n in blob_refs[(m, b)]})
                res["changed_apps"].append({"module": m, "title": mod_title.get(m, ""), "refs": refs_with})
    return res, state


def update_count(result: dict) -> int:
    return sum(len(result.get(k) or []) for k in ("new_forks", "changed_refs", "new_apps", "changed_apps"))


def summary_line(result: dict) -> str:
    t = result["totals"]
    if result["baseline"]:
        s = (f"Update check: baseline saved ({t['forks']} forks, {t['refs']} branches, "
             f"{t['apps']} apps)")
    elif not update_count(result):
        s = f"Update check: no changes ({t['forks']} forks, {t['refs']} branches, {t['apps']} apps)"
    else:
        parts = []
        for key, label in (("new_forks", "new fork"), ("changed_refs", "updated branch"),
                           ("new_apps", "new app"), ("changed_apps", "changed app")):
            n = len(result[key])
            if n:
                parts.append(f"{n} {label}{'es' if n > 1 and label.endswith('ch') else 's' if n > 1 else ''}")
        fw = sum(1 for r in result["changed_refs"] if r["firmware_changed"])
        s = "Update check: " + ", ".join(parts) + (f" ({fw} touch firmware)" if fw else "")
    if result.get("error"):
        s += f"  [warning: {result['error']}]"
    return s
