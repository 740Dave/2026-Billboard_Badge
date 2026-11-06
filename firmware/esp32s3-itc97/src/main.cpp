// -----------------------------------------------------------------------------
// Three demos, picked with -DDEMO_MODE in platformio.ini:
//
//   0  pages    one test page, slow first and then fast. The default.
//   1  changes  back-to-back fast refreshes with the picture changing every
//               frame. This is the workload that shows ghosting, so it is the
//               one to judge a waveform on.
//   2  sleep    one image a minute with the panel powered down and the ESP32
//               in deep sleep in between.
//
//     pio run -e changes -t upload
//     pio run -e sleep   -t upload
// -----------------------------------------------------------------------------
#include <Arduino.h>
#include <esp_sleep.h>
#include <esp_system.h>
#include "epd97.h"

#ifndef DEMO_MODE
#define DEMO_MODE 0
#endif

#define   DEMO_PAGES 0
#define DEMO_CHANGES 1
#define   DEMO_SLEEP 2
#define    DEMO_LFSR 3
#define   DEMO_NEVER 4

// -----------------------------------------------------------------------------
// Shared furniture
// -----------------------------------------------------------------------------
static void header(const char* title, epd_color_t bar)
{
  epd_fill_rect(0, 0, EPD_W, 70, bar);
  epd_text(24, 18, title, EPD_WHITE, 5);
}

static void footer(const char* mode)
{
  epd_printf(24, 645, EPD_BLACK, 2, "%s   %s   panel %u ms   frame %u ms",
             epd_panel_name(), mode,
             (unsigned)epd_last_panel_ms(), (unsigned)epd_last_frame_ms());
}

#if DEMO_MODE == DEMO_PAGES
// -----------------------------------------------------------------------------
// 0 - the test page
// -----------------------------------------------------------------------------
static uint32_t s_frame;

static void draw_page(uint32_t n, const char* mode)
{
  epd_clear();
  header("9.7\" iTC dual-COG", EPD_RED);

  epd_printf(24, 100, EPD_BLACK, 3, "panel : %s", epd_panel_name());
  epd_printf(24, 140, EPD_BLACK, 3, "mode  : %s", mode);
  epd_printf(24, 180, EPD_BLACK, 3, "frame : %u", (unsigned)n);
  epd_printf(24, 220, EPD_BLACK, 3, "panel %u ms   frame %u ms",
             (unsigned)epd_last_panel_ms(), (unsigned)epd_last_frame_ms());

  // The seam between the two COGs sits at x = 480. A step in this bar means the
  // per-line half swap is off by a byte.
  epd_rect(24, 280, EPD_W - 48, 60, 3, EPD_BLACK);
  epd_fill_rect(27, 283, (EPD_W - 54) / 2, 54, EPD_BLACK);
  epd_text(24, 356, "the bar above is half black: the seam is the COG boundary",
           EPD_BLACK, 2);

  const epd_color_t sw[6] = { EPD_BLACK, EPD_WHITE, EPD_RED,
                              EPD_RED,   EPD_BLACK, EPD_WHITE };
  for (int i = 0; i < 6; i++) {
    int x = 24 + i * 152;
    epd_rect(x, 400, 140, 140, 2, EPD_BLACK);
    epd_fill_rect(x + 2, 402, 136, 136, sw[i]);
  }

  // A one-pixel grid, which is what shows up drive gradients across the width.
  for (int x = 24; x < EPD_W - 24; x += 16) epd_vline(x, 570, 60, 1, EPD_BLACK);
  for (int y = 570; y < 630; y += 16)       epd_hline(24, y, EPD_W - 48, 1, EPD_BLACK);

  epd_printf(24, 645, EPD_BLACK, 2, "esp32-s3 @ %u MHz   psram %u kB free",
             (unsigned)getCpuFrequencyMhz(),
             (unsigned)(ESP.getFreePsram() / 1024));
}

void setup()
{
  Serial.begin(115200);
  delay(2000);
  Serial.println("\n=== 9.7\" iTC - page demo ===");
  if (!epd_begin()) return;

  // Whatever the last firmware left on the panel is still there. Drive it out
  // properly before the first picture rather than half-covering it.
  epd_clear_screen();
  epd_set_keep_awake(true);

  // The first picture goes on slowly: that is the mode that can make red, so
  // the boot image is the one showing what the panel is actually capable of.
  draw_page(s_frame, "slow (panel waveform)");
  refresh_display(EPD_SLOW);
  s_frame++;
}

void loop()
{
  if (!epd_panel()) { delay(5000); return; }
  const bool slow = (s_frame % 10u) == 0u;
  draw_page(s_frame, slow ? "slow (panel waveform)" : "fast (short waveform)");
  refresh_display(slow ? EPD_SLOW : EPD_FAST);
  s_frame++;
  //delay(3000);
}

#elif DEMO_MODE == DEMO_CHANGES
// -----------------------------------------------------------------------------
// 1 - many changes, no pauses
//
// Every element moves or inverts each frame, so nothing is redrawn as itself.
// That is deliberate: a pixel held at one value is not a test of anything, and
// a picture that only ever adds ink hides the case that actually goes wrong -
// ink that has to come back out. The checkerboard inverts wholesale every
// frame, which is the hardest of those and the standard ghosting workload.
// -----------------------------------------------------------------------------
static uint32_t s_frame;

static void draw_changes(uint32_t n)
{
  epd_clear();
  header("changes", EPD_RED);

  epd_printf(24, 92, EPD_BLACK, 6, "%lu", (unsigned long)n);
  epd_printf(280, 104, EPD_BLACK, 3, "fast refresh, back to back");

  // A bar sweeping right, and a red one sweeping left. Each frame moves them a
  // long way, so nearly every pixel under them changes.
  const int track = EPD_W - 48 - 160;
  const int p     = (int)(n % 24u);
  const int fwd   = (p < 12) ? p : (24 - p);            // ping-pong, 0..12
  epd_fill_rect(24 + track * fwd / 12,        180, 160, 60, EPD_BLACK);
  epd_fill_rect(24 + track * (12 - fwd) / 12, 250, 160, 60, EPD_RED);

  // The checkerboard, inverted on every odd frame. Ghosting shows here first:
  // the previous frame's squares stay faintly visible inside the new ones.
  const int cell = 40;
  const bool inv = (n & 1u) != 0;
  for (int y = 340; y + cell <= 560; y += cell) {
    for (int x = 24; x + cell <= EPD_W - 24; x += cell) {
      const bool on = (((x / cell) + (y / cell)) & 1) != 0;
      if (on != inv) epd_fill_rect(x, y, cell, cell, EPD_BLACK);
    }
  }

  // A red block walking along the bottom row of the board. On the fast frames
  // this comes out as ink - the short waveform cannot make red - so it turns
  // red only on the slow frame every fiftieth, which is the point: it shows at
  // a glance which waveform the last refresh used.
  epd_fill_rect(24 + (int)(n % 22u) * cell, 560, cell, cell, EPD_RED);

  epd_hline(24, 610, EPD_W - 48, 2, EPD_BLACK);
  footer("changes");
}

void setup()
{
  Serial.begin(115200);
  delay(2000);
  Serial.println("\n=== 9.7\" iTC - change/ghosting demo ===");
  if (!epd_begin()) return;
  // The session stays open, so a frame costs its plane data and the refresh
  // and nothing else. That is the point of this demo.
  epd_set_keep_awake(true);

  // A blank pass on the panel's own waveform first, so the film starts from a
  // properly driven state rather than from whatever the last firmware left on
  // it. Ghosting is what this demo measures, and starting on top of somebody
  // else's picture would put a ghost in frame one that this waveform never made.
  epd_clear_screen();

  draw_changes(s_frame);
  refresh_display(EPD_FAST);
  s_frame++;

}

void loop()
{
  if (!epd_panel()) { delay(5000); return; }
  // Every num_between_refreshes-th frame goes back to the panel's own waveform. Insurance: the
  // short waveform holds on its own, but the stock one is the only thing that
  // restores the film completely.
  uint8_t num_between_refreshes = 10;
  const bool slow = (s_frame % num_between_refreshes) == 0u;
  draw_changes(s_frame);
  refresh_display(slow ? EPD_SLOW : EPD_FAST);
  s_frame++;
  if (slow){
   delay(6000);
  }
}

#elif DEMO_MODE == DEMO_SLEEP
// -----------------------------------------------------------------------------
// 2 - one image a minute, everything asleep in between
//
// The panel is powered down and its supply switch latched off, and the ESP32
// goes into deep sleep. RAM does not survive that, so the framebuffer is built
// from scratch on every wake - which costs nothing, the panel holds the picture
// without power and the redraw is microseconds against a refresh in seconds.
//
// What does survive is RTC memory: the wake counter here, and inside the driver
// the detected panel and its OTP, which saves a whole probe power cycle.
// -----------------------------------------------------------------------------
#ifndef DEMO_PERIOD_S
#define DEMO_PERIOD_S 60
#endif

RTC_DATA_ATTR static uint32_t s_wake;

static void draw_sleep_page(uint32_t wake, bool slow)
{
  epd_clear();
  // No red anywhere in this demo. Red is a slow-refresh colour, and a wake
  // that has to clear red off the film cannot be a fast one - see the
  // promotion rule in epd97.h. A once-a-minute tag wants every wake cheap,
  // so it simply does not put red on the panel in the first place.
  header("sleep demo", EPD_BLACK);

  const uint32_t up_s = wake * (uint32_t)DEMO_PERIOD_S;
  epd_printf(24, 100, EPD_BLACK, 8, "%lu", (unsigned long)wake);
  epd_printf(24, 210, EPD_BLACK, 3, "wake-ups since power-on");

  epd_printf(24, 280, EPD_BLACK, 3, "period   : %u s", (unsigned)DEMO_PERIOD_S);
  epd_printf(24, 320, EPD_BLACK, 3, "on panel : %02u:%02u:%02u",
             (unsigned)(up_s / 3600u), (unsigned)((up_s / 60u) % 60u),
             (unsigned)(up_s % 60u));
  epd_printf(24, 360, EPD_BLACK, 3, "waveform : %s",
             slow ? "slow (panel's own)" : "fast (short)");
  epd_printf(24, 400, EPD_BLACK, 3, "panel    : %s", epd_panel_name());

  // A minute hand, so a glance says whether the cycle is still running.
  const int cx = EPD_W - 200, cy = 300, r = 130;
  epd_rect(cx - r, cy - r, 2 * r+10, 2 * r+10, 3, EPD_BLACK);
  const int step = (int)(wake % 12u);
  epd_fill_rect(cx - 8, cy - 8, 16, 16, EPD_BLACK);
  for (int i = 0; i < 12; i++) {
    // Twelve marks around the box, the current one filled red.
    const int px = cx - r + 10 + ((i % 4) * (2 * r - 40) / 3);
    const int py = cy - r + 10 + ((i / 4) * (2 * r - 40) / 2);
    // The current mark is filled, the rest are outlines.
    if (i == step) epd_fill_rect(px, py, 30, 30, EPD_BLACK);
    else           epd_rect(px, py, 30, 30, 3, EPD_BLACK);
  }

  epd_hline(24, 610, EPD_W - 48, 2, EPD_BLACK);
  footer("sleep");
}

void setup()
{
  Serial.begin(115200);
  delay(300);                       // enough for the CDC, not a whole second
  Serial.printf("\n=== 9.7\" iTC - sleep demo, wake %lu ===\n",
                (unsigned long)s_wake);

  if (!epd_begin()) {
    Serial.println("epd_begin() failed - sleeping anyway, retrying next wake");
    epd_deep_sleep_prepare();
    esp_deep_sleep((uint64_t)DEMO_PERIOD_S * 1000000ULL);
  }

  // A cold boot clears the panel first; a deep-sleep wake does not, because the
  // picture it is about to replace is its own from a minute ago.
  const bool cold = (esp_reset_reason() != ESP_RST_DEEPSLEEP);
  if (cold) {
    s_wake = 0;
    epd_clear_screen();
  }

  // Each wake is its own session by definition, so keep-awake would buy
  // nothing. Left off, which is also what the driver defaults to.
  // Slow on the first image after power-on, and every tenth wake after that to
  // keep the film recovered. Nothing here draws red, so no wake ever has to
  // spend a slow refresh clearing it.
  const bool slow = cold || (s_wake % 10u) == 0u;
  draw_sleep_page(s_wake, slow);
  refresh_display(slow ? EPD_SLOW : EPD_FAST);
  s_wake++;

  // Sleep out the rest of the period rather than a fixed minute: a slow refresh
  // is half a minute of it on this panel, and the cycle should still land on
  // the period rather than drifting by however long the refresh took.
  const uint64_t period_us = (uint64_t)DEMO_PERIOD_S * 1000000ULL;
  const uint64_t awake_us  = (uint64_t)millis() * 1000ULL;
  const uint64_t sleep_us  = (awake_us + 1000000ULL >= period_us)
                               ? 1000000ULL : (period_us - awake_us);
  Serial.printf("awake %lu ms, sleeping %lu ms\n",
                (unsigned long)millis(), (unsigned long)(sleep_us / 1000ULL));
  Serial.flush();

  epd_deep_sleep_prepare();
  esp_deep_sleep(sleep_us);
}

void loop() { }   // never reached: setup() ends in deep sleep
		  
#elif DEMO_MODE == DEMO_LFSR
static uint16_t lfsr = 123;

void setup()
{
  Serial.begin(115200);
  delay(2000);
  Serial.println("\n=== 9.7\" iTC - never demo ===");
  if (!epd_begin()) return;

  // Whatever the last firmware left on the panel is still there. Drive it out
  // properly before the first picture rather than half-covering it.
  epd_clear_screen();
  epd_set_keep_awake(true);
  // The first picture goes on slowly: that is the mode that can make red, so
  // the boot image is the one showing what the panel is actually capable of.
  // refresh_display(EPD_SLOW);
}

void loop()
{
  if (!epd_panel()) { delay(500); return; } // wait until panel ready
  epd_clear();

  // if (lfsr >= 3){
	  // lfsr = 0;
  // } else {
	// lfsr++;
  // }
  // epd_fill((epd_color_t) lfsr);

  for (uint16_t x =0 ; x<960; x++){
	for (uint16_t y=0 ; y<672; y++){
		uint8_t lsb = (lfsr & 1);
		lfsr >>= 1;
		if (lsb == 1) {            /* Only apply toggle mask if output bit is 1. */
			lfsr ^= 0xB400u;      /* Apply toggle mask, value has 1 at bits */
		}
		if ((lfsr & 0x0003u) == 3){ // the fourth color
			epd_pixel(x, y, EPD_WHITE);
		}
		else{
			epd_pixel(x, y, (epd_color_t) (lfsr & 0x0003));
		}

	}
  }
  refresh_display(EPD_SLOW);
  delay(2000);
}


#elif DEMO_MODE == DEMO_NEVER

void setup()
{
  Serial.begin(115200);
  delay(2000);
  
  if (!epd_begin()) return;
  epd_set_keep_awake(true);
  epd_clear_screen();
}

void loop()
{
  if (!epd_panel()) { delay(500); return; } // wait until panel ready
  epd_clear();

  for (uint16_t x =0 ; x<960; x++){
	for (uint16_t y=0 ; y<672; y++){
			
		// epd_pixel(x, y, EPD_WHITE);
		// decomposed epd_pixel
		if ((unsigned)x >= EPD_W || (unsigned)y >= EPD_H) return;
		plane_set(epd_plane_bw(),  x, y, c == EPD_BLACK);
		plane_set(epd_plane_red(), x, y, c == EPD_RED);
	}
  }
  refresh_display(EPD_SLOW);
  delay(2000);
}


#else
#error "DEMO_MODE must be 0 (pages), 1 (changes) or 2 (sleep) or 3 (never)"
#endif

