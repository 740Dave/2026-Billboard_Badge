// -----------------------------------------------------------------------------
// Cover 960x672 by cutting the longer side, then dither.
//
// Decoders splat source pixels in their own coordinate space. This averages
// them into the panel grid, keeps saturated red on the red plane, and
// Floyd-Steinberg dithers the rest to black and white.
// -----------------------------------------------------------------------------
#pragma once

#include <stdint.h>
#include <stdbool.h>

// full_w/full_h are the decoder's output dimensions (already scaled and
// oriented). source_desc is copied into the log line. gen is the USB write
// generation at the start of the decode; a change aborts the send.
bool raster_begin(int full_w, int full_h, uint32_t gen, const char* source_desc);

// 1 Floyd-Steinberg, 0 hard threshold. Taken from CFG.TXT before the decode.
void raster_set_dither(int on);

void raster_splat_rgb(int sx, int sy, int r, int g, int b);
void raster_splat_rgb565(int sx, int sy, uint16_t p);

// Host wrote the disk again since raster_begin().
bool raster_stale(void);

// Dither into the EPD planes and clock them out over SPI.
// False if the disk changed or the send failed. Frees the accumulator.
bool raster_finish(void);

// Drop the accumulator without sending.
void raster_abort(void);
