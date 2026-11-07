"""Nerd Detector: counts other Billboard Badges nearby over Bluetooth LE, and shows their names.

Every badge with this app installed (and broadcasting turned on) sends a tiny BLE advert:
    flags 02 01 06 | manufacturer data: FF FF "SC26" ver(1) id(3) name(0-16 ASCII)
The app listens for those adverts while it's the active app and shows who's around.

Settings: /data/nerd.json  {"id": "a1b2c3", "name": "Dave", "broadcast": true}
  - id is random, created on first boot. name is optional (max 16 ASCII chars).
  - Set the name with the Badge Manager's "Nerd Detector" tab, or by editing the file.

Controls while running:
  User A  redraw the screen now
  User B  turn broadcasting on/off (remembered)
LED bar: one LED per badge seen in the last minute (green = near, yellow = same room, blue = far).

Advertising starts at boot (apps are created at boot), so your badge is visible to others even
while you're using a different app. Scanning only runs while this app is open.

By 740Dave, Hackaday Supercon 2026. Written with Claude.
"""

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

CONFIG = "/data/nerd.json"
MAGIC = b"SC26"
COMPANY = b"\xff\xff"          # 0xFFFF = "no company / testing" per the Bluetooth SIG
VERSION = 1
NAME_MAX = 16

ADV_INTERVAL_US = 400_000       # advertise every 400 ms
SCAN_MS = 10_000                # each scan burst; re-armed while the app is open
SCAN_INTERVAL_US = 100_000
SCAN_WINDOW_US = 60_000         # listen 60% of the time while open
SEEN_S = 60                     # "in range" = heard within this many seconds
FORGET_S = 300
REDRAW_MIN_S = 25               # e-paper refresh takes ~30 s; don't redraw more often

_IRQ_SCAN_RESULT = 5
_IRQ_SCAN_DONE = 6

_WHITE = lvgl.color_hex(0xFFFFFF)
_BLACK = lvgl.color_hex(0x000000)
_RED = lvgl.color_hex(0xFF0000)


def _clean_name(s):
    s = "".join(c for c in str(s or "") if 32 <= ord(c) < 127)
    return s.encode()[:NAME_MAX].decode()


def _load_config():
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    changed = False
    if not isinstance(cfg.get("id"), str) or len(cfg["id"]) != 6:
        cfg["id"] = "".join("%02x" % b for b in os.urandom(3))
        changed = True
    cfg["name"] = _clean_name(cfg.get("name", ""))
    if "broadcast" not in cfg:
        cfg["broadcast"] = True
        changed = True
    if changed:
        _save_config(cfg)
    return cfg


def _save_config(cfg):
    try:
        with open(CONFIG, "w") as f:
            json.dump(cfg, f)
    except OSError as ex:
        print("nerd: could not save config:", ex)


def _adv_payload(badge_id, name):
    mfr = COMPANY + MAGIC + bytes([VERSION]) + ubinascii.unhexlify(badge_id) + name.encode()[:NAME_MAX]
    return b"\x02\x01\x06" + bytes([len(mfr) + 1, 0xFF]) + mfr


def _parse(adv):
    """Return (id_hex, name) if this advert is from a Nerd Detector badge, else None."""
    i, n = 0, len(adv)
    while i + 1 < n:
        ln = adv[i]
        if ln == 0:
            break
        if adv[i + 1] == 0xFF and ln >= 1 + 2 + 4 + 1 + 3:
            d = adv[i + 2:i + 1 + ln]
            if d[0:2] == COMPANY and d[2:6] == MAGIC:
                bid = "".join("%02x" % b for b in d[7:10])
                try:
                    name = bytes(d[10:]).decode()
                except Exception:
                    name = ""
                return bid, _clean_name(name)
        i += 1 + ln
    return None


def _proximity(rssi):
    if rssi >= -62:
        return "near", (0, 3, 0)
    if rssi >= -78:
        return "same room", (2, 2, 0)
    return "far", (0, 0, 3)


class NerdDetectorApp(BaseApp):
    def __init__(self, name=None):
        super().__init__("Nerd Detector")
        self.sleep_ms = 200
        self.cfg = _load_config()
        self.peers = {}        # id -> [name, rssi(smoothed), last_seen_s, first_seen_s]
        self.noise = {}        # BLE address -> last_seen_s (all devices, for curiosity)
        self.scanning = False
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
            print("nerd: BLE init failed:", ex)

    # ---------------------------------------------------------------- BLE
    def _apply_broadcast(self):
        if not self.ble:
            return
        try:
            if self.cfg["broadcast"]:
                self.ble.gap_advertise(ADV_INTERVAL_US, _adv_payload(self.cfg["id"], self.cfg["name"]),
                                       connectable=False)
                print("nerd: broadcasting as", self.cfg["id"], repr(self.cfg["name"]))
            else:
                self.ble.gap_advertise(None)
                print("nerd: broadcasting off")
        except Exception as ex:
            print("nerd: advertise failed:", ex)

    def _irq(self, event, data):
        if event == _IRQ_SCAN_RESULT:
            addr_type, addr, adv_type, rssi, adv = data
            now = time.time()
            self.noise[bytes(addr)] = now
            hit = _parse(bytes(adv))
            if hit and hit[0] != self.cfg["id"]:
                bid, name = hit
                p = self.peers.get(bid)
                if p is None:
                    self.peers[bid] = [name, rssi, now, now]
                else:
                    p[0] = name or p[0]
                    p[1] = (p[1] * 3 + rssi) // 4     # smooth RSSI
                    p[2] = now
        elif event == _IRQ_SCAN_DONE:
            self.scanning = False

    def _scan(self):
        if self.ble and not self.scanning:
            try:
                self.ble.gap_scan(SCAN_MS, SCAN_INTERVAL_US, SCAN_WINDOW_US, False)
                self.scanning = True
            except Exception as ex:
                print("nerd: scan failed:", ex)

    # ------------------------------------------------------------- state
    def _current(self):
        now = time.time()
        for bid in [b for b, p in self.peers.items() if now - p[2] > FORGET_S]:
            del self.peers[bid]
        for a in [a for a, t in self.noise.items() if now - t > SEEN_S]:
            del self.noise[a]
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
        self.l_title.set_text("NERD DETECTOR")
        self.scr = scr

    def _snapshot(self, live):
        return tuple((bid, p[0], _proximity(p[1])[0]) for bid, p in live)

    def _draw(self, live):
        if self.scr is None:
            self._build()
        me = self.cfg["name"] or ("badge " + self.cfg["id"])
        self.l_me.set_text(("Broadcasting as: " + me) if self.cfg["broadcast"]
                           else "Not broadcasting (press B to start)")
        n = len(live)
        self.l_count.set_text("No other badges yet..." if n == 0 else
                              "%d other badge%s in range" % (n, "" if n == 1 else "s"))
        rows = live[:16]
        self.l_names.set_text("\n".join((p[0] or ("badge " + bid)) for bid, p in rows))
        self.l_prox.set_text("\n".join("%s  (%d dBm)" % (_proximity(p[1])[0], p[1]) for bid, p in rows))
        more = " (+%d more)" % (n - 16) if n > 16 else ""
        self.l_foot.set_text("A: redraw   B: broadcast on/off   ·   %d Bluetooth devices around%s"
                             % (len(self.noise), more))
        leds.fill((0, 0, 2))          # blue while the panel refreshes
        display.clear()
        lvgl.screen_load(self.scr)
        self.scr.update_layout()
        self.scr.invalidate()
        lvgl.refr_now(display.lv_display)
        display.update()
        self.drawn = self._snapshot(live)
        self.last_draw = time.time()

    # -------------------------------------------------------------- loop
    def run(self):
        now = time.time()
        if self.launched or now - self.last_run > 5:
            self.scanning = False      # we were away; the previous burst has ended
            self.drawn = None          # force a redraw on entry
        self.last_run = now
        self._scan()

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
