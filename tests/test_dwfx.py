from pathlib import Path

import fitz
import pytest

import autocad_dwf
import dwfx
from conftest import make_fake_binary_dwf, make_min_xps


# --- classify ---

def test_classify_xps(tmp_path):
    assert dwfx.classify(make_min_xps(tmp_path / "a.dwfx")) == "xps"


def test_classify_binary_dwf(tmp_path):
    assert dwfx.classify(make_fake_binary_dwf(tmp_path / "b.dwfx")) == "dwf"


def test_classify_unknown_non_zip(tmp_path):
    p = tmp_path / "c.dwfx"
    p.write_text("not a zip")
    assert dwfx.classify(p) == "unknown"


# --- discover ---

def test_discover_recursive_both_extensions(tmp_path):
    (tmp_path / "A").mkdir()
    make_min_xps(tmp_path / "top.dwfx")
    make_fake_binary_dwf(tmp_path / "A" / "deep.DWF")
    (tmp_path / "A" / "note.txt").write_text("x")
    found = [p.name for p in dwfx.discover(tmp_path)]
    assert found == ["deep.DWF", "top.dwfx"]


# --- plan_output_path ---

def test_plan_output_path_mirrors_structure():
    src = Path("/in/A/B/draw.dwfx")
    assert dwfx.plan_output_path(src, Path("/in"), Path("/out")) == Path("/out/A/B/draw.pdf")


# --- xps_to_pdf ---

def test_xps_to_pdf_valid_and_landscape(tmp_path):
    src = make_min_xps(tmp_path / "s.dwfx")
    out = tmp_path / "nested" / "s.pdf"
    pages = dwfx.xps_to_pdf(src, out)
    assert pages == 1
    data = out.read_bytes()
    assert data[:4] == b"%PDF"
    doc = fitz.open(out)
    r = doc[0].rect
    assert r.width > r.height  # landscape preserved from the XPS page size


# --- run_batch: XPS path (fully testable on any OS) ---

def test_run_batch_converts_xps_and_mirrors(tmp_path):
    in_dir = tmp_path / "in"
    (in_dir / "A").mkdir(parents=True)
    make_min_xps(in_dir / "top.dwfx")
    make_min_xps(in_dir / "A" / "deep.dwfx")
    out_dir = tmp_path / "out"
    res = dwfx.run_batch(in_dir, out_dir)
    assert (out_dir / "top.pdf").exists()
    assert (out_dir / "A" / "deep.pdf").exists()
    assert (out_dir / dwfx.LOG_FILENAME).exists()
    assert res.ok == 2 and res.failed == 0 and res.binary_pending == 0


def test_run_batch_skip_existing(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "a.dwfx")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "a.pdf").write_text("OLD")
    res = dwfx.run_batch(in_dir, out_dir, skip_existing=True)
    assert res.skipped == 1 and res.ok == 0
    assert (out_dir / "a.pdf").read_text() == "OLD"


def test_run_batch_binary_reported_when_no_autocad(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_fake_binary_dwf(in_dir / "bin.dwfx")
    make_min_xps(in_dir / "good.dwfx")
    out_dir = tmp_path / "out"
    res = dwfx.run_batch(in_dir, out_dir)  # no _autocad_batch -> binary just reported
    assert res.ok == 1  # the xps one
    assert res.binary_pending == 1
    assert (out_dir / "good.pdf").exists()
    report = out_dir / dwfx.BINARY_REPORT_FILENAME
    assert report.exists() and "bin.dwfx" in report.read_text()


def test_run_batch_binary_converted_via_autocad_hook(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_fake_binary_dwf(in_dir / "bin.dwfx")
    out_dir = tmp_path / "out"

    def fake_autocad(jobs, *, config, log):
        # emulate AutoCAD+clawPDF producing each target PDF
        out = {}
        for src, target in jobs:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_bytes(b"%PDF-1.7\n")
            out[src] = True
        return out

    res = dwfx.run_batch(in_dir, out_dir, _autocad_batch=fake_autocad)
    assert res.ok == 1 and res.failed == 0
    assert (out_dir / "bin.pdf").read_bytes()[:4] == b"%PDF"


def test_run_batch_unknown_is_failure(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    (in_dir / "junk.dwfx").write_text("not a zip")
    out_dir = tmp_path / "out"
    res = dwfx.run_batch(in_dir, out_dir)
    assert res.failed == 1 and res.ok == 0


def test_run_batch_rejects_output_inside_input(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    with pytest.raises(ValueError):
        dwfx.run_batch(in_dir, in_dir / "sub")


# --- autocad driver helpers (no AutoCAD needed) ---

def test_find_acad_none_when_absent(tmp_path):
    assert autocad_dwf.find_acad(search_bases=(str(tmp_path),)) is None


def test_build_script_contains_each_file_and_plot(tmp_path):
    jobs = [(Path(r"C:\in\a.dwfx"), Path(r"C:\out\a.pdf")),
            (Path(r"C:\in\b.dwfx"), Path(r"C:\out\b.pdf"))]
    cfg = autocad_dwf.AutoCadConfig(clawpdf_output_dir=tmp_path)
    scr = autocad_dwf.build_script(jobs, cfg, tmp_path)
    assert "-DWFATTACH" in scr
    assert r"C:\in\a.dwfx" in scr and r"C:\in\b.dwfx" in scr
    assert scr.count("-PLOT") == 2
    assert "clawPDF" in scr
    assert scr.strip().endswith("Y")  # QUIT confirmation


def test_convert_batch_noop_off_windows(tmp_path):
    # On non-Windows this must not launch anything and must report all-False.
    jobs = [(tmp_path / "a.dwfx", tmp_path / "a.pdf")]
    logs = []
    res = autocad_dwf.convert_batch(jobs, config=autocad_dwf.AutoCadConfig(), log=logs.append)
    assert res == {tmp_path / "a.dwfx": False}
