# Maker Tag

A two-column name tag for the Hackaday Supercon 2026 Billboard Badge
(960x672 white/black/red e-paper).

```
+------------------------------+-------------------------------+
|        HI, MY NAME IS        |        I LIKE TO MAKE         |   <- red band, white text
+------------------------------+-------------------------------+
|                              |                               |
|            Dave              |    weird synthesizers out     |   <- big black text,
|                              |      of junk and old toys     |      auto-sized per column
|                              |                               |
+------------------------------+-------------------------------+
|                    Hackaday Supercon 2026                    |
+--------------------------------------------------------------+
```

## Install

Copy `apps/maker_tag.py` to `/apps/maker_tag.py` on the badge (for example with the
Badge Manager) and reboot. No other files are needed.

## Where the text comes from

The app **only reads** `/data/maker.json`, the same file the Badge Manager writes and the
Maker Detector app uses:

```json
{"id": "a1b2c3", "name": "Dave", "makes": "robots", "broadcast": true}
```

| key | used for |
|---|---|
| `display_name` | Optional. Shown as your name if present and non-empty. Lets the tag show a longer name than the 16-character Bluetooth broadcast name. |
| `name` | Shown as your name when there is no `display_name`. |
| `makes` | Shown in the right column. |

If the file is missing or unreadable, or a field is empty, the tag shows **Your name** /
**Something cool** plus a small red hint: *Set these in Badge Manager > Settings > User
Settings*.

Only printable ASCII is drawn (the built-in Montserrat fonts have no other letters).
Common accented letters are folded (é -> e, ü -> u, curly quotes -> straight quotes);
other characters are dropped.

Note: Maker Detector rewrites `maker.json` with only its own four keys when it has to fix
something in the file, so a hand-added `display_name` can be lost then. The tag will just
fall back to `name`.

## Controls

| button | action |
|---|---|
| User A | Re-read `/data/maker.json` and redraw |
| User B | Swap colors: red name and black header band (press again to go back) |

The screen is drawn once when you open the app (and again each time you come back to it),
not continuously. A full e-paper refresh takes about 30 s; button presses during a refresh
are remembered and applied when it finishes. The LED bar is blue while the panel refreshes
and dim green when it is done.

## Fitting

Each column is 480 px wide with 24 px margins (432 px of text). For each column the app
tries Montserrat 48, 46, ... down to 16 and uses the largest size whose word-wrapped text
fits: the name in at most 2 lines, "makes" in at most 3. If even 16 px does not fit, words
are shortened with "...". Text never runs past the column or into the divider.

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
