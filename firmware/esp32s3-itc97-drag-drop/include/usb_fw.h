#pragma once

enum {
  FW_NONE = 0,      // no firmware.bin on the volume
  FW_HANDLED = 1,   // accepted and rebooting, or rejected until the file changes
  FW_RETRY = 2      // the disk changed while the image was being read
};

// Print which slot is running, once at boot.
void usb_fw_boot_note(void);

// If firmware.bin or update.bin has settled on the stick, write it to the
// inactive app slot and reboot. Images are left for image_drop_poll.
int usb_fw_poll(void);
