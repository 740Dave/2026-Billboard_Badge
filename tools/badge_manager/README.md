# Billboard Badge Manager

A desktop app (customtkinter) for managing the Hackaday Supercon 2026 Billboard Badge over USB: track the community forks, install firmware and apps, manage artwork and Wi-Fi.

## Run it

```bash
cd tools/badge_manager
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

To try it **without a badge**, use a simulated badge (a temp folder seeded from upstream):

```bash
BADGE_FAKE=1 .venv/bin/python app.py
```

**Badge checklist:**
- Power switch **ON**. It disables the 3.3 V regulator, so with it off nothing appears even on USB.
- USB-C **data** cable.
- The badge shows up as `/dev/cu.usbmodem…` and is auto-selected in the top bar. Click **Connect**.

## Layout

| Area | What it's for |
|---|---|
| **Top bar** (always visible) | Port picker, Connect, **Hard reset**, connection state with firmware version and voltage, busy indicator |
| **Device** | Status (RAM, storage, USB or battery). File browser (view, download, upload, delete). Full backup and restore. **Lab**: LED test, SAO/I²C scan, voltage, run a Python snippet, "Restore badge UI" |
| **Firmware** | Upstream and every fork branch as a list: newest changes first, ahead/behind counts, whether each touches badge files. Unique commits and diffs, a **compare with badge** view (`*` changed, `+` new, `-` extra), and **Install** with an automatic backup. The *Runtime* page (advanced) covers esptool flash backup, flashing MicroPython, and factory restore |
| **Apps** | What's on the badge, with LED menu positions. Enable/disable (renames to `_name.py`, which the badge skips) and remove. A catalog of every app in every fork plus local folders, with dependency-aware install that pulls in needed `lib/` and `/data` files and never overwrites core libs. **Start at boot**: shows whether the badge resumes the last app (`/data/boot.json`, `/data/last_app.json`; needs the resume-last-app firmware) and sets it |
| **Artwork** | Thumbnails of `/data/artwork`: reorder, delete, show next. Add images with a live white/black/red preview (fit/fill, dither, red, rotate), convert, and upload. Installs the Artwork app if it's missing |
| **Maker Detector** | Your maker handle (read-only, from Settings → User Settings), the broadcast switch, writing `/data/owner.json` (+ legacy `/data/maker.json`), installing the badge app. Optional, experimental Mac-side BLE scan in a helper process |
| **Settings** | **User Settings**: maker handle (a handle, not your legal name), what you make, broadcast on/off, Save & send to badge. **Wi-Fi networks** master/detail: profiles in priority order, passwords in the macOS Keychain, write and read `/data/wifi.json` (kcriqui's `lib/wifi.py` format), test on the badge. **General**: appearance, backup folder, backup-before-install, local app folders, GitHub token |
| **Log** (bottom) | Every badge command and result. Hide or clear as needed |

## Safety rules built in

- **One badge operation at a time.** Every serial operation goes through one worker queue, so tabs can't collide on the port.
- **No soft resets.** LVGL crashes on soft reset, so every `mpremote` call uses `resume`, and changes finish with a hard reset.
- **`/data` is protected.** Firmware installs never delete it, and deletes on `/`, `/apps`, `/lib` and `/data` are refused.
- **Backups before installs.** A backup runs before each firmware install (on by default), and any app file about to be overwritten is saved locally first.
- **Flashing is gated.** esptool flashing requires bootloader mode and typing a confirmation word.
- **Secrets stay out of files.** Passwords and the GitHub token live only in the Keychain (service `badge-manager`), never in `~/.badge_manager/config.json` or the log.

## Code structure

```
app.py               window: connection bar, tab host, log console
core/
  context.py         AppContext passed to every tab: config, badge, sources, log, events,
                     run_task / run_device_task (background threads), ui() (thread-safe Tk calls)
  badge.py           BadgeService: mpremote/esptool wrapper (info, listdir, hashes, read/write, push_tree, backup, reset)
  fakebadge.py       simulated badge used when BADGE_FAKE=1
  sources.py         SourceManager: git cache with every fork as a remote (refs, commits, diffs, export, app discovery)
  config.py          ~/.badge_manager/config.json + Keychain secrets
  artwork_convert.py image -> 960x672 3-colour palette PNG (the badge decoder's exact format)
tabs/                one file per tab, each a BaseTab subclass (TITLE, build(), on_show())
tests/harness.py     smoke test: BADGE_FAKE=1 .venv/bin/python tests/harness.py [tabs.x XTab]
```

**Adding a tab:** create `tabs/foo.py` with `class FooTab(BaseTab)`, then add it to `TABS` in `app.py`. A tab that fails to import shows its error instead of crashing the app.

**Fork data:** when run from a clone of this repo the app uses that clone as its git cache (otherwise `~/.badge_manager/repo`), with every fork added as a git remote. **Firmware → "Check GitHub for new forks + fetch"** adds new forks and pulls updates. Without a token the GitHub API allows 60 calls an hour; add a token in Settings → General to raise that.

## Status

- All five tabs pass smoke tests against the simulated badge, and each tab's builder ran scripted tests of its main actions there.
- On the real badge, only the read paths have been used so far: port detection, info and file listing, plus a hard reset.
- Nothing has been installed or written to a real badge yet.
