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
