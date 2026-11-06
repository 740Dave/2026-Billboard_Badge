// Gate for a firmware.bin dropped on the USB stick. No Arduino: the flasher
// and the host check share this, so a JPEG renamed to .bin never reaches flash.
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// hdr is the first bytes of the file. 1 when it is an ESP32-S3 app image
// that fits in an OTA slot of slot_size bytes.
int fw_image_ok(const uint8_t* hdr, uint32_t hdr_len, uint32_t file_size, uint32_t slot_size);

#ifdef __cplusplus
}
#endif
