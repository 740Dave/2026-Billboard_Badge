// -----------------------------------------------------------------------------
// Drop a JPG, PNG or BMP on the USB drive the ESP opens. The picture is
// centre-cropped to the 9.7" panel and clocked out over SPI.
//
// Every picture uses red and the long LUT. CFG.TXT on the stick picks
// dithering. A reboot writes dithering 0 again. The file stays on the stick.
// -----------------------------------------------------------------------------
#include <Arduino.h>
#include "esp_system.h"

#include "epd97.h"
#include "usb_disk.h"
#include "usb_fw.h"
#include "image_drop.h"

static uint32_t s_seen_gen;

void setup()
{
  Serial.begin(115200);
  Serial.enableReboot(false);
  Serial.setTxTimeoutMs(20);
  delay(200);
  Serial.println("\n=== EPD drop ===");
  Serial.printf("drop: reset reason %d\n", (int)esp_reset_reason());
  Serial.println("drop: log is the native USB serial, there is no hardware UART");
  Serial.println("drop: JPG, PNG or BMP, red on, long LUT");
  Serial.println("drop: CFG.TXT dithering 0 by default, 1 is error diffusion");
  Serial.println("drop: the file stays on the stick after the refresh");
  usb_fw_boot_note();

  if (!usb_disk_begin()) Serial.println("drop: USB disk failed");

  epd_set_run_refresh(true);
  if (!epd_begin()) Serial.println("drop: epd_begin() failed");

  Serial.printf("drop: panel %s, headless %s, waveform refresh %s\n",
                epd_panel_name(),
                epd_headless() ? "yes" : "no",
                epd_run_refresh() ? "on" : "off");
  Serial.printf("drop: psram %u KB free\n", (unsigned)(ESP.getFreePsram() / 1024));
}

void loop()
{
  usb_disk_service_log();
  uint32_t gen = 0;
  if (!usb_disk_settled(&gen)) return;
  if (gen == s_seen_gen) return;

  int fw = usb_fw_poll();
  if (fw == FW_RETRY) return;
  if (fw == FW_HANDLED) {
    s_seen_gen = gen;
    return;
  }

  int rc = image_drop_poll();
  if (rc != DROP_RETRY) s_seen_gen = gen;
}
