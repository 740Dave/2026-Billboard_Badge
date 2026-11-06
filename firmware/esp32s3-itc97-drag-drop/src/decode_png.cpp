#include <Arduino.h>
#include <stdio.h>
#include <string.h>
#include <new>

#include "esp_heap_caps.h"
#include "PNGdec.h"

#include "decode_image.h"
#include "image_raster.h"
#include "usb_disk.h"

static UsbFile* s_file;
static PNG* s_png;
static uint16_t* s_line;
static int s_rows;

static void* png_open_cb(const char* name, int32_t* pSize)
{
  (void)name;
  if (!s_file) return nullptr;
  *pSize = (int32_t)s_file->size;
  return s_file;
}

static int32_t png_read(PNGFILE* pf, uint8_t* buf, int32_t len)
{
  if (!pf || len <= 0 || pf->iPos >= pf->iSize) return 0;
  UsbFile* f = (UsbFile*)pf->fHandle;
  int32_t n = usb_file_read_at(f, (uint32_t)pf->iPos, buf, (uint32_t)len);
  pf->iPos += n;
  return n;
}

static int32_t png_seek(PNGFILE* pf, int32_t pos)
{
  if (!pf) return 0;
  if (pos < 0) pos = 0;
  if (pf->iSize > 0 && pos > pf->iSize) pos = pf->iSize;
  pf->iPos = pos;
  return pos;
}

static int png_draw(PNGDRAW* p)
{
  if (!s_png || !s_line || !p) return 0;
  s_png->getLineAsRGB565(p, s_line, PNG_RGB565_LITTLE_ENDIAN, 0xFFFFFFFF);
  int w = p->iWidth;
  for (int x = 0; x < w; x++) raster_splat_rgb565(x, p->y, s_line[x]);
  if ((++s_rows & 15) == 0) delay(1);
  return raster_stale() ? 0 : 1;
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

bool decode_png_file(UsbFile* f)
{
  // The decoder object holds a 32 KB inflate window plus a line pair wide
  // enough for a photo. That belongs in PSRAM, not the internal heap.
  PNG* png = obj_new<PNG>(false);
  if (!png) {
    Serial.println("image: no memory for the PNG decoder");
    return false;
  }
  s_file = f;
  s_png = png;
  s_line = nullptr;
  s_rows = 0;

  // PNGdec's open() returns 0 for success. JPEGDEC is the other way round.
  if (png->open("img", png_open_cb, nullptr, png_read, png_seek, png_draw) != 0) {
    Serial.printf("image: %s is not a readable PNG (err %d)\n",
                  f->name, png->getLastError());
    s_png = nullptr;
    s_file = nullptr;
    obj_free(png);
    return false;
  }

  int w = png->getWidth();
  int h = png->getHeight();
  if (png->isInterlaced()) {
    Serial.printf("image: %s is an interlaced PNG, re-save it without interlacing\n", f->name);
    png->close();
    s_png = nullptr;
    s_file = nullptr;
    obj_free(png);
    return false;
  }
  if (w < 1 || h < 1 || w > 16384 || h > 32767) {
    Serial.printf("image: PNG %dx%d is outside the size we handle\n", w, h);
    png->close();
    s_png = nullptr;
    s_file = nullptr;
    obj_free(png);
    return false;
  }

  s_line = (uint16_t*)heap_caps_malloc((size_t)w * sizeof(uint16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!s_line) s_line = (uint16_t*)heap_caps_malloc((size_t)w * sizeof(uint16_t), MALLOC_CAP_INTERNAL);
  if (!s_line) {
    Serial.println("image: no memory for a PNG line");
    png->close();
    s_png = nullptr;
    s_file = nullptr;
    obj_free(png);
    return false;
  }

  char desc[96];
  snprintf(desc, sizeof desc, "%s  png %dx%d", f->name, w, h);
  if (!raster_begin(w, h, usb_disk_generation(), desc)) {
    heap_caps_free(s_line);
    s_line = nullptr;
    png->close();
    s_png = nullptr;
    s_file = nullptr;
    obj_free(png);
    return false;
  }

  int rc = png->decode(nullptr, 0);
  int err = png->getLastError();
  png->close();
  heap_caps_free(s_line);
  s_line = nullptr;
  s_png = nullptr;
  s_file = nullptr;
  obj_free(png);

  if (raster_stale()) {
    raster_abort();
    return false;
  }
  // decode() returns the PNG error code; 0 is success.
  if (rc != PNG_SUCCESS) {
    Serial.printf("image: %s PNG decode failed (err %d)\n", f->name, err);
    raster_abort();
    return false;
  }
  return raster_finish();
}
