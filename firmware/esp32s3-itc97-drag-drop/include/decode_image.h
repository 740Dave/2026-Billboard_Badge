#pragma once

#include "usb_disk.h"

// True when the picture was dithered and clocked out.
bool decode_jpeg_file(UsbFile* f);
bool decode_png_file(UsbFile* f);
bool decode_bmp_file(UsbFile* f);
