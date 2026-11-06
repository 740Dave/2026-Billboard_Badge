// -----------------------------------------------------------------------------
// Which waveform a frame actually runs. No Arduino: the driver and the host
// check share this so a fast request with 3-color red stays fast.
// -----------------------------------------------------------------------------
#pragma once

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct epd_choice_in {
  int requested_fast;
  int have_red;
  int allow_fast_red;       // cfg three_color while a fast frame is allowed to keep red
  int prev_has_red;
  int headless;
  int fast_allow_red_build; // compile-time EPD_FAST_ALLOW_RED
} epd_choice_in;

typedef struct epd_choice_out {
  int drive_fast;
  int draw_red;
} epd_choice_out;

void epd_refresh_choice(const epd_choice_in* in, epd_choice_out* out);

/* What refresh_display stores in s_prev_has_red after this frame. */
int epd_refresh_next_latch(const epd_choice_out* out);

#ifdef __cplusplus
}
#endif
