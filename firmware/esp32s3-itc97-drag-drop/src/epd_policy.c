#include "epd_policy.h"

void epd_refresh_choice(const epd_choice_in* in, epd_choice_out* out)
{
  int draw_red;
  int drive_fast;

  if (!in || !out) return;

  drive_fast = in->requested_fast ? 1 : 0;
  draw_red = in->have_red &&
      (!in->requested_fast || in->fast_allow_red_build || in->allow_fast_red);

  // three_color keeps red on the short waveform. Promoting that request
  // would draw the red again, leave prev_has_red set, and force every
  // later red image onto the full waveform.
  if (in->requested_fast && in->allow_fast_red) {
    drive_fast = 1;
  } else if (in->requested_fast && in->prev_has_red && !in->headless) {
    drive_fast = 0;
    draw_red = 0;
  }

  out->drive_fast = drive_fast;
  out->draw_red = draw_red;
}

int epd_refresh_next_latch(const epd_choice_out* out)
{
  if (!out) return 0;
  return out->draw_red ? 1 : 0;
}
