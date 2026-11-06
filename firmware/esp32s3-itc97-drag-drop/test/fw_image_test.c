// Host check for the firmware.bin gate. Compiles fw_image.c directly.
//   gcc -I include -o fw_image_test test/fw_image_test.c src/fw_image.c
#include "fw_image.h"

#include <stdio.h>
#include <stdint.h>
#include <string.h>

static int g_fail;

static void expect(const char* name, int got, int want)
{
  printf("%s -> %d\n", name, got);
  if (got != want) {
    printf("FAIL %s wanted %d\n", name, want);
    g_fail = 1;
  }
}

int main(void)
{
  uint8_t hdr[24];
  const uint32_t slot = 0x640000;
  FILE* fp;
  long sz;

  memset(hdr, 0, sizeof hdr);
  hdr[0] = 0xE9;
  hdr[1] = 6;
  hdr[12] = 0x09;
  hdr[13] = 0x00;
  expect("s3 header", fw_image_ok(hdr, 24, 474160, slot), 1);
  expect("short header", fw_image_ok(hdr, 8, 474160, slot), 0);
  hdr[0] = 0xFF;
  expect("jpeg magic", fw_image_ok(hdr, 24, 474160, slot), 0);
  hdr[0] = 0xE9;
  hdr[12] = 0x00;
  expect("esp32 not s3", fw_image_ok(hdr, 24, 474160, slot), 0);
  hdr[12] = 0x09;
  expect("larger than slot", fw_image_ok(hdr, 24, slot + 1, slot), 0);
  expect("empty file", fw_image_ok(hdr, 24, 0, slot), 0);
  hdr[1] = 0;
  expect("no segments", fw_image_ok(hdr, 24, 474160, slot), 0);

  fp = fopen(".pio/build/drop/firmware.bin", "rb");
  if (!fp) {
    printf("FAIL could not open .pio/build/drop/firmware.bin\n");
    return 1;
  }
  if (fread(hdr, 1, 24, fp) != 24) {
    printf("FAIL short firmware.bin\n");
    fclose(fp);
    return 1;
  }
  if (fseek(fp, 0, SEEK_END) != 0) {
    printf("FAIL seek firmware.bin\n");
    fclose(fp);
    return 1;
  }
  sz = ftell(fp);
  fclose(fp);
  printf("built firmware.bin %ld bytes magic %02X chip %u\n",
         sz, hdr[0], (unsigned)(hdr[12] | (hdr[13] << 8)));
  expect("built image", fw_image_ok(hdr, 24, (uint32_t)sz, slot), 1);

  if (g_fail) {
    printf("fw-image-test FAIL\n");
    return 1;
  }
  printf("fw-image-test ok\n");
  return 0;
}
