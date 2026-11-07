"""Maker Tag: a two-column name tag -- "HI, MY NAME IS" | "I LIKE TO MAKE".

Reads (never writes) /data/maker.json, the file the desktop Badge Manager writes and the
Maker Detector app shares:
    {"id": "a1b2c3", "name": "Dave", "makes": "robots", "broadcast": true}
Optional key "display_name" is preferred over "name" (the short broadcast string) when present.
Missing/invalid file or empty fields -> "Your name" / "Something cool" placeholders plus a hint.

Each column auto-picks the largest Montserrat font (48 down to 16) that fits; text wraps on
words (name: up to 2 lines, makes: up to 3 lines) and is truncated with "..." as a last resort,
so it never overflows.

Controls while running:
  User A  re-read /data/maker.json and redraw
  User B  swap colors (red name / black header band) and redraw
The screen is drawn once on entry (e-paper refresh takes ~30 s), not every loop.
LED bar: blue while the panel refreshes, dim green when done.

By 740Dave, Hackaday Supercon 2026. Written with Claude.
"""

import json
import time

import lvgl

from apps.base_app import BaseApp
from lib.button import UserA, UserB
from lib.epd import display
from lib.led_bar import leds

CONFIG = "/data/maker.json"

W = 960
H = 672
COL_W = W // 2              # 480 per column
MARGIN = 24
TEXT_W = COL_W - 2 * MARGIN  # 432 px usable text width per column

BAND_H = 110                # red header band, y 0..109
BODY_TOP = BAND_H + 20      # 130
BODY_BOTTOM = 590           # body area 130..590 (460 px tall)
HINT_Y = 598
FOOT_Y = 634
DIV_W = 4                   # vertical divider, centered on x = 480

NAME_LINES = 2
MAKES_LINES = 3
SIZES = (48, 46, 44, 42, 40, 38, 36, 34, 32, 30, 28, 26, 24, 22, 20, 18, 16)

PH_NAME = "Your name"
PH_MAKES = "Something cool"
HINT = "Set these in Badge Manager > Settings > User Settings"
FOOTER = "Hackaday Supercon 2026"

_WHITE = lvgl.color_hex(0xFFFFFF)
_BLACK = lvgl.color_hex(0x000000)
_RED = lvgl.color_hex(0xFF0000)

# Built-in Montserrat fonts only cover printable ASCII; fold common accents so names survive.
_FOLD = {
    "à": "a", "á": "a", "â": "a", "ã": "a", "ä": "a", "å": "a",
    "À": "A", "Á": "A", "Â": "A", "Ã": "A", "Ä": "A", "Å": "A",
    "è": "e", "é": "e", "ê": "e", "ë": "e",
    "È": "E", "É": "E", "Ê": "E", "Ë": "E",
    "ì": "i", "í": "i", "î": "i", "ï": "i",
    "Ì": "I", "Í": "I", "Î": "I", "Ï": "I",
    "ò": "o", "ó": "o", "ô": "o", "õ": "o", "ö": "o", "ø": "o",
    "Ò": "O", "Ó": "O", "Ô": "O", "Õ": "O", "Ö": "O", "Ø": "O",
    "ù": "u", "ú": "u", "û": "u", "ü": "u",
    "Ù": "U", "Ú": "U", "Û": "U", "Ü": "U",
    "ñ": "n", "Ñ": "N", "ç": "c", "Ç": "C", "ß": "ss",
    "ý": "y", "ÿ": "y", "Ý": "Y",
    "‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
}


def _clean(s):
    """Printable ASCII only, whitespace collapsed to single spaces."""
    if not isinstance(s, str):
        return ""
    out = []
    for c in s:
        c = _FOLD.get(c, c)
        for ch in c:
            o = ord(ch)
            if ch in "\t\r\n":
                out.append(" ")
            elif 32 <= o < 127:
                out.append(ch)
    return " ".join("".join(out).split())


def _load():
    """Return (name, makes, used_placeholder). Never raises, never writes."""
    cfg = {}
    try:
        with open(CONFIG) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
    except Exception as ex:
        print("maker_tag: no usable", CONFIG, ex)
        cfg = {}
    name = _clean(cfg.get("display_name")) or _clean(cfg.get("name"))
    makes = _clean(cfg.get("makes"))
    placeholder = not name or not makes
    return (name or PH_NAME), (makes or PH_MAKES), placeholder


def _font(size):
    return getattr(lvgl, "font_montserrat_%d" % size, None)


def _delete(obj):
    if obj is None:
        return
    try:
        obj.delete()
    except AttributeError:
        try:
            obj.del_()
        except Exception as ex:
            print("maker_tag: could not delete obj", ex)


class _Measurer:
    """Measures text with an off-screen, auto-sized label (no reliance on text_get_width)."""

    def __init__(self):
        self.scr = lvgl.obj()           # separate, never-loaded screen
        self.scr.remove_style_all()
        self.scr.set_size(W, H)
        self.lb = lvgl.label(self.scr)
        self.lb.set_text("")
        self.cache = {}

    def size(self, text, font_size):
        key = (font_size, text)
        hit = self.cache.get(key)
        if hit is None:
            self.lb.set_style_text_font(_font(font_size), 0)
            self.lb.set_text(text if text else " ")
            self.lb.update_layout()
            hit = (self.lb.get_width(), self.lb.get_height())
            self.cache[key] = hit
        return hit

    def width(self, text, font_size):
        return self.size(text, font_size)[0]

    def line_h(self, font_size):
        return self.size("Ag", font_size)[1]

    def close(self):
        _delete(self.scr)
        self.scr = None
        self.lb = None


def _wrap(m, words, size, max_w):
    """Greedy word wrap. Returns list of lines, or None if a single word is too wide."""
    lines = []
    cur = ""
    for w in words:
        if m.width(w, size) > max_w:
            return None
        t = (cur + " " + w) if cur else w
        if m.width(t, size) <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _shorten(m, text, size, max_w, ell):
    """Trim characters until text (+ ellipsis) fits."""
    if m.width(text, size) <= max_w and not ell:
        return text
    while text and m.width(text + "...", size) > max_w:
        text = text[:-1]
    return text.rstrip() + "..."


def _fit(m, text, max_lines, max_w, max_h, sizes=SIZES):
    """Pick the largest font size whose word-wrap fits. Returns (size, [lines])."""
    words = text.split() or [" "]
    for size in sizes:
        if _font(size) is None:
            continue
        lh = m.line_h(size)
        lim = min(max_lines, max(1, max_h // max(1, lh)))
        lines = _wrap(m, words, size, max_w)
        if lines is not None and len(lines) <= lim:
            return size, lines
    # Last resort: smallest font, shorten over-long words, cap the line count.
    size = sizes[-1]
    for s in reversed(sizes):
        if _font(s) is not None:
            size = s
            break
    words = [w if m.width(w, size) <= max_w else _shorten(m, w, size, max_w, True)
             for w in words]
    lines = _wrap(m, words, size, max_w) or [_shorten(m, text, size, max_w, True)]
    lim = min(max_lines, max(1, max_h // max(1, m.line_h(size))))
    if len(lines) > lim:
        lines = lines[:lim]
        lines[-1] = _shorten(m, lines[-1], size, max_w, True)
    return size, lines


class MakerTagApp(BaseApp):
    def __init__(self, name=None):
        super().__init__("Maker Tag")
        self.sleep_ms = 100
        self.inverted = False       # B: red name + black header band
        self.pending = False        # a redraw has been requested
        self.busy_since = None      # ticks_ms when we handed a frame to the panel
        self.scr = None

    # ------------------------------------------------------------ screen
    def _label(self, parent, x, y, w, size, color, text):
        lb = lvgl.label(parent)
        lb.set_width(w)
        lb.set_pos(x, y)
        lb.set_style_text_font(_font(size), 0)
        lb.set_style_text_color(color, 0)
        lb.set_style_text_align(lvgl.TEXT_ALIGN.CENTER, 0)
        lb.set_text(text)
        return lb

    def _box(self, parent, x, y, w, h, color):
        b = lvgl.obj(parent)
        b.remove_style_all()
        b.set_pos(x, y)
        b.set_size(w, h)
        b.set_style_bg_color(color, 0)
        b.set_style_bg_opa(lvgl.OPA.COVER, 0)
        return b

    def _draw(self):
        name, makes, placeholder = _load()
        print("maker_tag: drawing", repr(name), "/", repr(makes))
        band_color = _BLACK if self.inverted else _RED
        name_color = _RED if self.inverted else _BLACK

        body_h = BODY_BOTTOM - BODY_TOP
        m = _Measurer()
        try:
            h_size_l, h_l = _fit(m, "HI, MY NAME IS", 1, TEXT_W, BAND_H - 20)
            h_size_r, h_r = _fit(m, "I LIKE TO MAKE", 1, TEXT_W, BAND_H - 20)
            h_size = min(h_size_l, h_size_r)    # same header size on both sides
            h_lh = m.line_h(h_size)
            n_size, n_lines = _fit(m, name, NAME_LINES, TEXT_W, body_h)
            k_size, k_lines = _fit(m, makes, MAKES_LINES, TEXT_W, body_h)
            n_lh = m.line_h(n_size)
            k_lh = m.line_h(k_size)
        finally:
            m.close()

        scr = lvgl.obj()
        scr.remove_style_all()
        scr.set_size(W, H)
        scr.set_style_bg_color(_WHITE, 0)
        scr.set_style_bg_opa(lvgl.OPA.COVER, 0)

        # Row 1: header band, white text knocked out of red (or black).
        self._box(scr, 0, 0, W, BAND_H, band_color)
        hy = (BAND_H - h_lh) // 2
        self._label(scr, MARGIN, hy, TEXT_W, h_size, _WHITE, "HI, MY NAME IS")
        self._label(scr, COL_W + MARGIN, hy, TEXT_W, h_size, _WHITE, "I LIKE TO MAKE")

        # Divider between the columns (body area only).
        self._box(scr, COL_W - DIV_W // 2, BODY_TOP, DIV_W, body_h, _BLACK)

        # Row 2: name and makes, vertically centered in the body area.
        ny = BODY_TOP + (body_h - n_lh * len(n_lines)) // 2
        ky = BODY_TOP + (body_h - k_lh * len(k_lines)) // 2
        self._label(scr, MARGIN, ny, TEXT_W, n_size, name_color, "\n".join(n_lines))
        self._label(scr, COL_W + MARGIN, ky, TEXT_W, k_size, _BLACK, "\n".join(k_lines))

        if placeholder:
            self._label(scr, MARGIN, HINT_Y, W - 2 * MARGIN, 18, _RED, HINT)
        self._label(scr, MARGIN, FOOT_Y, W - 2 * MARGIN, 20, _BLACK, FOOTER)

        print("maker_tag: fonts header=%d name=%d x%d makes=%d x%d"
              % (h_size, n_size, len(n_lines), k_size, len(k_lines)))

        leds.fill((0, 0, 2))            # blue while the panel refreshes
        display.clear()
        lvgl.screen_load(scr)
        old = self.scr
        self.scr = scr
        if old is not None and old is not scr:
            _delete(old)                # LVGL heap is only 64 KB; don't leak screens
        scr.update_layout()
        scr.invalidate()
        lvgl.refr_now(display.lv_display)
        display.update()
        self.busy_since = time.ticks_ms()

    # -------------------------------------------------------------- loop
    def run(self):
        if self.launched:
            self.pending = True
        if UserA.pressed():
            self.pending = True
        if UserB.pressed():
            self.inverted = not self.inverted
            self.pending = True

        # display.update() only sets an event; give the display task a moment to start
        # (it clears is_done_updating) before trusting "done".
        if self.busy_since is not None:
            if time.ticks_diff(time.ticks_ms(), self.busy_since) < 1000:
                return
            if not display.is_done_updating():
                return
            self.busy_since = None
            leds.fill((0, 1, 0))

        if self.pending and display.is_done_updating():
            self.pending = False        # clear first so an exception can't loop forever
            self._draw()
