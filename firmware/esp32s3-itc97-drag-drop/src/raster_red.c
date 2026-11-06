#include "raster_red.h"

static int clamp8(int v)
{
  if (v < 0) return 0;
  if (v > 255) return 255;
  return v;
}

int raster_red_sample(int r, int g, int b)
{
  r = clamp8(r);
  g = clamp8(g);
  b = clamp8(b);
  if (r < 140) return 0;
  if (g > 80 || b > 80) return 0;
  if (r < g + 90 || r < b + 90) return 0;
  return 1;
}

int raster_use_red(int color3, int dither, int reds, int n, int gray, int ink)
{
  if (!color3 || n <= 0 || reds <= 0) return 0;
  if (dither) {
    // Same dot as black and white. Floyd-Steinberg already decided ink or
    // paper; red only replaces the dark dots. The white side stays paper,
    // so a red area breaks into a stipple instead of a solid plate.
    if (!ink) return 0;
    if (reds * 5 < n * 4) return 0;
    (void)gray;
    return 1;
  }
  if (reds * 4 < n * 3) return 0;
  if (gray >= 140) return 0;
  return 1;
}
