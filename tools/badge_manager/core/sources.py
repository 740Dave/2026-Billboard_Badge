"""SourceManager: the official repo plus every community fork, as one local git cache.

Every fork is a git remote in a single repository, so comparing and exporting any
fork/branch is a local git operation (fast, and it doesn't use up the GitHub API quota).

All methods are blocking (network or disk). Run them through ``ctx.run_task``.
"""

from __future__ import annotations

import json
import re
import subprocess
import tarfile
import io
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

UPSTREAM = "Hack-a-Day/2026-Billboard_Badge"
# The git remote that points at the official Hack-a-Day repo. In a plain clone of upstream
# that's "origin"; in a clone of a personal fork, origin is the fork and upstream is usually
# "upstream". SourceManager detects it (see detect_upstream_remote) and updates these.
UPSTREAM_REMOTE = "origin"
UPSTREAM_REF = "origin/main"


def detect_upstream_remote(repo_dir: str | Path) -> str:
    """Name of the remote whose URL contains Hack-a-Day/2026-Billboard_Badge (fallback "origin")."""
    try:
        cp = subprocess.run(["git", "-C", str(repo_dir), "remote", "-v"], capture_output=True,
                            text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return "origin"
    if cp.returncode != 0:
        return "origin"
    names = []
    for line in cp.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and UPSTREAM.lower() in parts[1].lower() and parts[0] not in names:
            names.append(parts[0])
    if not names:
        return "origin"
    return "origin" if "origin" in names else names[0]


def _set_upstream_remote(remote: str) -> None:
    global UPSTREAM_REMOTE, UPSTREAM_REF
    UPSTREAM_REMOTE = remote
    UPSTREAM_REF = f"{remote}/main"
BADGE_SUBDIR = "firmware/badge"          # mirrors the badge's / filesystem


class SourceError(Exception):
    pass


@dataclass
class Ref:
    name: str              # "kcriqui/main", "origin/main"
    remote: str            # "kcriqui"
    branch: str            # "main"
    sha: str
    date: str              # ISO date of tip commit
    subject: str
    author: str
    ahead: int = 0         # commits not in upstream
    behind: int = 0        # upstream commits missing here
    touched: list[str] = field(default_factory=list)   # files changed vs upstream (merge-base)

    @property
    def is_upstream(self) -> bool:
        return self.remote == UPSTREAM_REMOTE

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.remote_repo}/tree/{self.branch}"

    remote_repo: str = ""


@dataclass
class Commit:
    sha: str
    date: str
    author: str
    subject: str


@dataclass
class AppInfo:
    ref: str               # where it comes from (git ref or "local:<path>")
    path: str              # "firmware/badge/apps/xkcd.py" or local file path
    module: str            # "xkcd"
    title: str             # first docstring line
    classes: list[str]
    lib_deps: list[str]    # "lib/wifi.py" etc. imported from lib.*
    data_refs: list[str]   # "/data/..." string literals found in the source


class SourceManager:
    def __init__(self, repo_dir: str | Path, log=print, github_token: str | None = None):
        self.repo = Path(repo_dir)
        self.log = log
        self.token = github_token
        self.detect_upstream()

    def detect_upstream(self) -> str:
        """Find the official repo's remote and point UPSTREAM_REMOTE/UPSTREAM_REF at it."""
        _set_upstream_remote(detect_upstream_remote(self.repo) if (self.repo / ".git").exists() else "origin")
        return UPSTREAM_REMOTE

    @property
    def upstream_ref(self) -> str:
        return UPSTREAM_REF

    # ---------------------------------------------------------------- git
    def git(self, *args: str, timeout: float = 300, check: bool = True, binary: bool = False):
        cp = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True,
                            text=not binary, timeout=timeout)
        if check and cp.returncode != 0:
            err = cp.stderr if not binary else cp.stderr.decode(errors="replace")
            raise SourceError(f"git {args[0]} failed: {err.strip()[:300]}")
        return cp.stdout

    def ensure_repo(self) -> None:
        if (self.repo / ".git").exists():
            return
        self.log(f"Cloning {UPSTREAM} into {self.repo} ...")
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", f"https://github.com/{UPSTREAM}.git", str(self.repo)],
                       check=True, capture_output=True, timeout=600)
        self.detect_upstream()

    # ------------------------------------------------------------- forks
    def github_forks(self) -> list[dict]:
        """Fork list from the GitHub API (60 requests/hour without a token)."""
        forks, page = [], 1
        while True:
            url = f"https://api.github.com/repos/{UPSTREAM}/forks?per_page=100&page={page}&sort=newest"
            req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": "badge-manager"})
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            with urllib.request.urlopen(req, timeout=30) as r:
                batch = json.load(r)
            forks += batch
            if len(batch) < 100:
                return forks
            page += 1

    def sync_remotes(self) -> int:
        """Add a git remote for each fork that doesn't have one yet. Returns how many were added."""
        existing = set(self.git("remote").split())
        added = 0
        for f in self.github_forks():
            owner = f["owner"]["login"]
            if owner not in existing:
                self.git("remote", "add", owner, f["clone_url"])
                added += 1
        return added

    def fetch_all(self) -> str:
        return self.git("fetch", "--all", "--prune", "--jobs=8", timeout=900)

    def remote_repo(self, remote: str) -> str:
        url = self.git("remote", "get-url", remote, check=False).strip()
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
        return m.group(1) if m else url

    # --------------------------------------------------------------- refs
    def refs(self, with_changes: bool = True) -> list[Ref]:
        fmt = "%(refname:short)|%(objectname)|%(committerdate:iso-strict)|%(authorname)|%(subject)"
        out = self.git("for-each-ref", f"--format={fmt}", "refs/remotes")
        refs = []
        for line in out.splitlines():
            name, sha, date, author, subject = line.split("|", 4)
            if name.endswith("/HEAD") or "/" not in name:
                continue
            remote, branch = name.split("/", 1)
            r = Ref(name, remote, branch, sha, date, subject, author)
            if with_changes:
                counts = self.git("rev-list", "--left-right", "--count", f"{UPSTREAM_REF}...{name}").split()
                r.behind, r.ahead = int(counts[0]), int(counts[1])
                if r.ahead:
                    r.touched = self.git("diff", "--name-only", f"{UPSTREAM_REF}...{name}").split()
            refs.append(r)
        # upstream first, then forks with changes (newest first, timezone-aware), then unchanged forks
        from datetime import datetime
        when = lambda r: datetime.fromisoformat(r.date).timestamp()  # noqa: E731
        ups = [r for r in refs if r.is_upstream]
        changed = sorted([r for r in refs if not r.is_upstream and r.ahead], key=when, reverse=True)
        same = sorted([r for r in refs if not r.is_upstream and not r.ahead], key=lambda r: r.name.lower())
        return ups + changed + same

    def log_commits(self, ref: str, n: int = 50, only_unique: bool = True) -> list[Commit]:
        rng = [ref, "--not", UPSTREAM_REF] if only_unique and ref != UPSTREAM_REF else [ref]
        out = self.git("log", f"-{n}", "--format=%H|%ad|%an|%s", "--date=short", *rng)
        return [Commit(*l.split("|", 3)) for l in out.splitlines()]

    def diff_stat(self, ref: str, base: str | None = None) -> str:
        base = base or UPSTREAM_REF
        return self.git("diff", "--stat=100", f"{base}...{ref}")

    def show_file(self, ref: str, path: str) -> bytes:
        return self.git("show", f"{ref}:{path}", binary=True)

    def ls_tree(self, ref: str, path: str = BADGE_SUBDIR) -> list[str]:
        return self.git("ls-tree", "-r", "--name-only", ref, "--", path).split()

    def export_badge_tree(self, ref: str, dest: str | Path) -> Path:
        """Extract <ref>:firmware/badge into dest (dest then mirrors the badge's / filesystem)."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        data = self.git("archive", "--format=tar", ref, BADGE_SUBDIR, binary=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            for m in tf.getmembers():
                if not m.name.startswith(BADGE_SUBDIR + "/"):
                    continue
                m.name = m.name[len(BADGE_SUBDIR) + 1:]
                if m.name:
                    tf.extract(m, dest, filter="data")
        return dest

    # ---------------------------------------------------------------- apps
    def apps_in_ref(self, ref: str) -> list[AppInfo]:
        apps = []
        for path in self.ls_tree(ref, f"{BADGE_SUBDIR}/apps"):
            name = Path(path).name
            if not name.endswith(".py") or name.startswith("_") or name in ("base_app.py", "template.py"):
                continue
            src = self.show_file(ref, path).decode(errors="replace")
            apps.append(parse_app(src, ref=ref, path=path))
        return apps


def parse_app(src: str, ref: str, path: str) -> AppInfo:
    """Pull title, BaseApp subclasses and dependencies out of an app's source."""
    doc = re.match(r'\s*(?:#.*\n\s*)*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', src, re.S)
    title = (doc.group(1).strip().splitlines() or [""])[0] if doc else ""
    classes = re.findall(r"^class\s+(\w+)\s*\(\s*(?:\w+\.)?\w*App\w*\s*\)", src, re.M)
    libs = {m for m in re.findall(r"^\s*(?:from|import)\s+lib\.(\w+)", src, re.M)}
    for group in re.findall(r"^\s*from\s+lib\s+import\s+\(?([\w ,]+)\)?", src, re.M):
        libs |= {g.strip().split(" as ")[0] for g in group.split(",") if g.strip()}
    libs = sorted(f"lib/{m}.py" for m in libs)
    data = sorted(set(re.findall(r"""["'](/data/[^"'{}]+)["']""", src)))
    return AppInfo(ref, path, Path(path).stem, title, classes, libs, data)
