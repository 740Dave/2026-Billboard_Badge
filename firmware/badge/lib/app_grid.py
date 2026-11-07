"""App grid: a full screen overview of the installed apps, shown by the app switcher.

One row per menu page of 8 apps, labelled with the LED colour of that page (the e-paper panel
can't show the colour itself). Column N is LED N. The selected app is only shown on the LED
bar, so the grid is drawn once and not on every button press (a refresh takes ~30 s).
The pinned boot app is drawn in red and tagged "BOOT". A help block at the bottom explains the
buttons; the grid never overlaps it.

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
COL_GAP = 4
CELL_W = (W - 2 * MARGIN - (COLS - 1) * COL_GAP) // COLS
CELL_PAD = 4
ROW_GAP = 8
LABEL_GAP = 2

# Same order as lib.led_bar.MENU_BACKGROUND_COLORS
ROW_COLOR_NAMES = ("ORANGE", "CYAN", "MAGENTA", "RED", "GREEN", "BLUE")

TITLE = "APPS"
TITLE_SIZE = 28
INFO_SIZE = 16
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
HELP_SIZES = (24, 16)  # 24 px where the rows leave room
# (row label, slot number, app name, name lines), biggest first.
# A slot number size of 0 puts the number in front of the name.
PRESETS = (
    (28, 32, 24, 3),
    (24, 24, 24, 2),
    (24, 16, 16, 2),
    (16, 16, 16, 2),
    (16, 16, 16, 1),
    (14, 0, 14, 1),
)
# The only Montserrat sizes in this firmware build. A missing font crashes LVGL.
FONT_SIZES = (14, 16, 24, 28, 32, 36, 40, 44, 48)

BLACK = "black"
RED = "red"


def _row_label(row, count):
    first = row * COLS + 1
    last = min(count, first + COLS - 1)
    span = str(first) if first == last else "%d-%d" % (first, last)
    page = row % len(MENU_BACKGROUND_COLORS)
    color = ROW_COLOR_NAMES[page] if page < len(ROW_COLOR_NAMES) else "COLOUR %d" % (page + 1)
    return "ROW %d - %s (%s)" % (row + 1, color, span)


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


def _smaller(m, size):
    """Available font sizes from size down"""
    return [s for s in sorted(FONT_SIZES, reverse=True) if s <= size and m.has_font(s)] or [size]


def _avail(m, size):
    """size if this firmware has that font, else the nearest smaller (or larger) one"""
    for s in sorted(FONT_SIZES, key=lambda s: (s > size, abs(s - size))):
        if m.has_font(s):
            return s
    return size


def _fit_name(m, name, size, max_w, max_h):
    """Largest font from size down that fits name in the box, else truncate at the smallest."""
    for s in _smaller(m, size):
        lines, fits = _wrap(m, name, s, max_w, max(1, max_h // m.line_h(s)))
        if fits:
            return s, lines
    return s, lines


def layout(names, pinned=-1, selected=0, m=None, extra_help=()):
    """Lay out the grid for the app names. Returns a dict with "items", a list of
      ("text", x, y, w, size, color, [lines], align)  align: "left" or "right"
      ("box", x, y, w, h, border_width, color)        outline
      ("fill", x, y, w, h, color)
    and the chosen sizes. Nothing ever extends outside the panel, and the grid
    stays above "help_top". extra_help: more help texts, added after HELP. m measures text: width(text, size), line_h(size), has_font(size)."""
    count = len(names)
    rows = (count + COLS - 1) // COLS
    full_w = W - 2 * MARGIN
    items = []

    # Title line: "APPS" on the left, a short note on the right
    y = MARGIN
    title_size = _avail(m, TITLE_SIZE)
    info_size = _avail(m, INFO_SIZE)
    title_h = m.line_h(title_size)
    items.append(("text", MARGIN, y, full_w // 3, title_size, BLACK, [TITLE], "left"))
    title_bottom = y + title_h + 4
    items.append(("fill", MARGIN, title_bottom, full_w, 2, BLACK))
    grid_top = title_bottom + 2 + 8

    # Biggest cells that fit, preferring 24 px help text over the smallest cells.
    # The last candidate is used even if it doesn't fit (rows are cut below).
    big, small = _avail(m, HELP_SIZES[0]), _avail(m, HELP_SIZES[-1])
    for preset, help_size in [(p, big) for p in PRESETS[:3]] + [(p, small) for p in PRESETS]:
        label_size, num_size, name_size, name_lines = preset
        label_size = _avail(m, label_size)
        num_size = num_size and _avail(m, num_size)
        name_size = _avail(m, name_size)
        preset = (label_size, num_size, name_size, name_lines)
        cell_h = 2 * CELL_PAD + name_lines * m.line_h(name_size)
        if num_size:
            cell_h += m.line_h(num_size)
        row_h = m.line_h(label_size) + LABEL_GAP + cell_h
        help_lines = []
        for text in HELP + tuple(extra_help):
            help_lines += _wrap(m, text, help_size, full_w, 2)[0]
        help_top = H - MARGIN - len(help_lines) * m.line_h(help_size) - 2 - 6
        avail = help_top - 8 - grid_top
        choice = (preset, cell_h, row_h, help_size, help_lines, help_top, avail)
        if rows * row_h + max(0, rows - 1) * ROW_GAP <= avail:
            break
    preset, cell_h, row_h, help_size, help_lines, help_top, avail = choice
    label_size, num_size, name_size, name_lines = preset

    # Too many apps for the panel: show the rows around the selected one
    shown = min(rows, max(1, (avail + ROW_GAP) // (row_h + ROW_GAP)))
    first_row = 0
    if shown < rows:
        first_row = max(0, min(rows - shown, selected // COLS - shown // 2))

    info = "%d app%s. Lit LED = selected app, LED colour = row" % (count, "" if count == 1 else "s")
    if shown < rows:
        info = "Rows %d-%d of %d. Lit LED = selected app" % (first_row + 1, first_row + shown, rows)
    if not count:
        info = "No apps installed"
    info_w = full_w - full_w // 3
    info_y = y + title_h - m.line_h(info_size)
    items.append(("text", MARGIN + full_w // 3, info_y, info_w, info_size, BLACK,
                  _wrap(m, info, info_size, info_w, 1)[0], "right"))

    # Spread spare room a little between rows, and center the rows
    extra = avail - (shown * row_h + max(0, shown - 1) * ROW_GAP)
    gap = ROW_GAP + (min(extra // shown, 24) if shown > 1 else 0)
    y = grid_top + (avail - (shown * row_h + max(0, shown - 1) * gap)) // 2

    inner_w = CELL_W - 2 * CELL_PAD
    for row in range(first_row, first_row + shown):
        items.append(("text", MARGIN, y, full_w, label_size, BLACK,
                      _wrap(m, _row_label(row, count), label_size, full_w, 1)[0], "left"))
        cy = y + m.line_h(label_size) + LABEL_GAP
        for col in range(COLS):
            idx = row * COLS + col
            if idx >= count:
                break
            x = MARGIN + col * (CELL_W + COL_GAP)
            is_pinned = idx == pinned
            color = RED if is_pinned else BLACK
            items.append(("box", x, cy, CELL_W, cell_h, 3 if is_pinned else 1, color))
            tx = x + CELL_PAD
            ty = cy + CELL_PAD
            name = " ".join(str(names[idx]).split()) or "?"
            if num_size:
                num = str(idx + 1)
                items.append(("text", tx, ty, inner_w, num_size, color, [num], "left"))
                if is_pinned:
                    for tag in _smaller(m, num_size):
                        if m.width(num + "  BOOT", tag) <= inner_w:
                            break
                    items.append(("text", tx, ty, inner_w, tag, RED, ["BOOT"], "right"))
                ty += m.line_h(num_size)
            else:
                name = "%d %s" % (idx + 1, name)
            max_h = name_lines * m.line_h(name_size)
            size, lines = _fit_name(m, name, name_size, inner_w, max_h)
            items.append(("text", tx, ty, inner_w, size, color, lines, "left"))
        y += row_h + gap

    # Help block, always at the bottom
    items.append(("fill", MARGIN, help_top, full_w, 2, BLACK))
    items.append(("text", MARGIN, help_top + 2 + 6, full_w, help_size, BLACK, help_lines, "left"))

    return {
        "items": items,
        "rows": rows,
        "first_row": first_row,
        "rows_shown": shown,
        "preset": preset,
        "help_size": help_size,
        "help_top": help_top,
        "grid_top": grid_top,
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


def draw(names, pinned=-1, selected=0):
    """Draw the grid to the panel. Blocks while LVGL renders, like an app's screen draw.
    LVGL objects are freed again afterwards (its heap is small)."""
    m = _Measurer()
    try:
        lay = layout(names, pinned, selected, m, OWNER_HELP if _has_owner() else ())
    finally:
        m.close()

    colors = {BLACK: lvgl.color_hex(0x000000), RED: lvgl.color_hex(0xFF0000)}
    aligns = {"left": lvgl.TEXT_ALIGN.LEFT, "right": lvgl.TEXT_ALIGN.RIGHT}
    scr = lvgl.obj()
    scr.remove_style_all()
    scr.set_size(W, H)
    scr.set_style_bg_color(lvgl.color_hex(0xFFFFFF), 0)
    scr.set_style_bg_opa(lvgl.OPA.COVER, 0)
    objs = []
    for item in lay["items"]:
        kind = item[0]
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

    print("app_grid: %d apps, %d rows shown, sizes %s, help %d"
          % (len(names), lay["rows_shown"], lay["preset"], lay["help_size"]))
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
    display.update()

    # The pixels are in the framebuffer now. Go back to the default screen and free ours.
    lvgl.screen_load(display.lv_screen)
    try:
        display.lv_display.inv_p = 0
    except Exception:
        pass
    _delete(scr)
