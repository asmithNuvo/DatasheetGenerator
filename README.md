# Datasheet Builder

Generates Nuvotronics RF filter datasheets from a spreadsheet of part specs.
Each row in `parts.csv` (or `.xlsx`) becomes one PDF.

## What it does (per part)

1. Reads the row's spec values.
2. Plots S11 (Return Loss) and S21 (Insertion Loss) from the part's Touchstone
   `.s2p` file → saves as `images/sparams.png` in that part's build folder.
3. Copies the `.tex` template and replaces every `\newcommand{\Macro}{...}`
   value with the row's data. Brace-aware, so nested macros like
   `\ProductTitle{\LaunchType{} \Stackup, ...}` are not disturbed.
4. Runs `xelatex` twice to produce the final PDF.

## Setup

```bash
pip install scikit-rf pandas matplotlib openpyxl
```

Plus a working LaTeX install with `xelatex` (TeX Live or MiKTeX) and the
packages your template already uses (`fontspec`, `nicematrix`, `tcolorbox`,
`tikz`, etc.).

## Directory layout

```
project/
├── Datasheet-Template.tex     # your existing template (unchanged)
├── parts.csv                  # one row per part
├── build_datasheets.py
└── assets/
    ├── fonts/                 # FunnelSans-*.ttf (referenced by the template)
    ├── images/
    │   ├── company-logo.png   # shared
    │   ├── PSF1029223_product.png
    │   ├── PSF1029223_stackup.png
    │   └── PSF1029223_2D.png
    └── touchstone/
        └── PSF1029223.s2p
```

The script copies the shared assets (`fonts/`, anything in `assets/images/`)
into each part's build folder, then renames the per-part images to the names
the template expects (`product-image.png`, `pcb-stackup.png`, `2D-drawing.png`).

## Usage

Build everything in the sheet:
```bash
python build_datasheets.py parts.csv
```

Build one part only:
```bash
python build_datasheets.py parts.csv --only PSF1029223
```

Skip the `xelatex` step (just emit the `.tex` for inspection):
```bash
python build_datasheets.py parts.csv --no-compile
```

Custom paths:
```bash
python build_datasheets.py parts.csv \
    --template Datasheet-Template.tex \
    --assets-dir assets \
    --output-dir output
```

Output lands in `output/<PartNumber>/<PartNumber>_Datasheet.pdf`.

## Spreadsheet columns

| Column              | Macro / Use                        | Notes                                  |
|---------------------|-----------------------------------|----------------------------------------|
| `PartNumber`        | `\PartNumber`                     | also the output folder name            |
| `DocRev`            | `\DocRev`                         | e.g. `Rev. - | PRELIMINARY`            |
| `PartSize`          | `\PartSize`                       | e.g. `11.76 x 6.53 x 1.5 mm`           |
| `PartFilterOrder`   | `\PartFilterOrder`                | e.g. `5th`                             |
| `CenterFreqGHz`     | `\CenterFreqGHz`                  | number                                 |
| `PercentBW`         | `\PercentBW`                      | number                                 |
| `Stackup`           | `\Stackup`                        | e.g. `AQ210`                           |
| `LaunchType`        | `\LaunchType`                     | e.g. `SMT`                             |
| `PartMaxIL`,`PartTypIL`     | `\PartMaxIL`, `\PartTypIL` | dB                              |
| `PartMaxRL`,`PartTypRL`     | `\PartMaxRL`, `\PartTypRL` | dB                              |
| `MaxRej`,`MinRej`,`TypRej`  | `\MaxRej`, `\MinRej`, `\TypRej` | dB                         |
| `PassbandLowGHz`,`PassbandHighGHz`   | `\FreqRangeGHz`        | formatted as `lo--hi`           |
| `RejLowStartGHz`,`RejLowEndGHz`      | `\RejRangeLowGHz`      | formatted as `lo--hi`           |
| `RejHighStartGHz`,`RejHighEndGHz`    | `\RejRangeHighGHz`     | formatted as `lo--hi`           |
| `Measured`          | `\PlotDisclaimer`                  | `TRUE`/`FALSE` → measured vs. sim text |
| `S2PFile`           | source for `sparams.png`          | required, path relative to assets-dir  |
| `ProductImage`      | renamed → `product-image.png`     | optional                               |
| `StackupImage`      | renamed → `pcb-stackup.png`       | optional                               |
| `MechDrawing`       | renamed → `2D-drawing.png`        | optional                               |
| `PlotFreqMinGHz`,`PlotFreqMaxGHz`    | plot x-axis limits     | optional, defaults to data range       |

## Adding a new part

1. Add a new row to `parts.csv` with the part's specs.
2. Drop the Touchstone file in `assets/touchstone/`.
3. Drop the three per-part images in `assets/images/` (product photo, PCB
   stackup, 2D drawing).
4. Run `python build_datasheets.py parts.csv --only NEWPN`.

## Notes

- The `.tex` template is **never modified** — the script writes a per-part
  copy with the macro values replaced. You can still hand-compile the
  original template as before.
- The plot title is the numeric portion of the part number (matches the
  reference, e.g. `PSF1029223` → `1029223`). Edit `build_part()` if you want
  a different convention.
- Substitution is brace-aware: nested LaTeX inside macro values (like
  `\textit{...}` inside `\PlotDisclaimer`) round-trips correctly.
