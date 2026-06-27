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

**Tag _assy / _weld from text** (on by default) scans each sheet's text and
appends a suffix to the PDF name: `_assy` if it contains "assembly" or "assy",
`_weld` if it contains "weldment" (case-insensitive). A sheet with both gets
`_assy_weld`. Matching ignores spacing, so letter-spaced titles still match;
the trade-off is a rare false hit where unrelated words run together (e.g. a
"class y" note). Untick to keep plain names. Only XPS DWFx are scanned - binary
DWF6 files (manual conversion) are never tagged.

XPS-based DWFx convert automatically. Older **binary DWF6** files (e.g. from
Inventor) have no free converter and are listed in
`binary-dwf-needs-autocad.txt` in the output folder for manual conversion in
AutoCAD (DWFATTACH -> Zoom Extents -> Plot, A3 landscape). The run log is
`<output>/dwfx2pdf.log`.

## Getting the .exe

Every push to `master` builds `DWFX2PDF.exe` as a GitHub Actions artifact.
Tagging `vX.Y.Z` attaches the exe to a public release:

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
