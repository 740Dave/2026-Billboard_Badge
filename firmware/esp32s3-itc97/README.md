# esp32s3-itc97

An easy driver for the 9.7" Pervasive Displays iTC dual-COG e-paper panel on an
ESP32-S3 (16 MB flash / 8 MB PSRAM), with full and fast refresh and automatic
detection of the two panel variants.

Ported from the EFR32FG22 OEPL driver in `iTC_Short_LUT/FG22_OEPL_9.7_12`
(`firmware/drivers/oepl_display_driver_pervasive_itc_medium.c`), which is where
every measured number quoted in the comments comes from.

## The panel

960 x 672, black / white / red, two COGs behind one FPC. Each COG owns half of a
scan line, so a line goes out as 60 bytes to the master, chip select swapped,
60 bytes to the slave. Register writes go to both at once.

Two revisions live behind the same geometry and the same connector:

| | `E2969JS084` ("driver 8") | `E2969JS0B1` ("driver B") |
|---|---|---|
| OTP opener | `0xA8`, after `0xA1`/`0xA7`/`0xA3` | `0xB9`, after `0x70` |
| waveform table | 180 bytes, 5 x 9 groups x 4 | 400 bytes, 5 x 10 groups x 8 |
| own waveform | ~51 s | ~34 s |
| short waveform | ~3.3 s, one pull | ~1.1 s x 3 pulls |

Which one is attached is decided at runtime from the OTP, so nothing has to be
configured for it. `epd_panel_name()` says what was found.

## Wiring

| Display | ESP32-S3 | note |
|---|---|---|
| DC | GPIO21 | |
| SDA | GPIO47 | MOSI, turned around to read the OTP back |
| SCL | GPIO15 | |
| CS1 | GPIO16 | master COG |
| CS2 | GPIO14 | slave COG |
| Busy_Master | GPIO18 | |
| Busy_Slave | GPIO13 | set `EPD_PIN_BUSY_S` to `-1` if unwired |
| RST | GPIO7 | |
| VCC_EN | GPIO17 | panel supply switch |

**Check `EPD_PWR_ACTIVE_HIGH` first.** It defaults to 1, i.e. GPIO17 high powers
the panel. A high-side P-FET would be the other way round. Getting it wrong
looks exactly like a dead panel: `cog_initial failed` and an OTP that reads back
as garbage. Flip it in `platformio.ini`:

```ini
build_flags = -DEPD_PWR_ACTIVE_HIGH=0
```

Everything else — pins, reset timings, waveform constants — is a `#ifndef` in
`include/epd97_config.h` and can be overridden the same way.

## Using it

```cpp
#include "epd97.h"

void setup() {
  Serial.begin(115200);
  epd_begin();                          // claims pins, allocates the planes,
                                        // powers up once and reads the OTP
  Serial.println(epd_panel_name());

  epd_clear();
  epd_text(40, 40, "hello", EPD_BLACK, 6);
  epd_fill_rect(40, 120, 300, 80, EPD_RED);

  refresh_display(EPD_SLOW);            // the panel's own waveform
  refresh_display(EPD_FAST);            // the short waveform
}
```

### The two modes

`EPD_SLOW` runs the waveform out of the panel's own OTP. It is slow, it is the
only mode that can make red, and it is the only thing that restores the film
completely — so it is worth using for the first image after power-up and every
so often after that. The demos do one every tenth frame.

`EPD_FAST` writes a short waveform to register `0xB0` instead. On the 084 that
is a balanced shake followed by a chopped drive; the chopping is not an
optimisation but the whole point, because a long continuous run at the rail
drags the slower red pigment along behind the black, and the rests let it relax
back. On the B1 the format is different and the refresh runs for a fixed ~1.1 s
whatever the table asks for, so the dose is delivered as three pulls of the
already-latched waveform rather than one longer one.

**A fast refresh is black and white.** The short waveform cannot make a good
red: on the 084 the red table is barely reached in the time the short table
runs, and on the B1 the fixed ~1 s window leaves red pale and getting worse with
every frame that half-drives it. So red content is drawn as *ink* on a fast
refresh rather than dropped — the picture stays readable and just goes
monochrome until the next slow one. Set `-DEPD_FAST_ALLOW_RED=1` to drive it
anyway.

The red-clearing stages stay in play regardless: a fast frame that follows a red
one still spends its waveform groups pulling the red pigment back out, because
otherwise the old picture stays legible underneath.

### Clearing at boot

```cpp
epd_clear_screen();   // blank paper, panel's own waveform
```

E-paper holds whatever the last firmware left on it, and a short waveform cannot
cross the full distance in one pass, so drawing straight over old content leaves
it showing through. All three demos call this once at boot. It clears the
framebuffer as a side effect, so call it before you draw.

### Keeping the COG up

```cpp
epd_set_keep_awake(true);
```

A panel does not change between pictures, so the power-up, the reset and the
booster ramp — about five of the eight seconds an image costs — need not be
repeated. With this on an image costs only its plane data and the refresh. The
panel keeps its rails charged in between, which is why it is off by default; the
session is torn down every `EPD_SESSION_IMAGES` images either way. Call
`epd_sleep()` before a long idle.

### Drawing

Two 1-bpp planes, MSB first, `x = 0` in bit 7 of byte 0 of the line, both in
PSRAM and both reachable through `epd_plane_bw()` / `epd_plane_red()` if the
helpers are not enough.

A pixel is the *pair* of bits, and the pair picks one of the COG's four waveform
tables as `(red << 1) | bw`. So red is not black with red on top: a red pixel has
the red bit set and the bw bit **clear**. Setting both lands on table 3, which no
waveform drives. `epd_pixel()` and friends take an `epd_color_t` and get this
right for you.

```
epd_clear()                                  both planes to white
epd_fill(c)
epd_pixel(x, y, c)
epd_fill_rect(x, y, w, h, c)
epd_rect(x, y, w, h, thickness, c)
epd_hline / epd_vline(x, y, len, thickness, c)
epd_text(x, y, s, c, scale)                  5x7 font in a 6x8 cell
epd_printf(x, y, c, scale, fmt, ...)
```

## The three demos

| env | what it does |
|---|---|
| `pages` | one test page: COG seam bar, colour swatches, a 1 px grid. Slow at boot, then fast, slow every tenth. The default. |
| `changes` | back-to-back fast refreshes with everything moving: sweeping bars, an inverting checkerboard, a walking red block. The ghosting workload. ~3.6 s a frame on the B1. |
| `sleep` | one image a minute with the panel powered down and the ESP32 in deep sleep in between. |

```
pio run -t upload              # pages
pio run -e changes -t upload
pio run -e sleep   -t upload
```

### Deep sleep

`epd_deep_sleep_prepare()` closes the session, cuts the panel supply and then
**latches the supply pin**. That last part is not optional here: deep sleep lets
every pad go high-Z, and the MOSFET gate has no pull-up, so without the latch
the panel supply is undefined for the whole sleep — and the one thing a sleeping
tag must not do is power its panel.

The detected panel and its OTP are kept in RTC memory across the sleep, so a
wake skips the probe: a whole power cycle and about three seconds. The cache is
only trusted after a deep-sleep wake, so a power cycle re-probes.

RAM does not survive deep sleep, so the framebuffer is rebuilt on every wake.
That costs nothing — the panel holds the picture without power, and a redraw is
microseconds against a refresh in seconds.

## Build and flash

```
pio run
pio run -t upload
```

Flash through PlatformIO, not with a standalone `esptool` off `PATH`. A direct
call with esptool 5.3.1 wrote an image this chip would not boot — the ROM loader
failed at `ets_loader.c 79` and the board sat in a watchdog reset loop until it
was put back into download mode by hand (hold BOOT, tap RESET). PlatformIO's
bundled esptool 4.8.6 has been reliable.

The board is held in USB download mode by hand, so `platformio.ini` sets
`board_upload.after_reset = no_reset`: esptool leaves it in the bootloader
instead of rebooting into the app. `before_reset` stays at `default_reset`
because esptool still needs its usual handshake to sync — with `no_reset` there
it fails with *No serial data received*. Reset the board yourself afterwards.

## How it works

**Bus.** Register writes and the OTP readback are bitbanged: they are a few
dozen bytes and the OTP wants a ~50 kHz clock with the data pin turned around
for the read. The two 80640-byte planes go out over hardware SPI at 4 MHz, which
is what takes a frame from about eighteen seconds to under one. Both share MOSI
and SCK, so the GPIO matrix is re-pointed between the two
(`esp_rom_gpio_connect_out_signal`) rather than the bus being torn down and
rebuilt. Chip select stays GPIO throughout, because the two COGs are swapped
every 60 bytes.

**Detection.** The driver-B opener is tried first, as the stock firmware does.
If the returned block has `0x3B` in both window registers and the stage flag set
at `0x28`, it is a B1 and the block is remapped into the driver-8 layout so the
rest of the driver has one thing to read. Otherwise the driver-8 opener is tried
and the header must come back as `0xA5`. Either way all four DC/DC ramp stages
are sanity-checked before anything drives the booster: the ramp loop runs the
OTP repeat counts verbatim, and a stage that reads `0xFF` ramps the booster 127x
and hangs `BUSY`. The block is a fuse — it cannot change between refreshes — so
it is read once and cached.

**Timing that is not optional.** `EPD_T_OFF_MIN` (1500 ms) is the minimum the
panel supply stays off between two COG cycles. Below it the booster rails have
not bled down when the next cycle powers up, and the symptom is not a subtle
one: at 800 ms `cog_initial` fails and the OTP reads back corrupt. The reset
shape and the settling pauses have the same character — they were measured, and
shortening them announces itself as a failed init rather than a slightly worse
picture.

**Red.** Both coloured pigments carry the same charge and both rise on a
positive field; they differ only in threshold, red's being the lower one. On the
full rail black comes up with the red and has to be pulled back out afterwards —
and the pull that removes black removes red with it, because the two differ in
speed by about 2.5, not the ten to one the literature suggests. So red is driven
on the *reduced* positive rail, where black never comes up at all. The long red
sequence runs whenever red is on either side of the change: a frame that follows
a red one still has to clear the pigment out, or the old picture stays legible
under the new one.
