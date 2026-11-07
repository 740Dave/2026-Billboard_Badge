#!/usr/bin/env python3
"""Convert any images into badge-ready artwork and (optionally) copy them to the badge.

The Billboard Badge panel is 960x672 and can only show white, black and red.
Its PNG decoder needs a *palette* PNG that is not interlaced. This script:
  1. resizes each image to fit (letterbox, default) or fill (crop) 960x672,
  2. reduces it to the 3 panel colours, with Floyd-Steinberg dithering by default,
  3. saves a small 2-bit palette PNG into ./out/,
  4. with --push, copies the results to /data/artwork/ on the badge via mpremote.

Install once:   python3 -m pip install pillow mpremote
Examples:
    python3 prepare_artwork.py ~/Pictures/cat.jpg logo.png
    python3 prepare_artwork.py ~/Pictures/art/*.jpg --mode fill --push
    python3 prepare_artwork.py lineart.png --no-dither --push --port /dev/cu.usbmodem1101
"""

import argparse
import pathlib
import subprocess
import sys

from PIL import Image, ImageOps

W, H = 960, 672
PALETTE = [(255, 255, 255), (0, 0, 0), (255, 0, 0)]  # white, black, red


def palette_image() -> Image.Image:
    pal = Image.new("P", (1, 1))
    flat = [c for rgb in PALETTE for c in rgb]
    pal.putpalette(flat + flat[:3] * (256 - len(PALETTE)))  # pad with white
    return pal


def convert(src: pathlib.Path, dst: pathlib.Path, mode: str, dither: bool, no_red: bool, rotate: int) -> None:
    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    if rotate:
        img = img.rotate(-rotate, expand=True)
    # Flatten transparency onto white
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("RGB")

    if mode == "fill":
        img = ImageOps.fit(img, (W, H), Image.LANCZOS)
    else:
        img = ImageOps.contain(img, (W, H), Image.LANCZOS)
        canvas = Image.new("RGB", (W, H), (255, 255, 255))
        canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
        img = canvas

    pal = palette_image()
    if no_red:
        pal.putpalette([255, 255, 255, 0, 0, 0] * 128)
    q = img.quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE)
    # Keep only the 3 real palette entries so the PNG is written at 2 bits/pixel.
    q.putpalette([c for rgb in PALETTE for c in rgb])
    q.save(dst, format="PNG", optimize=True, bits=2)  # PIL never interlaces by default


def mpremote(port, *args) -> None:
    cmd = ["mpremote"] + (["connect", port] if port else []) + list(args)
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("images", nargs="+", type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("out"), help="output folder (default ./out)")
    ap.add_argument("--mode", choices=["fit", "fill"], default="fit", help="fit = letterbox on white, fill = crop to fill")
    ap.add_argument("--no-dither", action="store_true", help="hard threshold; best for logos and line art")
    ap.add_argument("--no-red", action="store_true", help="black and white only")
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270], help="rotate clockwise first")
    ap.add_argument("--push", action="store_true", help="copy results to :/data/artwork/ on the badge")
    ap.add_argument("--port", help="serial port, e.g. /dev/cu.usbmodem1101 (default: auto)")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    made = []
    for src in args.images:
        dst = args.out / (src.stem.replace(" ", "_") + ".png")
        convert(src, dst, args.mode, not args.no_dither, args.no_red, args.rotate)
        print(f"{src} -> {dst} ({dst.stat().st_size // 1024} KiB)")
        made.append(dst)

    if args.push:
        try:
            mpremote(args.port, "mkdir", ":/data/artwork")
        except subprocess.CalledProcessError:
            pass  # already exists
        for f in made:
            mpremote(args.port, "cp", str(f), f":/data/artwork/{f.name}")
        print("Copied. Re-open the Artwork app (Menu +/-, then A) to see new images.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
