# DWFx/DWF to PDF Helper

A small Windows GUI that batch-converts a folder of **DWFx/DWF** drawings (with
nested subfolders) into PDFs, recreating the folder structure in an output folder.
Page sizes are preserved as plotted (e.g. native A3 sheets). File names are kept.

## Two formats hide behind the `.dwfx` extension

| Format | How it's converted | Needs |
|---|---|---|
| **XPS-based DWFx** | Directly by the bundled PyMuPDF engine (vector, small) | Nothing — self-contained |
| **Binary DWF 6.x** (e.g. from Inventor) | AutoCAD attaches it as an underlay and plots to a GDI PDF printer | Full AutoCAD + clawPDF (see below) |

The tool auto-detects each file. XPS DWFx convert out of the box. Binary DWF files
are converted only if the AutoCAD path is set up; otherwise they are listed in
`binary-dwf-needs-autocad.txt` in the output folder so none are silently dropped.

## Usage

1. Run `DWFX2PDF.exe`.
2. Pick the **input folder** (your `.dwfx`/`.dwf` files, subfolders allowed).
3. Pick an **output folder** (not the input folder or inside it).
4. (Binary DWF only) set the **clawPDF output folder** — see setup below.
5. Click **Run**. Progress streams in the log; a tally shows at the end.

Output mirrors the input tree; a run log is written to `<output>/dwfx2pdf.log`.

## Binary DWF setup (AutoCAD + clawPDF)

Binary DWF has no free converter library, so the tool drives AutoCAD. Required on
the Windows machine:

1. **Full AutoCAD** (not LT) — provides the underlay + plot. Auto-detected under
   `C:\Program Files\Autodesk\AutoCAD *`.
2. **clawPDF** (free, open source) — a GDI PDF printer. AutoCAD's own PDF driver
   renders DWF-underlay text at ~0.6pt (invisible); a GDI printer renders it
   correctly. Configure a clawPDF profile with **auto-save** on, a fixed output
   folder, filename token `<Title>`, and "ensure unique filenames". Point the
   tool's "clawPDF output folder" at that same folder.

The tool then, per binary file: opens AutoCAD in batch mode, attaches the DWF,
zooms extents, plots A3 landscape (fit, centered) to clawPDF, and files the
resulting PDF into the mirrored output path.

> Note: the AutoCAD path is Windows + AutoCAD specific and could not be tested in
> CI. The `-PLOT` prompt sequence is English/version-specific; if a binary file
> fails, check `dwfx2pdf.log` — the generated `batch.scr` and AutoCAD output show
> exactly where it stopped, and the plot device/paper strings can be adjusted.

## Getting the .exe

Every push to `master` builds `DWFX2PDF.exe` as a GitHub Actions artifact
(Actions tab -> latest run -> `DWFX2PDF-exe`). Tagging `vX.Y.Z` attaches the exe
to a public GitHub Release:

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

The XPS conversion, format detection, folder mirroring, and orchestration are
tested on any OS with synthetic fixtures. The AutoCAD subprocess is mocked.

## Troubleshooting

- Binary DWFs listed in `binary-dwf-needs-autocad.txt` — set up AutoCAD + clawPDF.
- A file fails — it's logged in `dwfx2pdf.log`; the batch continues.
- The app won't start — look for `dwfx2pdf-crash.log` next to the exe.

## Legacy: DWG to PDF

`converter.py` is the earlier DWG-to-PDF engine (ODA File Converter + ezdxf),
kept and tested but no longer the default tool. See git history / the spec under
`docs/superpowers/`.
