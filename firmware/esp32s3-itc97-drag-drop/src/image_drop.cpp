#include <Arduino.h>
#include <string.h>

#include "decode_image.h"
#include "dither_cfg.h"
#include "image_drop.h"
#include "image_raster.h"
#include "usb_disk.h"

static uint32_t s_stamp;
static bool s_have_stamp;
static uint32_t s_seen_discard;

static uint32_t mix(uint32_t s, const uint8_t* p, uint32_t n)
{
  for (uint32_t i = 0; i < n; i++) s = s * 16777619u ^ p[i];
  return s;
}

static int load_dither(void)
{
  uint8_t buf[512];
  UsbFile f;
  memset(&f, 0, sizeof f);
  memset(buf, 0, sizeof buf);
  if (!usb_disk_open_named("cfg.txt", &f)) return 0;
  int32_t n = usb_file_read_at(&f, 0, buf, sizeof buf - 1);
  usb_disk_close(&f);
  if (n < 0) n = 0;
  return dither_cfg_parse((const char*)buf, (size_t)n);
}

static int sniff(const uint8_t* b, uint32_t n)
{
  if (n >= 3 && b[0] == 0xFF && b[1] == 0xD8 && b[2] == 0xFF) return USB_KIND_JPEG;
  if (n >= 8 && memcmp(b, "\x89PNG\r\n\x1a\n", 8) == 0) return USB_KIND_PNG;
  if (n >= 2 && b[0] == 'B' && b[1] == 'M') return USB_KIND_BMP;
  if (n >= 6 && (memcmp(b, "GIF87a", 6) == 0 || memcmp(b, "GIF89a", 6) == 0))
    return USB_KIND_UNSUPPORTED;
  if (n >= 12 && memcmp(b, "RIFF", 4) == 0 && memcmp(b + 8, "WEBP", 4) == 0)
    return USB_KIND_UNSUPPORTED;
  return USB_KIND_BAD;
}

static const char* kind_name(int kind)
{
  switch (kind) {
    case USB_KIND_JPEG: return "jpeg";
    case USB_KIND_PNG:  return "png";
    case USB_KIND_BMP:  return "bmp";
    case USB_KIND_UNSUPPORTED: return "unsupported";
    default: return "unrecognised";
  }
}

static uint32_t file_stamp(UsbFile* f)
{
  uint32_t s = f->size ^ (f->mtime * 0x9E3779B1u) ^ ((uint32_t)f->first_cluster << 16);
  uint8_t buf[64];
  memset(buf, 0, sizeof buf);
  int32_t n = usb_file_read_at(f, 0, buf, sizeof buf);
  if (n > 0) s = mix(s, buf, (uint32_t)n);
  if (f->size > sizeof buf) {
    memset(buf, 0, sizeof buf);
    n = usb_file_read_at(f, f->size - (uint32_t)sizeof buf, buf, sizeof buf);
    if (n > 0) s = mix(s, buf, (uint32_t)n);
  }
  return s;
}

int image_drop_poll(void)
{
  UsbFile f;
  uint32_t discarded = usb_disk_discard_count();
  memset(&f, 0, sizeof f);
  if (discarded != s_seen_discard) {
    s_seen_discard = discarded;
    s_have_stamp = false;
  }
  if (!usb_disk_newest(&f)) return DROP_IDLE;

  uint32_t g0 = usb_disk_generation();
  if (!usb_disk_open(&f)) {
    Serial.printf("drop: %s could not be read\n", f.name);
    return DROP_SKIP;
  }

  uint8_t hdr[16];
  memset(hdr, 0, sizeof hdr);
  int32_t hn = usb_file_read_at(&f, 0, hdr, sizeof hdr);
  int sniffed = hn > 0 ? sniff(hdr, (uint32_t)hn) : USB_KIND_BAD;
  if (sniffed == USB_KIND_JPEG || sniffed == USB_KIND_PNG || sniffed == USB_KIND_BMP ||
      sniffed == USB_KIND_UNSUPPORTED) {
    f.kind = sniffed;
  } else if (f.kind == USB_KIND_JPEG || f.kind == USB_KIND_PNG || f.kind == USB_KIND_BMP) {
    f.kind = USB_KIND_BAD;
  }

  uint32_t stamp = file_stamp(&f);
  // The file stays on the stick. The same bytes are not sent again until
  // the user replaces or deletes them.
  if (s_have_stamp && stamp == s_stamp) {
    usb_disk_close(&f);
    return DROP_IDLE;
  }
  if (usb_disk_generation() != g0) {
    usb_disk_close(&f);
    return DROP_RETRY;
  }

  Serial.printf("drop: %s  %u bytes  %s\n", f.name, (unsigned)f.size, kind_name(f.kind));
  int dither = load_dither();
  Serial.printf("cfg: dithering %d\n", dither);
  raster_set_dither(dither);

  bool ok = false;
  if (f.kind == USB_KIND_JPEG) ok = decode_jpeg_file(&f);
  else if (f.kind == USB_KIND_PNG) ok = decode_png_file(&f);
  else if (f.kind == USB_KIND_BMP) ok = decode_bmp_file(&f);
  else if (f.kind == USB_KIND_UNSUPPORTED)
    Serial.printf("drop: %s is not a JPG, PNG or BMP\n", f.name);
  else
    Serial.printf("drop: %s is not a picture this board can read\n", f.name);

  char sent_name[64];
  strncpy(sent_name, f.name, sizeof sent_name - 1);
  sent_name[sizeof sent_name - 1] = 0;
  usb_disk_close(&f);
  if (usb_disk_generation() != g0) return DROP_RETRY;

  if (ok) Serial.printf("drop: %s left on the stick\n", sent_name);

  s_stamp = stamp;
  s_have_stamp = true;
  return ok ? DROP_SENT : DROP_SKIP;
}
