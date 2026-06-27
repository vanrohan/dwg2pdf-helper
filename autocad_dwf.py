"""Drive AutoCAD to convert binary DWF6 files to PDF via a GDI PDF printer.

Binary DWF has no free library, but AutoCAD can attach it as an underlay and plot
it. The AutoCAD PDF pc3 driver renders DWF-underlay text at ~0.6pt (invisible), so
we plot to a GDI system printer (clawPDF), which renders correctly and, configured
for auto-save, writes the PDF silently. This module generates an AutoCAD script,
runs acad.exe in batch (/b), and files each output into its mirrored target path.

This path is Windows-only and depends on AutoCAD + a configured clawPDF auto-save
profile. It cannot be tested off-Windows; every step is logged for first-run
validation. The exact -PLOT prompt sequence is English/version-specific and may
need tuning against the installed AutoCAD.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_DEVICE = "clawPDF"
DEFAULT_PAPER = "ISO A3 (420.00 x 297.00 MM)"

Log = Callable[[str], None]


@dataclass
class AutoCadConfig:
    acad_exe: Path | None = None            # auto-discovered if None
    clawpdf_output_dir: Path | None = None  # folder clawPDF auto-saves into
    device: str = DEFAULT_DEVICE
    paper: str = DEFAULT_PAPER
    timeout_s: float = 1800.0
    staging_dir: Path | None = None


def find_acad(search_bases: tuple[str, ...] = (r"C:\Program Files\Autodesk",)) -> Path | None:
    """Locate the newest full AutoCAD's acad.exe (not LT, not TrueView)."""
    for base in search_bases:
        b = Path(base)
        if b.exists():
            hits = sorted(b.glob("AutoCAD */acad.exe"))
            if hits:
                return hits[-1]
    return None


def _token(i: int) -> str:
    return f"dwf2pdf_{i:04d}"


def build_script(jobs: list[tuple[Path, Path]], config: AutoCadConfig, staging: Path) -> str:
    """Build the AutoCAD .scr that attaches each DWF and plots it to the GDI printer.

    Each file is saved to a uniquely-named temp DWG first so the GDI printer's
    auto-save names the PDF predictably (<token>...pdf), which we then move to the
    real mirrored target.
    """
    lines = ["FILEDIA", "0", "CMDDIA", "0"]
    for i, (src, _target) in enumerate(jobs):
        tok = _token(i)
        dwg = staging / f"{tok}.dwg"
        lines += [
            "ERASE", "ALL", "",                 # clear previous underlay (no-op if empty)
            "-DWFATTACH", str(src), "0,0", "1", "0",
            "ZOOM", "E",
            "SAVEAS", "2018", str(dwg),         # drawing name -> printer job title
            "-PLOT",
            "Y",                                 # detailed plot configuration
            "",                                  # layout: Model (current)
            config.device,                       # output device
            config.paper,                        # paper size
            "M",                                 # millimeters
            "L",                                 # landscape
            "N",                                 # plot upside down
            "E",                                 # plot area: extents
            "F",                                 # plot scale: fit
            "C",                                 # plot offset: center
            "Y",                                 # plot with plot styles
            ".",                                 # plot style table: none
            "Y",                                 # plot with lineweights
            "",                                  # shade plot: as displayed
            "N",                                 # write plot to file: No (system printer)
            "N",                                 # save changes to page setup
            "Y",                                 # proceed with plot
        ]
    lines += ["ERASE", "ALL", "", "QUIT", "Y"]
    return "\n".join(lines) + "\n"


def convert_batch(
    jobs: list[tuple[Path, Path]],
    *,
    config: AutoCadConfig | None = None,
    log: Log = lambda m: None,
) -> dict:
    """Convert binary DWF jobs [(src, target_pdf), ...] via AutoCAD + clawPDF.

    Returns {src: bool} indicating which targets were produced. Never raises for a
    single bad file; logs everything.
    """
    results: dict = {src: False for src, _ in jobs}
    if not jobs:
        return results
    cfg = config or AutoCadConfig()

    if sys.platform != "win32":
        log("[autocad] not running on Windows - skipping AutoCAD batch.")
        return results

    acad = Path(cfg.acad_exe) if cfg.acad_exe else find_acad()
    if not acad or not acad.exists():
        log("[autocad] full AutoCAD (acad.exe) not found - cannot convert binary DWF.")
        return results
    if not cfg.clawpdf_output_dir:
        log("[autocad] clawPDF output folder not set - cannot collect plotted PDFs.")
        return results

    claw_dir = Path(cfg.clawpdf_output_dir)
    staging = cfg.staging_dir or (claw_dir / "_dwf2pdf_staging")
    staging.mkdir(parents=True, exist_ok=True)
    scr = staging / "batch.scr"
    scr.write_text(build_script(jobs, cfg, staging), encoding="utf-8")

    log(f"[autocad] launching {acad.name} on {len(jobs)} binary DWF file(s)...")
    proc = subprocess.Popen([str(acad), "/nologo", "/b", str(scr)])
    try:
        proc.wait(timeout=cfg.timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        log("[autocad] AutoCAD timed out and was terminated.")

    for i, (src, target) in enumerate(jobs):
        tok = _token(i)
        matches = sorted(claw_dir.glob(f"{tok}*.pdf"))
        if not matches:
            log(f"[autocad] no PDF produced for {src.name} (expected {tok}*.pdf).")
            continue
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        try:
            matches[0].replace(target)
            results[src] = True
            log(f"[autocad] {src.name} -> {target}")
        except OSError as e:
            log(f"[autocad] could not move output for {src.name}: {e}")
    return results
