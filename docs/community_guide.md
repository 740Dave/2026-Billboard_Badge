# Hackaday Supercon 2026 Billboard Badge: Field Guide

*Compiled 2026-11-06 from [Hack-a-Day/2026-Billboard_Badge](https://github.com/Hack-a-Day/2026-Billboard_Badge) (main @ `d69c96b`) and all 26 public forks as of that evening.*

**Hardware at a glance**

| | |
|---|---|
| MCU | ESP32-S3, 8 MB flash, octal PSRAM, native USB-C |
| Display | 9.7" Pervasive Displays E2969JS0B1 e-paper, **960 × 672**, white / black / red only |
| Inputs | 4 buttons (Menu −, Menu +, User A, User B) + BOOT + RESET |
| Outputs | 8 WS2812 RGB LEDs (GPIO38), status LED (GPIO1) |
| Expansion | 3 × SAO v2 headers, shared I²C bus; free test points GPIO9 and GPIO41 |
| Software | MicroPython **1.24.1** with LVGL built in (that's what a shipped badge reports; the repo README says 1.27); apps are plain `.py` files on the badge's filesystem |

Files that come with this guide (all paths relative to the repo root):

- [user_apps/artwork/apps/artwork.py](../user_apps/artwork/apps/artwork.py): the custom artwork viewer app (section D)
- [user_apps/artwork/prepare_artwork.py](../user_apps/artwork/prepare_artwork.py): converts any image into badge format and uploads it
- [user_apps/sao_blink/apps/sao_blink.py](../user_apps/sao_blink/apps/sao_blink.py) and [user_apps/sao_blink/esp32_sao_blinker/esp32_sao_blinker.ino](../user_apps/sao_blink/esp32_sao_blinker/esp32_sao_blinker.ino): the badge-plus-ESP32 SAO example (section E)
- [tools/back_cover/](../tools/back_cover/README.md): a printable snap-on back cover (STL, 3MF, STEP and the parametric source)
- [tools/badge_manager/](../tools/badge_manager/README.md): a desktop app that manages the badge over USB and keeps every community fork as a git remote of your clone (`git log kcriqui/main` and so on)

---

## A. Connecting the badge to your Mac to edit code

The badge runs MicroPython. You don't compile anything to change apps. You copy `.py` files onto its filesystem over USB, then reset it.

### 1. One-time setup

```bash
python3 -m venv ~/badge-venv
```
```bash
source ~/badge-venv/bin/activate && pip install mpremote esptool pillow
```
```bash
git clone https://github.com/Hack-a-Day/2026-Billboard_Badge.git && cd 2026-Billboard_Badge/firmware
```

(Homebrew Python refuses a global `pip install`, which is why the commands use a venv. Run `source ~/badge-venv/bin/activate` in each new terminal.)

### 2. Plug in and find the port

Use a **data-capable** USB-C cable and switch the badge on. The ESP32-S3's native USB shows up as a serial port:

```bash
ls /dev/cu.usbmodem*
```

You should see something like `/dev/cu.usbmodem1101` (USB ID `303a:4001`). If nothing appears, try another cable, since charge-only cables are common.

### 3. Talk to it

```bash
mpremote connect auto repl
```

You'll see the firmware's log, including a `Main 60s heartbeat --^v--^v--` line every minute. Press **Ctrl-C** to stop `main.py` and get a `>>>` prompt, and **Ctrl-]** to leave.

### 4. Day-to-day workflow (official helper script)

From `firmware/`:

| Command | What it does |
|---|---|
| `python3 scripts/update.py ls` | Compares `firmware/badge/` on your Mac with the badge (`*` changed, `+` local only, `-` badge only) |
| `python3 scripts/update.py push --reset` | Copies changed files to the badge, **deletes badge files that aren't in your local `badge/` folder (except under `/data`)**, then resets |
| `python3 scripts/update.py pull` | Backs up the badge's whole filesystem to `badge-backup/` |
| `mpremote cp myfile.py :/apps/myfile.py` | Copies a single file |
| `mpremote reset` | Hard reset (use this to restart after copying files) |

Your local `firmware/badge/` folder is a mirror of the badge's `/` (`/main.py`, `/apps/`, `/lib/`, `/data/`). **To add an app, drop a `.py` into `firmware/badge/apps/` and push.** The app switcher finds every class in `/apps/*.py` that subclasses `BaseApp` at boot. Use `apps/template.py` as a starting point.

### 5. Gotchas found across the forks

- **Avoid soft reboots.** Ctrl-D in the REPL, Thonny's "Run" button, and `mpremote run` all soft-reset. LVGL doesn't survive a soft reset, so the badge hangs or reboot-loops. Always use `mpremote reset` or the RESET button. ([todbot's fork](https://github.com/todbot/2026-Billboard_Badge) has a `main.py` patch that fixes this, and [kcriqui's](https://github.com/kcriqui/2026-Billboard_Badge) `dev.sh` documents it.)
- **Use `update.py push` with care.** If you hand-copy an app to the badge but it isn't in your local `firmware/badge/apps/`, the next `push` deletes it. Keep everything in your clone.
- **Expect slow redraws.** A full three-color refresh takes about 30 s or more on the stock driver. The forks from trandi, FacioErgoSum and kcriqui carry driver speed-ups.
- **The silkscreen is wrong.** It swaps Menu + and Menu −, per `lib/button.py`. The top-level README also lists User B as GPIO15, but the code uses **GPIO16**.
- **Thonny works too:** Interpreter → MicroPython (ESP32) → your `usbmodem` port. Keep the soft-reset caveat in mind.

### 6. Re-flashing or recovering the firmware (rarely needed)

Only do this if the badge won't boot or you want stock firmware back. First back up your badge's full 8 MB flash:

```bash
python3 -m esptool --no-stub read-flash 0 8M my_badge_backup.bin
```

To enter the bootloader, **hold BOOT** while you press and release **RESET**, or hold BOOT while plugging in. Then, from `firmware/bin/`, restore the full factory image, which includes the filesystem and apps:

```bash
python3 -m esptool --chip esp32s3 -b 460800 write-flash 0 supercon26_factory.bin
```

Or flash only the MicroPython+LVGL runtime and push the Python files afterwards with `update.py push`. That sequence lives in `firmware/bin/flash_command`. Use `--flash-size 8MB`: the production badges have 8 MB, and the 16 MB in the firmware README is stale, per [type11error's fix](https://github.com/type11error/2026-Billboard_Badge/tree/fix/8mb-flash-config).

There's also a **desktop simulator** in the forks from [nathancharlesjones](https://github.com/nathancharlesjones/2026-Billboard_Badge/tree/badge-simulator) and [cosmosified](https://github.com/cosmosified/2026-Billboard_Badge). It runs the real firmware on your Mac with emulated buttons, LEDs, the panel and SAOs, which is handy for developing without waiting on e-paper refreshes.

---

## B. The forks

The 26 forks fall into two groups. The 14 with their own changes come first, most substantial first. All activity was on 2026-11-06 (Supercon weekend).

### Forks with real changes

- **[cosmosified/2026-Billboard_Badge](https://github.com/cosmosified/2026-Billboard_Badge)** (Chris Morton, 38 commits). An integration fork that merges most of the community work: Mandelbrot, Game of Life, Quick Draw, the desktop simulator, the binfont loader and the SAO fixes. It adds a *Cosmotronics logo* app with a random rainbow LED show, a personalised nametag, boot and soft-reboot test scripts, and a `uv`/`pyproject.toml` setup for the host tools. **This is the best single fork to start from if you want everything.**
- **[kcriqui/2026-Billboard_Badge](https://github.com/kcriqui/2026-Billboard_Badge)** (Kevin Criqui, 22 commits). Adds an **XKCD app** that pulls a random comic over Wi-Fi every 5 minutes, resizes and filters it for e-paper, and uses fast refresh for black-and-white comics. Also adds Wi-Fi support (`lib/wifi.py` with `data/wifi.json`) and a general **`fit_png.py`** that scales any PNG on the badge. The nametag shows a photo, including a "recursive" photo of the badge wearing itself, and the badge reopens the last app after a restart. It merges the faster e-paper driver, provides a `dev.sh` workflow wrapper, and adds desktop tools (`make_nametag.py`, `make_photo.py`). It's also set up for Claude Code, with a `CLAUDE.md` and memory files.
- **[FacioErgoSum/2026-Billboard_Badge](https://github.com/FacioErgoSum/2026-Billboard_Badge/tree/firmware/fast-refresh-input-fixes-sketch)** (Geoff McIntyre, branch `firmware/fast-refresh-input-fixes-sketch`). Rewrites the e-paper driver to use hardware SPI, prepare frames faster and support an optional fast refresh. It fixes phantom button presses and stuck LED sequences, and adds a **Sketch (Etch-a-Sketch) app** driven by two AS5600 magnetic-knob SAOs, with shake-to-erase from an MPU6050. **This is the best real-world SAO example.**
- **[facingtomorrow/2026-Billboard_Badge](https://github.com/facingtomorrow/2026-Billboard_Badge)**. Adds a **battery indicator** app and a small battery icon in the screen corner on every app, reading the `GPIO14` voltage divider (`lib/battery.py`).
- **[trandi/SuperCon2026_Badge](https://github.com/trandi/SuperCon2026_Badge)** (branches `epd-speedup`, `fractal-app`). Optimises `epd.py` with hardware SPI and Viper code, which the author says saves about 28 s per refresh, and adds a **three-color Mandelbrot fractal viewer**. Both were sent upstream as PRs #4 and #6.
- **[nathancharlesjones/2026-Billboard_Badge](https://github.com/nathancharlesjones/2026-Billboard_Badge/tree/badge-simulator)** (branch `badge-simulator`). A **desktop simulator** (`firmware/sim/`) that runs the unmodified firmware on the LVGL MicroPython Unix port. You get a window with the panel, LEDs and clickable buttons, an option for realistic refresh timing, and a TCP "SAO bus" so you can emulate I²C add-ons in plain Python (`sao_example.py`). It also includes small fixes to the app switcher, LEDs and `sao.py`.
- **[Cosmin-Mare/2026-Billboard_Badge](https://github.com/Cosmin-Mare/2026-Billboard_Badge)** (branches `quick-draw-app`, `menu-page-overflow`). Adds **Quick Draw**, a two-player reaction game where A and B race, plus a `pixtext.py` pixel-text helper. Also fixes a crash in the LED menu when more than 48 apps are installed.
- **[godfatherhaunter/2026-Billboard_Badge](https://github.com/godfatherhaunter/2026-Billboard_Badge/tree/game_of_life_app)** (branch `game_of_life_app`). Adds a **Conway's Game of Life** app.
- **[trailhead/2026-Billboard_Badge](https://github.com/trailhead/2026-Billboard_Badge)** (Chris Miller). Adds `lib/binfont.py`, a pure-Python loader for LVGL binary fonts, so apps can use custom fonts and sizes beyond the built-in Montserrat set.
- **[todbot/2026-Billboard_Badge](https://github.com/todbot/2026-Billboard_Badge)** (Tod Kurt). A small but important `main.py` fix: it cleans up LVGL on exit and force-resets after a soft reboot, so Thonny and mpremote stop crashing the badge.
- **[parallellogic-/2026-Billboard_Badge](https://github.com/parallellogic-/2026-Billboard_Badge)**. Fixes `lib/sao.py`. Upstream `Sao.set_address()` ignores the address you pass and builds an I²C *target* instead of a controller. The fix also drops syntax that older MicroPython (1.24) rejects.
- **[eosti/2026-Billboard_Badge](https://github.com/eosti/2026-Billboard_Badge/tree/patch-1)** (branch `patch-1`). A one-line fix: `leds.set()` now actually writes to the LEDs.
- **[type11error/2026-Billboard_Badge](https://github.com/type11error/2026-Billboard_Badge/tree/fix/8mb-flash-config)** (branch `fix/8mb-flash-config`). Fixes the PlatformIO flash-size config for 8 MB badges in the C/Arduino projects (`firmware/esp32s3-itc97*`) that existed in an earlier version of the repo.
- **[K9KJ/2026-Billboard_Badge](https://github.com/K9KJ/2026-Billboard_Badge)** (TJ). Puts the owner's name into the nametag app, which is the two `set_text("")` lines in `apps/nametag.py`. A good example of the minimum change needed to personalise your badge.

### Forks with no changes of their own

These are unmodified copies of upstream, some a few commits behind. They're placeholders, or their owners' work is still local or already merged upstream. Naitotchi's `main.py` fix was merged upstream as PR #10.

- [Naitotchi](https://github.com/Naitotchi/2026-Billboard_Badge), [frogCircuit](https://github.com/frogCircuit/2026-Billboard_Badge), [miloh](https://github.com/miloh/2026-Billboard_Badge) and [murriel](https://github.com/murriel/2026-Billboard_Badge): identical to the current upstream.
- [sublettj](https://github.com/sublettj/2026-Billboard_Badge): 2 commits behind upstream.
- [devstuff](https://github.com/devstuff/Hack-a-Day_2026-Billboard_Badge), [ianfm](https://github.com/ianfm/2026-Billboard_Badge), [ilektron](https://github.com/ilektron/2026-Billboard_Badge), [wormyrocks](https://github.com/wormyrocks/2026-Billboard_Badge) and [zagrev](https://github.com/zagrev/2026-Billboard_Badge) (the latter also has an empty `hacks` branch): 4 behind.
- [jkunkee](https://github.com/jkunkee/2026-Billboard_Badge): 8 behind.
- [TomKeddie](https://github.com/TomKeddie/2026-Billboard_Badge): 10 behind.

### Do any forks have STL files for a printed case?

**No.** No branch of upstream or any fork contains an `.stl`, `.3mf`, `.obj` or OpenSCAD file. The closest material is in **upstream `hardware/`**:

- `billboard_mockup-Frame.step`: a **front frame/bezel** solid, 227.6 × 167.5 × **5 mm** flat. This is the one printable-ready part. Open it in FreeCAD, PrusaSlicer, Bambu Studio or Fusion and export or slice it.
- `billboard.FCStd` and `billboard_mockup.FCStd`: the FreeCAD models of the board with display, frame and "banana for scale". These are parametric, with a spreadsheet of dimensions, so you can derive your own parts from them.
- `billboard_pcb/billboard_pcb.step`: the full PCB with components. **Use this as the reference to model a back cover**, so you get clearance for the USB-C port, buttons, SAO headers and battery connector.
- `billboard_mockup-PCB_with_dimensions.svg`: a dimensioned 2D outline.

There's **no back shell anywhere** in the repos. **A snap-on back cover generated from these files is now in [tools/back_cover/](../tools/back_cover/README.md)** (STL, 3MF and STEP, plus a parametric script). For a front-and-back case, start from the Frame STEP for the front and model a back plate around `billboard_pcb.step`. Leave openings for the USB-C port, BOOT and RESET, the four buttons along the edge, the three SAO headers (plus the height of whatever you plug in), and the battery pads.

---

## C. Navigating the menus

There's no on-screen menu. **The LED bar is the menu.**

### Boot

1. On the first boot, or after the load counter in `/data/panel_config.json` is reset, it shows the Supercon splash screen. The 42nd boot shows an easter-egg image.
2. The badge then starts **in menu (selection) mode**. Each of the 8 LEDs stands for an app. The lit "background" LEDs show how many apps exist, and the selected one glows white. The background color tells you which "page" of 8 you're on: orange is apps 1–8, cyan 9–16, magenta 17–24, and so on.

### Buttons

| Button | In menu mode | Inside an app |
|---|---|---|
| **Menu +** | Move selection right | Return to the menu (current app pre-selected) |
| **Menu −** | Move selection left | Return to the menu |
| **User A** | **Launch** the selected app | App-specific |
| **User B** | **Launch** the selected app | App-specific |

Remember that Menu + and Menu − are swapped on the silkscreen. If "+" goes backwards, that's why.

### Stock apps (LED order)

Apps are ordered alphabetically by file name, then in class order within a file:

| # | App | What it does | Controls |
|---|---|---|---|
| 1 | **Gallery** | Random piece from 44 artworks by Joe Kim, with a new one every 5 minutes | none |
| 2 | **Nametag** | "HELLO I AM" sticker. The name is blank by default, so edit `apps/nametag.py` | none |
| 3–5 | **Sat Talks, Sun Talks, Workshops** | Supercon schedule tables from `/data/schedule.csv` | none |

When you install the Artwork app from section D, it becomes **#1**, since `artwork.py` sorts first, and everything else shifts right by one.

The LEDs also signal status inside apps. By convention, **cyan or blue means the panel is busy drawing** and **green means done**. Don't expect instant response, because a refresh takes many seconds.

---

## D. Artwork app: show your own images

### What you get

- **[user_apps/artwork/apps/artwork.py](../user_apps/artwork/apps/artwork.py)** runs on the badge. It shows every `.png` in `/data/artwork/` in alphabetical order. **User A** shows the next image and **User B** the previous one. Presses made during a refresh are queued, not lost. It remembers the last image shown across reboots, rescans the folder every time you open the app, and turns the LEDs red if the folder is empty or a file is bad. Set `AUTO_ADVANCE_S = 600` near the top for a 10-minute slideshow.
- **[user_apps/artwork/prepare_artwork.py](../user_apps/artwork/prepare_artwork.py)** runs on your Mac. It takes any JPG, PNG or other image Pillow can open and:
  1. fits it to 960 × 672, either letterboxed on white (`--mode fit`, the default) or cropped to fill (`--mode fill`);
  2. reduces it to white, black and red, with Floyd–Steinberg dithering by default (use `--no-dither` for logos and line art, `--no-red` for black and white only, `--rotate 90` for portrait images);
  3. writes the exact format the badge's decoder requires, a 2-bit *palette* PNG that isn't interlaced, usually 10–100 KB, into `./out/`;
  4. with `--push`, copies the results to `/data/artwork/` on the badge.

### Install (once)

From the repo root, copy the app into the firmware tree, then push it:

```bash
cp user_apps/artwork/apps/artwork.py firmware/badge/apps/
```
```bash
cd firmware && python3 scripts/update.py push --reset
```

### Add artwork (any time)

```bash
cd user_apps/artwork && python3 prepare_artwork.py ~/Pictures/badge/*.jpg --push
```

Then on the badge: press **Menu ±** to open the menu, select LED #1 (**Artwork**), and press **A**. Each image takes a few seconds to decode, then the panel refreshes.

Preview the files in `out/` on your Mac first. What you see there is exactly what the panel shows. Images in `/data/` aren't deleted by `update.py push`, so your artwork survives firmware-file updates. The filesystem has a few MB free, enough for dozens of images at 10–100 KB each.

**Tested so far:** the converter output was checked against the badge decoder's rules (palette PNG, 2-bit, not interlaced, white/black/red palette), and the app's button, queueing and wrap-around logic was run with simulated hardware. I haven't run it on a physical badge yet.

---

## E. Driving a custom ESP32 board from the SAO port

### SAO pinout on this badge

There are three identical SAO v2 headers (J8, J9, J10). Each is 2 × 3 at 2.54 mm, keyed. Viewed from the front with the notch up:

```
          ┌───notch───┐
  pin 1 → │ 3V3   GND │ ← pin 2
  pin 3 → │ SDA   SCL │ ← pin 4
  pin 5 → │ GPIO1 GPIO2│ ← pin 6
          └───────────┘
```

| Signal | SAO1 | SAO2 | SAO3 |
|---|---|---|---|
| 3V3 | badge 3.3 V rail | same | same |
| GND | GND | GND | GND |
| SDA / SCL | GPIO17 / GPIO18 (**one shared bus**) | same | same |
| GPIO1 | GPIO2 | GPIO4 | GPIO7 |
| GPIO2 | GPIO42 | GPIO5 | GPIO6 |

Check which header is which against the silkscreen or `hardware/billboard_pcb/sao.kicad_sch` before wiring.

### Power budget: the critical part

- The SAO **3V3** pin comes from the badge's AP2112K-3.3 regulator, which is rated **600 mA** and **shared** with the badge's own ESP32-S3, the e-paper booster and the LEDs. On battery, it's the battery's budget too.
- A bare ESP32 running a few LEDs with **Wi-Fi and Bluetooth off** draws about 20–50 mA, which is fine. With **Wi-Fi on**, it peaks at 300–400 mA, which can brown out the badge during an e-paper refresh. **Keep the SAO's radio off**, or limit its TX power.
- Feed the badge's 3.3 V into your ESP32 module's **3V3 pin, not 5V or VIN**. Running 3.3 V through a dev board's on-board regulator leaves it starved.
- Put a **100–220 µF** capacitor plus 100 nF across 3V3 and GND on your SAO board. Keep any LED load small. A handful of WS2812s at low brightness is fine, but full-white strips aren't.
- **Never connect the SAO ESP32's own USB while it's plugged into the badge.** Two 3.3 V supplies fight each other. Program it off-badge, or add a Schottky diode or ideal-diode on the 3V3 line from the badge.
- I²C pull-ups: there are no pull-up resistors on the SAO sheet of the schematic. **Put 4.7 kΩ pull-ups from SDA and SCL to 3V3 on your SAO board.** One set per bus is enough.

### Three levels of integration

**Level 1: power only.** The ESP32 runs its own firmware (Arduino, MicroPython, ESPHome, WLED with Wi-Fi off) and ignores the badge. Wire only pins 1 and 2. Nothing changes on the badge side.

**Level 2: a GPIO trigger wire.** Connect SAO GPIO1 to an input on your ESP32. The badge flips the pin, and the ESP32 reacts, for example by changing its blink pattern. The badge side is one line of MicroPython: `machine.Pin(4, machine.Pin.OUT).value(1)` (SAO2's GPIO1).

**Level 3: I²C commands, the most flexible.** The ESP32 acts as an I²C *target* (slave) at an address you pick, here `0x42`. The badge is the controller and can send and receive bytes. Create the bus yourself: `i2c = machine.I2C(0, scl=machine.Pin(18), sda=machine.Pin(17), freq=100_000)`. **Don't** import upstream `lib/sao.py`: shipped badges run MicroPython 1.24, which can't import it (it uses `stop=True, /` syntax), and its `Sao.set_address()` is broken too. parallellogic's fork fixes both.

### Worked example (included)

**On the ESP32: [user_apps/sao_blink/esp32_sao_blinker/esp32_sao_blinker.ino](../user_apps/sao_blink/esp32_sao_blinker/esp32_sao_blinker.ino)** (Arduino IDE, "esp32 by Espressif" core 3.x)

- Acts as an I²C target at 0x42. Write 1 byte to set the mode (0 off, 1 on, 2 slow blink, 3 fast blink). Read 1 byte to get the current mode.
- A rising edge on `TRIGGER_PIN`, wired to SAO GPIO1, also advances the mode, so Level 2 works without I²C.
- The defaults suit an ESP32-C3 SuperMini, which is tiny and low-power, with its on-board LED on GPIO8. Change the `#define`s for other boards, or replace `setLed()` with NeoPixel code.
- Wiring: SAO 3V3 → C3 `3V3`, GND → `GND`, SDA → GPIO6, SCL → GPIO7, GPIO1 → GPIO5, plus 4.7 kΩ pull-ups on SDA and SCL.

**On the badge: [user_apps/sao_blink/apps/sao_blink.py](../user_apps/sao_blink/apps/sao_blink.py)**

- Appears in the menu as **"SAO Blink"**. When opened, it scans the bus and prints what it finds, and the LEDs turn red if nothing is at 0x42.
- **User A** sends the next blink mode over I²C, and the LED bar shows the mode number.
- **User B** toggles the trigger line on SAO2 GPIO1 (GPIO4).

Install it the same way as the artwork app: copy it into `firmware/badge/apps/` and run `update.py push --reset`.

A quick bus check from the REPL (`mpremote connect auto repl`, then Ctrl-C):

```python
import machine
i2c = machine.I2C(0, scl=machine.Pin(18), sda=machine.Pin(17), freq=100_000)
i2c.scan()                 # expect [66] (= 0x42), plus other SAOs
i2c.writeto(0x42, b'\x03') # fast blink
i2c.readfrom(0x42, 1)      # b'\x03'
```

If the scan comes back empty, check the pull-ups, check that the ESP32 is actually powered (3V3 pin, not 5V), and try a slower bus with `machine.I2C(0, scl=18, sda=17, freq=100_000)`.

This example hasn't been run on hardware yet. Treat it as a known-good starting point, not a finished product.

### Going further

- **Fork examples to study:** FacioErgoSum's `apps/sketch.py` reads analog knobs on SAO GPIO1 pins, auto-detects which header they're on, and talks to an MPU6050 over the shared I²C bus. The simulator in nathancharlesjones' fork lets you emulate your SAO in Python (`sim/sao_example.py`) before building it.
- **Designing a real SAO PCB:** the [SAO v2 spec](https://hackaday.io/project/175182-simple-add-ons-sao) covers the header, keying and the optional ID EEPROM at 0x50. An ESP32-C3-MINI-1 module, an LDO-free 3.3 V input, pull-ups, a bulk cap, a USB-C port for programming behind a diode, and a few WS2812-2020s fit easily on a standard SAO footprint.
