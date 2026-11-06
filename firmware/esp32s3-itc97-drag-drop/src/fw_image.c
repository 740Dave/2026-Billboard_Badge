#include "fw_image.h"

int fw_image_ok(const uint8_t* hdr, uint32_t hdr_len, uint32_t file_size, uint32_t slot_size)
{
  uint16_t chip;

  if (!hdr || hdr_len < 24 || slot_size < 24) return 0;
  if (hdr[0] != 0xE9) return 0;
  if (hdr[1] < 1 || hdr[1] > 16) return 0;
  chip = (uint16_t)hdr[12] | ((uint16_t)hdr[13] << 8);
  if (chip != 0x0009) return 0;
  if (file_size < 24 || file_size > slot_size) return 0;
  return 1;
}
