"""Convert any image into Billboard Badge artwork (port of artwork_app/prepare_artwork.py).

Output format (what firmware/badge/lib/large_png.py can decode):
    960x672, palette PNG (colour type 3), 2 bits/pixel, NOT interlaced,
    palette = [white, black, red].
"""

from __future__ import annotations

import io
import re
import struct
from dataclasses import dataclass, asdict
from pathlib import Path

from PIL import Image, ImageOps

W, H = 960, 672
PALETTE = [(255, 255, 255), (0, 0, 0), (255, 0, 0)]  # white, black, red
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}


@dataclass
class ConvertOptions:
    mode: str = "fit"      # "fit" (letterbox on white) or "fill" (crop)
    dither: bool = True    # Floyd-Steinberg
    red: bool = True       # False = black and white only
    rotate: int = 0        # clockwise degrees: 0/90/180/270

    def copy(self) -> "ConvertOptions":
        return ConvertOptions(**asdict(self))


def _palette_image(red: bool) -> Image.Image:
    pal = Image.new("P", (1, 1))
    if red:
        flat = [c for rgb in PALETTE for c in rgb]
        pal.putpalette(flat + flat[:3] * (256 - len(PALETTE)))  # pad with white
    else:
        pal.putpalette([255, 255, 255, 0, 0, 0] * 128)
    return pal


def load_source(src) -> Image.Image:
    """Open an image (path or bytes), apply EXIF orientation, flatten alpha onto white, RGB."""
    img = Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    return img.convert("RGB")


def convert(src, opts: ConvertOptions | None = None) -> Image.Image:
    """Return the converted 960x672 'P' image (palette white/black/red). src: path, bytes or PIL image."""
    opts = opts or ConvertOptions()
    img = src.convert("RGB") if isinstance(src, Image.Image) else load_source(src)
    if opts.rotate:
        img = img.rotate(-opts.rotate, expand=True)
    if opts.mode == "fill":
        img = ImageOps.fit(img, (W, H), Image.LANCZOS)
    else:
        img = ImageOps.contain(img, (W, H), Image.LANCZOS)
        canvas = Image.new("RGB", (W, H), (255, 255, 255))
        canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
        img = canvas
    q = img.quantize(palette=_palette_image(opts.red),
                     dither=Image.Dither.FLOYDSTEINBERG if opts.dither else Image.Dither.NONE)
    # Keep only the 3 real palette entries so the PNG is written at 2 bits/pixel.
    q.putpalette([c for rgb in PALETTE for c in rgb])
    return q


def encode_png(img: Image.Image) -> bytes:
    """Encode a converted image exactly like prepare_artwork.py (2-bit palette, not interlaced)."""
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True, bits=2)  # PIL never interlaces
    return buf.getvalue()


def convert_to_png(src, opts: ConvertOptions | None = None) -> bytes:
    return encode_png(convert(src, opts))


def png_header(data: bytes) -> dict:
    """Parse IHDR (+ first PLTE entries) of a PNG. Raises ValueError if it isn't a PNG."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    w, h, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", data[16:29])
    out = {"width": w, "height": h, "bit_depth": depth, "color_type": ctype, "interlace": interlace,
           "palette": []}
    pos = 8
    while pos + 8 <= len(data):
        ln, typ = struct.unpack(">I4s", data[pos:pos + 8])
        if typ == b"PLTE":
            p = data[pos + 8:pos + 8 + ln]
            out["palette"] = [tuple(p[i:i + 3]) for i in range(0, len(p), 3)]
            break
        if typ == b"IDAT":
            break
        pos += 12 + ln
    return out


def badge_compatible(data: bytes) -> tuple[bool, str]:
    """Will the badge's large_png decoder draw this file? (palette or grayscale, not interlaced)."""
    try:
        h = png_header(data)
    except (ValueError, struct.error):
        return False, "not a PNG"
    if h["interlace"]:
        return False, "interlaced PNG"
    if h["color_type"] not in (0, 3):
        return False, f"colour type {h['color_type']} (need palette or grayscale)"
    if h["width"] > W or h["height"] > H:
        return False, f"{h['width']}x{h['height']} is larger than {W}x{H}"
    return True, "ok"


def preview_rgb(img: Image.Image) -> Image.Image:
    """How the panel will show a converted image (applies the badge's red/black/white rule)."""
    return img.convert("RGB")  # palette entries are pure white/black/red, so this is exact


def safe_name(stem: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", stem.strip()).strip("._-")
    return (s or "image")[:40]


def list_images(folder: str | Path) -> list[Path]:
    folder = Path(folder)
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                  and not p.name.startswith("."))


__all__ = ["W", "H", "PALETTE", "ConvertOptions", "convert", "encode_png", "convert_to_png", "png_header",
           "badge_compatible", "preview_rgb", "safe_name", "list_images", "load_source"]
