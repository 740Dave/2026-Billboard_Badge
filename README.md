# About this fork

Community tools and apps for the Supercon 2026 Billboard Badge by [740Dave](https://github.com/740Dave), built at Supercon 2026. Hackaday's original README follows below, unchanged.

## What's here

| Path | What it is |
|---|---|
| [`tools/badge_manager`](tools/badge_manager) | Desktop GUI for the badge over USB: Device, Firmware, Apps, Artwork, Maker Detector and Settings tabs. Dark UI, polls the community forks for updates every 15 minutes, first-run setup |
| [`tools/back_cover`](tools/back_cover) | Printable snap-on back cover. Tested on a Prusa XL in PLA, 0.20 mm layers, no supports |
| [`user_apps/maker_detector`](user_apps/maker_detector) | BLE badge finder: counts other badges nearby and shows their handles |
| [`user_apps/maker_tag`](user_apps/maker_tag) | Two-column "HELLO, I AM / I MAKE" name tag |
| [`user_apps/artwork`](user_apps/artwork) | Show your own images on the badge; rotates every 5 minutes |
| [`user_apps/sao_blink`](user_apps/sao_blink) | SAO blink example, plus an ESP32 SAO example ([`esp32_sao_blinker`](user_apps/sao_blink/esp32_sao_blinker)) |
| [`docs/community_guide.md`](docs/community_guide.md) | Community guide |

## Upstream proposals

Each one is independent unless noted.

- [#17 Printable back cover](https://github.com/Hack-a-Day/2026-Billboard_Badge/pull/17)
- [#18 Resume the last app at boot](https://github.com/Hack-a-Day/2026-Billboard_Badge/pull/18)
- [#19 Owner profile + Nametag handle](https://github.com/Hack-a-Day/2026-Billboard_Badge/pull/19)
- [#20 App grid launcher](https://github.com/Hack-a-Day/2026-Billboard_Badge/pull/20) (builds on #18)
- [#21 BLE presence broadcast](https://github.com/Hack-a-Day/2026-Billboard_Badge/pull/21) (opt-in, off by default)

## Branches

- `main` mirrors Hack-a-Day/main.
- `badge-manager` (default) has everything above.
- The PR branches are kept clean for upstream.

## Personalizing a badge by hand

Create `/data/owner.json` on the badge:

```json
{"maker_handle": "your-handle", "makes": "what you make"}
```

Use a handle, not your legal name. Edit it over USB with [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html), [Thonny](https://thonny.org) or [viper-ide.org](https://viper-ide.org).

## Badge Manager quick start

```bash
cd tools/badge_manager
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                 # with a badge plugged in
BADGE_FAKE=1 .venv/bin/python app.py    # try it without a badge
```

Built with [Claude Code](https://claude.com/claude-code).

---

![Billboard_Badge](assets/frame_graphic_demo.png "2026 Hackaday Supercon Billboard Badge")

# Badge Hardware
The badge uses a 9.7 inch Pervasive Displays E2969JS0B1 electronic paper panel that was recycled from price tags which were destined for a now bankrupt German department store chain, coupled with an ESP32-S3 microcontroller.

## Pins

The board only has two truly free GPIOs

* GPIO9
* GPIO41

The SAO ports, however, have many possibilities

* SAO1: GPIO2 and GPIO42
* SAO2: GPIO4 and GPIO5
* SAO3: GPIO7 and GPIO6
* I2C bus: SDA on GPIO17 and SCL on GPIO18

The row of WS2812-compatible LEDs is on GPIO38

## Buttons

* Menu +/-: GPIO40 / GPIO15
* User A/B: GPIO39 / GPIO15

# Applications
In its out of the box configuration the badge can show the 2026 Supercon schedule, act as a digital name tag, and cycle through a built-in collection of art from Hackaday's incredible Art Director Joe Kim. If you come up with additional apps that you'd like to share, feel free to make a pull request.

# Displaying Images

The badge's display only supports three colors: white, black, red. Shading between those needs to be implemented by dithering pixels of those colors together. Unlike LCD, CRT, or other displays you may be used to, you cannot set multiple colors within a single pixel, each pixel is only one of the three colors.

The badge firmware picks a color for each pixel based on this logic:
- If the red value is >50% and blue and green are both <50%, then the pixel will be red.
- If the red, green, and blue values of the pixel are all <50%, then the pixel will be black.
- Otherwise the pixel will be white.

Additionally, since the display is so large, loading large images can easily run out of memory. Therefore, it is best to compress each image down to a limited color space before placing on the badge. This is easily done with [ImageMagick](https://imagemagick.org/), namely with the command `magick oldfile.png -remap scripts/palette.gif newfile.png`. This reduces the image down to a colorspace that only supports 3 colors, which are white, black, and red.

Based on the above pixel logic, where each pixel is already one of the three supported colors, it's easy to see how the image will be displayed. Luckily, but converting the image on your computer, you can see what it will look like before rendering it on the badge.
