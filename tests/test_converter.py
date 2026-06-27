from pathlib import Path

import fitz
import pytest
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy

import converter
from conftest import make_dxf


# --- Task 1: constants / enums / datatypes ---

def test_enums_and_color_choices_exist():
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


# --- Task 2: plan_output_path ---

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


# --- Task 3: discover_drawings ---

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


# --- Task 4: find_oda ---

def test_find_oda_override_honored(tmp_path):
    exe = tmp_path / "ODAFileConverter.exe"
    exe.write_text("x")
    assert converter.find_oda(override=exe) == exe


def test_find_oda_override_missing_raises(tmp_path):
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
    with pytest.raises(converter.OdaNotFoundError):
        converter.find_oda(search_bases=(str(tmp_path),))


# --- Task 5: pick_render_layout + dxf_to_pdf ---

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


# --- Task 6: dwg_to_dxf ---

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


# --- Task 7: run_batch ---

def _seed_temp_dxf(input_dir, temp_dir):
    """Stand-in for ODA: mirror each .dwg to a renderable .dxf in temp_dir."""
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
        make_dxf(Path(temp_dir) / "good.dxf")
        (Path(temp_dir) / "bad.dxf").write_text("NOT A REAL DXF")  # parse fails

    res = converter.run_batch(
        in_dir, out_dir, _dwg_to_dxf=fake_oda, _find_oda=lambda o: Path("ODA.exe")
    )
    assert res.ok == 1 and res.failed == 1
    assert (out_dir / "good.pdf").exists()
    assert any("bad" in rel for rel, _ in res.failures)


def test_run_batch_dwg_shadows_same_stem_dxf(tmp_path):
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
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    with pytest.raises(ValueError):
        converter.run_batch(in_dir, in_dir / "sub")


def test_run_batch_rejects_output_equals_input(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    with pytest.raises(ValueError):
        converter.run_batch(in_dir, in_dir)
