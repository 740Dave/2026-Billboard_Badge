# Artwork

Show your own images on the badge's 960 x 672 white/black/red e-paper panel.

- `apps/artwork.py` runs on the badge. It shows every `.png` in `/data/artwork/` in
  alphabetical order: **User A** = next, **User B** = previous. It remembers the last image
  across reboots and lights the LED bar red if the folder is empty or an image fails to load.
  Set `AUTO_ADVANCE_S` near the top for a slideshow.
- `prepare_artwork.py` runs on your computer. It fits/fills any image to 960 x 672, reduces it
  to the three panel colours (dithered by default) and writes the non-interlaced 2-bit palette
  PNG the badge's decoder needs into `./out/`. `--push` uploads the results to `/data/artwork/`.

## Install

With [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html) (`pip install mpremote pillow`).
Use `resume` so mpremote doesn't soft-reset the badge (LVGL doesn't survive a soft reset):

```bash
mpremote resume cp user_apps/artwork/apps/artwork.py :/apps/artwork.py + reset
cd user_apps/artwork && python3 prepare_artwork.py ~/Pictures/*.jpg --push
```

Or copy `apps/artwork.py` into `firmware/badge/apps/` and run `python3 scripts/update.py push --reset`
from `firmware/`.

Or use the [Badge Manager](../../tools/badge_manager/README.md): the **Artwork** tab converts and
uploads images and installs this app if it's missing.

Credit: 740Dave
