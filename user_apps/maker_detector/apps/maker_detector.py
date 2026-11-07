"""Maker Detector: counts other Billboard Badges nearby over Bluetooth LE, and shows their names.

Every badge with this app installed (and broadcasting turned on) sends a tiny BLE advert:
    flags 02 01 06 | manufacturer data: FF FF "SC26" ver(1) id(3) name(0-16 ASCII)
(The over-the-air format is unchanged from the earlier version of this app, so both see each other.)
The app listens for those adverts while it's the active app and shows who's around.

Settings: /data/maker.json
    {"id": "a1b2c3", "name": "Dave", "makes": "robots", "broadcast": true}
  - id is random, created on first boot (or carried over from the old app's config file).
  - name is broadcast (max 16 printable ASCII chars).
  - makes is what you like to make (max 40 chars). Shown on your own screen only, never broadcast.

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

CONFIG = "/data/maker.json"
OLD_CONFIG = "/data/nerd.json"  # migration only: config file of the app's previous name
MAGIC = b"SC26"
COMPANY = b"\xff\xff"          # 0xFFFF = "no company / testing" per the Bluetooth SIG
VERSION = 1
NAME_MAX = 16
MAKES_MAX = 40
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
        self.cfg = _load_config()
        self._my_id = ubinascii.unhexlify(self.cfg["id"])
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
            self.ble.active(True)
            self.ble.irq(self._irq)
            self._apply_broadcast()
        except Exception as ex:   # never break boot
            print("maker: BLE init failed:", ex)

    # ---------------------------------------------------------------- BLE
    def _apply_broadcast(self):
        if not self.ble:
            return
        try:
            if self.cfg["broadcast"]:
                self.ble.gap_advertise(ADV_INTERVAL_US, _adv_payload(self.cfg["id"], self.cfg["name"]),
                                       connectable=False)
                print("maker: broadcasting as", self.cfg["id"], repr(self.cfg["name"]))
            else:
                self.ble.gap_advertise(None)
                print("maker: broadcasting off")
        except Exception as ex:
            print("maker: advertise failed:", ex)

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
                end = j - 1 + adv[j - 2]     # end of this AD field
                self._pending.append((bytes(adv[j:end]), data[3]))
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
        for d, rssi in q:
            try:
                if len(d) < 2 + 4 + 1 + 3 or d[7:10] == self._my_id:
                    continue
                bid = "".join("%02x" % b for b in d[7:10])
                name = _clean("".join(chr(c) for c in d[10:10 + NAME_MAX]), NAME_MAX)
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

        self.l_title = label(24, 16, lvgl.font_montserrat_48, _RED)
        self.l_me = label(24, 78, lvgl.font_montserrat_24, _BLACK)
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
        me = self.cfg["name"] or ("badge " + self.cfg["id"])
        line = (("Broadcasting as: " + me) if self.cfg["broadcast"]
                else "Not broadcasting (press B to start)")
        if self.cfg.get("makes"):
            line += _SEP + "makes " + self.cfg["makes"]
        # ~13 px/char at 24 px, ~9 px/char at 16 px; keep the line on one row (912 px)
        self.l_me.set_style_text_font(lvgl.font_montserrat_24 if len(line) <= 66
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
        lvgl.refr_now(display.lv_display)
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
            self.cfg["broadcast"] = not self.cfg["broadcast"]
            _save_config(self.cfg)
            self._apply_broadcast()
            self.drawn = None
        force = UserA.pressed()

        live = self._current()
        self._leds(live)
        if not display.is_done_updating():
            return
        changed = self._snapshot(live) != self.drawn
        if force or (changed and (self.drawn is None or now - self.last_draw >= REDRAW_MIN_S)):
            self._draw(live)
