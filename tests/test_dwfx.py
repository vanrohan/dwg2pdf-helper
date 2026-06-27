import zipfile
from pathlib import Path

import fitz
import pytest

import dwfx
from conftest import make_fake_binary_dwf, make_min_xps, make_multi_xps

# Real-world confidential drawings live in dwfx-samples/ (gitignored, never committed).
# Tests that use them run locally and skip everywhere the file is absent (e.g. CI).
SAMPLES = Path(__file__).resolve().parent.parent / "dwfx-samples"


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


# --- white background ---

def _corner_rgb(pdf_path, x=4, y=4):
    pix = fitz.open(pdf_path)[0].get_pixmap(dpi=72)
    return pix.pixel(x, y)


def test_whiten_page_background_recolors_paper_only():
    xml = (
        '<FixedPage Width="100" Height="80" xmlns="x">'
        '<Path Fill="#ededd6" Data="M 0, 0 L 100,0 100,80 0,80 z"/>'
        '<Path Fill="#FF000000" Data="M 10,10 L 50,10 50,40 10,40 Z"/>'
        '</FixedPage>'
    )
    out = dwfx._whiten_page_background(xml)
    assert '#ededd6' not in out
    assert 'Fill="#ffffff" Data="M 0, 0' in out  # paper turned white
    assert 'Fill="#FF000000"' in out  # geometry untouched


def test_whiten_page_background_argb_paper():
    xml = (
        '<FixedPage xmlns="x"><Path Fill="#FFededd6" Data="M 0,0 L 5,0 5,5 0,5 z"/></FixedPage>'
    )
    assert 'Fill="#ffffffff"' in dwfx._whiten_page_background(xml)


def test_whiten_page_background_ignores_non_origin_first_path():
    # First path is a mid-page filled rectangle, not the paper -> must not be touched.
    xml = (
        '<FixedPage xmlns="x"><Path Fill="#FF0000" Data="M 10,10 L 50,10 50,40 10,40 z"/></FixedPage>'
    )
    assert dwfx._whiten_page_background(xml) == xml


def test_whiten_page_background_already_white_is_noop():
    xml = '<FixedPage xmlns="x"><Path Fill="#ffffff" Data="M 0,0 L 5,0 5,5 0,5 z"/></FixedPage>'
    assert dwfx._whiten_page_background(xml) == xml


def test_xps_to_pdf_white_background_default(tmp_path):
    src = make_min_xps(tmp_path / "cream.dwfx", background="#ededd6")
    out = tmp_path / "cream.pdf"
    dwfx.xps_to_pdf(src, out)  # white_background defaults True
    assert _corner_rgb(out) == (255, 255, 255)


def test_xps_to_pdf_keeps_paper_when_white_background_false(tmp_path):
    src = make_min_xps(tmp_path / "cream.dwfx", background="#ededd6")
    out = tmp_path / "cream.pdf"
    dwfx.xps_to_pdf(src, out, white_background=False)
    assert _corner_rgb(out) == (237, 237, 214)  # original cream preserved


def test_xps_to_pdf_whitens_every_sheet_of_multipage(tmp_path):
    # Regression guard: whitening must apply to all .fpage parts, not just page 1.
    src = make_multi_xps(tmp_path / "multi.dwfx", pages=3, background="#ededd6")
    out = tmp_path / "multi.pdf"
    dwfx.xps_to_pdf(src, out)
    doc = fitz.open(out)
    for i in range(3):
        assert doc[i].get_pixmap(dpi=72).pixel(4, 4) == (255, 255, 255)


def test_whiten_page_background_fill_after_data():
    # AutoCAD attribute order varies; recolor must not depend on Fill preceding Data.
    xml = (
        '<FixedPage xmlns="x"><Path Data="M 0,0 L 5,0 5,5 0,5 z" Fill="#ededd6"/></FixedPage>'
    )
    assert 'Fill="#ffffff"' in dwfx._whiten_page_background(xml)


def test_run_batch_keeps_paper_when_white_background_false(tmp_path):
    # Verify the flag is actually threaded through run_batch, not just xps_to_pdf.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "cream.dwfx", background="#ededd6")
    out_dir = tmp_path / "out"
    res = dwfx.run_batch(in_dir, out_dir, white_background=False)
    assert res.ok == 1
    assert _corner_rgb(out_dir / "cream.pdf") == (237, 237, 214)


@pytest.mark.skipif(
    not (SAMPLES / "46267D.dwg.dwfx").exists(),
    reason="confidential real sample not present (gitignored)",
)
def test_real_drawing_whitens_paper_without_dropping_annotations(tmp_path):
    """Real AutoCAD sheet (dimensions, notes, title block, revision table). Whitening
    must give a white background AND keep every annotation: no inked location may go
    blank-white. Antialiased edges are allowed to lighten; ink must not disappear."""
    src = SAMPLES / "46267D.dwg.dwfx"
    white_pdf = tmp_path / "white.pdf"
    keep_pdf = tmp_path / "keep.pdf"
    dwfx.xps_to_pdf(src, white_pdf, white_background=True)
    dwfx.xps_to_pdf(src, keep_pdf, white_background=False)

    pw = fitz.open(white_pdf)[0].get_pixmap(dpi=100)
    pk = fitz.open(keep_pdf)[0].get_pixmap(dpi=100)
    assert pw.pixel(4, 4) == (255, 255, 255)  # background whitened
    assert pk.pixel(4, 4) == (237, 237, 214)  # original cream when off

    sw, sk, n = pw.samples, pk.samples, pw.n
    ink = dropped = 0
    for i in range(0, len(sk), n):
        if sk[i] + sk[i + 1] + sk[i + 2] < 300:  # ink on the cream original
            ink += 1
            if sw[i] + sw[i + 1] + sw[i + 2] >= 740:  # gone to plain white -> lost
                dropped += 1
    assert ink > 5000  # sanity: the sheet really rendered its line work + text
    assert dropped == 0  # every annotation that was inked is still inked on white


# --- annotation text -> filename suffix ---

def _fake_convert(src, target, *, white_background=True):
    """Stand-in converter: writes a dummy PDF so run_batch naming can be tested
    without rendering synthetic Glyphs (which carry no embedded fonts)."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"%PDF-1.7\n")
    return 1


def test_annotation_text_concatenates_and_normalizes(tmp_path):
    p = make_min_xps(tmp_path / "a.dwfx", text="Main ASSEMBLY Dwg")
    t = dwfx.annotation_text(p)
    assert "assembly" in t          # per-char runs rejoined
    assert t == t.lower() and " " not in t  # lowercased, whitespace stripped


def test_annotation_text_empty_for_non_xps(tmp_path):
    assert dwfx.annotation_text(make_fake_binary_dwf(tmp_path / "b.dwfx")) == ""
    junk = tmp_path / "j.dwfx"
    junk.write_text("not a zip")
    assert dwfx.annotation_text(junk) == ""


def test_suffix_for_keywords():
    assert dwfx.suffix_for("mainassembly") == "_assy"
    assert dwfx.suffix_for("weldassyhere") == "_assy"      # bare "assy"
    assert dwfx.suffix_for("asteelweldment") == "_weld"
    assert dwfx.suffix_for("weldmentassembly") == "_assy_weld"  # both, ordered
    assert dwfx.suffix_for("plaincoverplate") == ""


def test_run_batch_tags_assembly_filename(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "part.dwfx", text="MAIN ASSEMBLY")
    out_dir = tmp_path / "out"
    res = dwfx.run_batch(in_dir, out_dir, _convert_xps=_fake_convert)
    assert (out_dir / "part_assy.pdf").exists()
    assert not (out_dir / "part.pdf").exists()
    assert res.ok == 1


def test_run_batch_tags_weldment_filename(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "frame.dwfx", text="Welded Weldment frame")
    out_dir = tmp_path / "out"
    dwfx.run_batch(in_dir, out_dir, _convert_xps=_fake_convert)
    assert (out_dir / "frame_weld.pdf").exists()


def test_run_batch_tags_both_when_text_has_both(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "rig.dwfx", text="WELDMENT ASSEMBLY")
    out_dir = tmp_path / "out"
    dwfx.run_batch(in_dir, out_dir, _convert_xps=_fake_convert)
    assert (out_dir / "rig_assy_weld.pdf").exists()


def test_run_batch_no_tag_when_no_keyword(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "plate.dwfx", text="Shaft cover plate 2mm steel")
    out_dir = tmp_path / "out"
    dwfx.run_batch(in_dir, out_dir, _convert_xps=_fake_convert)
    assert (out_dir / "plate.pdf").exists()
    assert not (out_dir / "plate_assy.pdf").exists()


def test_run_batch_tag_from_text_off(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "part.dwfx", text="MAIN ASSEMBLY")
    out_dir = tmp_path / "out"
    dwfx.run_batch(in_dir, out_dir, tag_from_text=False, _convert_xps=_fake_convert)
    assert (out_dir / "part.pdf").exists()
    assert not (out_dir / "part_assy.pdf").exists()


def test_run_batch_skip_existing_uses_tagged_name(tmp_path):
    # Re-run must recognise the already-tagged output, not reconvert under foo.pdf.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "part.dwfx", text="MAIN ASSEMBLY")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "part_assy.pdf").write_bytes(b"OLD")
    res = dwfx.run_batch(in_dir, out_dir, skip_existing=True, _convert_xps=_fake_convert)
    assert res.skipped == 1 and res.ok == 0
    assert (out_dir / "part_assy.pdf").read_bytes() == b"OLD"


def test_run_batch_tags_nested_file_and_mirrors_tree(tmp_path):
    # Tagging renames the leaf only; the mirrored subfolder structure is preserved.
    in_dir = tmp_path / "in"
    (in_dir / "A" / "B").mkdir(parents=True)
    make_min_xps(in_dir / "A" / "B" / "draw.dwfx", text="WELD ASSEMBLY")
    out_dir = tmp_path / "out"
    dwfx.run_batch(in_dir, out_dir, _convert_xps=_fake_convert)
    assert (out_dir / "A" / "B" / "draw_assy.pdf").exists()


def test_annotation_text_finds_keyword_on_non_first_sheet(tmp_path):
    # A multi-sheet set with "ASSEMBLY" only in the last sheet's title block.
    src = make_multi_xps(tmp_path / "set.dwfx", pages=3, text_on_last="GA ASSEMBLY")
    assert dwfx.suffix_for(dwfx.annotation_text(src)) == "_assy"


def test_run_batch_tag_and_white_background_compose(tmp_path):
    # Straddles both features on one real conversion: tagged name AND white paper.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "part.dwfx", text="MAIN ASSEMBLY", background="#ededd6")
    out_dir = tmp_path / "out"
    res = dwfx.run_batch(in_dir, out_dir)  # real converter, both defaults on
    assert res.ok == 1
    tagged = out_dir / "part_assy.pdf"
    assert tagged.exists()
    assert _corner_rgb(tagged) == (255, 255, 255)


def test_run_batch_binary_dwf_never_tagged_even_if_path_has_keyword(tmp_path):
    # Tagging reads drawing text, never the path; a binary DWF stays plain-named.
    in_dir = tmp_path / "in"
    (in_dir / "weldment").mkdir(parents=True)
    make_fake_binary_dwf(in_dir / "weldment" / "assembly.dwf")
    out_dir = tmp_path / "out"
    res = dwfx.run_batch(in_dir, out_dir)  # no AutoCAD hook -> reported, not converted
    assert res.binary_pending == 1
    report = (out_dir / dwfx.BINARY_REPORT_FILENAME).read_text()
    assert "assembly.dwf" in report and "_assy" not in report


def test_suffix_for_substring_false_positives_are_intended():
    # Pin the documented trade-off: full whitespace stripping + substring matching.
    # "GAS SYSTEM" -> "gassystem" contains "assy"; "disassembly" contains "assembly".
    assert dwfx.suffix_for("gassystem") == "_assy"
    assert dwfx.suffix_for("disassembly") == "_assy"


def test_run_batch_enabling_tag_leaves_old_untagged_pdf(tmp_path):
    # Migration gotcha: an existing untagged part.pdf is not the tagged target, so
    # skip-existing does not match it and both files end up present. Pins behaviour.
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    make_min_xps(in_dir / "part.dwfx", text="MAIN ASSEMBLY")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "part.pdf").write_bytes(b"OLD")  # from an earlier tag-off run
    dwfx.run_batch(in_dir, out_dir, _convert_xps=_fake_convert)
    assert (out_dir / "part.pdf").exists()       # old one left untouched
    assert (out_dir / "part_assy.pdf").exists()  # new tagged one written


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
