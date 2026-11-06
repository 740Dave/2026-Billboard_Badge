// Which pixels may drive the red plane. No Arduino.
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

// 1 for a saturated red sample. Orange, pink and brown are 0.
int raster_red_sample(int r, int g, int b);

// 1 when this panel pixel should be red ink.
// With dithering, red only replaces a dark Floyd-Steinberg dot. The
// white side of that pattern stays paper.
int raster_use_red(int color3, int dither, int reds, int n, int gray, int ink);

#ifdef __cplusplus
}
#endif
