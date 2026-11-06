#include <Arduino.h>
#include <stdio.h>
#include <string.h>
#include <new>

#include "esp_heap_caps.h"
#include "JPEGDEC.h"

#include "decode_image.h"
#include "epd97.h"
#include "image_raster.h"
#include "usb_disk.h"

static int32_t jpg_read(JPEGFILE* jf, uint8_t* buf, int32_t len)
{
  if (!jf || len <= 0 || jf->iPos >= jf->iSize) return 0;
  UsbFile* f = (UsbFile*)jf->fHandle;
  int32_t n = usb_file_read_at(f, (uint32_t)jf->iPos, buf, (uint32_t)len);
  jf->iPos += n;
  return n;
}

static int32_t jpg_seek(JPEGFILE* jf, int32_t pos)
{
  if (!jf) return 0;
  if (pos < 0) pos = 0;
  if (jf->iSize > 0 && pos > jf->iSize) pos = jf->iSize;
  jf->iPos = pos;
  return pos;
}

static int s_blocks;
static int s_orient;
static int s_src_w;
static int s_src_h;

// JPEGDEC records the EXIF orientation and ignores JPEG_AUTO_ROTATE.
// Phone portraits are stored sideways; map each pixel into the upright frame.
static void jpeg_upright(int x, int y, int* ox, int* oy)
{
  int w = s_src_w;
  int h = s_src_h;
  switch (s_orient) {
    case 2: *ox = w - 1 - x; *oy = y; break;
    case 3: *ox = w - 1 - x; *oy = h - 1 - y; break;
    case 4: *ox = x; *oy = h - 1 - y; break;
    case 5: *ox = y; *oy = x; break;
    case 6: *ox = h - 1 - y; *oy = x; break;
    case 7: *ox = h - 1 - y; *oy = w - 1 - x; break;
    case 8: *ox = y; *oy = w - 1 - x; break;
    default: *ox = x; *oy = y; break;
  }
}

static int jpg_draw(JPEGDRAW* p)
{
  int stride = p->iWidth;
  int w = p->iWidthUsed;
  if (w <= 0 || w > stride) w = stride;
  int h = p->iHeight > 0 ? p->iHeight : 1;
  const uint16_t* px = p->pPixels;
  for (int row = 0; row < h; row++) {
    const uint16_t* line = px + row * stride;
    int y = p->y + row;
    for (int col = 0; col < w; col++) {
      int ox, oy;
      jpeg_upright(p->x + col, y, &ox, &oy);
      raster_splat_rgb565(ox, oy, line[col]);
    }
  }
  if ((++s_blocks & 15) == 0) delay(1);
  return raster_stale() ? 0 : 1;
}

// JPEGDEC rounds the scaled size up to the next MCU, same as jpeg.inl.
static int scaled_dim(int n, int scale)
{
  int shift = (scale == 2) ? 1 : (scale == 4) ? 2 : (scale == 8) ? 3 : 0;
  if (shift == 0) return n < 1 ? 1 : n;
  return (n + ((1 << shift) - 1)) >> shift;
}

static int choose_scale(int w, int h)
{
  const int cands[] = { 8, 4, 2, 1 };
  for (int s : cands) {
    if (scaled_dim(w, s) >= EPD_W && scaled_dim(h, s) >= EPD_H) return s;
  }
  return 1;
}

template <typename T>
static T* obj_new(bool internal_first)
{
  uint32_t a = internal_first ? (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)
                              : (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  uint32_t b = internal_first ? (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
                              : (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  void* m = heap_caps_malloc(sizeof(T), a);
  if (!m) m = heap_caps_malloc(sizeof(T), b);
  if (!m) return nullptr;
  return new (m) T();
}

template <typename T>
static void obj_free(T* p)
{
  if (!p) return;
  p->~T();
  heap_caps_free(p);
}

bool decode_jpeg_file(UsbFile* f)
{
  JPEGDEC* jpg = obj_new<JPEGDEC>(true);
  if (!jpg) {
    Serial.println("image: no memory for the JPEG decoder");
    return false;
  }
  s_blocks = 0;
  if (jpg->open(f, (int)f->size, nullptr, jpg_read, jpg_seek, jpg_draw) == 0) {
    Serial.printf("image: %s is not a readable JPEG (err %d)\n",
                  f->name, jpg->getLastError());
    obj_free(jpg);
    return false;
  }

  if (jpg->getJPEGType() == JPEG_MODE_PROGRESSIVE) {
    Serial.printf("image: %s is progressive JPEG, re-save it as baseline\n", f->name);
    jpg->close();
    obj_free(jpg);
    return false;
  }

  int raw_w = jpg->getWidth();
  int raw_h = jpg->getHeight();
  int orient = jpg->getOrientation();
  if (orient < 1 || orient > 8) orient = 1;
  bool swapped = orient >= 5;
  int disp_w = swapped ? raw_h : raw_w;
  int disp_h = swapped ? raw_w : raw_h;
  if (disp_w < 1 || disp_h < 1) {
    Serial.printf("image: %s has no pixels\n", f->name);
    jpg->close();
    obj_free(jpg);
    return false;
  }

  int scale = choose_scale(disp_w, disp_h);
  int sw = scaled_dim(disp_w, scale);
  int sh = scaled_dim(disp_h, scale);
  s_orient = orient;
  s_src_w = scaled_dim(raw_w, scale);
  s_src_h = scaled_dim(raw_h, scale);
  char desc[96];
  snprintf(desc, sizeof desc, "%s  jpeg %dx%d  1/%d  exif %d",
           f->name, raw_w, raw_h, scale, orient);

  if (!raster_begin(sw, sh, usb_disk_generation(), desc)) {
    jpg->close();
    obj_free(jpg);
    return false;
  }

  jpg->setPixelType(RGB565_LITTLE_ENDIAN);
  int opt = 0;
  if (scale == 2) opt |= JPEG_SCALE_HALF;
  else if (scale == 4) opt |= JPEG_SCALE_QUARTER;
  else if (scale == 8) opt |= JPEG_SCALE_EIGHTH;

  int rc = jpg->decode(0, 0, opt);
  int err = jpg->getLastError();
  jpg->close();
  obj_free(jpg);

  if (raster_stale()) {
    raster_abort();
    return false;
  }
  if (rc == 0) {
    Serial.printf("image: %s JPEG decode failed (err %d)\n", f->name, err);
    raster_abort();
    return false;
  }
  return raster_finish();
}
