// -----------------------------------------------------------------------------
// Cover the 960x672 panel, then dither.
//
// One scale factor for both axes, so the picture is never stretched. It
// always fills the panel. The longer side is cut, and the cut is centred.
// -----------------------------------------------------------------------------
#include <Arduino.h>
#include <string.h>
#include "esp_heap_caps.h"

#include "epd97.h"
#include "image_raster.h"
#include "raster_cover.h"
#include "raster_red.h"
#include "usb_disk.h"

static uint32_t* s_acc;
static int s_sw, s_sh;
static int s_dx, s_dy, s_dw, s_dh;
static int s_ox, s_oy, s_cw, s_ch, s_num, s_den;
static uint32_t s_gen;
static char s_desc[96];
static int16_t* s_err;
static int s_dither = 0;

void raster_set_dither(int on)
{
  s_dither = on ? 1 : 0;
}

static void free_acc(void)
{
  if (s_acc) {
    heap_caps_free(s_acc);
    s_acc = nullptr;
  }
}

bool raster_begin(int full_w, int full_h, uint32_t gen, const char* source_desc)
{
  free_acc();
  if (full_w < 1) full_w = 1;
  if (full_h < 1) full_h = 1;
  s_sw = full_w;
  s_sh = full_h;
  s_gen = gen;
  strncpy(s_desc, source_desc ? source_desc : "", sizeof s_desc - 1);
  s_desc[sizeof s_desc - 1] = 0;

  raster_cover_t cover;
  raster_cover_window(full_w, full_h, EPD_W, EPD_H, &cover);
  s_ox = cover.ox;
  s_oy = cover.oy;
  s_cw = cover.cw;
  s_ch = cover.ch;
  s_num = cover.num;
  s_den = cover.den < 1 ? 1 : cover.den;
  // The kept window is scaled onto the whole panel. No white margin.
  s_dx = 0;
  s_dy = 0;
  s_dw = EPD_W;
  s_dh = EPD_H;

  s_acc = (uint32_t*)heap_caps_malloc((size_t)EPD_W * EPD_H * sizeof(uint32_t),
                                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!s_acc) {
    Serial.printf("image: no PSRAM for the %dx%d accumulator (%u bytes free)\n",
                  EPD_W, EPD_H, (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    return false;
  }
  memset(s_acc, 0, (size_t)EPD_W * EPD_H * sizeof(uint32_t));
  Serial.printf("image: %s  centre-crop %dx%d at %d,%d from %dx%d onto %dx%d\n",
                s_desc, s_cw, s_ch, s_ox, s_oy, full_w, full_h, EPD_W, EPD_H);
  return true;
}

void raster_splat_rgb(int sx, int sy, int r, int g, int b)
{
  if (!s_acc) return;
  if ((unsigned)sx >= (unsigned)s_sw || (unsigned)sy >= (unsigned)s_sh) return;
  if (sx < s_ox || sy < s_oy || sx >= s_ox + s_cw || sy >= s_oy + s_ch) return;

  int lx = sx - s_ox;
  int ly = sy - s_oy;
  int x0 = (int)((int64_t)lx * s_num / s_den);
  int y0 = (int)((int64_t)ly * s_num / s_den);
  int x1 = (int)((int64_t)(lx + 1) * s_num / s_den);
  int y1 = (int)((int64_t)(ly + 1) * s_num / s_den);
  // Same factor on both axes. The last kept pixel lands on the panel edge
  // so the cover has no empty strip from integer rounding.
  if (lx + 1 == s_cw) x1 = EPD_W;
  if (ly + 1 == s_ch) y1 = EPD_H;
  if (x1 <= x0) x1 = x0 + 1;
  if (y1 <= y0) y1 = y0 + 1;
  if (x0 < 0) x0 = 0;
  if (y0 < 0) y0 = 0;
  if (x1 > EPD_W) x1 = EPD_W;
  if (y1 > EPD_H) y1 = EPD_H;
  if (x0 >= x1 || y0 >= y1) return;

  if (r < 0) r = 0;
  if (g < 0) g = 0;
  if (b < 0) b = 0;
  if (r > 255) r = 255;
  if (g > 255) g = 255;
  if (b > 255) b = 255;
  int luma = (r * 77 + g * 150 + b * 29) >> 8;
  bool red = raster_red_sample(r, g, b) != 0;

  for (int y = y0; y < y1; y++) {
    uint32_t* row = s_acc + (size_t)y * EPD_W;
    for (int x = x0; x < x1; x++) {
      uint32_t v = row[x];
      uint16_t sum = (uint16_t)v;
      uint8_t n = (uint8_t)(v >> 16);
      uint8_t rc = (uint8_t)(v >> 24);
      if (n == 255) {
        sum = (uint16_t)(sum / 2);
        n = 128;
        rc = (uint8_t)(rc / 2);
      }
      sum = (uint16_t)(sum + luma);
      n++;
      if (red && rc < 255) rc++;
      row[x] = (uint32_t)sum | ((uint32_t)n << 16) | ((uint32_t)rc << 24);
    }
  }
}

void raster_splat_rgb565(int sx, int sy, uint16_t p)
{
  int r5 = (p >> 11) & 31;
  int g6 = (p >> 5) & 63;
  int b5 = p & 31;
  int r = (r5 << 3) | (r5 >> 2);
  int g = (g6 << 2) | (g6 >> 4);
  int b = (b5 << 3) | (b5 >> 2);
  raster_splat_rgb(sx, sy, r, g, b);
}

bool raster_stale(void)
{
  return usb_disk_generation() != s_gen;
}

void raster_abort(void)
{
  free_acc();
}

static void spill(int16_t* row, int x, int wgt)
{
  if ((unsigned)x >= EPD_W) return;
  row[x] = (int16_t)(row[x] + wgt);
}

bool raster_finish(void)
{
  if (!s_acc || !epd_plane_bw()) {
    free_acc();
    return false;
  }
  if (raster_stale()) {
    free_acc();
    return false;
  }

  if (!s_err) {
    s_err = (int16_t*)heap_caps_malloc(sizeof(int16_t) * EPD_W * 2,
                                      MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!s_err)
      s_err = (int16_t*)heap_caps_malloc(sizeof(int16_t) * EPD_W * 2, MALLOC_CAP_SPIRAM);
  }

  epd_clear();
  uint8_t* bw = epd_plane_bw();
  uint8_t* rd = epd_plane_red();
  uint32_t n_black = 0, n_red = 0, n_hole = 0;

  int16_t* cur = s_err;
  int16_t* nxt = s_err ? s_err + EPD_W : nullptr;
  if (s_err) memset(s_err, 0, sizeof(int16_t) * EPD_W * 2);

  for (int y = 0; y < EPD_H; y++) {
    if (nxt) memset(nxt, 0, sizeof(int16_t) * EPD_W);
    int dir = (y & 1) ? -1 : 1;
    int x = (dir > 0) ? 0 : (EPD_W - 1);
    for (int i = 0; i < EPD_W; i++, x += dir) {
      uint32_t v = s_acc[(size_t)y * EPD_W + x];
      uint16_t sum = (uint16_t)v;
      uint8_t n = (uint8_t)(v >> 16);
      uint8_t reds = (uint8_t)(v >> 24);
      if (n == 0) {
        if (x >= s_dx && x < s_dx + s_dw && y >= s_dy && y < s_dy + s_dh) n_hole++;
        continue;
      }
      int gray = (int)sum / (int)n;
      int old = gray;
      if (s_dither && cur) old += cur[x];
      if (old < 0) old = 0;
      if (old > 255) old = 255;
      bool luma_ink = old < 128;
      bool force_red = raster_use_red(1, s_dither, reds, n, gray, luma_ink) != 0;
      bool ink = luma_ink || force_red;
      if (s_dither && cur) {
        int shown = ink ? 0 : 255;
        int err = old - shown;
        int e7 = err * 7 / 16;
        int e3 = err * 3 / 16;
        int e5 = err * 5 / 16;
        int e1 = err - e7 - e3 - e5;
        spill(cur, x + dir, e7);
        spill(nxt, x - dir, e3);
        spill(nxt, x, e5);
        spill(nxt, x + dir, e1);
      }
      if (!ink) continue;
      uint8_t* plane = force_red ? rd : bw;
      plane[(size_t)y * EPD_STRIDE + (x >> 3)] |= (uint8_t)(0x80u >> (x & 7));
      if (force_red) n_red++;
      else n_black++;
    }
    if ((y & 31) == 0) delay(1);
    if (cur && nxt) {
      int16_t* tmp = cur;
      cur = nxt;
      nxt = tmp;
    }
  }

  free_acc();
  Serial.printf("image: quant %s  three_color 1  black %u  red %u  white %u  holes %u\n",
                s_dither ? "dither" : "threshold",
                (unsigned)n_black, (unsigned)n_red,
                (unsigned)((uint32_t)EPD_W * EPD_H - n_black - n_red - n_hole),
                (unsigned)n_hole);

  if (raster_stale()) return false;
  // Always the long LUT. It updates the glass and waits on BUSY.
  epd_set_allow_fast_red(true);
  epd_set_run_refresh(true);
  bool ok = refresh_display(EPD_SLOW);
  Serial.printf("image: %s  spi %s, %u ms, waveform slow\n",
                s_desc, ok ? "sent" : "FAILED",
                (unsigned)epd_last_frame_ms());
  return ok;
}
