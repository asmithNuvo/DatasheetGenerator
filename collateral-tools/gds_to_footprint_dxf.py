#!/usr/bin/env python3
"""
gds_to_footprint_dxf.py

Generates sized DXF footprints from GDS files using KLayout's Python API.

Workflow per GDS file:
  1. Read GDS, extract polygons from user-specified layer (all datatypes merged)
  2. Mirror horizontally (about Y axis)
  3. Size (bloat) edges by three offsets to produce PCB layers:
       Layer 1/0 – Solder Paste :  +25.4  um  (1 mil per edge)
       Layer 2/0 – Top Copper   :  +50.8  um  (2 mil per edge)
       Layer 3/0 – Top Solder   :  +101.6 um  (4 mil per edge)
  4. Write a single DXF per input file with 1 um base unit.

Requirements:
    pip install klayout ezdxf

Note on sizing: Region.sized(d) grows every polygon edge outward by d
database units (per-edge offset), matching KLayout's Edit > Selection >
Size Shapes behavior.
"""

import klayout.db as pya
import ezdxf
import os
import sys
import glob


# ---------------------------------------------------------------------------
# Sizing definitions: (offset_um, layer_name, ACI_color)
#   ACI (AutoCAD Color Index): 1=red, 6=magenta/purple, 8=gray
#   Adjust ACI values here if your CAD tool expects different shades.
# ---------------------------------------------------------------------------
SIZING_SPECS = [
    (25.4,   "SolderPaste", 8),     # gray
    (50.8,   "TopCopper",   1),     # red
    (101.6,  "TopSolder",   6),     # purple (magenta)
]


def collect_region(layout, cell, layer_number):
    """Merge shapes from ALL datatypes of *layer_number* into one Region."""
    region = pya.Region()
    for li in layout.layer_indices():
        info = layout.get_info(li)
        if info.layer == layer_number:
            region += pya.Region(cell.begin_shapes_rec(li))
    return region


def process_gds(gds_path, layer_number, output_dir):
    """Read one GDS → write one DXF with three sized, colored layers."""
    stem = os.path.splitext(os.path.basename(gds_path))[0]

    # ---- read source -----------------------------------------------------
    src = pya.Layout()
    src.read(gds_path)
    dbu = src.dbu  # um per database unit (typically 0.001 = 1 nm)

    # Horizontal flip: mirror about Y-axis  →  (x, y) ↦ (−x, y)
    # ICplxTrans(mag, angle_deg, mirror_about_x_first, displacement)
    #   mirror about X:  (x,y) → (x,−y)
    #   then rotate 180: (x,−y) → (−x,y)   ← net = mirror about Y ✓
    flip = pya.ICplxTrans(1.0, 180.0, True, pya.Vector(0, 0))

    # ---- prepare DXF via ezdxf -------------------------------------------
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 13        # 13 = micrometers
    msp = doc.modelspace()

    # Register layers with colors
    for _, lname, aci in SIZING_SPECS:
        doc.layers.add(lname, color=aci)

    found_any = False
    for ci in src.each_top_cell():
        cell = src.cell(ci)
        region = collect_region(src, cell, layer_number)
        if region.is_empty():
            continue

        found_any = True
        flipped = region.transformed(flip)

        for size_um, lname, _ in SIZING_SPECS:
            d = int(round(size_um / dbu))       # offset in database units
            sized = flipped.sized(d)

            # Write each polygon as a closed LWPOLYLINE
            for poly in sized.each():
                # Outer hull
                pts = [(p.x * dbu, p.y * dbu) for p in poly.each_point_hull()]
                msp.add_lwpolyline(pts, close=True,
                                   dxfattribs={"layer": lname})
                # Holes (if any)
                for hi in range(poly.holes()):
                    hpts = [(p.x * dbu, p.y * dbu)
                            for p in poly.each_point_hole(hi)]
                    msp.add_lwpolyline(hpts, close=True,
                                       dxfattribs={"layer": lname})

        print(f"  [OK]   {cell.name}  ({region.count()} polygon(s))")

    if not found_any:
        print(f"  [SKIP] No shapes on layer {layer_number}")
        return False

    # ---- save DXF --------------------------------------------------------
    dxf_path = os.path.join(output_dir, f"{stem}_footprint.dxf")
    doc.saveas(dxf_path)
    print(f"  [SAVED] {dxf_path}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("  GDS → Sized DXF Footprint Generator")
    print("=" * 60)

    # 1) Input directory
    gds_dir = input("\nDirectory of GDS files: ").strip()
    if not os.path.isdir(gds_dir):
        sys.exit(f"Error: '{gds_dir}' is not a valid directory.")

    # 2) Layer number
    try:
        layer_number = int(input("GDS layer number for footprint source: ").strip())
    except ValueError:
        sys.exit("Error: layer number must be an integer.")

    # 3) Output directory (default = input dir)
    out_dir = input("Output directory [Enter = same as input]: ").strip() or gds_dir
    os.makedirs(out_dir, exist_ok=True)

    # ---- discover files --------------------------------------------------
    patterns = ("*.gds", "*.GDS", "*.gds2", "*.GDS2", "*.oas", "*.OAS")
    files = sorted(
        {os.path.realpath(p) for pat in patterns for p in glob.glob(os.path.join(gds_dir, pat))}
    )
    if not files:
        sys.exit(f"No GDS/OAS files found in '{gds_dir}'.")

    print(f"\n{len(files)} file(s) found — extracting layer {layer_number}\n")

    ok = 0
    for f in files:
        print(f"Processing: {os.path.basename(f)}")
        if process_gds(f, layer_number, out_dir):
            ok += 1
        print()

    print(f"Done. {ok}/{len(files)} file(s) produced DXF output.")


if __name__ == "__main__":
    main()