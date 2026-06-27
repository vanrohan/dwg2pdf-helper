import zipfile
from pathlib import Path

import fitz
import pytest

import dwfx
from conftest import make_fake_binary_dwf, make_min_xps, make_multi_xps


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


# --- additional real-world scenarios (from test review) ---

def test_run_batch_mixed_tree_one_binary_fails_does_not_abort(tmp_path):
    in_dir = tmp_path / "in"
    (in_dir / "xps").mkdir(parents=True)
    (in_dir / "bin").mkdir(parents=True)
    make_min_xps(in_dir / "xps" / "draw.dwfx")
    make_fake_binary_dwf(in_dir / "bin" / "good.dwfx")
    make_fake_binary_dwf(in_dir / "bin" / "bad.dwfx")
    out_dir = tmp_path / "out"

    def fake_autocad(jobs, *, config, log):
        out = {}
        for src, target in jobs:
            if src.name == "good.dwfx":
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).write_bytes(b"%PDF-1.7\n")
                out[src] = True
            else:
                out[src] = False  # AutoCAD failed on this one
        return out

    res = dwfx.run_batch(in_dir, out_dir, _autocad_batch=fake_autocad)
    assert res.ok == 2  # xps + good binary
    assert res.failed == 1  # bad binary; batch kept going
    assert (out_dir / "xps" / "draw.pdf").exists()
    assert (out_dir / "bin" / "good.pdf").exists()
    assert any("bad.dwfx" in rel for rel, _ in res.failures)


def test_run_batch_rejects_output_equals_input(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    with pytest.raises(ValueError):
        dwfx.run_batch(in_dir, in_dir)


def test_xps_to_pdf_multi_sheet_makes_multipage_pdf(tmp_path):
    src = make_multi_xps(tmp_path / "multi.dwfx", pages=3)
    out = tmp_path / "multi.pdf"
    pages = dwfx.xps_to_pdf(src, out)
    assert pages == 3
    assert fitz.open(out).page_count == 3


def test_run_batch_overwrites_when_skip_existing_false(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "a.dwfx")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "a.pdf").write_text("OLD")
    res = dwfx.run_batch(in_dir, out_dir, skip_existing=False)
    assert res.ok == 1 and res.skipped == 0
    assert (out_dir / "a.pdf").read_bytes()[:4] == b"%PDF"


def test_run_batch_empty_input(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    out_dir = tmp_path / "out"
    res = dwfx.run_batch(in_dir, out_dir)
    assert res.ok == 0 and res.failed == 0 and res.binary_pending == 0
    assert "No .dwfx" in (out_dir / dwfx.LOG_FILENAME).read_text()


def test_classify_zip_without_markers_is_unknown(tmp_path):
    p = tmp_path / "other.dwfx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("hello.txt", "no fpage, fdseq or w2d here")
    assert dwfx.classify(p) == "unknown"
