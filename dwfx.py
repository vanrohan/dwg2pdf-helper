"""DWFx/DWF -> PDF batch conversion, preserving folder structure.

Two source formats hide behind the .dwfx (and .dwf) extension:

  * XPS-based DWFx  -> converted directly by PyMuPDF (vector, small, self-contained).
  * Binary DWF 6.x  -> has no free library; rendered by driving AutoCAD to attach
                       the file as an underlay and plot to a GDI PDF printer
                       (clawPDF). See autocad_dwf.py. AutoCAD/clawPDF must be set
                       up on the machine; otherwise these files are reported, not
                       silently dropped.

classify(), discover(), plan_output_path() and xps_to_pdf() are pure and run on
any OS. run_batch() orchestrates and only touches AutoCAD for binary files.
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import fitz  # PyMuPDF

LOG_FILENAME = "dwfx2pdf.log"
CRASH_FILENAME = "dwfx2pdf-crash.log"
BINARY_REPORT_FILENAME = "binary-dwf-needs-autocad.txt"
SOURCE_EXTS = (".dwfx", ".dwf")

Log = Callable[[str], None]


@dataclass
class BatchResult:
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    binary_pending: int = 0  # binary DWFs that need AutoCAD and were not converted
    failures: list[tuple[str, str]] = field(default_factory=list)


def classify(path: Path) -> str:
    """Return 'xps', 'dwf', or 'unknown' by inspecting the package contents.

    XPS DWFx contains FixedPage/.fdseq parts; binary DWF6 contains .w2d WHIP
    streams. Both are ZIP/OPC packages.
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = [n.lower() for n in z.namelist()]
    except (zipfile.BadZipFile, OSError):
        return "unknown"
    if any(n.endswith(".fpage") or n.endswith(".fdseq") for n in names):
        return "xps"
    if any(n.endswith(".w2d") for n in names):
        return "dwf"
    return "unknown"


# Filename tags derived from a drawing's text. Each rule is (suffix, terms); a sheet
# whose annotation text contains any term gets that suffix. A sheet matching several
# rules collects every suffix, in this order (e.g. "_assy_weld").
SUFFIX_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("_assy", ("assembly", "assy")),
    ("_weld", ("weldment",)),
)

_UNICODE_STRING_RE = re.compile(r'UnicodeString="([^"]*)"')


def annotation_text(path: Path) -> str:
    """All text drawn on an XPS DWFx, normalized for keyword matching: every sheet's
    per-character <Glyphs> runs are concatenated, XML entities decoded, whitespace
    removed and the result lowercased. AutoCAD emits each glyph as its own positioned
    run, so a word like "ASSEMBLY" only reappears once the runs are joined. Returns ""
    for binary DWF, non-XPS, or unreadable packages."""
    try:
        with zipfile.ZipFile(path) as z:
            runs: list[str] = []
            for name in z.namelist():
                if name.lower().endswith(".fpage"):
                    xml = z.read(name).decode("utf-8", "replace")
                    runs.extend(_UNICODE_STRING_RE.findall(xml))
    except (zipfile.BadZipFile, OSError):
        return ""
    return re.sub(r"\s+", "", html.unescape("".join(runs))).lower()


def suffix_for(text: str) -> str:
    """Concatenated filename suffix(es) for already-normalized (lowercase, no
    whitespace) annotation text - "_assy", "_weld", "_assy_weld", or "" for none."""
    return "".join(
        suffix for suffix, terms in SUFFIX_RULES if any(t in text for t in terms)
    )


def discover(input_dir: Path) -> list[Path]:
    """All .dwfx/.dwf files under input_dir, recursive, case-insensitive, sorted."""
    input_dir = Path(input_dir)
    return [
        p
        for p in sorted(input_dir.rglob("*"))
        if p.is_file() and p.suffix.lower() in SOURCE_EXTS
    ]


def plan_output_path(src: Path, src_root: Path, output_dir: Path) -> Path:
    rel = Path(src).relative_to(src_root)
    return Path(output_dir) / rel.with_suffix(".pdf")


# AutoCAD writes the drawing's "paper" colour as the first <Path> of every sheet:
# a full-page rectangle whose Data starts at the origin (e.g. Fill="#ededd6", a pale
# cream). Recolouring just that rectangle to white drops the tinted background while
# leaving every other element untouched.
_FIRST_PATH_RE = re.compile(r'(<FixedPage\b[^>]*>\s*)(<Path\b[^>]*?/>)')
_ORIGIN_DATA_RE = re.compile(r'\bData="M\s*0\s*,\s*0\b')
_FILL_RE = re.compile(r'\bFill="(#[0-9a-fA-F]{6,8})"')


def _whiten_page_background(fpage_xml: str) -> str:
    """Recolour a sheet's paper rectangle to white. No-op unless the FixedPage's
    first <Path> is a full-page fill rooted at the origin, so real geometry (even a
    leading filled rectangle that isn't the page background) is never touched."""
    m = _FIRST_PATH_RE.search(fpage_xml)
    if not m:
        return fpage_xml
    path_el = m.group(2)
    if not _ORIGIN_DATA_RE.search(path_el):
        return fpage_xml
    fill = _FILL_RE.search(path_el)
    if not fill:
        return fpage_xml
    color = fill.group(1)
    if color.lower() in ("#ffffff", "#ffffffff"):
        return fpage_xml
    white = "#ffffffff" if len(color) == 9 else "#ffffff"
    new_path = path_el.replace(f'Fill="{color}"', f'Fill="{white}"', 1)
    return fpage_xml[: m.start(2)] + new_path + fpage_xml[m.end(2):]


def _xps_with_white_background(src: Path) -> bytes:
    """Return the XPS/DWFx package bytes with every sheet's paper rectangle set to
    white. Only the .fpage parts are rewritten; all other parts are copied verbatim."""
    with zipfile.ZipFile(src) as zin:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename.lower().endswith(".fpage"):
                    data = _whiten_page_background(data.decode("utf-8")).encode("utf-8")
                zout.writestr(item, data)
    return buf.getvalue()


def xps_to_pdf(src: Path, dst: Path, *, white_background: bool = True) -> int:
    """Convert an XPS-based DWFx to PDF, preserving native page sizes. Returns the
    page count. A multi-sheet DWFx becomes a multi-page PDF.

    white_background recolours AutoCAD's tinted paper fill (commonly #ededd6 cream)
    to white before conversion, so line work prints on white instead of a coloured
    background. Set False to keep the drawing's original paper colour."""
    if white_background:
        doc = fitz.open(stream=_xps_with_white_background(src), filetype="xps")
    else:
        doc = fitz.open(str(src), filetype="xps")
    try:
        pdf_bytes = doc.convert_to_pdf()
    finally:
        doc.close()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(pdf_bytes)
    return fitz.open("pdf", pdf_bytes).page_count


def _validate_dirs(input_dir: Path, output_dir: Path) -> None:
    in_res = input_dir.resolve()
    out_res = output_dir.resolve()
    if in_res == out_res:
        raise ValueError("Output folder must be different from the input folder.")
    if in_res in out_res.parents:
        raise ValueError("Output folder must not be inside the input folder.")


def run_batch(
    input_dir: Path,
    output_dir: Path,
    *,
    skip_existing: bool = True,
    white_background: bool = True,
    tag_from_text: bool = True,
    log: Log = lambda m: None,
    autocad_config: "object | None" = None,
    _convert_xps: Callable[..., int] = xps_to_pdf,
    _autocad_batch: "Callable[..., dict] | None" = None,
) -> BatchResult:
    """Convert every .dwfx/.dwf under input_dir into output_dir, mirroring the tree.

    XPS DWFx are converted in-process via PyMuPDF. Binary DWF6 files are handed to
    the AutoCAD driver when one is available (Windows + AutoCAD + clawPDF); when it
    is not, they are listed in a report file and counted as binary_pending rather
    than failed, so the user knows exactly which files still need the AutoCAD route.
    """
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    _validate_dirs(input_dir, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    result = BatchResult()
    log_fh = open(output_dir / LOG_FILENAME, "a", encoding="utf-8")

    def tee(msg: str) -> None:
        log(msg)
        try:
            log_fh.write(msg + "\n")
            log_fh.flush()
        except Exception:
            pass

    try:
        files = discover(input_dir)
        if not files:
            tee("No .dwfx or .dwf files found in input.")
            return result

        binary_jobs: list[tuple[Path, Path, str]] = []  # (src, target_pdf, rel)
        for src in files:
            rel = str(src.relative_to(input_dir))
            kind = classify(src)
            target = plan_output_path(src, input_dir, output_dir)
            if kind == "xps" and tag_from_text:
                suffix = suffix_for(annotation_text(src))
                if suffix:
                    target = target.with_name(f"{target.stem}{suffix}{target.suffix}")
            if skip_existing and target.exists():
                result.skipped += 1
                tee(f"[skip] {rel} (PDF exists)")
                continue
            if kind == "xps":
                try:
                    pages = _convert_xps(src, target, white_background=white_background)
                    result.ok += 1
                    tee(f"[ok]   {rel} -> {target.name} ({pages} page(s))")
                except Exception as e:
                    result.failed += 1
                    result.failures.append((rel, str(e)))
                    tee(f"[FAIL] {rel}: {e}")
            elif kind == "dwf":
                binary_jobs.append((src, target, rel))
            else:
                result.failed += 1
                result.failures.append((rel, "unrecognized (not XPS DWFx or binary DWF)"))
                tee(f"[FAIL] {rel}: unrecognized package (not XPS DWFx or binary DWF)")

        if binary_jobs:
            if _autocad_batch is not None:
                outcomes = _autocad_batch(
                    [(s, t) for s, t, _ in binary_jobs],
                    config=autocad_config,
                    log=tee,
                )
                for src, target, rel in binary_jobs:
                    if outcomes.get(src) and target.exists():
                        result.ok += 1
                        tee(f"[ok]   {rel} -> {target.name} (AutoCAD)")
                    else:
                        result.failed += 1
                        result.failures.append((rel, "AutoCAD did not produce a PDF"))
                        tee(f"[FAIL] {rel}: AutoCAD did not produce a PDF")
            else:
                report = output_dir / BINARY_REPORT_FILENAME
                with open(report, "w", encoding="utf-8") as fh:
                    fh.write("Binary DWF files - convert manually:\n\n")
                    for _, _, rel in binary_jobs:
                        fh.write(rel + "\n")
                result.binary_pending += len(binary_jobs)
                tee("")
                tee(f"[manual] {len(binary_jobs)} binary DWF - convert manually (see {report.name}):")
                for _, _, rel in binary_jobs:
                    tee(f"    - {rel}")

        tee(
            f"\nDone. {result.ok} converted, {result.skipped} skipped, "
            f"{result.failed} failed, {result.binary_pending} binary pending "
            f"-> {output_dir}"
        )
        return result
    finally:
        log_fh.close()
