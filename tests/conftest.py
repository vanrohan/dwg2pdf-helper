import zipfile
from pathlib import Path

import ezdxf
import pytest


def make_min_xps(path: Path, *, width: float = 1122.56, height: float = 793.76) -> Path:
    """Build a minimal valid XPS package (the format inside an XPS-based DWFx),
    with one FixedPage containing a filled rectangle. PyMuPDF opens this as 'xps'.
    Default size is A3-landscape in XPS units (1/96 inch)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="fdseq" ContentType="application/vnd.ms-package.xps-fixeddocumentsequence+xml"/>'
            '<Default Extension="fdoc" ContentType="application/vnd.ms-package.xps-fixeddocument+xml"/>'
            '<Default Extension="fpage" ContentType="application/vnd.ms-package.xps-fixedpage+xml"/>'
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
            '<Path Fill="#FF000000" Data="M 100,100 L 500,100 500,400 100,400 Z"/>'
            "</FixedPage>"
        ),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
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
