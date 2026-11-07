"""App grid: a full screen table of the installed apps, shown by the app switcher.

A header row "LED Color", "LED 1" ... "LED 8", then one row per menu page of 8 apps,
starting with the LED colour of that page (the e-paper panel can't show the colour itself).
Unused slots say "Empty", and there are always two empty rows after the last app.
All app names use one font size: the largest at which every name fits its cell.
The selected app is only shown on the LED bar, so the table is drawn once and not on every
button press (a refresh takes ~30 s). The pinned boot app is drawn in red and tagged "BOOT".
A help block at the bottom explains the buttons; the table never overlaps it.

layout() is pure Python (testable off the badge); draw() renders it with LVGL.
"""

import os

import lvgl

from lib.epd import display
from lib.led_bar import MENU_BACKGROUND_COLORS, NUM_LEDS

W = 960
H = 672
MARGIN = 12
COLS = NUM_LEDS
COLOR_W = 128  # first column, the LED colour of the row
PAD = 6
LINE = 1  # table lines
EXTRA_ROWS = 2  # empty rows after the last app

# Same order as lib.led_bar.MENU_BACKGROUND_COLORS
ROW_COLOR_NAMES = ("ORANGE", "CYAN", "MAGENTA", "RED", "GREEN", "BLUE")

TITLE = "Hackaday Supercon 2026 Billboard Badge Apps"
TITLE_SIZES = (28, 24, 16)
INFO_SIZE = 16
HEADER_SIZES = (24, 16, 14)  # column headers and colour names
TAG_SIZE = 14  # "BOOT"
EMPTY = "Empty"
HELP = (
    "NAVIGATE: Menu + / Menu - move the lit LED.  LAUNCH: User A or B.",
    "GRID: press Menu + and Menu - together (in any app) for this screen.",
    "SET BOOT APP: press all four buttons together. Again to unpin.",
    "Hold Menu + or Menu - while powering on to skip the boot app.",
)
OWNER_HELP = (
    "MAKE IT YOURS: edit /data/owner.json via USB (mpremote or Thonny):",
    '{"maker_handle": "...", "makes": "..."}',
)
"""Only shown when the badge has lib/owner.py (owner profile support)"""
HELP_SIZE = 16
SIGNATURE_FILE = "/data/signature.pbm"
"""Optional 1-bit binary PBM (P4), drawn in the lower right corner"""
SIGNATURE_MAX = (320, 80)
# The only Montserrat sizes in this firmware build. A missing font crashes LVGL.
FONT_SIZES = (14, 16, 24, 28, 32, 36, 40, 44, 48)

# Hackaday "Jolly Wrencher", 46x46, 1 bit per pixel (MONO_HLSB, rows padded to 6 bytes,
# 1 = black). Blitted into the black framebuffer after LVGL has rendered.
LOGO_W = 46
LOGO_H = 46
LOGO_HEX = (
    "00000000000000000000000007000000038003c000000f0001e000001e0000e000001c0001f000003e0083f000003f04"
    "c7f000003f8ceff000003fdc7ffc0000fff87ffe0781fff83fff3fe3fff00ffe7ff9ffc0007cfffcf800003dfffef000"
    "001bffff60000007ffff80000007ffff80000007ffff8000000fffffc000000f0783c000000e0781c000000e0781c000"
    "000e0fc1c000000e3ff1c000000f7ffbc0000007feff80000007fcff80000017fcffa000003bffff7000007dfffef800"
    "0ffcfffcffc03ffe7ff9fff07ffefffdfff87ff87ff87ff8e7f073383f9cc7f000003f8c83f000003f0401f000003e00"
    "00e000001c0001e000001e0003c000000f00070000000380000000000000000000000000"
)

BLACK = "black"
RED = "red"


def color_name(row):
    page = row % len(MENU_BACKGROUND_COLORS)
    return ROW_COLOR_NAMES[page] if page < len(ROW_COLOR_NAMES) else "COLOR %d" % (page + 1)


def _shorten(m, text, size, max_w):
    """Trim text until it fits with "..." appended."""
    text = text.rstrip()
    while text and m.width(text + "...", size) > max_w:
        text = text[:-1].rstrip()
    return text + "..."


def _wrap(m, text, size, max_w, max_lines):
    """Greedy word wrap. Returns (lines, fits). If it doesn't fit, lines are
    still within max_w and max_lines, ending in "..." where text was cut."""
    lines = []
    cur = ""
    fits = True
    for word in text.split():
        if m.width(word, size) > max_w:
            fits = False
            word = _shorten(m, word, size, max_w)
        t = cur + " " + word if cur else word
        if not cur or m.width(t, size) <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = word
    if cur or not lines:
        lines.append(cur)
    if len(lines) > max_lines:
        fits = False
        last = lines[max_lines - 1] + " " + lines[max_lines]
        lines = lines[:max_lines]
        lines[-1] = _shorten(m, last, size, max_w)
    return lines, fits


def _avail(m, size):
    """size if this firmware has that font, else the nearest smaller (or larger) one"""
    for s in sorted(FONT_SIZES, key=lambda s: (s > size, abs(s - size))):
        if m.has_font(s):
            return s
    return size


def _sizes(m, sizes):
    """The sizes this firmware has, biggest first. Never empty."""
    return [s for s in sorted(sizes, reverse=True) if m.has_font(s)] or [_avail(m, min(sizes))]


def _first_fit(m, sizes, texts, max_w):
    """Biggest size at which every text fits max_w on one line"""
    sizes = _sizes(m, sizes)
    for s in sizes:
        if all(m.width(t, s) <= max_w for t in texts):
            return s
    return sizes[-1]


def layout(names, pinned=-1, selected=0, m=None, extra_help=(), signature=None):
    """Lay out the app table. Returns a dict with "items", a list of
      ("text", x, y, w, size, color, [lines], align)  align: "left", "center" or "right"
      ("box", x, y, w, h, border_width, color)        outline
      ("fill", x, y, w, h, color)
      ("logo", x, y, w, h)                             drawn by draw() after LVGL
      ("signature", x, y, w, h)                        same, signature=(w, h) if there is one
    "cells" [(row, col, x, y, w, h)] and the chosen sizes. Nothing ever extends outside
    the panel, and the table stays above "help_top". m measures text: width(text, size),
    line_h(size), has_font(size). extra_help: more help texts, added after HELP."""
    count = len(names)
    rows = (count + COLS - 1) // COLS + EXTRA_ROWS
    full_w = W - 2 * MARGIN
    items = []

    # Header: logo, title, app count
    items.append(("logo", MARGIN, MARGIN, LOGO_W, LOGO_H))
    info_size = _avail(m, INFO_SIZE)
    info = "%d app%s" % (count, "" if count == 1 else "s")
    info_w = m.width("999 apps", info_size)
    title_x = MARGIN + LOGO_W + 12
    title_w = full_w - (title_x - MARGIN) - info_w - 12
    title_size = _first_fit(m, TITLE_SIZES, [TITLE], title_w)
    title_lines = _wrap(m, TITLE, title_size, title_w, 1)[0]
    items.append(("text", title_x, MARGIN + (LOGO_H - m.line_h(title_size)) // 2, title_w,
                  title_size, BLACK, title_lines, "left"))
    rule_y = MARGIN + LOGO_H + 6
    items.append(("fill", MARGIN, rule_y, full_w, 2, BLACK))
    table_top = rule_y + 2 + 8

    # Help block at the bottom, narrower if a signature sits in the lower right corner
    help_size = _avail(m, HELP_SIZE)
    help_w = full_w
    sig_h = 0
    if signature:
        sig_w, sig_h = signature
        help_w = full_w - sig_w - 16
        items.append(("signature", W - MARGIN - sig_w, H - MARGIN - sig_h, sig_w, sig_h))
    help_lines = []
    for text in HELP + tuple(extra_help):
        help_lines += _wrap(m, text, help_size, help_w, 2)[0]
    help_h = max(len(help_lines) * m.line_h(help_size), sig_h)
    help_top = H - MARGIN - help_h - 2 - 6
    items.append(("fill", MARGIN, help_top, full_w, 2, BLACK))
    items.append(("text", MARGIN, help_top + 2 + 6, help_w, help_size, BLACK, help_lines, "left"))
    table_bottom = help_top - 8

    # Columns
    led_w = (full_w - COLOR_W) // COLS
    color_w = full_w - COLS * led_w
    xs = [MARGIN, MARGIN + color_w] + [MARGIN + color_w + led_w * (i + 1) for i in range(COLS)]
    head_texts = ["LED Color"] + ["LED %d" % (i + 1) for i in range(COLS)]
    head_size = min(_first_fit(m, HEADER_SIZES, head_texts[:1] + [color_name(r) for r in range(rows)],
                               color_w - 2 * PAD),
                    _first_fit(m, HEADER_SIZES, head_texts[1:], led_w - 2 * PAD))
    head_h = m.line_h(head_size) + 2 * PAD

    # One font for every app name (and "Empty"): the biggest at which all fit in 2 lines
    inner_w = led_w - 2 * PAD
    texts = [" ".join(str(n).split()) or "?" for n in names]
    tag_h = m.line_h(_avail(m, TAG_SIZE)) if 0 <= pinned < count else 0
    avail = table_bottom - table_top - head_h
    sizes = _sizes(m, FONT_SIZES)
    for name_size in sizes:
        n_lines = 0
        fits = True
        for t in texts + [EMPTY]:
            lines, ok = _wrap(m, t, name_size, inner_w, 2)
            fits = fits and ok
            n_lines = max(n_lines, len(lines))
        cell_h = 2 * PAD + n_lines * m.line_h(name_size) + tag_h
        if fits and rows * cell_h <= avail:
            break
    # (if nothing fits, the smallest font is used, names are cut and rows are scrolled)

    # Too many rows for the panel: show the rows around the selected app
    shown = min(rows, max(1, avail // cell_h))
    first_row = 0
    if shown < rows:
        first_row = max(0, min(rows - shown, selected // COLS - shown // 2))
        info = "rows %d-%d of %d" % (first_row + 1, first_row + shown, rows)
    row_h = max(cell_h, min(avail // shown, cell_h + 48))
    items.append(("text", W - MARGIN - info_w, MARGIN + (LOGO_H - m.line_h(info_size)) // 2,
                  info_w, info_size, BLACK, _wrap(m, info, info_size, info_w, 1)[0], "right"))

    # Table lines
    table_h = head_h + shown * row_h
    for i, y in enumerate([table_top, table_top + head_h] +
                          [table_top + head_h + row_h * (r + 1) for r in range(shown)]):
        items.append(("fill", MARGIN, y, full_w, 2 if i == 1 else LINE, BLACK))
    for x in xs:
        items.append(("fill", min(x, W - MARGIN - LINE), table_top, LINE, table_h + LINE, BLACK))

    # Column headers
    cells = []
    for col, text in enumerate(head_texts):
        x, w = xs[col], xs[col + 1] - xs[col]
        items.append(("text", x + PAD, table_top + PAD, w - 2 * PAD, head_size, BLACK, [text], "center"))
        cells.append((-1, col, x, table_top, w, head_h))

    # Rows
    lh = m.line_h(name_size)
    for k in range(shown):
        row = first_row + k
        y = table_top + head_h + k * row_h
        items.append(("text", xs[0] + PAD, y + (row_h - m.line_h(head_size)) // 2, color_w - 2 * PAD,
                      head_size, BLACK, [color_name(row)], "center"))
        cells.append((row, 0, xs[0], y, color_w, row_h))
        for col in range(COLS):
            idx = row * COLS + col
            x = xs[col + 1]
            cells.append((row, col + 1, x, y, led_w, row_h))
            is_pinned = idx == pinned and idx < count
            text = texts[idx] if idx < count else EMPTY
            lines = _wrap(m, text, name_size, inner_w, 2)[0]
            color = RED if is_pinned else BLACK
            ty = y + (row_h - (tag_h if is_pinned else 0) - len(lines) * lh) // 2
            items.append(("text", x + PAD, ty, inner_w, name_size, color, lines, "center"))
            if is_pinned:
                items.append(("box", x + 2, y + 2, led_w - 3, row_h - 3, 3, RED))
                items.append(("text", x + PAD, y + row_h - PAD - tag_h, inner_w, _avail(m, TAG_SIZE),
                              RED, ["BOOT"], "center"))

    return {
        "items": items,
        "cells": cells,
        "rows": rows,
        "first_row": first_row,
        "rows_shown": shown,
        "name_size": name_size,
        "head_size": head_size,
        "title_size": title_size,
        "help_size": help_size,
        "help_top": help_top,
        "table_top": table_top,
    }


# --------------------------------------------------------------------------- LVGL


def _font(size):
    """The Montserrat font of that size, else the nearest one this firmware has. Never None."""
    f = getattr(lvgl, "font_montserrat_%d" % size, None)
    if f is not None:
        return f
    for s in sorted(FONT_SIZES, key=lambda s: abs(s - size)):
        f = getattr(lvgl, "font_montserrat_%d" % s, None)
        if f is not None:
            return f
    return lvgl.font_get_default()


def _delete(obj):
    try:
        obj.delete()
    except Exception as exc:
        print("app_grid: could not delete obj", exc)


class _Measurer:
    """Measures text with an auto-sized label on a screen that is never shown"""

    def __init__(self):
        self.scr = lvgl.obj()
        self.scr.remove_style_all()
        self.scr.set_size(4 * W, H)  # wide, so a label never wraps while measuring
        self.label = lvgl.label(self.scr)
        self.cache = {}

    def has_font(self, size):
        return getattr(lvgl, "font_montserrat_%d" % size, None) is not None

    def _size(self, text, size):
        key = (size, text)
        hit = self.cache.get(key)
        if hit is None:
            self.label.set_style_text_font(_font(size), 0)
            self.label.set_text(text or " ")
            self.label.update_layout()
            hit = (self.label.get_width(), self.label.get_height())
            self.cache[key] = hit
        return hit

    def width(self, text, size):
        w, h = self._size(text, size)
        if h > self.line_h(size) * 3 // 2:
            return 4 * W  # LVGL wrapped it anyway: certainly too wide
        return w

    def line_h(self, size):
        return self._size("Ag", size)[1]

    def close(self):
        _delete(self.scr)


def _has_owner():
    """Does this badge have owner profile support (lib/owner.py)?"""
    for path in ("/lib/owner.py", "/lib/owner.mpy"):
        try:
            os.stat(path)
            return True
        except OSError:
            pass
    return False


_logo_fb = None  # framebuf.FrameBuffer, built on first use; False = unusable


def _logo():
    """The logo as a framebuf.FrameBuffer, or None if this firmware can't draw it"""
    global _logo_fb
    if _logo_fb is None:
        _logo_fb = False
        try:
            import binascii
            import framebuf

            buf = bytearray(binascii.unhexlify("".join(LOGO_HEX)))
            if len(buf) == (LOGO_W + 7) // 8 * LOGO_H:
                _logo_fb = framebuf.FrameBuffer(buf, LOGO_W, LOGO_H, framebuf.MONO_HLSB)
            else:
                print("app_grid: logo data has the wrong size")
        except Exception as exc:
            print("app_grid: logo unavailable:", exc)
    return _logo_fb or None


def parse_pbm(data, max_size=SIGNATURE_MAX):
    """(width, height, bits) from binary PBM ("P4") data, or None if it isn't one or is
    bigger than max_size. bits are MONO_HLSB rows padded to a byte, 1 = black."""
    if data[:2] != b"P4":
        return None
    fields = []
    i = 2
    while len(fields) < 2:
        while i < len(data) and data[i] in (9, 10, 13, 32):
            i += 1
        if i < len(data) and data[i] == 35:  # "#" comment up to the end of the line
            while i < len(data) and data[i] not in (10, 13):
                i += 1
            continue
        start = i
        n = 0
        while i < len(data) and 48 <= data[i] <= 57:
            n = n * 10 + data[i] - 48
            i += 1
        if i == start or i >= len(data):
            return None
        fields.append(n)
    w, h = fields
    i += 1  # the single whitespace after the height
    size = (w + 7) // 8 * h
    if not (0 < w <= max_size[0] and 0 < h <= max_size[1]) or len(data) - i < size:
        return None
    return w, h, bytearray(data[i:i + size])


def _signature():
    """(w, h, framebuf) from SIGNATURE_FILE, or None"""
    try:
        with open(SIGNATURE_FILE, "rb") as f:
            data = f.read(SIGNATURE_MAX[0] * SIGNATURE_MAX[1] // 8 + 512)
    except OSError:
        return None  # no signature, nothing to say
    try:
        pbm = parse_pbm(data)
        if pbm is None:
            print("app_grid: skipping", SIGNATURE_FILE, "- not a P4 PBM of at most %dx%d" % SIGNATURE_MAX)
            return None
        import framebuf

        w, h, bits = pbm
        return w, h, framebuf.FrameBuffer(bits, w, h, framebuf.MONO_HLSB)
    except Exception as exc:
        print("app_grid: signature unavailable:", exc)
        return None


def _blit(fb, x, y, w, h):
    """Blit a 1-bit image into the black framebuffer, after LVGL has rendered and before
    display.update(). Never raises: no picture is better than no screen."""
    try:
        if fb is None:
            return
        display.black.blit(fb, x, y, 0)  # key 0: only the black pixels are copied
        display.red.fill_rect(x, y, w, h, 0)  # keep it pure black
    except Exception as exc:
        print("app_grid: image draw failed:", exc)


def draw(names, pinned=-1, selected=0):
    """Draw the table to the panel. Blocks while LVGL renders, like an app's screen draw.
    LVGL objects are freed again afterwards (its heap is small)."""
    sig = _signature()
    m = _Measurer()
    try:
        lay = layout(names, pinned, selected, m, OWNER_HELP if _has_owner() else (),
                     sig and sig[:2])
    finally:
        m.close()

    colors = {BLACK: lvgl.color_hex(0x000000), RED: lvgl.color_hex(0xFF0000)}
    aligns = {"left": lvgl.TEXT_ALIGN.LEFT, "center": lvgl.TEXT_ALIGN.CENTER,
              "right": lvgl.TEXT_ALIGN.RIGHT}
    scr = lvgl.obj()
    scr.remove_style_all()
    scr.set_size(W, H)
    scr.set_style_bg_color(lvgl.color_hex(0xFFFFFF), 0)
    scr.set_style_bg_opa(lvgl.OPA.COVER, 0)
    objs = []
    images = []
    for item in lay["items"]:
        kind = item[0]
        if kind in ("logo", "signature"):
            images.append(((_logo() if kind == "logo" else sig[2]),) + item[1:])
            continue
        if kind == "text":
            _, x, y, w, size, color, lines, align = item
            o = lvgl.label(scr)
            o.set_pos(x, y)
            o.set_width(w)
            o.set_style_text_font(_font(size), 0)
            o.set_style_text_color(colors[color], 0)
            o.set_style_text_align(aligns[align], 0)
            o.set_text("\n".join(lines))
        else:
            o = lvgl.obj(scr)
            o.remove_style_all()
            o.set_pos(item[1], item[2])
            o.set_size(item[3], item[4])
            if kind == "box":
                o.set_style_border_width(item[5], 0)
                o.set_style_border_color(colors[item[6]], 0)
                o.set_style_border_opa(lvgl.OPA.COVER, 0)
            else:
                o.set_style_bg_color(colors[item[5]], 0)
                o.set_style_bg_opa(lvgl.OPA.COVER, 0)
        objs.append(o)

    print("app_grid: %d apps, rows %d of %d, names %d px, help %d px"
          % (len(names), lay["rows_shown"], lay["rows"], lay["name_size"], lay["help_size"]))
    display.clear()
    lvgl.screen_load(scr)
    scr.update_layout()
    try:
        # The framebuffer was just cleared to white: only render what's on it
        display.lv_display.inv_p = 0
        for o in objs:
            o.invalidate()
    except Exception:
        scr.invalidate()
    lvgl.refr_now(display.lv_display)
    for image in images:
        _blit(*image)
    display.update()

    # The pixels are in the framebuffer now. Go back to the default screen and free ours.
    lvgl.screen_load(display.lv_screen)
    try:
        display.lv_display.inv_p = 0
    except Exception:
        pass
    _delete(scr)
