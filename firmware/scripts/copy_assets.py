#!/bin/env python3

"""Copy the graphical assets from the assets/ directory in the root of this repo to the badge data directory.
    Use ImageMagick to convert the images to a reduced colorspace so they fit and load better on the badge.
"""

import os
import os.path
import pathlib
import subprocess

source_assets_dir = os.path.abspath(os.path.dirname(__file__) + "/../../assets/graphics/")
dest_dir = os.path.abspath(os.path.dirname(__file__) + "/../badge/data/graphics/")
palette_path = os.path.abspath(os.path.dirname(__file__) + "/palette.gif")
os.makedirs(dest_dir, exist_ok=True)
top_path = pathlib.Path(source_assets_dir)
for file_path in top_path.iterdir():
    if not file_path.is_file():
        continue
    if file_path.suffix != ".png":
        continue
    basename = os.path.basename(file_path)
    print(basename)
    subprocess.run(["magick", file_path, "-remap", palette_path, dest_dir + "/" + basename], check=False)
