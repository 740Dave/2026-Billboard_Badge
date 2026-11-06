// Host check for cover-crop and the red gate.
//   gcc -I include -o raster_test test/raster_test.c src/raster_cover.c src/raster_red.c
#include "raster_cover.h"
#include "raster_red.h"

#include <stdio.h>

static int g_fail;

static void expect_crop(const char* name, int w, int h, int ox, int oy, int cw, int ch)
{
  raster_cover_t c;
  raster_cover_window(w, h, 960, 672, &c);
  printf("%s crop %dx%d at %d,%d num/den %d/%d\n", name, c.cw, c.ch, c.ox, c.oy, c.num, c.den);
  if (c.ox != ox || c.oy != oy || c.cw != cw || c.ch != ch) {
    printf("FAIL %s wanted %dx%d at %d,%d\n", name, cw, ch, ox, oy);
    g_fail = 1;
  }
  if (c.den < 1 || c.num < 1) {
    printf("FAIL %s bad scale\n", name);
    g_fail = 1;
  }
}

static void expect_i(const char* name, int got, int want)
{
  printf("%s -> %d\n", name, got);
  if (got != want) {
    printf("FAIL %s wanted %d\n", name, want);
    g_fail = 1;
  }
}

int main(void)
{
  expect_crop("wide", 160, 40, 51, 0, 57, 40);
  expect_crop("jpeg", 180, 100, 19, 0, 142, 100);
  expect_crop("square", 48, 48, 0, 7, 48, 33);
  expect_crop("tiny", 8, 8, 0, 1, 8, 5);
  expect_crop("tall", 480, 900, 0, 282, 480, 336);

  expect_i("pure red", raster_red_sample(255, 0, 0), 1);
  expect_i("deep red", raster_red_sample(180, 20, 30), 1);
  expect_i("orange", raster_red_sample(255, 140, 0), 0);
  expect_i("pink", raster_red_sample(255, 200, 200), 0);
  expect_i("brown", raster_red_sample(140, 90, 40), 0);

  expect_i("dither deep", raster_use_red(1, 1, 80, 80, 76, 1), 1);
  expect_i("dither light red", raster_use_red(1, 1, 80, 80, 110, 1), 1);
  expect_i("dither half vote", raster_use_red(1, 1, 40, 80, 76, 1), 0);
  expect_i("dither white side", raster_use_red(1, 1, 80, 80, 76, 0), 0);
  expect_i("threshold mid", raster_use_red(1, 0, 80, 80, 110, 1), 1);
  expect_i("off", raster_use_red(0, 1, 80, 80, 76, 1), 0);

  if (g_fail) {
    printf("raster-test FAIL\n");
    return 1;
  }
  printf("raster-test ok\n");
  return 0;
}
