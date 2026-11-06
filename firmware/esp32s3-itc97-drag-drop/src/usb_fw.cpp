#include <Arduino.h>
#include <string.h>

#include "esp_ota_ops.h"
#include "esp_system.h"

#include "fw_image.h"
#include "usb_disk.h"
#include "usb_fw.h"

static uint32_t s_done_stamp;
static int s_have_done;

static bool open_bin(UsbFile* f)
{
  memset(f, 0, sizeof *f);
  if (usb_disk_open_named("firmware.bin", f)) return true;
  memset(f, 0, sizeof *f);
  return usb_disk_open_named("update.bin", f);
}

static uint32_t bin_stamp(const UsbFile* f)
{
  return f->size ^ (f->mtime * 0x9E3779B1u) ^ ((uint32_t)f->first_cluster << 16);
}

void usb_fw_boot_note(void)
{
  const esp_partition_t* run = esp_ota_get_running_partition();
  Serial.printf("fw: running %s. Drop firmware.bin on EPD97 to update over USB.\n",
                run ? run->label : "unknown");
}

int usb_fw_poll(void)
{
  UsbFile f;
  uint8_t hdr[24];
  uint8_t* buf = nullptr;
  esp_ota_handle_t handle = 0;
  const esp_partition_t* slot;
  uint32_t g0;
  uint32_t off;
  int result = FW_HANDLED;

  if (!open_bin(&f)) return FW_NONE;

  g0 = usb_disk_generation();
  {
    uint32_t st = bin_stamp(&f);
    if (s_have_done && st == s_done_stamp) {
      usb_disk_close(&f);
      return FW_NONE;
    }
    s_done_stamp = st;
    s_have_done = 1;
  }
  Serial.printf("fw: %s  %u bytes\n", f.name, (unsigned)f.size);

  slot = esp_ota_get_next_update_partition(NULL);
  if (!slot) {
    Serial.println("fw: no inactive app slot");
    usb_disk_close(&f);
    return FW_HANDLED;
  }

  memset(hdr, 0, sizeof hdr);
  if (usb_file_read_at(&f, 0, hdr, sizeof hdr) != (int32_t)sizeof hdr ||
      !fw_image_ok(hdr, sizeof hdr, f.size, slot->size)) {
    Serial.printf("fw: %s is not an ESP32-S3 firmware.bin for a %u-byte slot\n",
                  f.name, (unsigned)slot->size);
    usb_disk_close(&f);
    return FW_HANDLED;
  }
  if (usb_disk_generation() != g0) {
    s_have_done = 0;
    usb_disk_close(&f);
    return FW_RETRY;
  }

  Serial.printf("fw: writing %s at 0x%x\n", slot->label, (unsigned)slot->address);
  if (esp_ota_begin(slot, f.size, &handle) != ESP_OK) {
    Serial.println("fw: esp_ota_begin failed");
    usb_disk_close(&f);
    return FW_HANDLED;
  }

  buf = (uint8_t*)malloc(4096);
  if (!buf) {
    Serial.println("fw: no buffer");
    esp_ota_abort(handle);
    usb_disk_close(&f);
    return FW_HANDLED;
  }

  off = 0;
  while (off < f.size) {
    uint32_t ask = f.size - off;
    int32_t n;
    if (ask > 4096) ask = 4096;
    n = usb_file_read_at(&f, off, buf, ask);
    if (n <= 0 || esp_ota_write(handle, buf, (size_t)n) != ESP_OK) {
      Serial.printf("fw: write failed at %u\n", (unsigned)off);
      result = FW_HANDLED;
      goto done;
    }
    off += (uint32_t)n;
    if (usb_disk_generation() != g0) {
      Serial.println("fw: disk changed, update aborted");
      s_have_done = 0;
      result = FW_RETRY;
      goto done;
    }
    if ((off & 0xFFFF) == 0) {
      Serial.printf("fw: %u / %u\n", (unsigned)off, (unsigned)f.size);
      delay(1);
    }
  }

  if (esp_ota_end(handle) != ESP_OK || esp_ota_set_boot_partition(slot) != ESP_OK) {
    Serial.println("fw: image rejected, still on the current firmware");
    handle = 0;
    free(buf);
    usb_disk_close(&f);
    return FW_HANDLED;
  }
  handle = 0;
  free(buf);
  usb_disk_close(&f);
  Serial.printf("fw: verified, rebooting into %s\n", slot->label);
  Serial.flush();
  delay(200);
  esp_restart();
  return FW_HANDLED;

done:
  if (handle) esp_ota_abort(handle);
  free(buf);
  usb_disk_close(&f);
  return result;
}
