// -----------------------------------------------------------------------------
// RAM-disk mass storage on the ESP32-S3 native USB port.
//
// The host sees a FAT16 volume named EPD97. Drop a JPG, PNG or BMP on it.
// The disk lives in PSRAM and is reformatted on every boot.
// -----------------------------------------------------------------------------
#pragma once

#include <stdint.h>
#include <stdbool.h>

enum {
  USB_KIND_NONE = 0,
  USB_KIND_JPEG = 1,
  USB_KIND_PNG  = 2,
  USB_KIND_BMP  = 3,
  USB_KIND_UNSUPPORTED = 4,
  USB_KIND_BAD  = 5
};

// A file on the volume. map is filled by usb_disk_open() and owned by the
// caller until usb_disk_close().
struct UsbFile {
  char     name[64];
  uint32_t size;
  uint32_t mtime;          // FAT date in the high half, time in the low half
  uint16_t first_cluster;
  int      kind;
  uint16_t* map;
  uint32_t map_n;
  uint32_t cluster_bytes;
  uint32_t data_lba;
};

// False if the PSRAM disk could not be allocated. USB is up when this returns
// true: the host can mount as soon as the cable enumerates.
bool usb_disk_begin(void);

// Host writes have been quiet long enough that a copied file is complete,
// or the host ejected. *gen is the write generation that decision was made on.
bool usb_disk_settled(uint32_t* gen);

uint32_t usb_disk_generation(void);
uint32_t usb_disk_bytes(void);

// Bumps when a released directory entry is dropped. Image files stay.
uint32_t usb_disk_discard_count(void);

// USB callbacks only latch these lines. Print them from the main loop:
// a CDC write inside the USB task deadlocks the stick.
void usb_disk_service_log(void);

// Newest JPG/PNG/BMP (or an explicitly unsupported image type) in the root
// and one directory level down. False when there is nothing to show.
bool usb_disk_newest(UsbFile* out);

// Open one file by name (case-insensitive), root and one directory level.
// A named root file is found this way. The caller closes it.
bool usb_disk_open_named(const char* name, UsbFile* out);

bool usb_disk_open(UsbFile* f);
void usb_disk_close(UsbFile* f);

// Read at an absolute file offset. Returns bytes copied (short at EOF).
int32_t usb_file_read_at(UsbFile* f, uint32_t offset, uint8_t* dst, uint32_t len);

// Delete the picture that was just drawn. The cable stays plugged in.
// Marks that root file deleted. False if that cluster is not in the root.
bool usb_disk_release_sent(const char* name, uint32_t size, uint32_t mtime, uint16_t first_cluster);
