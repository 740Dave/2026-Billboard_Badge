// How a picture covers the panel. No Arduino: the raster and the host check
// share this. The picture is scaled by one factor on both axes so it fills
// the panel. The longer side is cut, centred. Nothing is stretched.
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

typedef struct raster_cover {
  int ox, oy;          // top-left of the kept source window
  int cw, ch;          // kept source window
  int num, den;        // dest = (src - origin) * num / den, both axes
} raster_cover_t;

void raster_cover_window(int full_w, int full_h, int panel_w, int panel_h, raster_cover_t* out);

#ifdef __cplusplus
}
#endif
