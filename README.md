# DWG2PDF Helper

A tiny Windows GUI that batch-converts a folder of AutoCAD **DWG** drawings (and
any nested subfolders) into **A3-landscape PDFs**, recreating the input folder
structure in an output folder you choose. File names are preserved.

## How it works

`DWG --(ODA File Converter)--> DXF --(ezdxf + PyMuPDF)--> A3 landscape PDF`

- ezdxf reads DXF (not DWG), so the free ODA File Converter does the DWG->DXF
  step first.
- ezdxf renders model space directly, sidestepping AutoCAD "layout not
  initialized" issues on model-only drawings.

## Requirements (on the Windows machine)

- The free **ODA File Converter** must be installed:
  https://www.opendesign.com/guestfiles/oda_file_converter
  On first launch it may show a one-time license dialog you must accept before
  batch mode works.
- Nothing else - the `.exe` bundles Python, ezdxf, PyMuPDF and Pillow.

## Usage

1. Run `DWG2PDF.exe`.
2. Pick the **input folder** (with your `.dwg` files, subfolders allowed).
3. Pick an **output folder** (must not be the input folder or inside it).
4. Choose a **color** mode and whether to skip PDFs that already exist.
5. Click **Run**. Progress streams in the log; a tally shows at the end.

Output mirrors the input tree. A run log is written to `<output>/dwg2pdf.log`.
Conversion is additive: renaming or deleting a source does not remove old PDFs.

## Getting the .exe

Every push to `master` builds the `.exe` as a GitHub Actions artifact
(Actions tab -> latest run -> `DWG2PDF-exe`). Tagging a release `vX.Y.Z`
attaches the `.exe` to the GitHub Release.

## Build locally (Windows only)

```
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name DWG2PDF ^
  --collect-all ezdxf --collect-all fontTools ^
  --collect-all pymupdf --collect-all PIL app.py
```

PyInstaller cannot cross-compile; the `.exe` must be built on Windows.

## Troubleshooting

- "ODA File Converter not found" - install it (link above).
- A drawing fails - it is logged in `dwg2pdf.log` and the batch continues.
- The app won't start at all - look for `dwg2pdf-crash.log` next to the `.exe`.

## Development

```
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
pytest -q
```

Core logic (`converter.py`) is tested on any OS; the ODA subprocess is mocked.
