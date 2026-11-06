// dithering key in CFG.TXT. No Arduino. Default is off.
#pragma once

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// Fresh file written on every boot. One FAT cluster, well under 512 bytes.
const char* dither_cfg_stock(void);

// 1 or 0. Missing file, missing key, blank lines and comments stay at 0.
int dither_cfg_parse(const char* text, size_t len);

#ifdef __cplusplus
}
#endif
