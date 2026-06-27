# DWG2PDF Helper — Design Spec

Date: 2026-06-27
Status: Approved (design); implementation pending

## 1. Problem

A Windows 11 user needs to convert a folder of `.dwg` drawings (with arbitrary
nested subfolders) into PDFs. The output must mirror the input folder structure
in a chosen target directory, with one PDF per source drawing, file names
preserved. Each PDF is A3 landscape, fit-to-page.

Deliverable: a self-contained Windows `.exe` with a minimal GUI. The user
already has the free ODA File Converter installed.

## 2. Goals / Non-goals

Goals:
- Recursively discover `.dwg` (and pre-existing `.dxf`) files under an input dir.
- Recreate the input folder tree under the output dir, with `<name>.pdf` per drawing.
- A3 landscape output, fit-to-page, centered.
- Minimal GUI: input picker, output picker, color dropdown, skip-existing
  checkbox, Run button, live progress log.
- Ship as one `.exe` built by GitHub Actions on a `windows-latest` runner.
- Core conversion logic testable on macOS (CI-independent of Windows/ODA).

Non-goals:
- Bundling ODA File Converter (external dependency the user already has).
- Page sizes other than A3 landscape (explicitly fixed by the user).
- Layout/paper-space plot-config fidelity beyond "render model space, fit to page".
- Per-file page-size detection, batching queues, scheduling, or a CLI distribution
  (the `.exe` GUI is the only shipped interface; a thin CLI may exist for tests).

## 3. Constraints / Environment

- Build host: macOS arm64 (dev). PyInstaller cannot cross-compile, so the Windows
  `.exe` is produced by GitHub Actions (`windows-latest`). The repo is public.
- Runtime host: Windows 11 with ODA File Converter installed.
- The `.exe` is self-contained for Python + ezdxf + pymupdf + tkinter, but still
  requires ODA File Converter present on the machine.

## 4. Pipeline

```
DWG --(ODA File Converter, recurse)--> DXF (temp mirror) --(ezdxf+pymupdf)--> A3 PDF
```

ezdxf reads DXF, not DWG, so ODA does DWG->DXF first. ezdxf renders model space
directly, sidestepping AutoCAD "layout not initialized" issues on model-only drawings.

Data flow, structure-preserving:

```
input/                       temp_dxf/ (mirror)        output/ (mirror)
  A/x.dwg          ODA          A/x.dxf      ezdxf        A/x.pdf
  A/B/y.dwg   ───────────>      A/B/y.dxf   ───────>      A/B/y.pdf
  z.dxf            (copied/used directly)                 z.pdf
```

Steps:
1. Walk `input` recursively; collect `*.dwg` and `*.dxf` (case-insensitive).
2. If any `.dwg`: create the temp dir, then run ODA once with this exact CLI
   (output-dir must pre-exist or ODA does nothing):

   `ODAFileConverter <input> <temp> ACAD2018 DXF 1 <1|0 audit> "*.DWG"`

   The `1` is recurse-on; the `"*.DWG"` filter ensures ODA only touches `.dwg`
   files (so it never clobbers a same-named original `.dxf` in the temp tree and
   wastes no work). ODA mirrors the input tree as `.dxf`. Build the expected set
   from the relative paths of the input `.dwg` files. ODA is a Qt GUI app that may
   not self-close, so manage its lifecycle defensively (see step 2a).
2a. ODA process lifecycle (avoids truncating the last-written DXF):
    - Poll the temp dir for the expected `.dxf` set while the process runs.
    - Prefer a clean exit: once all expected files are present, give the process a
      short grace period to exit on its own (`proc.wait(timeout=grace)`); only
      `terminate()` (then `kill()`) if it overstays. If `proc` exits before the
      set is complete, stop waiting.
    - A path appearing in the directory listing does NOT mean its write finished.
      Before counting a DXF "done", require it to be size-stable across two
      consecutive polls. Files that never appear or never stabilize before the
      global timeout are reported as failures by relative path.
    - Parse validity is the final gate: truncated/partial DXFs that slip through
      raise on `ezdxf.readfile` at render time and are caught as per-file failures.
3. Build the render set as a map keyed by the target PDF path, preserving each
   source's path relative to its root: ODA-produced DXFs under temp, plus any
   original `.dxf` already present in input. When a `.dwg`-derived target and a
   `.dxf`-derived target collide (same stem, same folder), the DWG source wins and
   the shadowed DXF is logged as a skipped warning — output is deterministic, never
   silently overwritten. Render each entry to `output/<relpath>.pdf`, creating
   parent dirs as needed.
4. Skip rendering if the target PDF exists and skip-existing is enabled.
5. Delete the temp dir when done (try/finally, even if rendering raised).

Relative paths are preserved, so duplicate stems in *different* subfolders never
collide. The one same-folder collision (a `.dwg` and `.dxf` sharing a stem) is
resolved deterministically in favor of the DWG, with a logged warning. Renames are
handled (a renamed source yields a renamed PDF); the old PDF is not auto-removed —
output is additive, not a mirror/prune.

## 5. Modules and interfaces

### `converter.py` (pure logic, no GUI, no hard Windows dependency at import time)

- `find_oda(override: Path | None = None) -> Path`
  Auto-discover `C:\Program Files\ODA\*\ODAFileConverter.exe` (and the x86 dir),
  newest install last; raise `OdaNotFoundError` if absent. Honor an explicit override.
- `discover_drawings(input_dir: Path) -> tuple[list[Path], list[Path]]`
  Return (`dwg_paths`, `dxf_paths`) found recursively, case-insensitive on extension.
- `plan_output_path(src: Path, src_root: Path, output_dir: Path) -> Path`
  Map a source file to its `.pdf` target preserving the relative path.
- `dwg_to_dxf(input_dir, temp_dir, oda_exe, *, audit=True, timeout_s=900, log) -> None`
  Run ODA recurse=1 DWG->DXF with the `"*.DWG"` filter; manage the process
  lifecycle per section 4 step 2a (clean-exit-preferred, size-stable check,
  terminate only on overstay).
- `pick_render_layout(doc)`
  Model space if non-empty; else first non-empty paper-space layout; else model space.
- `dxf_to_pdf(dxf_path: Path, pdf_path: Path, *, color: ColorPolicy) -> None`
  Render to A3 landscape (420x297 mm, 10 mm margins), fit-to-page, centered.
  ALWAYS sets `BackgroundPolicy.WHITE` (model space defaults to a dark background;
  without this, `ColorPolicy.BLACK` yields black-on-black/blank pages). The color
  argument only controls entity color.
- `run_batch(input_dir, output_dir, *, color, skip_existing, oda_override=None, log) -> BatchResult`
  Orchestrate the whole pipeline; never abort on a single bad file. Builds the
  collision-resolved target map (DWG over same-folder DXF). `log` is a
  `Callable[[str], None]`. Also tees every log line to a `dwg2pdf.log` file written
  in the output dir, giving a field-diagnosable trace of batch execution on a
  `--windowed` build (pre-GUI startup crashes are covered separately by the app.py
  crash handler). Returns counts `(ok, skipped, failed)` and the failure list.

`ColorPolicy`/`BackgroundPolicy` are ezdxf's `addons.drawing.config` enums (not a
local wrapper). Exposed color choices map to: Black on white -> `ColorPolicy.BLACK`,
Original colors -> `ColorPolicy.COLOR`, Greyscale -> `ColorPolicy.MONOCHROME_LIGHT_BG`.
Exact enum member names are confirmed against the installed ezdxf at implementation
time (a unit test imports them so a rename breaks the build, not the user's run).
Constants: `PAGE_MM = (420.0, 297.0)`, `MARGIN_MM = 10.0`, `DXF_VERSION = "ACAD2018"`.

### `app.py` (tkinter GUI)

- Widgets: input folder entry + Browse, output folder entry + Browse, color
  dropdown (Black on white / Original colors / Greyscale), "Skip existing PDFs"
  checkbox, Run button, scrolling read-only log, status line with final tally.
- Threading: Run spawns a worker thread calling `run_batch` with a `log` callback
  that pushes lines onto a `queue.Queue`; the Tk main loop drains the queue via
  `after(100, ...)` and appends to the log widget. Run is disabled while busy.
- Startup safety: the `__main__` entry wraps GUI construction in a try/except that
  writes a crash file (`dwg2pdf-crash.log`) next to the `.exe`, since a
  `--windowed` build shows no console for import/startup failures that occur before
  `run_batch` (which owns `output/dwg2pdf.log`) ever runs.
- Validation/errors: empty/invalid input dir, output == input, output nested
  inside input (would recurse over its own results on a re-run), and
  `OdaNotFoundError` surface as a clear message in the log and a status line; the
  app never crashes the UI thread on a conversion error.

### `.github/workflows/build.yml`

- Trigger: push to `master` and tags `v*` (and manual `workflow_dispatch`).
- `windows-latest`: setup-python 3.12, `pip install -r requirements.txt pyinstaller`,
  then PyInstaller one-file windowed build. Upload the `.exe` as a build artifact
  on every run; on a `v*` tag, attach it to a GitHub Release.
- PyInstaller flags: `--onefile --windowed --name DWG2PDF`, plus
  `--collect-all ezdxf --collect-all fontTools --collect-all pymupdf
  --collect-all PIL` to ensure the drawing add-on, fonts, the pymupdf backend,
  and Pillow (a hard dependency of ezdxf 1.4's drawing add-on) are bundled.

## 6. Error handling

- ODA missing: caught up front, shown in GUI, batch aborts cleanly (nothing to do).
- One bad drawing: logged `[FAIL] <relpath>: <reason>`, counted, batch continues.
- ODA file(s) that never convert / never stabilize before timeout: logged as failures.
- Truncated/partial DXF that slips past the size-stable check: caught at render
  time by `ezdxf.readfile` and counted as a per-file failure (not a crash).
- Same-folder dwg+dxf stem collision: DWG wins, shadowed DXF logged as a warning.
- Empty/uninitialized layouts: handled by `pick_render_layout` fallback chain;
  a drawing with no geometry yields a valid blank A3 page, not an error.
- Temp dir cleanup runs even if rendering raised (try/finally).
- All log lines are also written to `output/dwg2pdf.log` for field diagnosis,
  since the `--windowed` build has no console.

## 7. Testing strategy

Core logic runs on macOS (ezdxf + pymupdf are cross-platform); only the ODA
subprocess is Windows-only and is mocked. TDD with pytest:

- `plan_output_path`: nested dirs, file at root, duplicate stems in different
  folders, mixed dwg/dxf, paths with spaces and unicode, and a rename case
  (renamed source -> renamed PDF target).
- `discover_drawings`: recursion, case-insensitive extensions, ignores non-drawings.
- `dxf_to_pdf`: render a small generated DXF fixture (lines + text) and assert a
  non-empty, valid PDF (starts with `%PDF`, openable by pymupdf, one A3 page).
  Assert a white background / visible black entities for the default color path
  (guards against the black-on-black regression).
- `dxf_to_pdf` blank case: empty model + empty paperspace renders a valid blank
  A3 page, not an exception.
- enum import test: `ColorPolicy.BLACK/COLOR/MONOCHROME_LIGHT_BG` and
  `BackgroundPolicy.WHITE` exist (build breaks on an ezdxf rename, user run doesn't).
- `pick_render_layout`: model-space-populated vs empty-model-with-paperspace.
- `run_batch`: with ODA mocked (temp DXF pre-seeded), assert output tree mirrors
  input, skip-existing skips, failures are tallied and don't abort, temp cleaned up,
  and `output/dwg2pdf.log` is written.
- collision test: a `.dwg` and `.dxf` with the same stem in the same folder ->
  DWG wins, DXF logged as shadowed, exactly one PDF produced.
- `find_oda`: override honored; `OdaNotFoundError` raised when nothing found
  (filesystem globbing mocked / pointed at an empty temp tree).

After tests are written, dispatch an independent subagent to review the test set
for a pareto-optimal set of real-world scenarios and add any missing cases
(per project testing policy) before declaring tests complete.

CI cannot exercise the real ODA subprocess (Windows + proprietary GUI app), so
the release checklist includes a manual Windows+ODA smoke test: run the built
`.exe` against a small nested tree containing at least one `.dwg`, confirm the
output mirror and a readable A3 PDF. Verify ODA's recurse-mirrors-structure
behavior on the user's installed ODA version (if a future version flattens, fall
back to per-subdir ODA invocations).

## 8. Project layout

```
dwg2pdf-helper/
  converter.py
  app.py
  requirements.txt          # ezdxf, pymupdf, Pillow (drawing add-on dep), pinned
  .github/workflows/build.yml
  tests/
    test_converter.py
    conftest.py             # DXF fixture generation
  README.md                 # usage, build/release, and troubleshooting:
                            #   - ODA must be installed; first run may show a
                            #     one-time EULA dialog that must be accepted before
                            #     batch mode works
                            #   - where dwg2pdf.log is written
                            #   - rename/delete is additive (stale PDFs persist)
  .gitignore
  docs/superpowers/specs/2026-06-27-dwg2pdf-helper-design.md
```

## 9. Open questions

None. A3 landscape fixed, GitHub Actions chosen for the build, GUI scope = folders
plus color + skip-existing.
