#include "dither_cfg.h"

#include <string.h>

const char* dither_cfg_stock(void)
{
  return
      "# dithering  1 = error diffusion, 0 = hard threshold\n"
      "# A reboot puts this file back to dithering 0.\n"
      "dithering 0\n";
}

static int same_key(const char* s, size_t n, const char* lit)
{
  size_t i;
  for (i = 0; i < n; i++) {
    char a = s[i];
    if (lit[i] == 0) return 0;
    if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
    if (a != lit[i]) return 0;
  }
  return lit[n] == 0;
}

static int value_bit(const char* s, size_t n, int* bit)
{
  if (n == 1 && (s[0] == '0' || s[0] == '1')) {
    *bit = s[0] == '1';
    return 1;
  }
  if (same_key(s, n, "on") || same_key(s, n, "true") || same_key(s, n, "yes")) {
    *bit = 1;
    return 1;
  }
  if (same_key(s, n, "off") || same_key(s, n, "false") || same_key(s, n, "no")) {
    *bit = 0;
    return 1;
  }
  return 0;
}

int dither_cfg_parse(const char* text, size_t len)
{
  int dither = 0;
  size_t i = 0;
  if (!text) return 0;
  if (len >= 3 && (unsigned char)text[0] == 0xEF &&
      (unsigned char)text[1] == 0xBB && (unsigned char)text[2] == 0xBF) {
    i = 3;
  }
  while (i < len) {
    size_t line = i;
    while (i < len && text[i] != '\n' && text[i] != '\r') i++;
    size_t end = i;
    if (i < len && text[i] == '\r') i++;
    if (i < len && text[i] == '\n') i++;
    while (line < end && (text[line] == ' ' || text[line] == '\t')) line++;
    if (line >= end || text[line] == '#' || text[line] == ';') continue;
    size_t key = line;
    while (line < end && text[line] != ' ' && text[line] != '\t' && text[line] != '=') line++;
    size_t key_n = line - key;
    while (line < end && (text[line] == ' ' || text[line] == '\t' || text[line] == '=')) line++;
    size_t val = line;
    while (line < end && text[line] != ' ' && text[line] != '\t') line++;
    size_t val_n = line - val;
    int bit = 1;
    if (!value_bit(text + val, val_n, &bit)) continue;
    if (same_key(text + key, key_n, "dithering") || same_key(text + key, key_n, "dither"))
      dither = bit;
  }
  return dither ? 1 : 0;
}
