#!/usr/bin/env python3
"""
build_datasheets.py — Generate Nuvotronics RF filter datasheets from a spreadsheet.

Workflow:
  1. Read parts.csv (or .xlsx). Each row = one part.
  2. For each part, generate sparams.png from the part's Touchstone (.s2p) file.
  3. Substitute the row's values into a copy of the LaTeX template.
  4. Run xelatex to produce the PDF.

Usage:
    python build_datasheets.py parts.csv \\
        --template Datasheet-Template.tex \\
        --assets-dir assets \\
        --output-dir output

If --only PARTNUMBER is given, only that row is built.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import skrf as rf


# ---------------------------------------------------------------------------
# CSV column → LaTeX macro mapping (direct, 1-to-1 substitutions)
# ---------------------------------------------------------------------------
DIRECT_MACROS = [
    "PartNumber",
    "PartSize",
    "PartFilterOrder",
    "CenterFreqGHz",
    "PercentBW",
    "Stackup",
    "LaunchType",
    "PartMaxIL",
    "PartTypIL",
    "PartMaxRL",
    "PartTypRL",
    "MaxRej",
    "MinRej",
    "TypRej",
    "DocRev",
]

# Helper columns combined into single macro values. See build_macro_values().
#   PassbandLowGHz, PassbandHighGHz  -> \FreqRangeGHz       (e.g. "13.55--14.72")
#   RejLowStartGHz, RejLowEndGHz     -> \RejRangeLowGHz     (e.g. "0--12.98")
#   RejHighStartGHz, RejHighEndGHz   -> \RejRangeHighGHz    (e.g. "15.34--30.0")
#   Measured (bool)                  -> \PlotDisclaimer
#
# Other special columns (not LaTeX macros):
#   S2PFile          path to Touchstone file (required)
#   ProductImage     path to product photo  (optional)
#   StackupImage     path to PCB stackup    (optional)
#   MechDrawing      path to 2D drawing     (optional)
#   PlotFreqMinGHz   plot x-axis min        (optional, defaults to data range)
#   PlotFreqMaxGHz   plot x-axis max        (optional, defaults to data range)


# ---------------------------------------------------------------------------
# LaTeX macro substitution (brace-balanced, so nested macros stay intact)
# ---------------------------------------------------------------------------
def replace_newcommand(text: str, macro: str, new_body: str) -> tuple[str, bool]:
    """
    Replace the body of  \\newcommand{\\<macro>}{...}  with new_body.
    Handles nested braces inside the body. Returns (new_text, replaced?).
    """
    marker = "\\newcommand{\\" + macro + "}{"
    start = text.find(marker)
    if start == -1:
        return text, False
    body_start = start + len(marker)
    depth = 1
    i = body_start
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    if depth != 0:
        return text, False
    body_end = i - 1
    return text[:body_start] + new_body + text[body_end:], True


def substitute_all(template_text: str, macro_values: dict[str, str]) -> str:
    """Apply every macro substitution; warn on any not found."""
    out = template_text
    for name, val in macro_values.items():
        out, ok = replace_newcommand(out, name, val)
        if not ok:
            print(f"  WARN: macro \\{name} not found in template (skipped)")
    return out


# ---------------------------------------------------------------------------
# Row → macro values
# ---------------------------------------------------------------------------
def fmt_range(lo, hi) -> str:
    """Format a numeric range with a LaTeX en-dash."""
    return f"{lo}--{hi}"


def disclaimer_text(measured: bool) -> str:
    if measured:
        return r"\textit{Plots above are measured, probed data.}"
    return r"\textit{Plots above are preliminary simulation data.}"


def build_macro_values(row: pd.Series) -> dict[str, str]:
    """Build the dict of macro_name -> latex_value_string from a CSV row."""
    vals: dict[str, str] = {}

    # Direct copies (cast to str, strip whitespace)
    for col in DIRECT_MACROS:
        if col in row and not pd.isna(row[col]):
            vals[col] = str(row[col]).strip()

    # Composite ranges
    if "PassbandLowGHz" in row and "PassbandHighGHz" in row:
        vals["FreqRangeGHz"] = fmt_range(row["PassbandLowGHz"], row["PassbandHighGHz"])
    if "RejLowStartGHz" in row and "RejLowEndGHz" in row:
        vals["RejRangeLowGHz"] = fmt_range(row["RejLowStartGHz"], row["RejLowEndGHz"])
    if "RejHighStartGHz" in row and "RejHighEndGHz" in row:
        vals["RejRangeHighGHz"] = fmt_range(row["RejHighStartGHz"], row["RejHighEndGHz"])

    # Disclaimer
    if "Measured" in row and not pd.isna(row["Measured"]):
        vals["PlotDisclaimer"] = disclaimer_text(bool(row["Measured"]))

    return vals


# ---------------------------------------------------------------------------
# S-parameter plot
# ---------------------------------------------------------------------------
def generate_sparams_plot(
    s2p_path: Path,
    out_png: Path,
    plot_title: str,
    fmin_ghz: float | None = None,
    fmax_ghz: float | None = None,
) -> None:
    """Render the RF performance plot (S11 dotted blue, S21 solid red)."""
    ntwk = rf.Network(str(s2p_path))
    freq_ghz = ntwk.f / 1e9
    s11_db = 20.0 * np.log10(np.abs(ntwk.s[:, 0, 0]))
    s21_db = 20.0 * np.log10(np.abs(ntwk.s[:, 1, 0]))

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(freq_ghz, s11_db, color="blue", linestyle=":", linewidth=1.6, label="Return Loss")
    ax.plot(freq_ghz, s21_db, color="red",  linestyle="-", linewidth=1.6, label="Insertion Loss")

    ax.set_xlabel("Frequency (GHz)")
    ax.set_title(plot_title, fontweight="bold")
    ax.legend(loc="upper right", framealpha=1.0, edgecolor="black")

    if fmin_ghz is not None or fmax_ghz is not None:
        ax.set_xlim(fmin_ghz, fmax_ghz)
    # Match the example plot's y-range, but don't crop above 0 dB
    ax.set_ylim(-80, 5)

    # Light border, no grid (to match the reference)
    for spine in ax.spines.values():
        spine.set_color("black")
        spine.set_linewidth(1.0)

    fig.tight_layout()
    fig.savefig(out_png, dpi=200, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Build a single part
# ---------------------------------------------------------------------------
def build_part(
    row: pd.Series,
    template_path: Path,
    assets_dir: Path,
    output_root: Path,
    skip_compile: bool = False,
) -> Path | None:
    part_number = str(row["PartNumber"]).strip()
    print(f"\n=== Building {part_number} ===")

    part_dir = output_root / part_number
    images_dir = part_dir / "images"
    fonts_src = assets_dir / "fonts"
    images_dir.mkdir(parents=True, exist_ok=True)

    # Copy shared assets (fonts, company logo) into the build dir
    if fonts_src.exists():
        shutil.copytree(fonts_src, part_dir / "fonts", dirs_exist_ok=True)
    shared_images = assets_dir / "images"
    if shared_images.exists():
        for img in shared_images.iterdir():
            shutil.copy2(img, images_dir / img.name)

    # Per-part images: rename to the names the template expects
    image_columns = {
        "ProductImage": "product-image.png",
        "StackupImage": "pcb-stackup.png",
        "MechDrawing":  "2D-drawing.png",
    }
    for col, dest_name in image_columns.items():
        if col in row and not pd.isna(row[col]):
            src = Path(row[col])
            if not src.is_absolute():
                src = assets_dir / src
            if src.exists():
                shutil.copy2(src, images_dir / dest_name)
            else:
                print(f"  WARN: {col} file not found: {src}")

    # Generate sparams.png from the Touchstone file
    s2p_path = Path(row["S2PFile"])
    if not s2p_path.is_absolute():
        s2p_path = assets_dir / s2p_path
    if not s2p_path.exists():
        print(f"  ERROR: S2PFile not found: {s2p_path}")
        return None

    fmin = row.get("PlotFreqMinGHz")
    fmax = row.get("PlotFreqMaxGHz")
    fmin = None if pd.isna(fmin) else float(fmin)
    fmax = None if pd.isna(fmax) else float(fmax)

    # Plot title matches the reference (numeric part of P/N, e.g. "1029223")
    title = "".join(ch for ch in part_number if ch.isdigit()) or part_number
    generate_sparams_plot(s2p_path, images_dir / "sparams.png", title, fmin, fmax)

    # Substitute macros into the template
    template_text = template_path.read_text()
    macro_values = build_macro_values(row)
    new_text = substitute_all(template_text, macro_values)

    tex_out = part_dir / f"{part_number}_Datasheet.tex"
    tex_out.write_text(new_text)
    print(f"  Wrote {tex_out.relative_to(output_root.parent) if output_root.parent in tex_out.parents else tex_out}")

    if skip_compile:
        return tex_out

    # Compile with xelatex (twice for refs/TOC if you add them later)
    for pass_n in (1, 2):
        result = subprocess.run(
            ["xelatex", "-interaction=nonstopmode", "-halt-on-error", tex_out.name],
            cwd=part_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  xelatex pass {pass_n} FAILED. Last 40 lines of log:")
            print("\n".join(result.stdout.splitlines()[-40:]))
            return None

    pdf_path = tex_out.with_suffix(".pdf")
    print(f"  ✓ PDF: {pdf_path}")
    return pdf_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spreadsheet", help="parts.csv or parts.xlsx")
    ap.add_argument("--template", default="Datasheet-Template.tex", help="LaTeX template path")
    ap.add_argument("--assets-dir", default="assets", help="Dir containing fonts/, images/, touchstone files")
    ap.add_argument("--output-dir", default="output", help="Where built PDFs go")
    ap.add_argument("--only", help="Build only this part number")
    ap.add_argument("--no-compile", action="store_true", help="Just write .tex, skip xelatex")
    args = ap.parse_args()

    sheet = Path(args.spreadsheet)
    if sheet.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(sheet)
    else:
        df = pd.read_csv(sheet)

    if args.only:
        df = df[df["PartNumber"].astype(str).str.strip() == args.only.strip()]
        if df.empty:
            print(f"No row found with PartNumber == {args.only}")
            sys.exit(1)

    template_path = Path(args.template)
    assets_dir = Path(args.assets_dir)
    output_root = Path(args.output_dir)
    output_root.mkdir(parents=True, exist_ok=True)

    built = []
    for _, row in df.iterrows():
        result = build_part(row, template_path, assets_dir, output_root, skip_compile=args.no_compile)
        if result:
            built.append(result)

    print(f"\nDone. {len(built)} part(s) built.")


if __name__ == "__main__":
    main()
