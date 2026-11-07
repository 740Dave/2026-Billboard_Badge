# Maker Tag

A two-column name tag for the Hackaday Supercon 2026 Billboard Badge
(960x672 white/black/red e-paper).

```
+------------------------------+-------------------------------+
|         HELLO, I AM          |            I MAKE             |   <- red band, white text
+------------------------------+-------------------------------+
|                              |                               |
|            dave              |    weird synthesizers out     |   <- big black text,
|                              |      of junk and old toys     |      auto-sized per column
|                              |                               |
+------------------------------+-------------------------------+
|                    Hackaday Supercon 2026                    |
+--------------------------------------------------------------+
```

If you haven't set what you make, the right column, its header and the divider are left
out and the tag is a single full-width "HELLO, I AM" band with your handle centered below
it (fitted to the full 912 px width, so it can use a bigger font).

## Install

Copy `apps/maker_tag.py` to `/apps/maker_tag.py` on the badge (for example with the
Badge Manager) and reboot. No other files are needed.

## Where the text comes from

The app **only reads**. First the owner profile `/data/owner.json`, written by the Badge
Manager (read through `lib/owner.py` when the firmware has it):

```json
{"maker_handle": "dave", "makes": "robots"}
```

Anything missing there falls back to the legacy Maker Detector file `/data/maker.json`:

| value | from |
|---|---|
| handle | `owner.json` `maker_handle`, else `maker.json` `display_name`, else `maker.json` `name` |
| makes | `owner.json` `makes` if the key exists (an empty string means "none"), else `maker.json` `makes` |

If no handle is set anywhere, the tag shows **Your maker handle** plus a small red hint:
*Set it in Badge Manager > Settings > User Settings*. An empty "makes" is not a
placeholder: it switches to the one-column layout.

Only printable ASCII is drawn (the built-in Montserrat fonts have no other letters).
Common accented letters are folded (é -> e, ü -> u, curly quotes -> straight quotes);
other characters are dropped.

## Controls

| button | action |
|---|---|
| User A | Re-read the profile and redraw |
| User B | Swap colors: red handle and black header band (press again to go back) |

The screen is drawn once when you open the app (and again each time you come back to it),
not continuously. A full e-paper refresh takes about 30 s; button presses during a refresh
are remembered and applied when it finishes. The LED bar is blue while the panel refreshes
and dim green when it is done.

## Fitting

Each column is 480 px wide with 24 px margins (432 px of text); in the one-column layout
the handle gets the full 912 px. For each column the app tries Montserrat 48, 46, ... down
to 16 and uses the largest size whose word-wrapped text fits: the handle in at most 2
lines, "makes" in at most 3. Both headers use the same size. If even 16 px does not fit,
words are shortened with "...". Text never runs past the column or into the divider.

## Menu position

Apps are listed by the badge in the order it discovers them, which is normally
alphabetical by file name. With the stock apps plus Maker Detector that is:

| LED | app |
|---|---|
| 1 | gallery |
| 2 | maker_detector |
| 3 | **maker_tag** |
| 4 | nametag |
| 5 | schedule |

So Maker Tag is the 3rd LED, between Maker Detector and Nametag. (Without Maker Detector
installed it is the 2nd.) The firmware does not sort the list itself, so check the
"Added app:" lines on the serial console if the order looks different.

By 740Dave, Hackaday Supercon 2026. Written with Claude.
