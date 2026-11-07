"""Maker Detector: counts other Billboard Badges nearby over Bluetooth LE, and shows their names.

Every badge with this app installed (and broadcasting turned on) sends a tiny BLE advert:
    flags 02 01 06 | manufacturer data: FF FF "SC26" ver(1) id(3) name(0-16 ASCII)
(The over-the-air format is unchanged from the earlier version of this app, so both see each other.)
The app listens for those adverts while it's the active app and shows who's around.

Identity (read on boot and each time the app is opened):
  /data/owner.json  {"maker_handle": "dave", "makes": "robots", "broadcast": true}
      the owner profile (Badge Manager writes it). Read first.
  /data/maker.json  {"id": "a1b2c3", "name": "Dave", "makes": "robots", "broadcast": true}
      legacy file of this app; fallback for each value missing from owner.json, and home of
      the badge id when lib.presence is not installed.
  - the handle is broadcast (clipped to 16 printable ASCII chars).
  - makes (max 64 chars) is shown on your own screen only, never broadcast.

If the firmware has lib/presence.py (BLE presence broadcast), that module owns the badge id and
the advertising (so we never advertise twice); this app only scans, and B calls
presence.set_broadcast(). Without it, the app advertises itself in the same format.

Controls while running:
  User A  redraw the screen now
  User B  turn broadcasting on/off (remembered)
LED bar: one LED per badge seen in the last minute (green = near, yellow = same room, blue = far).

Advertising starts at boot (apps are created at boot), so your badge is visible to others even
while you're using a different app. Scanning only runs while this app is open (each scan burst
is short, and is only re-armed from run(), which only runs while the app is in front).

Robustness notes (crowded RF, e.g. a conference hall):
  - the BLE IRQ handler does no dict work and almost no allocation for non-matching adverts;
  - matches are queued (bounded) and processed in run(), not in the IRQ;
  - "devices around" is an allocation-free approximate count, reset every scan burst;
  - low scan duty cycle (30 ms every 100 ms), re-armed only after scan-done, with a watchdog.

By 740Dave, Hackaday Supercon 2026. Written with Claude.
"""

import gc
import json
import os
import time

import bluetooth
import lvgl
import ubinascii

from apps.base_app import BaseApp
from lib.button import UserA, UserB
from lib.epd import display
from lib.led_bar import leds

try:
    from lib import presence as _presence     # optional upstream module (PR "ble-presence")
except Exception:                            # ImportError, or a broken module: never crash
    _presence = None
try:
    from lib.owner import owner as _owner     # optional upstream module (PR "owner-profile")
except Exception:
    _owner = None

CONFIG = "/data/maker.json"
OWNER = "/data/owner.json"
OLD_CONFIG = "/data/nerd.json"  # migration only: config file of the app's previous name
MAGIC = b"SC26"
COMPANY = b"\xff\xff"          # 0xFFFF = "no company / testing" per the Bluetooth SIG
VERSION = 1
NAME_MAX = 16
HANDLE_MAX = 40                # shown on screen; the broadcast copy is clipped to NAME_MAX
MAKES_MAX = 64
ADV_MAX = 31                   # legacy advertising payload limit

ADV_INTERVAL_US = 400_000      # advertise every 400 ms
SCAN_MS = 8_000                # each scan burst; re-armed from run() while the app is open
SCAN_INTERVAL_US = 100_000
SCAN_WINDOW_US = 30_000        # listen 30% of the time while open
SCAN_WATCHDOG_MS = SCAN_MS + 5_000
SEEN_S = 60                    # "in range" = heard within this many seconds
FORGET_S = 300
REDRAW_MIN_S = 25              # e-paper refresh takes ~30 s; don't redraw more often
MAX_PEERS = 64                 # hard cap on remembered badges
MAX_PENDING = 32               # hard cap on matched adverts queued by the IRQ
SHOW_ROWS = 16

_IRQ_SCAN_RESULT = 5
_IRQ_SCAN_DONE = 6

# Minimum manufacturer AD length byte: type(1) + company(2) + magic(4) + ver(1) + id(3)
_MFR_MIN_LEN = 1 + 2 + 4 + 1 + 3

_BITS = 1024                   # bitmap size for the approximate "devices around" count
_SEP = "  •  "            # bullet; included in LVGL's built-in Montserrat fonts

# Hackaday "Jolly Wrencher", 92x92, 1 bit per pixel (MONO_HLSB: MSB = leftmost pixel, rows
# padded to 12 bytes, 1 = black). Drawn with framebuf.blit() straight into display.black
# after LVGL has rendered, so it needs no extra file, no PNG decoding and no LVGL heap.
LOGO_W = 92
LOGO_H = 92
LOGO_X = 24
LOGO_Y = 14
TEXT_X = LOGO_X + LOGO_W + 20  # title and "Broadcasting as" line start right of the logo
_LOGO_HEX = (
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "007c0000000000000003e000003f800000000000001fc000001fe00000000000007f8000000ff0000000000000ff0000"
    "0007f8000000000001fe00000003f8000000000001fc00000000fc000000000003f000000000fc000000000007f00000"
    "0001fe000000000007f800000003fe000000000007fc00008007fe000000000007fe0010c00ffe000000000007ff0030"
    "e01ffe000000000007ff8070f83ffe000000000007ffc1f0fc7fff00000000000fffe3e07e7fff80000000001fffe7e0"
    "7fffffe0000000007fffffe03ffffff000000000ffffffc03ffffff800000001ffffffc01ffffffc003fc003ffffff80"
    "0ffffffe01fff807ffffff0007ffffff0ffffe0ffffffe0001fffffe1fffff87fffff8000000fffc7fffffe3fff00000"
    "00007ff8fffffff1ffe0000000003ff1fffffff8ffc0000000001ff3fffffffcff800000000007e7fffffffe7e000000"
    "000003c7fffffffe3c000000000001cfffffffff380000000000009fffffffff900000000000001fffffffff80000000"
    "0000003fffffffffc00000000000003fffffffffc00000000000003fffffffffc00000000000007fffffffffe0000000"
    "0000007fffffffffe00000000000007fc1fff83fe00000000000007f807fe01fe0000000000000ff003fc00fe0000000"
    "000000fe003fc007f0000000000000fc001f8003f0000000000000fc003fc003f0000000000000fc003fc003f0000000"
    "000000fc007fe003f0000000000000fc01fff803f0000000000000fc0fffff03f00000000000007e1fffff87e0000000"
    "0000007e1fffff87e00000000000007fbfffffdfe00000000000007ffff9ffffe00000000000003ffff8ffffc0000000"
    "0000003ffff0ffffc00000000000003ffff0ffffc00000000000011ffff0ffff880000000000079ffff6ffff9e000000"
    "00000f8ffff6ffff1f00000000001fcfffffffff3f80000000003fe7fffffffe7fc0000000007fe3fffffffc7fe00000"
    "0000fff1fffffff8fff0000001fffff8fffffff1fffff80007fffffc7fffffe3fffffe000ffffffe3fffffc7ffffff00"
    "1ffffffc7fffffe3ffffff803ffffff87fbfdfe1ffffffc03fffffe07fbfdfe07fffffc07fffffc03fbf9fc03fffffe0"
    "7e7fff803f1f8fc01fffe7e0f83fff000c0603000fffc1e0f01ffe000000000007ff80f0e01ffe000000000007ff8070"
    "c00ffe000000000007ff00308007fe000000000007fe00100003fe000000000007fc00000001fe000000000007f80000"
    "0000fc000000000007f000000001fc000000000003f800000003f8000000000001fc00000007f8000000000001fe0000"
    "000ff0000000000000ff0000001fe00000000000007f8000003f800000000000001fc000007c0000000000000003e000"
    "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
)
_logo_fb = None                # framebuf.FrameBuffer, built on first draw; False = unusable

_WHITE = lvgl.color_hex(0xFFFFFF)
_BLACK = lvgl.color_hex(0x000000)
_RED = lvgl.color_hex(0xFF0000)


def _clean(s, limit):
    s = "".join(c for c in str(s or "") if 32 <= ord(c) < 127)
    return s[:limit]


def _valid_id(v):
    if not isinstance(v, str) or len(v) != 6:
        return False
    try:
        ubinascii.unhexlify(v)
        return True
    except Exception:
        return False


def _read_json(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _load_config():
    changed = False
    try:
        os.stat(CONFIG)
        cfg = _read_json(CONFIG)
    except OSError:
        cfg = {}
        changed = True
        # Migration from the app's old name: keep the same id so other badges
        # still recognise us. The old file is left in place.
        old = _read_json(OLD_CONFIG)
        for k in ("id", "name", "broadcast"):
            if k in old:
                cfg[k] = old[k]
        if old:
            print("maker: migrated settings from", OLD_CONFIG)
    if not _valid_id(cfg.get("id")):
        cfg["id"] = "".join("%02x" % b for b in os.urandom(3))
        changed = True
    cfg["id"] = cfg["id"].lower()
    for key, limit in (("name", NAME_MAX), ("makes", MAKES_MAX)):
        v = _clean(cfg.get(key, ""), limit)
        if v != cfg.get(key):
            cfg[key] = v
            changed = True
    if not isinstance(cfg.get("broadcast"), bool):
        cfg["broadcast"] = True
        changed = True
    if changed:
        _save_config(cfg)
    return cfg


def _save_config(cfg):
    try:
        # Keep keys other apps/tools add (e.g. Maker Tag's "display_name").
        out = _read_json(CONFIG)
        if not isinstance(out, dict):
            out = {}
        for k in ("id", "name", "makes", "broadcast"):
            out[k] = cfg[k]
        with open(CONFIG, "w") as f:
            json.dump(out, f)
    except Exception as ex:
        print("maker: could not save config:", ex)


def _read_owner():
    """Owner profile as a dict (only keys that are actually set). Uses lib.owner when the
    firmware has it, else reads /data/owner.json directly. Never raises."""
    if _owner is not None:
        try:
            try:
                _owner.reload()
            except Exception:
                pass
            d = {}
            for k in ("maker_handle", "makes", "broadcast"):
                v = _owner.get(k, None)
                if v is not None:
                    d[k] = v
            return d
        except Exception as ex:
            print("maker: lib.owner failed, reading file:", ex)
    return _read_json(OWNER)


def _identity(cfg):
    """(handle, makes, broadcast) from owner.json first, maker.json (cfg) as fallback."""
    own = _read_owner()
    if isinstance(own.get("maker_handle"), str):    # set in the profile (even if left blank)
        handle = _clean(own["maker_handle"], HANDLE_MAX).strip()
    else:
        handle = cfg.get("name", "")
    if isinstance(own.get("makes"), str):
        makes = _clean(own["makes"], MAKES_MAX)      # set in the profile (even if cleared)
    else:
        makes = cfg.get("makes", "")
    bc = own.get("broadcast")
    if not isinstance(bc, bool):
        bc = cfg.get("broadcast", True)
    return handle, makes, bc


def _save_owner_broadcast(on):
    """Persist the broadcast flag to owner.json, keeping every other key."""
    try:
        d = _read_json(OWNER)
        d["broadcast"] = bool(on)
        with open(OWNER, "w") as f:
            json.dump(d, f)
    except Exception as ex:
        print("maker: could not save", OWNER, ex)


def _parse(adv):
    """Our own parser (fallback for presence.parse): full advert -> (id_hex, text) or None."""
    n = len(adv)
    j = _find_mfr(adv, n)
    if j < 0:
        return None
    end = j - 1 + adv[j - 2]
    d = adv[j:end]
    if len(d) < 2 + 4 + 1 + 3 or d[6] != VERSION:
        return None
    bid = "".join("%02x" % b for b in d[7:10])
    return bid, _clean("".join(chr(c) for c in d[10:10 + NAME_MAX]), NAME_MAX)


def _parse_any(adv):
    if _presence is not None:
        try:
            r = _presence.parse(adv)
            if (isinstance(r, tuple) and len(r) == 2 and _valid_id(r[0])
                    and isinstance(r[1], str)):
                return r[0].lower(), _clean(r[1], NAME_MAX)
        except Exception:
            pass
    return _parse(adv)


def _adv_payload(badge_id, name):
    mfr = COMPANY + MAGIC + bytes([VERSION]) + ubinascii.unhexlify(badge_id) + name.encode()[:NAME_MAX]
    return b"\x02\x01\x06" + bytes([len(mfr) + 1, 0xFF]) + mfr


def _find_mfr(adv, n):
    """Index of the first byte after the 0xFF AD type of a matching SC26 manufacturer
    field, or -1. Allocation-free: only integer indexing, no slicing."""
    i = 0
    while i + 1 < n:
        ln = adv[i]
        if ln == 0 or i + 1 + ln > n:
            return -1
        if (adv[i + 1] == 0xFF and ln >= _MFR_MIN_LEN
                and adv[i + 2] == 0xFF and adv[i + 3] == 0xFF
                and adv[i + 4] == 0x53 and adv[i + 5] == 0x43     # 'S' 'C'
                and adv[i + 6] == 0x32 and adv[i + 7] == 0x36):   # '2' '6'
            return i + 2
        i += 1 + ln
    return -1


def _logo():
    """The logo as a framebuf.FrameBuffer, or None if this firmware can't draw it."""
    global _logo_fb
    if _logo_fb is None:
        _logo_fb = False
        try:
            import framebuf
            buf = bytearray(ubinascii.unhexlify(_LOGO_HEX))   # FrameBuffer needs a writable buffer
            if len(buf) == ((LOGO_W + 7) // 8) * LOGO_H:
                _logo_fb = framebuf.FrameBuffer(buf, LOGO_W, LOGO_H, framebuf.MONO_HLSB)
            else:
                print("maker: logo data has the wrong size")
        except Exception as ex:
            print("maker: logo unavailable:", ex)
    return _logo_fb or None


def _draw_logo():
    """Blit the logo into the black framebuffer (call after LVGL has flushed, before
    display.update()). Only black pixels are set (key=0), so the white LVGL background
    shows through. Never raises: no logo is better than no screen."""
    try:
        fb = _logo()
        black = getattr(display, "black", None)
        if fb is None or black is None:
            return
        black.blit(fb, LOGO_X, LOGO_Y, 0)
        red = getattr(display, "red", None)
        if red is not None:          # keep logo pixels pure black (red plane wins otherwise)
            red.fill_rect(LOGO_X, LOGO_Y, LOGO_W, LOGO_H, 0)
    except Exception as ex:
        print("maker: logo draw failed:", ex)


def _proximity(rssi):
    if rssi >= -62:
        return "near", (0, 3, 0)
    if rssi >= -78:
        return "same room", (2, 2, 0)
    return "far", (0, 0, 3)


class MakerDetectorApp(BaseApp):
    def __init__(self, name=None):
        super().__init__("Maker Detector")
        self.sleep_ms = 200            # BaseApp.arun() awaits this between run() calls
        self.cfg = _load_config()      # maker.json: legacy id + fallback identity
        self.presence = None           # lib.presence module when usable
        self.my_id = self.cfg["id"]
        if _presence is not None:
            try:
                _presence.start()      # idempotent; main.py normally did this already
                pid = _presence.badge_id()
                if _valid_id(pid):
                    self.my_id = pid.lower()
                    self.presence = _presence
                else:
                    print("maker: presence.badge_id() invalid, advertising ourselves")
            except Exception as ex:
                print("maker: lib.presence unusable, advertising ourselves:", ex)
        self.handle, self.makes, self.broadcast = _identity(self.cfg)
        self._sync_broadcast()
        self.peers = {}                # id -> [name, rssi(smoothed), last_seen_s, first_seen_s]
        self._pending = []             # (bytes mfr data, rssi) queued by the IRQ, bounded
        self._bits = bytearray(_BITS // 8)   # address-hash bitmap for this burst
        self._burst_devices = 0        # distinct-ish addresses heard in the current burst
        self.devices = 0               # value from the last finished burst (shown on screen)
        self._scan_done = True         # set by the IRQ; scan is only re-armed after this
        self.scanning = False
        self._scan_started = 0
        self.last_run = 0
        self.last_draw = 0
        self.drawn = None
        self.scr = None
        self.ble = None
        try:
            self.ble = bluetooth.BLE()
            # Never active(True) an already-active radio: on this firmware that restarts the
            # stack and silently drops lib.presence's advert (started just above).
            if not self.ble.active():
                self.ble.active(True)
            self.ble.irq(self._irq)
            self._apply_broadcast()
        except Exception as ex:   # never break boot
            print("maker: BLE init failed:", ex)

    # ---------------------------------------------------------------- BLE
    def _sync_broadcast(self):
        """With lib.presence, its enabled() is the truth for the broadcast flag."""
        if self.presence is not None:
            try:
                self.broadcast = bool(self.presence.enabled())
            except Exception as ex:
                print("maker: presence.enabled() failed:", ex)

    def _apply_broadcast(self):
        if not self.ble:
            return
        if self.presence is not None:
            return                 # lib.presence advertises; never advertise twice
        try:
            if self.broadcast:
                name = _clean(self.handle, NAME_MAX)
                self.ble.gap_advertise(ADV_INTERVAL_US, _adv_payload(self.my_id, name),
                                       connectable=False)
                print("maker: broadcasting as", self.my_id, repr(name))
            else:
                self.ble.gap_advertise(None)
                print("maker: broadcasting off")
        except Exception as ex:
            print("maker: advertise failed:", ex)

    def _ensure_ble(self):
        """lib.presence shares the one BLE object: make sure it's still on and the IRQ
        handler is ours (presence doesn't scan, but may have (re)activated the radio)."""
        if not self.ble:
            return
        try:
            if not self.ble.active():
                self.ble.active(True)
            self.ble.irq(self._irq)
        except Exception as ex:
            print("maker: BLE re-arm failed:", ex)

    def _toggle_broadcast(self):
        on = not self.broadcast
        if self.presence is not None:
            try:
                self.presence.set_broadcast(on)    # persists to owner.json and (re)applies
            except Exception as ex:
                print("maker: presence.set_broadcast failed:", ex)
            self._sync_broadcast()
            self._ensure_ble()
            return
        self.broadcast = on
        _save_owner_broadcast(on)
        self.cfg["broadcast"] = on                 # keep maker.json in sync for old tools
        _save_config(self.cfg)
        self._apply_broadcast()

    def _refresh_identity(self):
        """Re-read owner.json/maker.json on app entry (Badge Manager may have changed them)."""
        try:
            fresh = _read_json(CONFIG)       # fallback values may have changed too (id: never)
            for key, limit in (("name", NAME_MAX), ("makes", MAKES_MAX)):
                if isinstance(fresh.get(key), str):
                    self.cfg[key] = _clean(fresh[key], limit)
            new = _identity(self.cfg)
        except Exception as ex:
            print("maker: identity reload failed:", ex)
            return
        if self.presence is not None:
            self.handle, self.makes = new[0], new[1]
            try:
                self.presence.refresh()
            except Exception as ex:
                print("maker: presence.refresh failed:", ex)
            self._sync_broadcast()
            self._ensure_ble()
            return
        changed = (_clean(new[0], NAME_MAX) != _clean(self.handle, NAME_MAX)
                   or new[2] != self.broadcast)
        self.handle, self.makes, self.broadcast = new
        if changed:
            self._apply_broadcast()

    def _irq(self, event, data):
        # Keep this short: it runs once per advert heard, which in a crowded hall is
        # hundreds of times a second. No printing, no dicts, no BLE calls, no time.time().
        try:
            if event == _IRQ_SCAN_RESULT:
                addr = data[1]
                adv = data[4]
                # approximate distinct-device count: set one bit per address hash
                h = ((addr[0] << 2) ^ (addr[4] << 1) ^ addr[5]) & (_BITS - 1)
                m = 1 << (h & 7)
                b = self._bits
                if not b[h >> 3] & m:
                    b[h >> 3] |= m
                    self._burst_devices += 1
                n = len(adv)
                if n < 1 + _MFR_MIN_LEN or n > ADV_MAX:
                    return
                j = _find_mfr(adv, n)
                if j < 0 or len(self._pending) >= MAX_PENDING:
                    return
                self._pending.append((bytes(adv), data[3]))   # <= 31 bytes; parsed in run()
            elif event == _IRQ_SCAN_DONE:
                self._scan_done = True
        except Exception:
            pass   # a malformed advert must never kill the handler

    def _stop_scan(self):
        try:
            self.ble.gap_scan(None)
        except Exception as ex:
            print("maker: scan stop failed:", ex)
        self.scanning = False
        self._scan_done = True

    def _scan(self):
        if not self.ble:
            return
        now = time.ticks_ms()
        if self.scanning and not self._scan_done:
            if time.ticks_diff(now, self._scan_started) > SCAN_WATCHDOG_MS:
                print("maker: scan-done never arrived; restarting scan")
                self._stop_scan()
            else:
                return
        if self.scanning:          # previous burst finished: publish its device count
            self.devices = self._burst_devices
            self.scanning = False
        for i in range(len(self._bits)):
            self._bits[i] = 0
        self._burst_devices = 0
        try:
            self._scan_done = False
            self.ble.gap_scan(SCAN_MS, SCAN_INTERVAL_US, SCAN_WINDOW_US, False)
            self.scanning = True
            self._scan_started = now
        except Exception as ex:
            self._scan_done = True
            print("maker: scan failed:", ex)

    # ------------------------------------------------------------- state
    def _drain(self):
        """Move adverts queued by the IRQ into self.peers (runs in the main loop)."""
        if not self._pending:
            return
        q = self._pending
        self._pending = []
        now = time.time()
        for adv, rssi in q:
            try:
                r = _parse_any(adv)
                if r is None or r[0] == self.my_id:
                    continue
                bid, name = r
                p = self.peers.get(bid)
                if p is None:
                    if len(self.peers) >= MAX_PEERS:
                        oldest = min(self.peers, key=lambda k: self.peers[k][2])
                        del self.peers[oldest]
                    self.peers[bid] = [name, rssi, now, now]
                else:
                    p[0] = name or p[0]
                    p[1] = (p[1] * 3 + rssi) // 4     # smooth RSSI
                    p[2] = now
            except Exception as ex:
                print("maker: bad advert skipped:", ex)

    def _current(self):
        now = time.time()
        for bid in [b for b, p in self.peers.items() if now - p[2] > FORGET_S]:
            del self.peers[bid]
        live = [(bid, p) for bid, p in self.peers.items() if now - p[2] <= SEEN_S]
        live.sort(key=lambda x: -x[1][1])
        return live

    def _leds(self, live):
        vals = [(0, 0, 0)] * 8
        for i, (bid, p) in enumerate(live[:8]):
            vals[i] = _proximity(p[1])[1]
        if not live:
            vals[0] = (1, 0, 0) if (time.ticks_ms() // 1000) % 2 else (0, 0, 0)   # blinking: searching
        leds.set(vals)
        leds.write()

    # ------------------------------------------------------------ screen
    def _build(self):
        scr = lvgl.obj()
        scr.remove_style_all()
        scr.set_size(lvgl.pct(100), lvgl.pct(100))
        scr.set_style_bg_color(_WHITE, 0)
        scr.set_style_bg_opa(lvgl.OPA.COVER, 0)

        def label(x, y, font, color):
            lb = lvgl.label(scr)
            lb.set_pos(x, y)
            lb.set_style_text_font(font, 0)
            lb.set_style_text_color(color, 0)
            lb.set_text("")
            return lb

        self.l_title = label(TEXT_X, 16, lvgl.font_montserrat_48, _RED)
        self.l_me = label(TEXT_X, 78, lvgl.font_montserrat_24, _BLACK)
        self.l_count = label(24, 130, lvgl.font_montserrat_48, _BLACK)
        self.l_names = label(24, 205, lvgl.font_montserrat_24, _BLACK)
        self.l_prox = label(520, 205, lvgl.font_montserrat_24, _RED)
        self.l_foot = label(24, 630, lvgl.font_montserrat_16, _BLACK)
        self.l_title.set_text("MAKER DETECTOR")
        self.scr = scr

    def _snapshot(self, live):
        return tuple((bid, p[0], _proximity(p[1])[0]) for bid, p in live)

    def _draw(self, live):
        if self.scr is None:
            self._build()
        me = self.handle or ("badge " + self.my_id)
        line = (("Broadcasting as: " + me) if self.broadcast
                else "Not broadcasting (press B to start)")
        if self.makes:
            line += _SEP + "makes " + self.makes
        # ~13 px/char at 24 px, ~9 px/char at 16 px; keep the line on one row right of the
        # logo (960 - TEXT_X - 24 = 800 px): <= 60 chars at 24 px, else 16 px, clipped at 88.
        if len(line) > 88:
            line = line[:85] + "..."
        self.l_me.set_style_text_font(lvgl.font_montserrat_24 if len(line) <= 60
                                      else lvgl.font_montserrat_16, 0)
        self.l_me.set_text(line)
        n = len(live)
        self.l_count.set_text("No other makers yet..." if n == 0 else
                              "%d other maker%s in range" % (n, "" if n == 1 else "s"))
        rows = live[:SHOW_ROWS]
        self.l_names.set_text("\n".join((p[0] or ("badge " + bid)) for bid, p in rows))
        self.l_prox.set_text("\n".join("%s  (%d dBm)" % (_proximity(p[1])[0], p[1]) for bid, p in rows))
        more = " (+%d more)" % (n - SHOW_ROWS) if n > SHOW_ROWS else ""
        self.l_foot.set_text("A: redraw   B: broadcast on/off" + _SEP + "~%d Bluetooth devices around%s"
                             % (max(self.devices, self._burst_devices), more))
        leds.fill((0, 0, 2))          # blue while the panel refreshes
        display.clear()
        lvgl.screen_load(self.scr)
        self.scr.update_layout()
        self.scr.invalidate()
        lvgl.refr_now(display.lv_display)    # LVGL flushes into the framebuffers here
        _draw_logo()                          # then the logo goes on top
        display.update()
        self.drawn = self._snapshot(live)
        self.last_draw = time.time()
        gc.collect()

    # -------------------------------------------------------------- loop
    # Returns quickly every time; BaseApp.arun() then awaits self.sleep_ms, so the
    # other asyncio tasks (buttons, display, LEDs, heartbeat) and USB/REPL keep running.
    # The framework has no "app left" hook: when the user switches away, run() stops
    # being called, so no new burst is armed and the current one ends within SCAN_MS.
    def run(self):
        now = time.time()
        if self.launched or now - self.last_run > 5:
            self.drawn = None          # force a redraw on entry
            self._refresh_identity()
            # we were away: if the last burst should long be over, don't wait for the watchdog
            if (self.ble and self.scanning and not self._scan_done
                    and time.ticks_diff(time.ticks_ms(), self._scan_started) > SCAN_MS + 1_000):
                self._stop_scan()
        self.last_run = now
        try:
            self._scan()
        except Exception as ex:
            print("maker: scan error:", ex)
        try:
            self._drain()
        except Exception as ex:
            self._pending = []
            print("maker: drain error:", ex)

        if UserB.pressed():
            try:
                self._toggle_broadcast()
            except Exception as ex:
                print("maker: toggle failed:", ex)
            self.drawn = None
        force = UserA.pressed()

        live = self._current()
        self._leds(live)
        if not display.is_done_updating():
            return
        changed = self._snapshot(live) != self.drawn
        if force or (changed and (self.drawn is None or now - self.last_draw >= REDRAW_MIN_S)):
            self._draw(live)
