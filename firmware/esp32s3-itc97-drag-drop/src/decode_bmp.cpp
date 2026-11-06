#include <Arduino.h>
#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "decode_image.h"
#include "image_raster.h"
#include "usb_disk.h"

static uint16_t rd16(const uint8_t* p)
{
  return (uint16_t)(p[0] | (p[1] << 8));
}

static uint32_t rd32(const uint8_t* p)
{
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void pix_from_pal(const uint8_t* pal, int idx, int* r, int* g, int* b)
{
  const uint8_t* e = pal + idx * 4;
  *b = e[0];
  *g = e[1];
  *r = e[2];
}

bool decode_bmp_file(UsbFile* f)
{
  uint8_t hdr[256];
  memset(hdr, 0, sizeof hdr);
  if (usb_file_read_at(f, 0, hdr, sizeof hdr) < 54 || hdr[0] != 'B' || hdr[1] != 'M') {
    Serial.printf("image: %s is not a BMP\n", f->name);
    return false;
  }

  uint32_t offbits = rd32(hdr + 10);
  uint32_t bisize = rd32(hdr + 14);
  int32_t width = (int32_t)rd32(hdr + 18);
  int32_t hraw = (int32_t)rd32(hdr + 22);
  uint16_t planes = rd16(hdr + 26);
  uint16_t bpp = rd16(hdr + 28);
  uint32_t comp = rd32(hdr + 30);
  uint32_t clr_used = rd32(hdr + 46);

  if (bisize < 40 || planes != 1 || comp != 0) {
    Serial.printf("image: %s BMP is compressed or not a Windows header (size %u comp %u)\n",
                  f->name, (unsigned)bisize, (unsigned)comp);
    return false;
  }
  bool top_down = hraw < 0;
  int height = top_down ? (int)(-hraw) : (int)hraw;
  if (width < 1 || height < 1 || width > 32767 || height > 32767) {
    Serial.printf("image: BMP %dx%d is outside the size we handle\n", (int)width, height);
    return false;
  }
  if (bpp != 1 && bpp != 4 && bpp != 8 && bpp != 16 && bpp != 24 && bpp != 32) {
    Serial.printf("image: BMP %u bits per pixel is not supported\n", bpp);
    return false;
  }

  uint8_t pal[256 * 4];
  memset(pal, 0, sizeof pal);
  if (bpp <= 8) {
    uint32_t colors = clr_used ? clr_used : (1u << bpp);
    if (colors > 256) colors = 256;
    uint32_t pal_off = 14 + bisize;
    usb_file_read_at(f, pal_off, pal, colors * 4);
  }

  uint32_t stride = ((uint32_t)width * bpp + 31u) / 32u * 4u;
  uint8_t* row = (uint8_t*)heap_caps_malloc(stride, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!row) row = (uint8_t*)heap_caps_malloc(stride, MALLOC_CAP_INTERNAL);
  if (!row) {
    Serial.println("image: no memory for a BMP row");
    return false;
  }

  char desc[96];
  snprintf(desc, sizeof desc, "%s  bmp %dx%d %ubpp", f->name, (int)width, height, bpp);
  if (!raster_begin(width, height, usb_disk_generation(), desc)) {
    heap_caps_free(row);
    return false;
  }

  bool aborted = false;
  for (int file_row = 0; file_row < height; file_row++) {
    int sy = top_down ? file_row : (height - 1 - file_row);
    memset(row, 0, stride);
    usb_file_read_at(f, offbits + (uint32_t)file_row * stride, row, stride);
    for (int x = 0; x < width; x++) {
      int r = 0, g = 0, b = 0;
      if (bpp == 24) {
        b = row[x * 3 + 0];
        g = row[x * 3 + 1];
        r = row[x * 3 + 2];
      } else if (bpp == 32) {
        b = row[x * 4 + 0];
        g = row[x * 4 + 1];
        r = row[x * 4 + 2];
      } else if (bpp == 16) {
        uint16_t v = rd16(row + x * 2);
        int b5 = v & 31;
        int g5 = (v >> 5) & 31;
        int r5 = (v >> 10) & 31;
        r = (r5 << 3) | (r5 >> 2);
        g = (g5 << 3) | (g5 >> 2);
        b = (b5 << 3) | (b5 >> 2);
      } else if (bpp == 8) {
        pix_from_pal(pal, row[x], &r, &g, &b);
      } else if (bpp == 4) {
        uint8_t byte = row[x >> 1];
        int idx = (x & 1) ? (byte & 0x0F) : (byte >> 4);
        pix_from_pal(pal, idx, &r, &g, &b);
      } else {
        uint8_t byte = row[x >> 3];
        int idx = (byte & (0x80 >> (x & 7))) ? 1 : 0;
        pix_from_pal(pal, idx, &r, &g, &b);
      }
      raster_splat_rgb(x, sy, r, g, b);
    }
    if ((file_row & 15) == 0) delay(1);
    if (raster_stale()) {
      aborted = true;
      break;
    }
  }
  heap_caps_free(row);
  if (aborted) {
    raster_abort();
    return false;
  }
  return raster_finish();
}
