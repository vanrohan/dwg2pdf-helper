# DWFx/DWF to PDF Helper

Windows GUI that batch-converts a folder of **DWFx** drawings (with subfolders) to
PDFs, recreating the folder structure in an output folder. Page sizes are preserved
as plotted; file names are kept.

## Usage

1. Run `DWFX2PDF.exe`.
2. Pick the **input folder** and an **output folder** (not the input folder or inside it).
3. Click **Run**.

**White background** (on by default) drops the tinted "paper" colour AutoCAD
writes into many drawings (e.g. pale cream `#ededd6`) so line work prints on
white. Untick it to keep the drawing's original background colour.

**Filename tags** scan each sheet's text and append a suffix to the PDF name.
There is one checkbox per tag (all on by default):

- `_assy` - text contains "assembly" or "assy"
- `_weld` - text contains "weldment"
- `_machine` - text contains "machine"

Matching is case-insensitive and ignores spacing, so letter-spaced titles still
match; the trade-off is a rare false hit where unrelated words run together (e.g.
a "class y" note). A sheet matching several enabled tags collects each suffix in
order, e.g. `frame_assy_weld.pdf`. Only XPS DWFx are scanned - binary DWF6 files
(manual conversion) are never tagged.

**Embedded images.** Most drawings are pure vector and convert to a crisp, small
PDF. Some embed a raster image (inserted photo, shaded/rendered view or scanned
underlay) that the vector converter silently drops to a black box. Each drawing
is converted as vector first, then checked: if the vector PDF lost content the
real page has (dropped colour, or a shaded/tiled-pattern view blanked to a hollow
outline), that file is re-rendered page-by-page so the image is reproduced
(larger and not text-selectable, but correct; a 200 dpi A3 sheet is ~1-2 MB,
lossless). Drawings the vector path handles cleanly - including ones with images
it renders fine - keep the crisp, tiny vector output. A drawing that cannot be
converted at all is reported as failed rather than written as a broken PDF.

XPS-based DWFx convert automatically. Older **binary DWF6** files (e.g. from
Inventor) have no free converter and are listed in
`binary-dwf-needs-autocad.txt` in the output folder for manual conversion in
AutoCAD (DWFATTACH -> Zoom Extents -> Plot, A3 landscape). The run log is
`<output>/dwfx2pdf.log`.

## Getting the .exe

Every push to `master` builds `DWFX2PDF.exe` as a GitHub Actions artifact.
Tagging `vX.Y.Z` attaches two copies to a public release: a version-stamped
`DWFX2PDF-vX.Y.Z.exe` (so a downloaded file is self-identifying) and a fixed
name for the always-latest URL:

`https://github.com/vanrohan/dwg2pdf-helper/releases/latest/download/DWFX2PDF.exe`

## Build locally (Windows only)

```
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name DWFX2PDF --collect-all pymupdf app.py
```

PyInstaller cannot cross-compile; the `.exe` must be built on Windows.

## Development / tests

```
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
pytest -q
```

XPS conversion, format detection, folder mirroring, and orchestration are tested
on any OS with synthetic fixtures.

## Legacy: DWG to PDF

`converter.py` is the earlier DWG-to-PDF engine (ODA File Converter + ezdxf),
kept and tested but no longer the default tool.
