#pragma once

enum {
  DROP_IDLE = 0,    // nothing new
  DROP_SENT = 1,    // planes clocked out
  DROP_RETRY = 2,   // the disk changed while we were reading
  DROP_SKIP = 3     // not an image we can decode; won't retry until it changes
};

int image_drop_poll(void);
