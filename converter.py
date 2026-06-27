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


def plan_output_path(src: Path, src_root: Path, output_dir: Path) -> Path:
    rel = Path(src).relative_to(src_root)
    return Path(output_dir) / rel.with_suffix(".pdf")


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


def _write_blank_a3(pdf_path: Path) -> None:
    """Write a blank white A3-landscape page. ezdxf's fit-to-page needs a content
    bounding box, so a geometry-less drawing is rendered directly via PyMuPDF."""
    import fitz

    w_pt = PAGE_MM[0] * 72.0 / 25.4
    h_pt = PAGE_MM[1] * 72.0 / 25.4
    doc = fitz.open()
    doc.new_page(width=w_pt, height=h_pt)  # PDF pages default to white
    pdf_path.write_bytes(doc.tobytes())


def dxf_to_pdf(
    dxf_path: Path,
    pdf_path: Path,
    *,
    color: ColorPolicy = ColorPolicy.BLACK,
) -> None:
    doc = ezdxf.readfile(str(dxf_path))
    target = pick_render_layout(doc)
    pdf_path = Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    if not any(True for _ in target):  # no geometry -> blank A3 (avoids empty-bbox)
        _write_blank_a3(pdf_path)
        return

    cfg = Configuration().with_changes(
        background_policy=BackgroundPolicy.WHITE,
        color_policy=color,
    )
    backend = PyMuPdfBackend()
    Frontend(RenderContext(doc), backend, config=cfg).draw_layout(target, finalize=True)
    page = layout.Page(
        PAGE_MM[0], PAGE_MM[1], layout.Units.mm, margins=layout.Margins.all(MARGIN_MM)
    )
    pdf_path.write_bytes(backend.get_pdf_bytes(page))


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

        # target pdf path -> (source dxf path, relpath label for logging)
        targets: dict[Path, tuple[Path, str]] = {}

        if dwg_files:
            exe = _find_oda(oda_override)
            tee(f"[ODA] using {exe}")
            _dwg_to_dxf(input_dir, temp_dir, exe, log=tee)
            for dwg in dwg_files:
                rel = dwg.relative_to(input_dir)
                pdf = plan_output_path(dwg, input_dir, output_dir)
                targets[pdf] = (temp_dir / rel.with_suffix(".dxf"), str(rel))

        for dxf in dxf_files:
            pdf = plan_output_path(dxf, input_dir, output_dir)
            rel = str(dxf.relative_to(input_dir))
            if pdf in targets:
                tee(f"[skip] {rel} shadowed by same-named DWG")
                continue
            targets[pdf] = (dxf, rel)

        for pdf, (src, rel) in sorted(targets.items()):
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
