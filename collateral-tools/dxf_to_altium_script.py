#!/usr/bin/env python3
"""
dxf_to_altium_script.py

Reads sized DXF footprint files (as produced by gds_to_footprint_dxf.py) and
generates Altium Designer DelphiScript (.pas) files that create pad geometry
on the Top Copper layer — bypassing the DXF import dialog entirely.

Paste and solder mask layers are handled by Altium's pad expansion settings
after custom pad creation, so only the TopCopper DXF layer is used.

The generated script creates closed Track outlines for each pad on eTopLayer.
After running in Altium:
    1. Select one pad's track group
    2. Tools > Convert > Create Region from Selected Primitives
    3. Tools > Convert > Create Custom Pad from Selected
    4. Set paste/mask expansions on the pad properties as needed

Requirements:
    pip install ezdxf
"""

import ezdxf
import os
import sys
import glob
from collections import defaultdict


SOURCE_LAYER = "TopCopper"


# ── DXF reading ──────────────────────────────────────────────────────────────

def read_dxf(dxf_path):
    """Read closed LWPOLYLINE entities from the TopCopper layer.

    Returns list of {'pts': [(x,y),...], 'cx': float, 'cy': float}
    Coordinates are in the DXF's native units (micrometers).
    """
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    pads = []
    for entity in msp:
        if entity.dxftype() != "LWPOLYLINE":
            continue
        if entity.dxf.layer != SOURCE_LAYER:
            continue
        pts = [(float(x), float(y)) for x, y in entity.get_points(format="xy")]
        if not pts:
            continue
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        pads.append({"pts": pts, "cx": cx, "cy": cy})

    return pads


# ── DelphiScript generation ──────────────────────────────────────────────────

def _coord(um_value):
    """Format a micrometer value as an Altium MMsToCoord() call."""
    mm = um_value * 0.001
    return f"MMsToCoord({mm:.6f})"


def generate_pas(pads, component_name):
    """Build the .pas source that creates track outlines on Top Layer."""

    lines = []

    def w(text=""):
        lines.append(text)

    total_tracks = sum(len(p["pts"]) for p in pads)

    w(f"// =============================================================")
    w(f"// Auto-generated Altium footprint script")
    w(f"// Component : {component_name}")
    w(f"// Pads      : {len(pads)}  ({total_tracks} track segments)")
    w(f"//")
    w(f"// USAGE")
    w(f"//   1. Open a PCB Library, navigate to the target footprint.")
    w(f"//   2. DXP > Run Script > select this file.")
    w(f"//   3. Run  CreateFootprint")
    w(f"//")
    w(f"// After running, for each pad:")
    w(f"//   a) Select the pad's track outline (click one track segment,")
    w(f"//      then Edit > Select > Connected Tracks, or select manually)")
    w(f"//   b) Tools > Convert > Create Region from Selected Primitives")
    w(f"//   c) With region selected:")
    w(f"//      Tools > Convert > Create Custom Pad from Selected")
    w(f"//   d) Set paste/mask expansions in pad properties as needed")
    w(f"// =============================================================")
    w()
    w("Procedure CreateFootprint;")
    w("Var")
    w("    Board : IPCB_Board;")
    w("    Track : IPCB_Track;")
    w("Begin")
    w("    Board := PCBServer.GetCurrentPCBBoard;")
    w("    If Board = Nil Then")
    w("    Begin")
    w("        ShowMessage('Please open a PCB Library or document first.');")
    w("        Exit;")
    w("    End;")
    w()
    w("    PCBServer.PreProcess;")
    w()

    for pad_num, pad in enumerate(pads, start=1):
        pts = pad["pts"]
        n = len(pts)
        w(f"    // ---- Pad {pad_num} ({n} segments) ----")
        for k in range(n):
            x1, y1 = pts[k]
            x2, y2 = pts[(k + 1) % n]
            w(f"    Track := PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default);")
            w(f"    Track.Layer := eTopLayer;")
            w(f"    Track.Width := MMsToCoord(0.001);")
            w(f"    Track.x1 := {_coord(x1)};  Track.y1 := {_coord(y1)};")
            w(f"    Track.x2 := {_coord(x2)};  Track.y2 := {_coord(y2)};")
            w(f"    Board.AddPCBObject(Track);")
        w()

    w("    PCBServer.PostProcess;")
    w("    Board.ViewManager_FullUpdate;")
    w(f"    ShowMessage('{len(pads)} pad outlines created as tracks on Top Layer.');")
    w("End;")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def process_one(dxf_path, output_dir):
    """Process a single DXF → .pas file."""
    stem = os.path.splitext(os.path.basename(dxf_path))[0]
    print(f"Processing: {stem}")

    pads = read_dxf(dxf_path)
    if not pads:
        print(f"  [SKIP] No TopCopper polygons found")
        return False

    print(f"  {len(pads)} pad(s) on TopCopper")

    pas_source = generate_pas(pads, stem)
    out_path = os.path.join(output_dir, f"{stem}.pas")
    with open(out_path, "w") as f:
        f.write(pas_source)

    print(f"  [SAVED] {out_path}")
    return True


def main():
    print("=" * 60)
    print("  DXF Footprint → Altium DelphiScript Generator")
    print("=" * 60)

    dxf_input = input("\nDXF file or directory: ").strip()
    if os.path.isfile(dxf_input):
        dxf_files = [dxf_input]
    elif os.path.isdir(dxf_input):
        dxf_files = sorted(glob.glob(os.path.join(dxf_input, "*.dxf"))) + \
                    sorted(glob.glob(os.path.join(dxf_input, "*.DXF")))
        seen = set()
        dxf_files = [f for f in dxf_files
                     if os.path.realpath(f) not in seen and not seen.add(os.path.realpath(f))]
    else:
        sys.exit(f"Error: '{dxf_input}' not found.")

    if not dxf_files:
        sys.exit("No DXF files found.")

    default_out = os.path.dirname(dxf_files[0]) or "."
    out_dir = input(f"Output directory [Enter = {default_out}]: ").strip() or default_out
    os.makedirs(out_dir, exist_ok=True)

    print(f"\n{len(dxf_files)} file(s) to process.\n")

    ok = sum(process_one(f, out_dir) for f in dxf_files)
    print(f"\nDone. {ok}/{len(dxf_files)} script(s) generated.")


if __name__ == "__main__":
    main()