#include "raster_cover.h"

void raster_cover_window(int full_w, int full_h, int panel_w, int panel_h, raster_cover_t* out)
{
  if (!out) return;
  if (full_w < 1) full_w = 1;
  if (full_h < 1) full_h = 1;
  if (panel_w < 1) panel_w = 1;
  if (panel_h < 1) panel_h = 1;

  if ((long long)full_w * panel_h >= (long long)full_h * panel_w) {
    out->den = full_h;
    out->num = panel_h;
    out->ch = full_h;
    out->cw = (int)(((long long)full_h * panel_w) / panel_h);
    if (out->cw < 1) out->cw = 1;
    if (out->cw > full_w) out->cw = full_w;
    out->ox = (full_w - out->cw) / 2;
    out->oy = 0;
  } else {
    out->den = full_w;
    out->num = panel_w;
    out->cw = full_w;
    out->ch = (int)(((long long)full_w * panel_h) / panel_w);
    if (out->ch < 1) out->ch = 1;
    if (out->ch > full_h) out->ch = full_h;
    out->ox = 0;
    out->oy = (full_h - out->ch) / 2;
  }
}
