# DWG2PDF Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-contained Windows GUI `.exe` that batch-converts a nested folder of DWG drawings into A3-landscape PDFs, mirroring the input folder structure in a chosen output folder.

**Architecture:** Pure conversion logic lives in `converter.py` (testable on macOS via ezdxf+pymupdf; the ODA subprocess is the only Windows-only piece and is mocked in tests). A thin tkinter GUI in `app.py` runs `run_batch` on a worker thread and streams a log to the UI via a queue. GitHub Actions (`windows-latest`) builds the one-file `.exe` with PyInstaller.

**Tech Stack:** Python 3.12, ezdxf 1.4.4 (+ Pillow, fontTools, numpy), PyMuPDF 1.27.2.3, tkinter (stdlib), pytest, PyInstaller, ODA File Converter (external, user-installed).

## Global Constraints

- Python 3.12.
- Dependencies pinned: `ezdxf==1.4.4`, `PyMuPDF==1.27.2.3`, `Pillow==12.2.0`. (fontTools/numpy come in transitively via ezdxf.)
- Pillow is a HARD dependency of ezdxf 1.4's drawing add-on (`import PIL.Image` at import time). It MUST be in requirements and bundled.
- Page is ALWAYS A3 landscape: `PAGE_MM = (420.0, 297.0)`, `MARGIN_MM = 10.0`. Fit-to-page, centered. Not user-configurable.
- `dxf_to_pdf` ALWAYS sets `BackgroundPolicy.WHITE`; the color choice only sets entity `ColorPolicy`. Without WHITE, the default `ColorPolicy.BLACK` renders black-on-black/blank.
- ODA CLI invocation is exactly: `ODAFileConverter <in> <out> ACAD2018 DXF 1 <audit 0|1> "*.DWG"` (recurse on, filter to DWG only).
- ODA enums/exact names confirmed: `ColorPolicy.{BLACK,COLOR,MONOCHROME_LIGHT_BG}`, `BackgroundPolicy.WHITE` (module `ezdxf.addons.drawing.config`).
- Relative paths preserved end-to-end so structure is mirrored and cross-folder stem duplicates never collide. Same-folder `.dwg`+`.dxf` stem collision resolves in favor of DWG, with a logged shadow warning.
- Output additive (renamed source -> renamed PDF; stale PDFs are not pruned).
- One bad file never aborts the batch.
- Every log line is teed to `output/dwg2pdf.log`. `app.py`'s entry point writes `dwg2pdf-crash.log` next to the exe on a startup crash.
- Commit after every task. Run on `master`.

---

## File Structure

- `converter.py` — all conversion logic; no tkinter import. Constants, enums, `OdaNotFoundError`, `BatchResult`, `find_oda`, `discover_drawings`, `plan_output_path`, `pick_render_layout`, `dxf_to_pdf`, `dwg_to_dxf`, `run_batch`.
- `app.py` — tkinter GUI; imports `converter`. Worker thread + queue + `after()` log pump. Startup crash handler.
- `tests/conftest.py` — pytest fixtures (DXF generation helper).
- `tests/test_converter.py` — all unit tests.
- `requirements.txt` — pinned runtime deps.
- `.github/workflows/build.yml` — Windows build + artifact/release.
- `README.md` — usage, build/release, troubleshooting.

---

### Task 1: Scaffolding — deps, constants, enums, datatypes

**Files:**
- Create: `requirements.txt`
- Create: `converter.py`
- Create: `tests/conftest.py`
- Create: `tests/test_converter.py`

**Interfaces:**
- Produces: `converter.PAGE_MM`, `converter.MARGIN_MM`, `converter.DXF_VERSION`, `converter.ODA_TIMEOUT_S`, `converter.LOG_FILENAME`, `converter.CRASH_FILENAME`, `converter.COLOR_CHOICES` (dict label->ColorPolicy), `converter.DEFAULT_COLOR_LABEL`, `converter.OdaNotFoundError`, `converter.BatchResult` (dataclass: `ok:int, skipped:int, failed:int, failures:list[tuple[str,str]]`). Re-exports `ColorPolicy`, `BackgroundPolicy`.

- [ ] **Step 1: Write `requirements.txt`**

```
ezdxf==1.4.4
PyMuPDF==1.27.2.3
Pillow==12.2.0
```

- [ ] **Step 2: Write the failing test** in `tests/test_converter.py`

```python
import converter
from ezdxf.addons.drawing.config import ColorPolicy, BackgroundPolicy


def test_enums_and_color_choices_exist():
    # exact enum members the GUI maps to must exist (build breaks, not user run)
    assert ColorPolicy.BLACK
    assert ColorPolicy.COLOR
    assert ColorPolicy.MONOCHROME_LIGHT_BG
    assert BackgroundPolicy.WHITE
    assert converter.COLOR_CHOICES == {
        "Black on white": ColorPolicy.BLACK,
        "Original colors": ColorPolicy.COLOR,
        "Greyscale": ColorPolicy.MONOCHROME_LIGHT_BG,
    }
    assert converter.DEFAULT_COLOR_LABEL == "Black on white"


def test_constants():
    assert converter.PAGE_MM == (420.0, 297.0)
    assert converter.MARGIN_MM == 10.0
    assert converter.DXF_VERSION == "ACAD2018"


def test_batchresult_defaults():
    r = converter.BatchResult()
    assert (r.ok, r.skipped, r.failed, r.failures) == (0, 0, 0, [])
```

- [ ] **Step 3: Run test to verify it fails**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'converter'`).

- [ ] **Step 4: Write `converter.py`** (header + constants + datatypes)

```python
from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext, layout
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend

# --- A3 landscape, fixed ---
PAGE_MM = (420.0, 297.0)
MARGIN_MM = 10.0
DXF_VERSION = "ACAD2018"
ODA_TIMEOUT_S = 900
LOG_FILENAME = "dwg2pdf.log"
CRASH_FILENAME = "dwg2pdf-crash.log"
TEMP_DIRNAME = "_dxf_tmp"

COLOR_CHOICES: dict[str, ColorPolicy] = {
    "Black on white": ColorPolicy.BLACK,
    "Original colors": ColorPolicy.COLOR,
    "Greyscale": ColorPolicy.MONOCHROME_LIGHT_BG,
}
DEFAULT_COLOR_LABEL = "Black on white"

# Default Windows search roots for the ODA File Converter executable.
_DEFAULT_ODA_BASES = (r"C:\Program Files\ODA", r"C:\Program Files (x86)\ODA")

Log = Callable[[str], None]


class OdaNotFoundError(Exception):
    """Raised when the ODA File Converter executable cannot be located."""


@dataclass
class BatchResult:
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -q`
Expected: 3 passed.

- [ ] **Step 6: Write `tests/conftest.py`** (DXF fixture helper used by later tasks)

```python
from pathlib import Path

import ezdxf
import pytest


def make_dxf(path: Path, *, with_geometry: bool = True) -> Path:
    """Create a minimal valid DXF; optionally with a line + text in model space."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = ezdxf.new("R2018")
    if with_geometry:
        msp = doc.modelspace()
        msp.add_line((0, 0), (100, 100))
        text = msp.add_text("HELLO", dxfattribs={"height": 5})
        text.set_placement((10, 10))
    doc.saveas(str(path))
    return path


@pytest.fixture
def sample_dxf(tmp_path) -> Path:
    return make_dxf(tmp_path / "sample.dxf")
```

- [ ] **Step 7: Commit**

```bash
git add requirements.txt converter.py tests/
git commit -m "feat: scaffold converter constants, enums, datatypes + test fixtures"
```

---

### Task 2: `plan_output_path`

**Files:**
- Modify: `converter.py`
- Test: `tests/test_converter.py`

**Interfaces:**
- Produces: `plan_output_path(src: Path, src_root: Path, output_dir: Path) -> Path` — maps a source file to `output_dir/<src relative to src_root>` with the suffix replaced by `.pdf`.

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path


def test_plan_output_path_nested():
    src = Path("/in/A/B/y.dwg")
    assert converter.plan_output_path(src, Path("/in"), Path("/out")) == Path("/out/A/B/y.pdf")


def test_plan_output_path_root_file():
    src = Path("/in/z.dxf")
    assert converter.plan_output_path(src, Path("/in"), Path("/out")) == Path("/out/z.pdf")


def test_plan_output_path_duplicate_stems_distinct_targets():
    a = converter.plan_output_path(Path("/in/A/x.dwg"), Path("/in"), Path("/out"))
    b = converter.plan_output_path(Path("/in/B/x.dwg"), Path("/in"), Path("/out"))
    assert a == Path("/out/A/x.pdf")
    assert b == Path("/out/B/x.pdf")
    assert a != b


def test_plan_output_path_spaces_and_unicode():
    src = Path("/in/Pro ject/ñ/draw ing.dwg")
    out = converter.plan_output_path(src, Path("/in"), Path("/out"))
    assert out == Path("/out/Pro ject/ñ/draw ing.pdf")


def test_plan_output_path_rename_yields_new_target():
    old = converter.plan_output_path(Path("/in/A/old.dwg"), Path("/in"), Path("/out"))
    new = converter.plan_output_path(Path("/in/A/new.dwg"), Path("/in"), Path("/out"))
    assert old == Path("/out/A/old.pdf")
    assert new == Path("/out/A/new.pdf")
```

- [ ] **Step 2: Run to verify it fails**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k plan_output_path -q`
Expected: FAIL (`AttributeError: module 'converter' has no attribute 'plan_output_path'`).

- [ ] **Step 3: Implement in `converter.py`** (append after datatypes)

```python
def plan_output_path(src: Path, src_root: Path, output_dir: Path) -> Path:
    rel = Path(src).relative_to(src_root)
    return Path(output_dir) / rel.with_suffix(".pdf")
```

- [ ] **Step 4: Run to verify pass**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k plan_output_path -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add converter.py tests/test_converter.py
git commit -m "feat: plan_output_path preserves relative structure"
```

---

### Task 3: `discover_drawings`

**Files:**
- Modify: `converter.py`
- Test: `tests/test_converter.py`

**Interfaces:**
- Produces: `discover_drawings(input_dir: Path) -> tuple[list[Path], list[Path]]` — `(dwg_paths, dxf_paths)` found recursively, case-insensitive on extension, sorted, files only.

- [ ] **Step 1: Write the failing test**

```python
def test_discover_drawings_recursive_and_case_insensitive(tmp_path):
    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "B").mkdir()
    (tmp_path / "top.DWG").write_text("x")
    (tmp_path / "A" / "mid.dwg").write_text("x")
    (tmp_path / "A" / "B" / "deep.Dxf").write_text("x")
    (tmp_path / "A" / "notes.txt").write_text("x")
    dwg, dxf = converter.discover_drawings(tmp_path)
    assert [p.name for p in dwg] == ["mid.dwg", "top.DWG"]
    assert [p.name for p in dxf] == ["deep.Dxf"]


def test_discover_drawings_empty(tmp_path):
    assert converter.discover_drawings(tmp_path) == ([], [])
```

- [ ] **Step 2: Run to verify it fails**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k discover -q`
Expected: FAIL (no attribute `discover_drawings`).

- [ ] **Step 3: Implement in `converter.py`**

```python
def discover_drawings(input_dir: Path) -> tuple[list[Path], list[Path]]:
    input_dir = Path(input_dir)
    dwg: list[Path] = []
    dxf: list[Path] = []
    for p in sorted(input_dir.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext == ".dwg":
            dwg.append(p)
        elif ext == ".dxf":
            dxf.append(p)
    return dwg, dxf
```

- [ ] **Step 4: Run to verify pass**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k discover -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add converter.py tests/test_converter.py
git commit -m "feat: recursive case-insensitive drawing discovery"
```

---

### Task 4: `find_oda`

**Files:**
- Modify: `converter.py`
- Test: `tests/test_converter.py`

**Interfaces:**
- Produces: `find_oda(override: Path | None = None, search_bases: tuple[str, ...] = _DEFAULT_ODA_BASES) -> Path`. Returns override if it exists; else newest `*/ODAFileConverter.exe` under a search base; else raises `OdaNotFoundError`. `search_bases` is injectable so the Windows-only default can be tested on any OS.

- [ ] **Step 1: Write the failing test**

```python
def test_find_oda_override_honored(tmp_path):
    exe = tmp_path / "ODAFileConverter.exe"
    exe.write_text("x")
    assert converter.find_oda(override=exe) == exe


def test_find_oda_override_missing_raises(tmp_path):
    import pytest
    with pytest.raises(converter.OdaNotFoundError):
        converter.find_oda(override=tmp_path / "nope.exe")


def test_find_oda_discovers_newest_under_base(tmp_path):
    (tmp_path / "ODA 22.0").mkdir()
    (tmp_path / "ODA 25.0").mkdir()
    older = tmp_path / "ODA 22.0" / "ODAFileConverter.exe"
    newer = tmp_path / "ODA 25.0" / "ODAFileConverter.exe"
    older.write_text("x")
    newer.write_text("x")
    found = converter.find_oda(search_bases=(str(tmp_path),))
    assert found == newer  # sorted() -> newest name last


def test_find_oda_not_found_raises(tmp_path):
    import pytest
    with pytest.raises(converter.OdaNotFoundError):
        converter.find_oda(search_bases=(str(tmp_path),))
```

- [ ] **Step 2: Run to verify it fails**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k find_oda -q`
Expected: FAIL (no attribute `find_oda`).

- [ ] **Step 3: Implement in `converter.py`**

```python
def find_oda(
    override: Path | None = None,
    search_bases: tuple[str, ...] = _DEFAULT_ODA_BASES,
) -> Path:
    if override is not None:
        p = Path(override)
        if p.exists():
            return p
        raise OdaNotFoundError(f"ODA File Converter not found at: {p}")
    for base in search_bases:
        bp = Path(base)
        if bp.exists():
            hits = sorted(bp.glob("*/ODAFileConverter.exe"))
            if hits:
                return hits[-1]
    raise OdaNotFoundError(
        "ODA File Converter not found. Install the free converter from "
        "opendesign.com, or point the app at ODAFileConverter.exe."
    )
```

- [ ] **Step 4: Run to verify pass**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k find_oda -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add converter.py tests/test_converter.py
git commit -m "feat: find_oda with injectable search bases"
```

---

### Task 5: `pick_render_layout` + `dxf_to_pdf`

**Files:**
- Modify: `converter.py`
- Test: `tests/test_converter.py`

**Interfaces:**
- Produces:
  - `pick_render_layout(doc)` — returns model space if it has entities; else the first non-empty paper-space layout; else model space.
  - `dxf_to_pdf(dxf_path: Path, pdf_path: Path, *, color: ColorPolicy = ColorPolicy.BLACK) -> None` — renders the chosen layout to a fit-to-page A3-landscape PDF on a WHITE background; creates parent dirs; writes bytes.

- [ ] **Step 1: Write the failing test** (uses `make_dxf` from conftest)

```python
import fitz
from ezdxf.addons.drawing.config import ColorPolicy
from conftest import make_dxf


def test_dxf_to_pdf_is_valid_a3_landscape(sample_dxf, tmp_path):
    out = tmp_path / "nested" / "out.pdf"
    converter.dxf_to_pdf(sample_dxf, out, color=ColorPolicy.BLACK)
    assert out.exists()
    data = out.read_bytes()
    assert data[:4] == b"%PDF"
    doc = fitz.open(out)
    assert doc.page_count == 1
    rect = doc[0].rect
    assert rect.width > rect.height  # landscape
    # A3 landscape ~ 1190.5 x 841.9 pt; allow generous tolerance
    assert abs(rect.width - 1190.5) < 5
    assert abs(rect.height - 841.9) < 5


def test_dxf_to_pdf_white_bg_with_black_ink(sample_dxf, tmp_path):
    out = tmp_path / "out.pdf"
    converter.dxf_to_pdf(sample_dxf, out, color=ColorPolicy.BLACK)
    doc = fitz.open(out)
    pix = doc[0].get_pixmap()
    px = pix.samples
    n = pix.n
    white = dark = 0
    for i in range(0, len(px) - n, n):
        r, g, b = px[i], px[i + 1], px[i + 2]
        if r > 240 and g > 240 and b > 240:
            white += 1
        elif r < 40 and g < 40 and b < 40:
            dark += 1
    assert white > 0, "expected a white background"
    assert dark > 0, "expected visible black geometry (not black-on-black)"


def test_dxf_to_pdf_blank_drawing_still_valid(tmp_path):
    blank = make_dxf(tmp_path / "blank.dxf", with_geometry=False)
    out = tmp_path / "blank.pdf"
    converter.dxf_to_pdf(blank, out, color=ColorPolicy.BLACK)
    assert out.read_bytes()[:4] == b"%PDF"
    assert fitz.open(out).page_count == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k dxf_to_pdf -q`
Expected: FAIL (no attribute `pick_render_layout`/`dxf_to_pdf`).

- [ ] **Step 3: Implement in `converter.py`**

```python
def pick_render_layout(doc):
    msp = doc.modelspace()
    if any(True for _ in msp):
        return msp
    for name in doc.layout_names_in_taborder():
        if name == "Model":
            continue
        lay = doc.layout(name)
        if any(True for _ in lay):
            return lay
    return msp


def dxf_to_pdf(
    dxf_path: Path,
    pdf_path: Path,
    *,
    color: ColorPolicy = ColorPolicy.BLACK,
) -> None:
    doc = ezdxf.readfile(str(dxf_path))
    target = pick_render_layout(doc)
    cfg = Configuration().with_changes(
        background_policy=BackgroundPolicy.WHITE,
        color_policy=color,
    )
    backend = PyMuPdfBackend()
    Frontend(RenderContext(doc), backend, config=cfg).draw_layout(target, finalize=True)
    page = layout.Page(
        PAGE_MM[0], PAGE_MM[1], layout.Units.mm, margins=layout.Margins.all(MARGIN_MM)
    )
    pdf_path = Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(backend.get_pdf_bytes(page))
```

- [ ] **Step 4: Run to verify pass**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k "dxf_to_pdf or render_layout" -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add converter.py tests/test_converter.py
git commit -m "feat: dxf_to_pdf renders fit-to-page A3 landscape on white bg"
```

---

### Task 6: `dwg_to_dxf` (ODA subprocess with safe lifecycle)

**Files:**
- Modify: `converter.py`
- Test: `tests/test_converter.py`

**Interfaces:**
- Produces: `dwg_to_dxf(input_dir, temp_dir, oda_exe, *, audit=True, timeout_s=ODA_TIMEOUT_S, poll_interval=1.0, grace_s=10.0, log=lambda m: None, _popen=subprocess.Popen) -> None`. Creates `temp_dir`, builds the exact ODA command, polls for the expected `.dxf` mirror set, requires each file size-stable across two polls, prefers a clean process exit (waits `grace_s`), terminates only on overstay, logs missing files. `_popen` is injectable for tests.

- [ ] **Step 1: Write the failing test** (fake Popen seeds the temp tree)

```python
import subprocess as _sp


class _FakePopen:
    """Mimics ODA: on construction writes the expected .dxf mirror, then 'runs'
    for one poll before exiting, so the size-stable + clean-exit paths execute."""

    def __init__(self, cmd, *a, **k):
        self.cmd = cmd
        in_dir = Path(cmd[1])
        out_dir = Path(cmd[2])
        self._polls = 0
        for dwg in in_dir.rglob("*.dwg"):
            rel = dwg.relative_to(in_dir).with_suffix(".dxf")
            dst = out_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text("DXF-CONTENT")  # stable size across polls
        self._returncode = None

    def poll(self):
        self._polls += 1
        if self._polls >= 2:
            self._returncode = 0
        return self._returncode

    def wait(self, timeout=None):
        self._returncode = 0
        return 0

    def terminate(self):
        self._returncode = -15

    def kill(self):
        self._returncode = -9


def test_dwg_to_dxf_builds_command_and_mirrors(tmp_path):
    in_dir = tmp_path / "in"
    (in_dir / "A").mkdir(parents=True)
    (in_dir / "top.dwg").write_text("x")
    (in_dir / "A" / "deep.dwg").write_text("x")
    temp = tmp_path / "temp"
    captured = {}

    def popen(cmd, *a, **k):
        captured["cmd"] = cmd
        return _FakePopen(cmd, *a, **k)

    converter.dwg_to_dxf(
        in_dir, temp, Path("ODA.exe"), poll_interval=0.0, grace_s=0.0, _popen=popen
    )
    # exact CLI contract
    assert captured["cmd"] == [
        "ODA.exe", str(in_dir), str(temp), "ACAD2018", "DXF", "1", "1", "*.DWG"
    ]
    assert (temp / "top.dxf").exists()
    assert (temp / "A" / "deep.dxf").exists()


def test_dwg_to_dxf_no_dwg_is_noop(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    temp = tmp_path / "temp"
    called = {"n": 0}

    def popen(cmd, *a, **k):
        called["n"] += 1
        return _FakePopen(cmd, *a, **k)

    converter.dwg_to_dxf(in_dir, temp, Path("ODA.exe"), _popen=popen)
    assert called["n"] == 0  # ODA never launched when there are no DWGs
```

- [ ] **Step 2: Run to verify it fails**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k dwg_to_dxf -q`
Expected: FAIL (no attribute `dwg_to_dxf`).

- [ ] **Step 3: Implement in `converter.py`**

```python
def dwg_to_dxf(
    input_dir: Path,
    temp_dir: Path,
    oda_exe: Path,
    *,
    audit: bool = True,
    timeout_s: float = ODA_TIMEOUT_S,
    poll_interval: float = 1.0,
    grace_s: float = 10.0,
    log: Log = lambda m: None,
    _popen=subprocess.Popen,
) -> None:
    input_dir = Path(input_dir)
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    expected = {
        p.relative_to(input_dir).with_suffix(".dxf")
        for p in input_dir.rglob("*")
        if p.is_file() and p.suffix.lower() == ".dwg"
    }
    if not expected:
        return

    cmd = [
        str(oda_exe), str(input_dir), str(temp_dir),
        DXF_VERSION, "DXF", "1", "1" if audit else "0", "*.DWG",
    ]
    log(f"[ODA] converting {len(expected)} DWG -> DXF (recursive) ...")
    proc = _popen(cmd)

    deadline = time.monotonic() + timeout_s
    sizes: dict[Path, int] = {}
    stable: set[Path] = set()
    while time.monotonic() < deadline:
        for p in temp_dir.rglob("*.dxf"):
            rel = p.relative_to(temp_dir)
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if sz > 0 and sizes.get(rel) == sz:
                stable.add(rel)
            sizes[rel] = sz
        if expected.issubset(stable):
            if proc.poll() is None:
                try:
                    proc.wait(timeout=grace_s)
                except subprocess.TimeoutExpired:
                    pass
            break
        if proc.poll() is not None:
            break
        time.sleep(poll_interval)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    made = {p.relative_to(temp_dir) for p in temp_dir.rglob("*.dxf")}
    missing = expected - made
    if missing:
        log(
            f"[ODA] WARNING: {len(missing)} file(s) did not convert: "
            f"{sorted(str(m) for m in missing)}"
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k dwg_to_dxf -q`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add converter.py tests/test_converter.py
git commit -m "feat: dwg_to_dxf ODA invocation with safe process lifecycle"
```

---

### Task 7: `run_batch` (orchestration, collision, skip, log file, guards)

**Files:**
- Modify: `converter.py`
- Test: `tests/test_converter.py`

**Interfaces:**
- Produces: `run_batch(input_dir, output_dir, *, color=ColorPolicy.BLACK, skip_existing=True, oda_override=None, log=lambda m: None, _dwg_to_dxf=dwg_to_dxf, _find_oda=find_oda) -> BatchResult`. Validates dirs (output != input, output not inside input -> `ValueError`), runs ODA for DWGs into `output/_dxf_tmp`, builds a target-keyed render map (DWG over same-folder DXF, shadow logged), renders each (skip-existing honored), tees all logs to `output/dwg2pdf.log`, cleans up temp in `finally`, returns counts. A source whose DXF was never produced is a per-file failure.

- [ ] **Step 1: Write the failing test**

```python
def _seed_temp_dxf(input_dir, temp_dir):
    """Stand-in for ODA: mirror each .dwg to a renderable .dxf in temp_dir."""
    from conftest import make_dxf
    for dwg in Path(input_dir).rglob("*.dwg"):
        rel = dwg.relative_to(input_dir).with_suffix(".dxf")
        make_dxf(Path(temp_dir) / rel)


def test_run_batch_mirrors_tree_and_writes_log(tmp_path):
    in_dir = tmp_path / "in"
    (in_dir / "A" / "B").mkdir(parents=True)
    (in_dir / "top.dwg").write_text("x")
    (in_dir / "A" / "B" / "deep.dwg").write_text("x")
    out_dir = tmp_path / "out"

    def fake_oda(input_dir, temp_dir, oda_exe, **k):
        _seed_temp_dxf(input_dir, temp_dir)

    res = converter.run_batch(
        in_dir, out_dir, _dwg_to_dxf=fake_oda, _find_oda=lambda o: Path("ODA.exe")
    )
    assert (out_dir / "top.pdf").exists()
    assert (out_dir / "A" / "B" / "deep.pdf").exists()
    assert (out_dir / converter.LOG_FILENAME).exists()
    assert res.ok == 2 and res.failed == 0
    assert not (out_dir / converter.TEMP_DIRNAME).exists()  # temp cleaned


def test_run_batch_skip_existing(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "a.dwg").write_text("x")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "a.pdf").write_text("OLD")  # pre-existing

    def fake_oda(input_dir, temp_dir, oda_exe, **k):
        _seed_temp_dxf(input_dir, temp_dir)

    res = converter.run_batch(
        in_dir, out_dir, skip_existing=True,
        _dwg_to_dxf=fake_oda, _find_oda=lambda o: Path("ODA.exe"),
    )
    assert res.skipped == 1 and res.ok == 0
    assert (out_dir / "a.pdf").read_text() == "OLD"  # untouched


def test_run_batch_one_bad_file_does_not_abort(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "good.dwg").write_text("x")
    (in_dir / "bad.dwg").write_text("x")
    out_dir = tmp_path / "out"

    def fake_oda(input_dir, temp_dir, oda_exe, **k):
        from conftest import make_dxf
        make_dxf(Path(temp_dir) / "good.dxf")
        (Path(temp_dir) / "bad.dxf").write_text("NOT A REAL DXF")  # parse fails

    res = converter.run_batch(
        in_dir, out_dir, _dwg_to_dxf=fake_oda, _find_oda=lambda o: Path("ODA.exe")
    )
    assert res.ok == 1 and res.failed == 1
    assert (out_dir / "good.pdf").exists()
    assert any("bad" in rel for rel, _ in res.failures)


def test_run_batch_dwg_shadows_same_stem_dxf(tmp_path):
    from conftest import make_dxf
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "x.dwg").write_text("x")
    make_dxf(in_dir / "x.dxf")  # same stem, same folder -> shadowed
    out_dir = tmp_path / "out"
    logs = []

    def fake_oda(input_dir, temp_dir, oda_exe, **k):
        make_dxf(Path(temp_dir) / "x.dxf")

    res = converter.run_batch(
        in_dir, out_dir, log=logs.append,
        _dwg_to_dxf=fake_oda, _find_oda=lambda o: Path("ODA.exe"),
    )
    assert res.ok == 1  # exactly one PDF for x
    assert (out_dir / "x.pdf").exists()
    assert any("shadow" in m.lower() for m in logs)


def test_run_batch_dxf_only_skips_oda(tmp_path):
    from conftest import make_dxf
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_dxf(in_dir / "only.dxf")
    out_dir = tmp_path / "out"
    called = {"oda": 0}

    def fake_oda(*a, **k):
        called["oda"] += 1

    res = converter.run_batch(
        in_dir, out_dir, _dwg_to_dxf=fake_oda, _find_oda=lambda o: Path("ODA.exe")
    )
    assert called["oda"] == 0  # no DWGs -> ODA not invoked
    assert res.ok == 1 and (out_dir / "only.pdf").exists()


def test_run_batch_rejects_output_inside_input(tmp_path):
    import pytest
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    with pytest.raises(ValueError):
        converter.run_batch(in_dir, in_dir / "sub")


def test_run_batch_rejects_output_equals_input(tmp_path):
    import pytest
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    with pytest.raises(ValueError):
        converter.run_batch(in_dir, in_dir)
```

- [ ] **Step 2: Run to verify it fails**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -k run_batch -q`
Expected: FAIL (no attribute `run_batch`).

- [ ] **Step 3: Implement in `converter.py`**

```python
def run_batch(
    input_dir: Path,
    output_dir: Path,
    *,
    color: ColorPolicy = ColorPolicy.BLACK,
    skip_existing: bool = True,
    oda_override: Path | None = None,
    log: Log = lambda m: None,
    _dwg_to_dxf=dwg_to_dxf,
    _find_oda=find_oda,
) -> BatchResult:
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)

    in_res = input_dir.resolve()
    out_res = output_dir.resolve()
    if in_res == out_res:
        raise ValueError("Output folder must be different from the input folder.")
    if in_res in out_res.parents:
        raise ValueError("Output folder must not be inside the input folder.")

    output_dir.mkdir(parents=True, exist_ok=True)
    result = BatchResult()
    temp_dir = output_dir / TEMP_DIRNAME
    log_fh = open(output_dir / LOG_FILENAME, "a", encoding="utf-8")

    def tee(msg: str) -> None:
        log(msg)
        try:
            log_fh.write(msg + "\n")
            log_fh.flush()
        except Exception:
            pass

    try:
        dwg_files, dxf_files = discover_drawings(input_dir)
        if not dwg_files and not dxf_files:
            tee("No .dwg or .dxf files found in input.")
            return result

        # target pdf path -> {"src": dxf path, "rel": relpath label}
        targets: dict[Path, dict[str, object]] = {}

        if dwg_files:
            exe = _find_oda(oda_override)
            tee(f"[ODA] using {exe}")
            _dwg_to_dxf(input_dir, temp_dir, exe, log=tee)
            for dwg in dwg_files:
                rel = dwg.relative_to(input_dir)
                pdf = plan_output_path(dwg, input_dir, output_dir)
                targets[pdf] = {
                    "src": temp_dir / rel.with_suffix(".dxf"),
                    "rel": str(rel),
                }

        for dxf in dxf_files:
            pdf = plan_output_path(dxf, input_dir, output_dir)
            rel = str(dxf.relative_to(input_dir))
            if pdf in targets:
                tee(f"[skip] {rel} shadowed by same-named DWG")
                continue
            targets[pdf] = {"src": dxf, "rel": rel}

        for pdf, info in sorted(targets.items()):
            rel = info["rel"]
            src = Path(info["src"])
            if skip_existing and pdf.exists():
                result.skipped += 1
                tee(f"[skip] {rel} (PDF exists)")
                continue
            if not src.exists():
                result.failed += 1
                result.failures.append((rel, "DXF not produced by ODA"))
                tee(f"[FAIL] {rel}: DXF not produced by ODA")
                continue
            try:
                dxf_to_pdf(src, pdf, color=color)
                result.ok += 1
                tee(f"[ok]   {rel} -> {pdf.name}")
            except Exception as e:  # one bad file must not stop the batch
                result.failed += 1
                result.failures.append((rel, str(e)))
                tee(f"[FAIL] {rel}: {e}")

        tee(
            f"\nDone. {result.ok} converted, {result.skipped} skipped, "
            f"{result.failed} failed -> {output_dir}"
        )
        return result
    finally:
        log_fh.close()
        shutil.rmtree(temp_dir, ignore_errors=True)
```

- [ ] **Step 4: Run to verify pass**

Run: `. .venv/bin/activate && python -m pytest tests/test_converter.py -q`
Expected: all tests pass (full suite green).

- [ ] **Step 5: Independent test review (project policy)**

Dispatch an independent subagent to review `tests/test_converter.py` for a pareto-optimal real-world scenario set (e.g., a rename across runs, mixed dwg+dxf in different folders, a DXF that ODA failed to produce). Apply any missing cases it identifies, re-run `python -m pytest -q`, confirm green.

- [ ] **Step 6: Commit**

```bash
git add converter.py tests/test_converter.py
git commit -m "feat: run_batch orchestration with collision handling, skip, logging, guards"
```

---

### Task 8: tkinter GUI (`app.py`)

**Files:**
- Create: `app.py`
- Test: manual + `py_compile`

**Interfaces:**
- Consumes: `converter.run_batch`, `converter.COLOR_CHOICES`, `converter.DEFAULT_COLOR_LABEL`, `converter.OdaNotFoundError`, `converter.CRASH_FILENAME`.
- Produces: `main()` entry point; `App(tk.Tk)` window.

- [ ] **Step 1: Write `app.py`**

```python
from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

import converter
from converter import OdaNotFoundError


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DWG -> PDF (A3 landscape)")
        self.geometry("720x500")
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._build()
        self.after(100, self._drain)

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="Input folder:").grid(row=0, column=0, sticky="w")
        self.in_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.in_var).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse...", command=self._pick_in).grid(row=0, column=2)
        ttk.Label(top, text="Output folder:").grid(row=1, column=0, sticky="w", pady=4)
        self.out_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.out_var).grid(row=1, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse...", command=self._pick_out).grid(row=1, column=2)
        top.columnconfigure(1, weight=1)

        opt = ttk.Frame(self)
        opt.pack(fill="x", padx=8, pady=4)
        ttk.Label(opt, text="Color:").pack(side="left")
        self.color_var = tk.StringVar(value=converter.DEFAULT_COLOR_LABEL)
        ttk.Combobox(
            opt, textvariable=self.color_var, state="readonly",
            values=list(converter.COLOR_CHOICES), width=18,
        ).pack(side="left", padx=6)
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Skip existing PDFs", variable=self.skip_var).pack(
            side="left", padx=12
        )
        self.run_btn = ttk.Button(opt, text="Run", command=self._run)
        self.run_btn.pack(side="right")

        self.log_widget = tk.Text(self, wrap="word", state="disabled", height=20)
        self.log_widget.pack(fill="both", expand=True, padx=8, pady=4)
        self.status = ttk.Label(self, text="Ready", anchor="w", relief="sunken")
        self.status.pack(fill="x", padx=8, pady=(0, 6))

    def _pick_in(self) -> None:
        d = filedialog.askdirectory(title="Select input folder")
        if d:
            self.in_var.set(d)

    def _pick_out(self) -> None:
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.out_var.set(d)

    def _append(self, msg: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", msg + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "done":
                    self.run_btn.configure(state="normal")
                    self.status.configure(text=payload)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _run(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        inp = self.in_var.get().strip()
        out = self.out_var.get().strip()
        if not inp or not Path(inp).is_dir():
            self._append("[error] Please choose a valid input folder.")
            return
        if not out:
            self._append("[error] Please choose an output folder.")
            return
        color = converter.COLOR_CHOICES[self.color_var.get()]
        skip = self.skip_var.get()
        self.run_btn.configure(state="disabled")
        self.status.configure(text="Working...")
        self._append(f"Input:  {inp}")
        self._append(f"Output: {out}")
        self._worker = threading.Thread(
            target=self._work, args=(inp, out, color, skip), daemon=True
        )
        self._worker.start()

    def _work(self, inp: str, out: str, color, skip: bool) -> None:
        def log(m: str) -> None:
            self._q.put(("log", m))

        try:
            res = converter.run_batch(
                Path(inp), Path(out), color=color, skip_existing=skip, log=log
            )
            self._q.put((
                "done",
                f"Done. {res.ok} converted, {res.skipped} skipped, {res.failed} failed.",
            ))
        except OdaNotFoundError as e:
            self._q.put(("log", f"[error] {e}"))
            self._q.put(("done", "ODA File Converter not found."))
        except ValueError as e:
            self._q.put(("log", f"[error] {e}"))
            self._q.put(("done", "Invalid folder selection."))
        except Exception as e:
            self._q.put(("log", f"[error] {e}"))
            self._q.put(("done", "Failed - see log."))


def main() -> None:
    try:
        App().mainloop()
    except Exception:
        exe_dir = Path(sys.argv[0]).resolve().parent
        try:
            (exe_dir / converter.CRASH_FILENAME).write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Syntax/compile check**

Run: `. .venv/bin/activate && python -m py_compile app.py && echo OK`
Expected: `OK` (no syntax errors).

- [ ] **Step 3: Manual smoke (best-effort on macOS)**

Run: `. .venv/bin/activate && python app.py` (if a display is available). Pick a small input tree with a couple `.dwg` files and an output folder; confirm the log streams, the tally appears, the output tree mirrors input, and PDFs open. If no display/ODA on this host, defer to the Windows release smoke test (Task 9 / spec section 7). Note the result.

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat: minimal tkinter GUI with threaded run_batch + crash log"
```

---

### Task 9: CI build (`build.yml`), README, finalize

**Files:**
- Create: `.github/workflows/build.yml`
- Create: `README.md`

**Interfaces:** none (delivery + docs).

- [ ] **Step 1: Write `.github/workflows/build.yml`**

```yaml
name: build-exe

on:
  push:
    branches: [master]
    tags: ["v*"]
  workflow_dispatch:

jobs:
  build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          pip install pyinstaller pytest

      - name: Run tests
        run: pytest -q

      - name: Build one-file exe
        run: >
          pyinstaller --onefile --windowed --name DWG2PDF
          --collect-all ezdxf
          --collect-all fontTools
          --collect-all pymupdf
          --collect-all PIL
          app.py

      - name: Upload artifact
        uses: actions/upload-artifact@v4
        with:
          name: DWG2PDF-exe
          path: dist/DWG2PDF.exe

      - name: Attach to release
        if: startsWith(github.ref, 'refs/tags/v')
        uses: softprops/action-gh-release@v2
        with:
          files: dist/DWG2PDF.exe
```

- [ ] **Step 2: Write `README.md`**

```markdown
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
```

- [ ] **Step 3: Validate the workflow YAML**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/build.yml')); print('yaml ok')"`
(If PyYAML absent: `pip install pyyaml` first.)
Expected: `yaml ok`.

- [ ] **Step 4: Commit and push**

```bash
git add .github/workflows/build.yml README.md
git commit -m "ci: windows pyinstaller build + docs"
git push
```

- [ ] **Step 5: Verify the GitHub Actions run**

Check the Actions tab on github.com/vanrohan/dwg2pdf-helper. Confirm the
`build-exe` workflow passes tests and uploads the `DWG2PDF-exe` artifact.
Download it and run the Windows + ODA smoke test from spec section 7 (a small
nested tree with at least one `.dwg`): confirm the mirrored output tree and a
readable A3 PDF.

---

## Self-Review

- **Spec coverage:** self-contained exe (Task 9), DWG+nested -> PDF (Tasks 5-7),
  structure mirrored (Tasks 2, 7), ODA installed/external (Tasks 4, 6, README),
  minimal GUI (Task 8), A3 landscape fixed (Task 5 + Global Constraints),
  GitHub Actions build (Task 9), GUI = folders + color + skip (Task 8). White
  background regression guarded (Task 5). dwg+dxf collision, skip-existing,
  output-inside-input guard, log file, crash log (Tasks 7, 8). Pillow dep
  (Task 1, 9). All covered.
- **Placeholders:** none — every code step contains full code.
- **Type consistency:** `run_batch`/`dwg_to_dxf`/`find_oda` signatures and the
  `_dwg_to_dxf`/`_find_oda`/`_popen` injection seams match across tasks;
  `BatchResult` fields match Task 1; `COLOR_CHOICES` keys match Task 8 combobox.
```
