import html
import zipfile
from pathlib import Path

import ezdxf
import pytest


def _red_png() -> bytes:
    """A 50x50 solid-red PNG, embedded so image-rendering paths have a real raster."""
    import fitz

    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 50), False)
    pm.set_rect(pm.irect, (255, 0, 0))
    return pm.tobytes("png")


def _glyph_runs(text: str) -> str:
    """Mimic AutoCAD ePlot: one positioned <Glyphs> run per character, so words
    only reappear when the per-character UnicodeString runs are concatenated."""
    return "".join(
        f'<Glyphs UnicodeString="{html.escape(c, quote=True)}"/>' for c in text
    )


def make_min_xps(path: Path, *, width: float = 1122.56, height: float = 793.76,
                 background: str | None = None, text: str | None = None,
                 image: bool = False) -> Path:
    """Build a minimal valid XPS package (the format inside an XPS-based DWFx),
    with one FixedPage containing a filled rectangle. PyMuPDF opens this as 'xps'.
    Default size is A3-landscape in XPS units (1/96 inch).

    background, when given (e.g. "#ededd6"), prepends a full-page paper rectangle
    starting at the origin - mirroring the non-white 'paper' fill AutoCAD writes as
    the first Path of every real sheet.

    text, when given, adds it as per-character <Glyphs> runs (like AutoCAD), so
    annotation-text extraction can be exercised.

    image, when True, paints a real embedded PNG via an ImageBrush (red square) - the
    raster case has_raster_image detects and xps_to_pdf renders page-by-page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    bg = (
        f'<Path Fill="{background}" Data="M 0, 0 L {width},0 {width},{height} 0,{height} z"/>'
        if background
        else ""
    )
    glyphs = _glyph_runs(text) if text else ""
    img = (
        '<Path Data="M 100,500 L 400,500 400,700 100,700 z"><Path.Fill>'
        '<ImageBrush ImageSource="/raster.png" Viewbox="0,0,50,50" ViewboxUnits="Absolute" '
        'Viewport="100,500,300,200" ViewportUnits="Absolute" TileMode="None"/>'
        "</Path.Fill></Path>"
        if image
        else ""
    )
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="fdseq" ContentType="application/vnd.ms-package.xps-fixeddocumentsequence+xml"/>'
            '<Default Extension="fdoc" ContentType="application/vnd.ms-package.xps-fixeddocument+xml"/>'
            '<Default Extension="fpage" ContentType="application/vnd.ms-package.xps-fixedpage+xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.microsoft.com/xps/2005/06/fixedrepresentation" Target="/FixedDocumentSequence.fdseq"/>'
            "</Relationships>"
        ),
        "FixedDocumentSequence.fdseq": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<FixedDocumentSequence xmlns="http://schemas.microsoft.com/xps/2005/06">'
            '<DocumentReference Source="/Documents/1/FixedDocument.fdoc"/>'
            "</FixedDocumentSequence>"
        ),
        "Documents/1/FixedDocument.fdoc": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<FixedDocument xmlns="http://schemas.microsoft.com/xps/2005/06">'
            '<PageContent Source="Pages/1.fpage"/>'
            "</FixedDocument>"
        ),
        "Documents/1/Pages/1.fpage": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<FixedPage Width="{width}" Height="{height}" '
            'xmlns="http://schemas.microsoft.com/xps/2005/06" xml:lang="en-US">'
            f'{bg}'
            '<Path Fill="#FF000000" Data="M 100,100 L 500,100 500,400 100,400 Z"/>'
            f'{glyphs}'
            f'{img}'
            "</FixedPage>"
        ),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
        if image:
            z.writestr("raster.png", _red_png())
    return path


def make_multi_xps(path: Path, *, pages: int = 2,
                   width: float = 1122.56, height: float = 793.76,
                   background: str | None = None,
                   text_on_last: str | None = None,
                   image_on_last: bool = False) -> Path:
    """Like make_min_xps but with N FixedPages -> an N-page PDF after conversion.
    background, when given, prepends a full-page paper rectangle to every sheet.
    text_on_last, when given, puts that text on the LAST sheet only - so tests can
    prove keyword detection scans every sheet, not just the first.
    image_on_last, when True, embeds a red PNG via ImageBrush on the LAST sheet only -
    so tests can prove the raster route keeps every sheet when one carries an image."""
    path.parent.mkdir(parents=True, exist_ok=True)
    bg = (
        f'<Path Fill="{background}" Data="M 0, 0 L {width},0 {width},{height} 0,{height} z"/>'
        if background
        else ""
    )
    img = (
        '<Path Data="M 100,500 L 400,500 400,700 100,700 z"><Path.Fill>'
        '<ImageBrush ImageSource="/raster.png" Viewbox="0,0,50,50" ViewboxUnits="Absolute" '
        'Viewport="100,500,300,200" ViewportUnits="Absolute" TileMode="None"/>'
        "</Path.Fill></Path>"
    )
    page_refs = "".join(
        f'<PageContent Source="Pages/{i}.fpage"/>' for i in range(1, pages + 1)
    )
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="fdseq" ContentType="application/vnd.ms-package.xps-fixeddocumentsequence+xml"/>'
            '<Default Extension="fdoc" ContentType="application/vnd.ms-package.xps-fixeddocument+xml"/>'
            '<Default Extension="fpage" ContentType="application/vnd.ms-package.xps-fixedpage+xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            "</Types>"
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.microsoft.com/xps/2005/06/fixedrepresentation" Target="/FixedDocumentSequence.fdseq"/>'
            "</Relationships>"
        ),
        "FixedDocumentSequence.fdseq": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<FixedDocumentSequence xmlns="http://schemas.microsoft.com/xps/2005/06">'
            '<DocumentReference Source="/Documents/1/FixedDocument.fdoc"/>'
            "</FixedDocumentSequence>"
        ),
        "Documents/1/FixedDocument.fdoc": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<FixedDocument xmlns="http://schemas.microsoft.com/xps/2005/06">'
            f'{page_refs}'
            "</FixedDocument>"
        ),
    }
    for i in range(1, pages + 1):
        glyphs = _glyph_runs(text_on_last) if (text_on_last and i == pages) else ""
        img_markup = img if (image_on_last and i == pages) else ""
        parts[f"Documents/1/Pages/{i}.fpage"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<FixedPage Width="{width}" Height="{height}" '
            'xmlns="http://schemas.microsoft.com/xps/2005/06" xml:lang="en-US">'
            f'{bg}'
            '<Path Fill="#FF000000" Data="M 100,100 L 500,100 500,400 100,400 Z"/>'
            f'{glyphs}'
            f'{img_markup}'
            "</FixedPage>"
        )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
        if image_on_last:
            z.writestr("raster.png", _red_png())
    return path


def make_fake_binary_dwf(path: Path) -> Path:
    """Build a ZIP package that looks like a binary DWF6 (contains a .w2d stream).
    Enough for classify() to detect 'dwf'; not a renderable WHIP stream."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.xml", '<dwf:Manifest xmlns:dwf="DWF-Manifest:6.0" version="6.0"/>')
        z.writestr("section/F0.w2d", b"(W2D V06.00)\x00\x00fake")
    return path


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
