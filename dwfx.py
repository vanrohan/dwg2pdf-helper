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
from typing import Callable, Iterable

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
# whose annotation text contains any term gets that suffix when the tag is enabled. A
# sheet matching several rules collects every suffix, in this order (e.g. "_assy_weld").
# The suffix doubles as the tag's id - the GUI builds one checkbox per rule from here,
# so adding a rule adds its checkbox automatically.
TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("_assy", ("assembly", "assy")),
    ("_weld", ("weldment",)),
    ("_machine", ("machine",)),
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


def suffix_for(text: str, enabled: "Iterable[str] | None" = None) -> str:
    """Concatenated filename suffix(es) for already-normalized (lowercase, no
    whitespace) annotation text - e.g. "_assy", "_weld", "_assy_machine", or "".
    enabled, when given, limits which tag suffixes may apply (e.g. {"_assy"}); None
    enables every rule."""
    allow = None if enabled is None else set(enabled)
    return "".join(
        suffix
        for suffix, terms in TAG_RULES
        if (allow is None or suffix in allow) and any(t in text for t in terms)
    )


def has_raster_image(path: Path) -> bool:
    """True if an XPS DWFx paints a raster image - an <ImageBrush>/ImageSource (inserted
    photos, shaded/rendered views, scanned underlays). The reference may sit in the
    FixedPage or in a resource-dictionary .xml the page links to, so both are scanned.
    PyMuPDF's vector XPS->PDF device drops some of these; this gates the render-and-
    compare check in xps_to_pdf. Thumbnail/preview PNGs that are not painted carry no
    ImageSource and do not count. False for binary DWF or unreadable packages."""
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith((".fpage", ".xml")):
                    if "ImageSource" in z.read(name).decode("utf-8", "replace"):
                        return True
    except (zipfile.BadZipFile, OSError):
        return False
    return False


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


def _open_xps(src: Path, white_background: bool):
    """Open an XPS DWFx, whitening the paper background first when requested."""
    if white_background:
        return fitz.open(stream=_xps_with_white_background(src), filetype="xps")
    return fitz.open(str(src), filetype="xps")


def _convert_vector(doc) -> tuple[int, bytes]:
    """Translate XPS to PDF as vectors - crisp, small, selectable text. PyMuPDF's
    pdf-write device drops a few element kinds (e.g. raster ImageBrushes), so this is
    used only for drawings with no embedded image."""
    pdf_bytes = doc.convert_to_pdf()
    return fitz.open("pdf", pdf_bytes).page_count, pdf_bytes


def _convert_raster(doc, dpi: int) -> tuple[int, bytes]:
    """Render each sheet to a raster page at dpi and assemble a PDF. Slower and not
    text-selectable, but reproduces everything PyMuPDF can draw - including the raster
    images the vector device drops. Streams are deflated (a 200dpi A3 sheet is ~1-2 MB,
    lossless). Native page sizes are preserved."""
    out = fitz.open()
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            target = out.new_page(width=page.rect.width, height=page.rect.height)
            target.insert_image(target.rect, pixmap=pix)
        return out.page_count, out.tobytes(deflate=True, garbage=3)
    finally:
        out.close()


def _lost_color_px(true_pix, vec_pix) -> int:
    """Count pixels that are clearly coloured in the true render but greyscale in the
    vector render - i.e. an image the vector device flattened to black/grey/white.
    A size mismatch is treated as total loss (forces the raster path)."""
    if (true_pix.width, true_pix.height) != (vec_pix.width, vec_pix.height):
        return true_pix.width * true_pix.height
    t, v, n = true_pix.samples, vec_pix.samples, true_pix.n
    lost = 0
    for i in range(0, len(t), n):
        if max(t[i], t[i + 1], t[i + 2]) - min(t[i], t[i + 1], t[i + 2]) > 40:
            if max(v[i], v[i + 1], v[i + 2]) - min(v[i], v[i + 1], v[i + 2]) < 25:
                lost += 1
    return lost


def _vector_lost_color(doc, vec_pdf_bytes: bytes, check_dpi: int = 50) -> bool:
    """True if the vector PDF lost colour content the page actually has - the reliable
    signal that convert_to_pdf dropped an embedded image. Compares each page's true
    raster render against the vector render at a coarse dpi; >0.2% of a page lost is
    enough (benign images that the vector device handles score ~0)."""
    vec = fitz.open("pdf", vec_pdf_bytes)
    try:
        for i in range(doc.page_count):
            true_pix = doc[i].get_pixmap(dpi=check_dpi)
            vec_pix = vec[i].get_pixmap(dpi=check_dpi)
            if _lost_color_px(true_pix, vec_pix) > 0.002 * true_pix.width * true_pix.height:
                return True
    finally:
        vec.close()
    return False


def xps_to_pdf(src: Path, dst: Path, *, white_background: bool = True,
               raster_dpi: int = 200) -> int:
    """Convert an XPS-based DWFx to PDF, preserving native page sizes. Returns the
    page count. A multi-sheet DWFx becomes a multi-page PDF.

    white_background recolours AutoCAD's tinted paper fill (commonly #ededd6 cream)
    to white before conversion, so line work prints on white instead of a coloured
    background. Set False to keep the drawing's original paper colour.

    Conversion is vector (crisp, small, selectable) by default. If the drawing embeds a
    raster image and the vector device is found to have dropped it (the true render has
    colour the vector PDF lost), the file is re-rendered page-by-page at raster_dpi so
    the image is reproduced. Pure line drawings never pay that cost."""
    doc = _open_xps(src, white_background)
    try:
        pages, pdf_bytes = _convert_vector(doc)
        if has_raster_image(src) and _vector_lost_color(doc, pdf_bytes):
            pages, pdf_bytes = _convert_raster(doc, raster_dpi)
    finally:
        doc.close()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(pdf_bytes)
    return pages


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
    tags: "Iterable[str] | None" = None,
    log: Log = lambda m: None,
    autocad_config: "object | None" = None,
    _convert_xps: Callable[..., int] = xps_to_pdf,
    _autocad_batch: "Callable[..., dict] | None" = None,
) -> BatchResult:
    """Convert every .dwfx/.dwf under input_dir into output_dir, mirroring the tree.

    XPS DWFx are converted in-process via PyMuPDF (image-bearing sheets are rendered;
    if a sheet cannot be converted the file is counted as failed). tags limits which
    filename tags may apply (suffix ids from TAG_RULES, e.g. {"_assy"}); None enables
    all, an empty set disables tagging. Binary DWF6 files are handed to the AutoCAD
    driver when one is available (Windows + AutoCAD + clawPDF); when it is not, they
    are listed in a report file and counted as binary_pending rather than failed.
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
            if kind == "xps":
                suffix = suffix_for(annotation_text(src), enabled=tags)
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
