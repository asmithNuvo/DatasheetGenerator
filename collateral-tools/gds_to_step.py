#!/usr/bin/env python3
"""
gds_to_step.py

Converts GDS layout files into 3D STEP models for SolidWorks (or any
STEP-capable CAD tool) by extruding each layer's polygons according to
a layer stack-up definition.

Accepts stack-up as either:
  - .tech file  (as output by LayerStackupCalculation.xlsm; units in nm)
  - .json file  (see example_stackup.json; units in um)

Pipeline:
  KLayout (read GDS polygons)  →  CadQuery / OCCT (extrude per layer)  →  STEP

Each layer in the stack-up becomes a named body in the STEP assembly,
with its specified z-offset, thickness, and color.

Requirements:
    pip install klayout
    conda install -c cadquery cadquery
    (or: pip install cadquery  — needs a working OCP/OpenCascade build)
"""

import klayout.db as pya
import cadquery as cq
import json
import os
import sys
import glob

UM_TO_MM = 1e-3  # CadQuery works in mm; GDS is in um


# ---------------------------------------------------------------------------
# Stack-up config — supports .json and .tech formats
# ---------------------------------------------------------------------------

# Color-name-to-RGB map for .tech files.  Extend as needed.
TECH_COLORS = {
    "red":     [0.85, 0.20, 0.20],
    "green":   [0.20, 0.70, 0.20],
    "blue":    [0.20, 0.40, 0.85],
    "yellow":  [0.85, 0.85, 0.20],
    "cyan":    [0.20, 0.75, 0.75],
    "magenta": [0.75, 0.20, 0.75],
    "orange":  [0.90, 0.55, 0.10],
    "gray":    [0.60, 0.60, 0.60],
    "grey":    [0.60, 0.60, 0.60],
    "white":   [0.90, 0.90, 0.90],
}

NM_TO_UM = 1e-3  # tech file units (nm) → internal units (um)


def load_tech_file(tech_path):
    """Parse a .tech stack-up file (as produced by LayerStackupCalculation.xlsm).

    Expected format (whitespace-separated, '//' comment lines):
        GDS_LAYER  NAME  COLOR  Z1_nm  THICKNESS_nm

    Returns the same list-of-dicts structure as load_json_stackup().
    """
    layers = []
    with open(tech_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                gds_layer = int(parts[0])
                name = parts[1]
                color = parts[2].lower()
                z_nm = float(parts[3])
                thick_nm = float(parts[4])
            except (ValueError, IndexError):
                continue

            layers.append({
                "gds_layer":    gds_layer,
                "name":         name,
                "z_um":         z_nm * NM_TO_UM,
                "thickness_um": thick_nm * NM_TO_UM,
                "color_rgb":    TECH_COLORS.get(color, [0.6, 0.6, 0.6]),
            })
    return layers


def load_json_stackup(json_path):
    """Load layer stack-up from a JSON config file.

    Each entry needs at minimum:
        gds_layer, name, z_um, thickness_um
    Optional:
        gds_datatype  (default: match any)
        color_rgb     (default: [0.6, 0.6, 0.6])
    """
    with open(json_path) as f:
        data = json.load(f)
    return data["layers"]


def load_stackup(path):
    """Auto-detect format by extension and load the stack-up."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        layers = load_json_stackup(path)
    elif ext == ".tech":
        layers = load_tech_file(path)
    else:
        sys.exit(f"Error: unrecognized stack-up format '{ext}' (expected .json or .tech)")
    return layers


# ---------------------------------------------------------------------------
# GDS helpers (KLayout)
# ---------------------------------------------------------------------------

def collect_region(layout, cell, gds_layer, gds_datatype=None):
    """Merge all shapes on a GDS layer (optionally filtered by datatype)."""
    region = pya.Region()
    for li in layout.layer_indices():
        info = layout.get_info(li)
        if info.layer == gds_layer:
            if gds_datatype is not None and info.datatype != gds_datatype:
                continue
            region += pya.Region(cell.begin_shapes_rec(li))
    region.merge()
    return region


# ---------------------------------------------------------------------------
# 3-D extrusion (CadQuery)
# ---------------------------------------------------------------------------

def _extrude_simple(pts_mm, z_mm, thick_mm):
    """Extrude a single closed polygon (no holes) via the Workplane API."""
    wp = cq.Workplane("XY").workplane(offset=z_mm)
    wp = wp.moveTo(pts_mm[0][0], pts_mm[0][1])
    for x, y in pts_mm[1:]:
        wp = wp.lineTo(x, y)
    wp = wp.close()
    return wp.extrude(thick_mm)


def extrude_polygon(hull_mm, holes_mm, z_mm, thick_mm):
    """Extrude a polygon, subtracting any holes via boolean cut.

    Parameters
    ----------
    hull_mm   : list of (x, y) in mm — outer boundary
    holes_mm  : list of lists of (x, y) in mm — inner boundaries
    z_mm      : base z-height in mm
    thick_mm  : extrusion thickness in mm
    """
    solid = _extrude_simple(hull_mm, z_mm, thick_mm)

    for hole_pts in holes_mm:
        # Extend cut slightly beyond the faces to guarantee a clean boolean
        cut_body = _extrude_simple(hole_pts, z_mm - 1e-4, thick_mm + 2e-4)
        solid = solid.cut(cut_body)

    return solid


# ---------------------------------------------------------------------------
# Main conversion
# ---------------------------------------------------------------------------

def process_gds(gds_path, stackup, output_dir):
    """Read one GDS → write one STEP with extruded layers."""
    stem = os.path.splitext(os.path.basename(gds_path))[0]

    src = pya.Layout()
    src.read(gds_path)
    dbu = src.dbu  # um per database unit

    assy = cq.Assembly(name=stem)
    has_geometry = False

    for ci in src.each_top_cell():
        cell = src.cell(ci)

        for ldef in stackup:
            gds_layer = ldef["gds_layer"]
            gds_dt = ldef.get("gds_datatype")       # None → any datatype
            name = ldef["name"]
            z_mm = ldef["z_um"] * UM_TO_MM
            thick_mm = ldef["thickness_um"] * UM_TO_MM
            rgb = ldef.get("color_rgb", [0.6, 0.6, 0.6])

            region = collect_region(src, cell, gds_layer, gds_dt)
            if region.is_empty():
                print(f"  [SKIP] {name}: no shapes on layer {gds_layer}")
                continue

            layer_bodies = []
            skipped = 0

            for poly in region.each():
                # Convert hull and holes from dbu → mm
                hull = [(p.x * dbu * UM_TO_MM, p.y * dbu * UM_TO_MM)
                        for p in poly.each_point_hull()]
                holes = []
                for hi in range(poly.holes()):
                    h = [(p.x * dbu * UM_TO_MM, p.y * dbu * UM_TO_MM)
                         for p in poly.each_point_hole(hi)]
                    holes.append(h)

                try:
                    body = extrude_polygon(hull, holes, z_mm, thick_mm)
                    layer_bodies.append(body)
                except Exception as exc:
                    skipped += 1
                    if skipped <= 3:
                        print(f"  [WARN] {name}: polygon skipped ({exc})")

            if not layer_bodies:
                continue

            # Add each extruded polygon to the assembly under the layer name.
            # SolidWorks will show these as separate bodies inside the part.
            color = cq.Color(*rgb)
            if len(layer_bodies) == 1:
                assy.add(layer_bodies[0], name=name, color=color)
            else:
                # Group under a sub-assembly so the STEP tree stays tidy
                sub = cq.Assembly(name=name)
                for i, b in enumerate(layer_bodies):
                    sub.add(b, name=f"{name}_{i}", color=color)
                assy.add(sub, name=name)

            has_geometry = True
            msg = f"  [OK]   {name}: {len(layer_bodies)} solid(s)"
            if skipped:
                msg += f"  ({skipped} skipped)"
            print(msg)

    if not has_geometry:
        print("  No geometry produced.")
        return False

    step_path = os.path.join(output_dir, f"{stem}.step")
    assy.save(step_path, "STEP")
    print(f"  [SAVED] {step_path}")
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  GDS → 3-D STEP Converter")
    print("=" * 60)

    # 1) GDS input — single file or directory
    gds_input = input("\nGDS file or directory: ").strip()
    if os.path.isfile(gds_input):
        gds_files = [gds_input]
    elif os.path.isdir(gds_input):
        patterns = ("*.gds", "*.GDS", "*.gds2", "*.GDS2", "*.oas", "*.OAS")
        gds_files = sorted(
            {os.path.realpath(p)
             for pat in patterns
             for p in glob.glob(os.path.join(gds_input, pat))}
        )
    else:
        sys.exit(f"Error: '{gds_input}' not found.")

    if not gds_files:
        sys.exit("No GDS/OAS files found.")

    # 2) Stack-up definition (.tech or .json)
    stackup_path = input("Layer stack-up file (.tech or .json): ").strip()
    if not os.path.isfile(stackup_path):
        sys.exit(f"Error: '{stackup_path}' not found.")
    stackup = load_stackup(stackup_path)
    print(f"  Stack-up: {len(stackup)} layer(s) defined")

    # 3) Output directory
    default_out = os.path.dirname(gds_files[0]) or "."
    out_dir = input(f"Output directory [Enter = {default_out}]: ").strip() or default_out
    os.makedirs(out_dir, exist_ok=True)

    # ---- process ---------------------------------------------------------
    print(f"\nProcessing {len(gds_files)} file(s)...\n")

    ok = 0
    for f in gds_files:
        print(f"--- {os.path.basename(f)} ---")
        try:
            if process_gds(f, stackup, out_dir):
                ok += 1
        except Exception as exc:
            print(f"  [ERROR] {exc}")
        print()

    print(f"Done. {ok}/{len(gds_files)} file(s) converted to STEP.")


if __name__ == "__main__":
    main()
