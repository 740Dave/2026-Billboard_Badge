// -----------------------------------------------------------------------------
// FAT16 volume in PSRAM, exported as a USB mass-storage device.
//
// FAT16 (not FAT12) because a multi-megabyte disk with 512-byte clusters has
// more than 4084 clusters, and that is the line where the spec says the
// volume is FAT16. Windows mounts a super-floppy BPB on a removable MSC
// device, which is what TinyUSB reports, so there is no partition table.
//
// The two CPU caches are not coherent. Host writes are writeback'd before
// the callback returns; the app core invalidates a sector before reading it.
// -----------------------------------------------------------------------------
#include <Arduino.h>
#include <string.h>
#include <stdio.h>

#include "esp_heap_caps.h"
#include "rom/cache.h"
#include "USB.h"
#include "USBMSC.h"

#include "dither_cfg.h"
#include "usb_disk.h"

#if ARDUINO_USB_MODE != 0
#error Native USB must be in OTG mode (ARDUINO_USB_MODE=0) for the drop drive
#endif

static const uint32_t kIdleMs = 1500;
static const uint32_t kSector = 512;



struct Geom {
  uint32_t sectors;
  uint16_t reserved;
  uint8_t  fats;
  uint16_t root_entries;
  uint16_t root_sectors;
  uint16_t spc;
  uint16_t fat_sectors;
  uint32_t fat_lba;
  uint32_t root_lba;
  uint32_t data_lba;
  uint32_t clusters;
};

static uint8_t* s_disk;
static uint32_t s_disk_bytes;
static Geom     s_geo;
static USBMSC   s_msc;

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t s_gen;
static volatile uint32_t s_last_write_ms;
static volatile bool s_force_settle;
static volatile uint32_t s_discard_count;
static volatile uint8_t s_log_plug;     // 1 plugged, 2 unplugged
static volatile int s_log_removed = -1;
static volatile uint8_t s_log_eject;
struct ReleasedFile {
  char name[64];
  uint32_t size;
  uint32_t mtime;
  uint16_t cluster;
};
static ReleasedFile s_rel[8];
static uint8_t s_rel_n;

static uint32_t s_fat_cache_lba = 0xFFFFFFFFu;
static uint8_t  s_fat_cache[512];

// -----------------------------------------------------------------------------
// Cache maintenance. Ranges stay inside the disk allocation, which is 64-byte
// aligned, so rounding to the 32-byte line size cannot touch a neighbour.
// -----------------------------------------------------------------------------
static void cache_wb(uint32_t off, uint32_t len)
{
  if (!s_disk || len == 0 || off >= s_disk_bytes) return;
  uint32_t start = off & ~31u;
  uint32_t end = (off + len + 31u) & ~31u;
  if (end > s_disk_bytes) end = s_disk_bytes & ~31u;
  if (end <= start) return;
  Cache_WriteBack_Addr((uint32_t)(s_disk + start), end - start);
}

static void cache_inv(uint32_t off, uint32_t len)
{
  if (!s_disk || len == 0 || off >= s_disk_bytes) return;
  uint32_t start = off & ~31u;
  uint32_t end = (off + len + 31u) & ~31u;
  if (end > s_disk_bytes) end = s_disk_bytes & ~31u;
  if (end <= start) return;
  Cache_Invalidate_Addr((uint32_t)(s_disk + start), end - start);
}

static void disk_copy_out(uint32_t lba, uint32_t off, uint8_t* dst, uint32_t n)
{
  if (n == 0) return;
  if (lba >= s_geo.sectors || off + n > kSector) {
    memset(dst, 0, n);
    return;
  }
  uint32_t byte_off = lba * kSector + off;
  portENTER_CRITICAL(&s_mux);
  cache_inv(byte_off, n);
  memcpy(dst, s_disk + byte_off, n);
  portEXIT_CRITICAL(&s_mux);
}

static void wr16(uint8_t* p, uint16_t v)
{
  p[0] = (uint8_t)v;
  p[1] = (uint8_t)(v >> 8);
}

static void wr32(uint8_t* p, uint32_t v)
{
  p[0] = (uint8_t)v;
  p[1] = (uint8_t)(v >> 8);
  p[2] = (uint8_t)(v >> 16);
  p[3] = (uint8_t)(v >> 24);
}

static uint16_t rd16(const uint8_t* p)
{
  return (uint16_t)(p[0] | (p[1] << 8));
}

static uint32_t rd32(const uint8_t* p)
{
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

// -----------------------------------------------------------------------------
// Geometry. 512-byte clusters keep the implementation a flat list of sectors.
// The cluster count has to land in the FAT16 window or Windows will not
// mount the volume.
// -----------------------------------------------------------------------------
static bool geom_fat16(uint32_t sectors, Geom* g)
{
  g->sectors = sectors;
  g->reserved = 1;
  g->fats = 2;
  g->root_entries = 512;
  g->root_sectors = (uint16_t)((g->root_entries * 32) / kSector);
  g->spc = 1;

  uint16_t fat = 1;
  for (int guard = 0; guard < 8; guard++) {
    int32_t data = (int32_t)sectors - g->reserved - (int32_t)g->fats * fat - g->root_sectors;
    if (data < 1) return false;
    uint32_t clusters = (uint32_t)data / g->spc;
    uint32_t bytes = (clusters + 2) * 2;
    uint32_t need = (bytes + (kSector - 1)) / kSector;
    if (need <= fat) {
      if (clusters < 4085 || clusters >= 65525) return false;
      g->fat_sectors = fat;
      g->fat_lba = g->reserved;
      g->root_lba = g->fat_lba + (uint32_t)g->fats * fat;
      g->data_lba = g->root_lba + g->root_sectors;
      g->clusters = clusters;
      if (g->data_lba + clusters * g->spc > sectors) return false;
      return true;
    }
    if (need > 400) return false;
    fat = (uint16_t)need;
  }
  return false;
}

static void fat_format_put(uint16_t cluster, uint16_t val)
{
  uint32_t off = (uint32_t)cluster * 2u;
  for (int f = 0; f < s_geo.fats; f++) {
    uint8_t* p = s_disk + (s_geo.fat_lba + (uint32_t)f * s_geo.fat_sectors) * kSector + off;
    wr16(p, val);
  }
}

static void write_dirent(uint8_t* e, const char* name11, uint8_t attr,
                         uint16_t cluster, uint32_t size)
{
  memcpy(e, name11, 11);
  e[11] = attr;
  // Fixed stamp so a freshly formatted volume is deterministic. 2026-09-24 12:00.
  const uint16_t date = (uint16_t)((46u << 9) | (9u << 5) | 24u);
  const uint16_t time = (uint16_t)(12u << 11);
  wr16(e + 14, time);
  wr16(e + 16, date);
  wr16(e + 18, date);
  wr16(e + 22, time);
  wr16(e + 24, date);
  wr16(e + 26, cluster);
  wr32(e + 28, size);
}

static void format_disk(void)
{
  memset(s_disk, 0, s_disk_bytes);

  uint8_t* bpb = s_disk;
  bpb[0] = 0xEB;
  bpb[1] = 0x3C;
  bpb[2] = 0x90;
  memcpy(bpb + 3, "MSDOS5.0", 8);
  wr16(bpb + 11, (uint16_t)kSector);
  bpb[13] = (uint8_t)s_geo.spc;
  wr16(bpb + 14, s_geo.reserved);
  bpb[16] = s_geo.fats;
  wr16(bpb + 17, s_geo.root_entries);
  if (s_geo.sectors < 65536) wr16(bpb + 19, (uint16_t)s_geo.sectors);
  bpb[21] = 0xF8;
  wr16(bpb + 22, s_geo.fat_sectors);
  wr16(bpb + 24, 63);
  wr16(bpb + 26, 255);
  wr32(bpb + 28, 0);
  wr32(bpb + 32, s_geo.sectors < 65536 ? 0 : s_geo.sectors);
  bpb[36] = 0x00;
  bpb[38] = 0x29;
  wr32(bpb + 39, esp_random());
  memcpy(bpb + 43, "EPD97      ", 11);
  memcpy(bpb + 54, "FAT16   ", 8);
  bpb[510] = 0x55;
  bpb[511] = 0xAA;

  fat_format_put(0, 0xFFF8);
  fat_format_put(1, 0xFFFF);

  // CFG.TXT only. Reboot formats the RAM disk again, so dithering returns to 0.
  const char* cfg_text = dither_cfg_stock();
  const uint32_t cfg_n = (uint32_t)strlen(cfg_text);
  fat_format_put(2, 0xFFFF);
  memcpy(s_disk + (size_t)s_geo.data_lba * kSector, cfg_text, cfg_n);

  uint8_t* root = s_disk + s_geo.root_lba * kSector;
  write_dirent(root, "EPD97      ", 0x08, 0, 0);
  write_dirent(root + 32, "CFG     TXT", 0x20, 2, cfg_n);
}

static bool parse_bpb(void)
{
  uint8_t bpb[512];
  // Direct, same-core read of what we just wrote or of a host rewrite.
  // Invalidate first so a host rewrite of sector 0 is visible.
  disk_copy_out(0, 0, bpb, 512);
  if (bpb[510] != 0x55 || bpb[511] != 0xAA) {
    Serial.println("usb: boot sector has no 55 AA signature");
    return false;
  }
  uint16_t bps = rd16(bpb + 11);
  if (bps != kSector) {
    Serial.printf("usb: sector size %u is not 512\n", bps);
    return false;
  }
  Geom g;
  memset(&g, 0, sizeof g);
  g.spc = bpb[13];
  g.reserved = rd16(bpb + 14);
  g.fats = bpb[16];
  g.root_entries = rd16(bpb + 17);
  uint16_t tot16 = rd16(bpb + 19);
  uint32_t tot32 = rd32(bpb + 32);
  g.sectors = tot16 ? tot16 : tot32;
  g.fat_sectors = rd16(bpb + 22);
  if (g.spc == 0 || (g.spc & (g.spc - 1)) != 0 || g.fats == 0 || g.reserved == 0 ||
      g.fat_sectors == 0 || g.sectors == 0 || g.sectors > s_disk_bytes / kSector) {
    Serial.println("usb: BPB geometry is not usable");
    return false;
  }
  g.root_sectors = (uint16_t)(((uint32_t)g.root_entries * 32 + (kSector - 1)) / kSector);
  g.fat_lba = g.reserved;
  g.root_lba = g.fat_lba + (uint32_t)g.fats * g.fat_sectors;
  g.data_lba = g.root_lba + g.root_sectors;
  if (g.data_lba >= g.sectors) {
    Serial.println("usb: BPB has no data region");
    return false;
  }
  uint32_t data = g.sectors - g.data_lba;
  g.clusters = data / g.spc;
  if (g.clusters < 4085 || g.clusters >= 65525) {
    Serial.printf("usb: %u clusters is not FAT16 (4085..65524)\n",
                  (unsigned)g.clusters);
    return false;
  }
  s_geo = g;
  s_fat_cache_lba = 0xFFFFFFFFu;
  return true;
}

static uint16_t fat_get(uint16_t cluster)
{
  uint32_t off = (uint32_t)cluster * 2u;
  uint32_t lba = s_geo.fat_lba + off / kSector;
  uint32_t i = off % kSector;
  if (lba != s_fat_cache_lba) {
    disk_copy_out(lba, 0, s_fat_cache, kSector);
    s_fat_cache_lba = lba;
  }
  return rd16(s_fat_cache + i);
}

// -----------------------------------------------------------------------------
// USB callbacks. No Serial in the read/write path: it is too slow and the
// host times the transfer out.
// -----------------------------------------------------------------------------
static bool same_ci(const char* a, const char* b)
{
  if (!a || !b) return false;
  while (*a && *b) {
    char ca = *a, cb = *b;
    if (ca >= 'A' && ca <= 'Z') ca = (char)(ca - 'A' + 'a');
    if (cb >= 'A' && cb <= 'Z') cb = (char)(cb - 'A' + 'a');
    if (ca != cb) return false;
    a++;
    b++;
  }
  return *a == 0 && *b == 0;
}

static void dir_short_name(const uint8_t* e, char* out, size_t out_n)
{
  size_t n = 0;
  int i;
  if (out_n == 0) return;
  for (i = 0; i < 8 && e[i] != ' '; i++) {
    if (n + 1 >= out_n) break;
    out[n++] = (char)e[i];
  }
  if (e[8] != ' ') {
    if (n + 1 < out_n) out[n++] = '.';
    for (i = 8; i < 11 && e[i] != ' '; i++) {
      if (n + 1 >= out_n) break;
      out[n++] = (char)e[i];
    }
  }
  out[n] = 0;
}

static void remember_released(const char* name, uint32_t size, uint32_t mtime, uint16_t cluster)
{
  ReleasedFile* slot;
  if (s_rel_n < 8) slot = &s_rel[s_rel_n++];
  else {
    memmove(&s_rel[0], &s_rel[1], sizeof(ReleasedFile) * 7);
    slot = &s_rel[7];
  }
  memset(slot, 0, sizeof *slot);
  if (name) strncpy(slot->name, name, sizeof slot->name - 1);
  slot->size = size;
  slot->mtime = mtime;
  slot->cluster = cluster;
}

static bool dirent_was_released(const uint8_t* e)
{
  char shortn[16];
  uint16_t cluster = rd16(e + 26);
  uint32_t size = rd32(e + 28);
  uint32_t mtime = ((uint32_t)rd16(e + 24) << 16) | rd16(e + 22);
  uint8_t i;
  dir_short_name(e, shortn, sizeof shortn);
  for (i = 0; i < s_rel_n && i < 8; i++) {
    bool same_name = same_ci(s_rel[i].name, shortn);
    bool same_when = s_rel[i].size == size && s_rel[i].mtime == mtime;
    bool same_cluster = cluster >= 2 && s_rel[i].cluster != 0 &&
                        s_rel[i].cluster == cluster;
    // The host writing the old directory entry back: same picture, same time.
    if (same_name && same_when) return true;
    if (same_cluster && same_when) return true;
    // A new drop reused the freed cluster, or the same name has a new time.
    if (same_cluster) s_rel[i].cluster = 0;
  }
  return false;
}

// A host that still has the old folder cached will write that folder back.
// Drop those picture entries again so the next read does not show them.
// Caller holds s_mux. A host writeback of a released name is stripped.
static void scrub_released_dirents(void)
{
  uint32_t entries;
  uint32_t i;
  if (!s_disk || s_rel_n == 0) return;
  if (s_geo.root_sectors == 0) return;
  entries = (uint32_t)s_geo.root_sectors * (kSector / 32u);
  for (i = 0; i < entries; i++) {
    uint8_t* e = s_disk + (s_geo.root_lba * kSector) + i * 32u;
    uint32_t back;
    if (e[0] == 0x00) break;
    if (e[0] == 0xE5 || e[11] == 0x0F) continue;
    if (e[11] & 0x18) continue;
    if (!dirent_was_released(e)) continue;
    e[0] = 0xE5;
    for (back = 1; back <= i; back++) {
      uint8_t* prev = e - back * 32u;
      if (prev[11] != 0x0F || prev[0] == 0x00 || prev[0] == 0xE5) break;
      prev[0] = 0xE5;
    }
  }
}

static int32_t on_read(uint32_t lba, uint32_t offset, void* buffer, uint32_t bufsize)
{
  if (!s_disk || bufsize == 0) return 0;
  uint8_t* dst = (uint8_t*)buffer;
  uint64_t pos = (uint64_t)lba * kSector + offset;
  uint32_t done = 0;
  portENTER_CRITICAL(&s_mux);
  {
    uint64_t root = (uint64_t)s_geo.root_lba * kSector;
    uint64_t root_end = root + (uint64_t)s_geo.root_sectors * kSector;
    uint64_t start = (uint64_t)lba * kSector + offset;
    if (s_geo.root_sectors && start < root_end && start + bufsize > root) {
      scrub_released_dirents();
      cache_wb((uint32_t)root, (uint32_t)s_geo.root_sectors * kSector);
    }
  }
  while (done < bufsize && pos < s_disk_bytes) {
    uint32_t n = kSector - (uint32_t)(pos % kSector);
    if (n > bufsize - done) n = bufsize - done;
    if (pos + n > s_disk_bytes) n = (uint32_t)(s_disk_bytes - pos);
    cache_inv((uint32_t)pos, n);
    memcpy(dst + done, s_disk + (uint32_t)pos, n);
    pos += n;
    done += n;
  }
  portEXIT_CRITICAL(&s_mux);
  return (int32_t)done;
}

static int32_t on_write(uint32_t lba, uint32_t offset, uint8_t* buffer, uint32_t bufsize)
{
  if (!s_disk || bufsize == 0) return 0;
  uint32_t now = millis();
  uint64_t pos = (uint64_t)lba * kSector + offset;
  uint32_t done = 0;
  portENTER_CRITICAL(&s_mux);
  while (done < bufsize && pos < s_disk_bytes) {
    uint32_t n = kSector - (uint32_t)(pos % kSector);
    if (n > bufsize - done) n = bufsize - done;
    if (pos + n > s_disk_bytes) n = (uint32_t)(s_disk_bytes - pos);
    memcpy(s_disk + (uint32_t)pos, buffer + done, n);
    pos += n;
    done += n;
  }
  if (done) {
    uint64_t start = (uint64_t)lba * kSector + offset;
    cache_wb((uint32_t)start, done);
    s_gen++;
    s_last_write_ms = now;
    s_force_settle = false;
    s_fat_cache_lba = 0xFFFFFFFFu;
    {
      uint64_t root = (uint64_t)s_geo.root_lba * kSector;
      uint64_t root_end = root + (uint64_t)s_geo.root_sectors * kSector;
      if (start < root_end && start + done > root) {
        scrub_released_dirents();
        cache_wb((uint32_t)root, (uint32_t)s_geo.root_sectors * kSector);
      }
    }
  }
  portEXIT_CRITICAL(&s_mux);
  return (int32_t)done;
}

// Caller holds s_mux. fat_get() takes that lock too, so read the FAT directly.
static uint16_t fat_get_locked(uint16_t cluster)
{
  uint32_t off = (uint32_t)cluster * 2u;
  uint32_t lba = s_geo.fat_lba + off / kSector;
  uint8_t* p;
  if (lba >= s_geo.sectors) return 0;
  cache_inv(lba * kSector, kSector);
  p = s_disk + lba * kSector + (off % kSector);
  return (uint16_t)(p[0] | (p[1] << 8));
}

static void free_chain(uint16_t cluster)
{
  int guard = 0;
  while (cluster >= 2 && cluster < 0xFFF8 && guard++ < 20000) {
    uint16_t next = fat_get_locked(cluster);
    fat_format_put(cluster, 0);
    s_fat_cache_lba = 0xFFFFFFFFu;
    if (next < 2 || next >= 0xFFF8) break;
    cluster = next;
  }
}

bool usb_disk_release_sent(const char* name, uint32_t size, uint32_t mtime, uint16_t first_cluster)
{
  uint32_t entries;
  uint32_t i;
  bool found = false;
  if (!s_disk || first_cluster < 2) return false;
  if (!parse_bpb()) return false;
  entries = (uint32_t)s_geo.root_sectors * (kSector / 32u);
  portENTER_CRITICAL(&s_mux);
  for (i = 0; i < entries; i++) {
    uint8_t* e = s_disk + (s_geo.root_lba * kSector) + i * 32u;
    uint32_t back;
    if (e[0] == 0x00) break;
    if (e[0] == 0xE5 || e[11] == 0x0F) continue;
    if (e[11] & 0x18) continue;
    if (rd16(e + 26) != first_cluster) continue;
    char shortn[16];
    dir_short_name(e, shortn, sizeof shortn);
    free_chain(first_cluster);
    e[0] = 0xE5;
    for (back = 1; back <= i; back++) {
      uint8_t* prev = e - back * 32u;
      if (prev[11] != 0x0F || prev[0] == 0x00 || prev[0] == 0xE5) break;
      prev[0] = 0xE5;
    }
    remember_released(shortn[0] ? shortn : name, size, mtime, first_cluster);
    found = true;
    break;
  }
  if (found) {
    cache_wb(s_geo.root_lba * kSector, (uint32_t)s_geo.root_sectors * kSector);
    cache_wb(s_geo.fat_lba * kSector, (uint32_t)s_geo.fats * s_geo.fat_sectors * kSector);
    s_discard_count++;
  }
  portEXIT_CRITICAL(&s_mux);
  return found;
}

static bool on_start_stop(uint8_t power_condition, bool start, bool load_eject)
{
  (void)power_condition;
  if (!start && load_eject) {
    s_log_eject = 1;
    s_force_settle = true;
  }
  return true;
}

static void on_usb_event(void* arg, esp_event_base_t base, int32_t id, void* data)
{
  (void)arg;
  (void)data;
  if (base != ARDUINO_USB_EVENTS) return;
  if (id == ARDUINO_USB_STARTED_EVENT) s_log_plug = 1;
  else if (id == ARDUINO_USB_STOPPED_EVENT) {
    s_log_plug = 2;
  }
}

void usb_disk_service_log(void)
{
  uint8_t plug = s_log_plug;
  int removed = s_log_removed;
  uint8_t eject = s_log_eject;
  s_log_plug = 0;
  s_log_removed = -1;
  s_log_eject = 0;
  if (plug == 1) Serial.println("usb: plugged");
  else if (plug == 2) Serial.println("usb: unplugged");
  if (eject) Serial.println("usb: ejected");
  if (removed >= 0) Serial.printf("usb: removed %d image file(s) on disconnect\n", removed);
}

bool usb_disk_begin(void)
{
  const uint32_t sizes[] = {
    4u * 1024u * 1024u,
    3u * 1024u * 1024u,
    2560u * 1024u
  };
  for (uint32_t sz : sizes) {
    if (!geom_fat16(sz / kSector, &s_geo)) continue;
    s_disk = (uint8_t*)heap_caps_aligned_alloc(64, sz, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_disk) continue;
    s_disk_bytes = sz;
    break;
  }
  if (!s_disk) {
    Serial.println("usb: could not allocate the drop disk");
    return false;
  }

  format_disk();
  cache_wb(0, s_disk_bytes);
  if (!parse_bpb()) {
    Serial.println("usb: formatted BPB did not parse back");
    return false;
  }
  Serial.printf("usb: EPD97  %u KB  FAT16  %u clusters\n",
                (unsigned)(s_disk_bytes / 1024), (unsigned)s_geo.clusters);

  USB.manufacturerName("EPD97");
  USB.productName("EPD Drop");
  USB.onEvent(on_usb_event);

  s_msc.vendorID("EPD97");
  s_msc.productID("Drop");
  s_msc.productRevision("1.0");
  s_msc.onStartStop(on_start_stop);
  s_msc.onRead(on_read);
  s_msc.onWrite(on_write);
  s_msc.mediaPresent(true);
  // s_msc.isWritable(true);
  if (!s_msc.begin(s_geo.sectors, (uint16_t)kSector)) {
    Serial.println("usb: MSC.begin() failed");
    return false;
  }
  if (!USB.begin()) {
    Serial.println("usb: USB.begin() failed");
    return false;
  }
  Serial.println("usb: drop drive is up");
  return true;
}

bool usb_disk_settled(uint32_t* gen)
{
  if (s_force_settle && s_gen != 0) {
    *gen = s_gen;
    return true;
  }
  uint32_t g1 = s_gen;
  if (g1 == 0) return false;
  uint32_t t = s_last_write_ms;
  if ((uint32_t)millis() - t < kIdleMs) return false;
  if (s_gen != g1) return false;
  if (s_last_write_ms != t) return false;
  *gen = g1;
  return true;
}

uint32_t usb_disk_generation(void) { return s_gen; }
uint32_t usb_disk_bytes(void) { return s_disk_bytes; }
uint32_t usb_disk_discard_count(void) { return s_discard_count; }

// -----------------------------------------------------------------------------
// Directory walk
// -----------------------------------------------------------------------------
static int kind_from_name(const char* name)
{
  const char* dot = strrchr(name, '.');
  if (!dot || !dot[1] || strlen(dot) > 8) return USB_KIND_NONE;
  char e[8];
  size_t n = strlen(dot + 1);
  if (n >= sizeof e) return USB_KIND_NONE;
  for (size_t i = 0; i < n; i++) {
    char c = dot[1 + i];
    if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
    e[i] = c;
  }
  e[n] = 0;
  if (!strcmp(e, "jpg") || !strcmp(e, "jpeg") || !strcmp(e, "jpe") || !strcmp(e, "jfif"))
    return USB_KIND_JPEG;
  if (!strcmp(e, "png")) return USB_KIND_PNG;
  if (!strcmp(e, "bmp") || !strcmp(e, "dib")) return USB_KIND_BMP;
  if (!strcmp(e, "gif") || !strcmp(e, "webp") || !strcmp(e, "tif") ||
      !strcmp(e, "tiff") || !strcmp(e, "heic") || !strcmp(e, "heif"))
    return USB_KIND_UNSUPPORTED;
  return USB_KIND_NONE;
}

static void short_name(const uint8_t* e, char* out, size_t out_n)
{
  size_t n = 0;
  uint8_t first = e[0] == 0x05 ? 0xE5 : e[0];
  if (n + 1 < out_n) out[n++] = (char)first;
  for (int i = 1; i < 8 && e[i] != ' '; i++) {
    if (n + 1 >= out_n) break;
    out[n++] = (char)e[i];
  }
  if (e[8] != ' ') {
    if (n + 1 < out_n) out[n++] = '.';
    for (int i = 8; i < 11 && e[i] != ' '; i++) {
      if (n + 1 >= out_n) break;
      out[n++] = (char)e[i];
    }
  }
  out[n] = 0;
}

static uint8_t lfn_checksum(const uint8_t* e)
{
  uint8_t sum = 0;
  for (int i = 0; i < 11; i++)
    sum = (uint8_t)(((sum & 1) ? 0x80 : 0) + (sum >> 1) + e[i]);
  return sum;
}

static const int kLfnOff[13] = { 1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30 };

struct Walk {
  UsbFile best;
  bool found;
  char lfn[260];
  uint8_t lfn_sum;
  bool lfn_ok;
  const char* want;   // case-insensitive name, or NULL for the newest image
};

static bool name_eq_ci(const char* a, const char* b)
{
  while (*a && *b) {
    char ca = *a, cb = *b;
    if (ca >= 'A' && ca <= 'Z') ca = (char)(ca - 'A' + 'a');
    if (cb >= 'A' && cb <= 'Z') cb = (char)(cb - 'A' + 'a');
    if (ca != cb) return false;
    a++;
    b++;
  }
  return *a == 0 && *b == 0;
}

static void consider_file(Walk* w, const char* name, const uint8_t* e)
{
  int kind = kind_from_name(name);
  if (w->want) {
    if (!name_eq_ci(name, w->want)) return;
  } else if (kind == USB_KIND_NONE) {
    return;
  }
  uint32_t size = rd32(e + 28);
  uint16_t cluster = rd16(e + 26);
  if (size == 0 || cluster < 2) return;
  if (size > s_disk_bytes) return;
  uint32_t mtime = ((uint32_t)rd16(e + 24) << 16) | rd16(e + 22);
  if (w->found && mtime < w->best.mtime) return;

  memset(&w->best, 0, sizeof w->best);
  strncpy(w->best.name, name, sizeof w->best.name - 1);
  w->best.size = size;
  w->best.mtime = mtime;
  w->best.first_cluster = cluster;
  w->best.kind = kind;
  w->found = true;
}

static void scan_sectors(uint32_t lba, uint32_t sectors, int depth, Walk* w);

static void scan_chain(uint16_t start, int depth, Walk* w)
{
  uint16_t c = start;
  for (int guard = 0; guard < 64 && c >= 2 && c < 0xFFF8; guard++) {
    if (c >= s_geo.clusters + 2) return;
    uint32_t lba = s_geo.data_lba + (uint32_t)(c - 2) * s_geo.spc;
    scan_sectors(lba, s_geo.spc, depth, w);
    uint16_t next = fat_get(c);
    if (next < 2 || next >= 0xFFF8) return;
    c = next;
  }
}

static void scan_sectors(uint32_t lba, uint32_t sectors, int depth, Walk* w)
{
  for (uint32_t s = 0; s < sectors; s++) {
    uint8_t sec[512];
    disk_copy_out(lba + s, 0, sec, 512);
    for (int off = 0; off < 512; off += 32) {
      uint8_t* e = sec + off;
      if (e[0] == 0x00) return;
      if (e[0] == 0xE5) {
        w->lfn_ok = false;
        continue;
      }
      if (e[11] == 0x0F) {
        int ord = e[0] & 0x1F;
        if (e[0] & 0x40) {
          memset(w->lfn, 0, sizeof w->lfn);
          w->lfn_sum = e[13];
          w->lfn_ok = true;
        }
        if (e[13] != w->lfn_sum || ord < 1 || ord > 20) {
          w->lfn_ok = false;
          continue;
        }
        int base = (ord - 1) * 13;
        if (base + 13 >= (int)sizeof w->lfn) {
          w->lfn_ok = false;
          continue;
        }
        for (int i = 0; i < 13; i++) {
          uint8_t lo = e[kLfnOff[i]];
          uint8_t hi = e[kLfnOff[i] + 1];
          if (lo == 0 && hi == 0) {
            w->lfn[base + i] = 0;
            break;
          }
          if (lo == 0xFF && hi == 0xFF) {
            w->lfn[base + i] = 0;
            break;
          }
          w->lfn[base + i] = (hi == 0 && lo >= 32 && lo < 127) ? (char)lo : '?';
        }
        continue;
      }
      if (e[11] & 0x08) {
        w->lfn_ok = false;
        continue;
      }

      char name[64];
      bool used_lfn = w->lfn_ok && w->lfn[0] && lfn_checksum(e) == w->lfn_sum;
      if (used_lfn) strncpy(name, w->lfn, sizeof name - 1), name[sizeof name - 1] = 0;
      else short_name(e, name, sizeof name);
      w->lfn_ok = false;

      if (e[11] & 0x10) {
        if (name[0] == '.') continue;
        if (depth >= 1) continue;
        if (e[11] & (0x02 | 0x04)) continue;   // hidden or system
        scan_chain(rd16(e + 26), depth + 1, w);
        continue;
      }
      if (e[11] & (0x02 | 0x04)) continue;
      consider_file(w, name, e);
    }
  }
}

bool usb_disk_newest(UsbFile* out)
{
  if (!s_disk) return false;
  if (!parse_bpb()) return false;
  Walk w;
  memset(&w, 0, sizeof w);
  scan_sectors(s_geo.root_lba, s_geo.root_sectors, 0, &w);
  if (!w.found) return false;
  *out = w.best;
  return true;
}

bool usb_disk_open_named(const char* name, UsbFile* out)
{
  if (!s_disk || !name || !out) return false;
  if (!parse_bpb()) return false;
  Walk w;
  memset(&w, 0, sizeof w);
  w.want = name;
  scan_sectors(s_geo.root_lba, s_geo.root_sectors, 0, &w);
  if (!w.found) return false;
  *out = w.best;
  return usb_disk_open(out);
}

bool usb_disk_open(UsbFile* f)
{
  if (!f || f->map) return false;
  uint32_t cb = (uint32_t)s_geo.spc * kSector;
  if (cb == 0 || f->size == 0 || f->first_cluster < 2) return false;
  uint32_t need = (f->size + cb - 1) / cb;
  if (need == 0 || need > s_geo.clusters) return false;

  uint16_t* map = (uint16_t*)heap_caps_malloc(need * sizeof(uint16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (!map) map = (uint16_t*)heap_caps_malloc(need * sizeof(uint16_t), MALLOC_CAP_SPIRAM);
  if (!map) return false;

  s_fat_cache_lba = 0xFFFFFFFFu;
  uint16_t c = f->first_cluster;
  for (uint32_t i = 0; i < need; i++) {
    if (c < 2 || c >= s_geo.clusters + 2) {
      heap_caps_free(map);
      return false;
    }
    map[i] = c;
    uint16_t next = fat_get(c);
    if (i + 1 == need) break;
    if (next < 2 || next >= 0xFFF8) {
      heap_caps_free(map);
      return false;
    }
    c = next;
  }
  f->map = map;
  f->map_n = need;
  f->cluster_bytes = cb;
  f->data_lba = s_geo.data_lba;
  return true;
}

void usb_disk_close(UsbFile* f)
{
  if (!f || !f->map) return;
  heap_caps_free(f->map);
  f->map = nullptr;
  f->map_n = 0;
}

int32_t usb_file_read_at(UsbFile* f, uint32_t offset, uint8_t* dst, uint32_t len)
{
  if (!f || !f->map || !dst || len == 0 || offset >= f->size) return 0;
  if (offset + len > f->size) len = f->size - offset;
  uint32_t done = 0;
  while (done < len) {
    uint32_t pos = offset + done;
    uint32_t idx = pos / f->cluster_bytes;
    if (idx >= f->map_n) break;
    uint32_t within = pos % f->cluster_bytes;
    uint32_t lba = f->data_lba + (uint32_t)(f->map[idx] - 2) * (f->cluster_bytes / kSector) + within / kSector;
    uint32_t off = within % kSector;
    uint32_t n = kSector - off;
    if (n > len - done) n = len - done;
    disk_copy_out(lba, off, dst + done, n);
    done += n;
  }
  return (int32_t)done;
}
