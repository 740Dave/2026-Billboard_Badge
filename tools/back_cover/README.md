# Billboard Badge back cover

This is a snap-on back shell for the Supercon 2026 Billboard Badge. It mates with the printed front bezel (`hardware/billboard_mockup-Frame.step` in the Hackaday repo) and encloses the back of the e-paper panel and the PCB.

| File | Use |
|---|---|
| `out/billboard_back_cover.3mf` / `.stl` | Print this |
| `out/billboard_back_cover.step` | Open in Fusion, FreeCAD or Onshape to modify |
| `billboard_back_cover.py` | Parametric source (`pip install build123d`, then `python3 billboard_back_cover.py --check`) |
| `out/preview_*.png`, `out/section_*.png` | Renders and fit cross-sections |

## Where the dimensions come from

| Feature | Source | Value |
|---|---|---|
| Bezel outline | Frame STEP | 227.6 × 167.12 × 5.0 mm, R3 corners |
| Display pocket | Frame STEP | 3 mm deep. Back borders: 7.8 mm bottom, 8.2 mm right, 2.4 mm left |
| PCB outline | KiCad Edge.Cuts | 225.6 × 50 mm strip, 1.6 mm thick, sits behind the top of the display and sticks out 22.2 mm above the bezel |
| Lanyard holes | KiCad | Ø8 at (±106.8, 99.74) |
| Stack-up | FreeCAD mockup | Bezel back face = PCB front face (Z = 0) |
| Parts on PCB back | KiCad footprints + vendor 3D models | Tact switches 5.0 mm, power slide 4.6 mm, battery connector 5.6 mm, display flex loops reach 6.05 mm behind the bezel |

## What the cover has

- **Snap lips**, 1 mm, in 7 short segments on the left, right and bottom walls. They hook over the bezel's front face. The top edge has no wall because the PCB continues there.
- **Seat ribs** that press the bezel's back border forward against the lips. Nothing presses on the display glass.
- **Lanyard bosses** that pass up through the PCB's 8 mm holes. They locate the PCB and keep Ø4.8 lanyard holes open through the cover.
- **12 PCB support posts**, placed by script to stay at least 0.8 mm clear of every part on the PCB back.
- **4 flex buttons** (10 × 16 mm flaps, 1.2 mm thick, with a nub) over the tact switches: **MENU−, MENU+, A, B**. The A and B flaps run vertically so they miss the power switch.
- **Openings:**
  - USB-C plug window (18 × 45 mm). The port faces down behind the display, so the plug goes in through the window.
  - Power slide switch.
  - Battery connector (PH2.0, vertical).
  - Paper-clip holes with guide tubes for **BOOT** (SW2 → IO0) and **RST** (SW1 → EN).
  - Light holes for the charge and status LEDs.
- **Engraved labels**, readable from the back. From the back, the menu buttons are mirrored: **MENU− and A are on the right**, MENU+ and B on the left.
- **8.5 mm internal depth** behind the display, which fits a LiPo pouch up to about 7 mm thick (for example a 603450). Stick it on with foam tape.

Overall size is 232 × 194 × 17 mm, and the result is one valid solid. The fit check (`--check`) intersects the cover with the bezel STEP, the assembled PCB STEP, the display, and every back-side part including the flex loops and a typical USB-C plug: all 18 checks show zero overlap.

## Printing

- **Bed:** needs at least 235 × 197 mm. Bambu X1/P1/A1, Prusa MK4/Core One and Voron 250+ all work. An Ender-3 (220 mm) does not.
- **Orientation:** back face on the bed, exactly as exported. No supports are needed. The lips overhang only 1 mm.
- **Settings:** PLA or PETG, 0.2 mm layers, 3 walls, 15% infill. Roughly 70–90 g.
- **Buttons:** PETG gives the flaps a nicer spring, but PLA works.

## Assembly

1. Lay the cover on the table, inside facing up.
2. Hold the bezel, display and PCB together. Lower them in face-up, with the PCB's lanyard holes over the two bosses.
3. Press down along the left, right and bottom edges until the lips click over the bezel's front face.
4. To remove, flex a side wall outward at a lip segment and lift that edge.

## Check before printing

- **Your bezel's outer size.** Measure it with calipers: the model expects **227.6 × 167.1 mm, 5.0 mm thick**.
  - If it measures bigger, raise `CLEAR_BEZEL`, which defaults to 0.25 mm per side.
  - If it's thicker or thinner than 5.0 mm, change `BEZEL_T`.
  - Then re-run the script.
- **Battery thickness.** If yours is thicker than about 7 mm, raise `CAVITY`. With no battery, you can drop it to 7.5 for a thinner case.
- **USB cable.** Plug the cable in through the window. A slim plug, or a 90° plug angled toward the back, is easiest.

## Not yet verified

- This hasn't been printed or test-fitted on a real badge. Every dimension comes from the repo's CAD and KiCad files, not from measuring a physical board.
- The tact-switch, slide-switch, USB and battery-connector heights come from their vendor 3D models, not from the as-built badge.
- The supplied bezel or your print may differ slightly from the STEP file, so check your measurements against the expected values first.
