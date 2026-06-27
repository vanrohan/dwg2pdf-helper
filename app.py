from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

import dwfx


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DWFx/DWF -> PDF")
        self.geometry("760x540")
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._build()
        self.after(100, self._drain)

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="Input folder:").grid(row=0, column=0, sticky="w")
        self.in_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.in_var).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse...", command=self._pick_in).grid(row=0, column=2)
        ttk.Label(top, text="Output folder:").grid(row=1, column=0, sticky="w", pady=4)
        self.out_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.out_var).grid(row=1, column=1, sticky="we", padx=4)
        ttk.Button(top, text="Browse...", command=self._pick_out).grid(row=1, column=2)
        top.columnconfigure(1, weight=1)

        opt = ttk.Frame(self)
        opt.pack(fill="x", padx=8, pady=4)
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Skip existing PDFs", variable=self.skip_var).pack(side="left")
        self.white_bg_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt, text="White background", variable=self.white_bg_var
        ).pack(side="left", padx=(12, 0))
        self.run_btn = ttk.Button(opt, text="Run", command=self._run)
        self.run_btn.pack(side="right")

        # One checkbox per filename-tag rule, derived from dwfx.TAG_RULES so new rules
        # appear automatically. Each maps its suffix id to an enabled flag.
        tagbar = ttk.Frame(self)
        tagbar.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(tagbar, text="Filename tags:").pack(side="left")
        self.tag_vars: dict[str, tk.BooleanVar] = {}
        for rule in dwfx.TAG_RULES:
            suffix = rule[0]
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(tagbar, text=suffix, variable=var).pack(side="left", padx=(8, 0))
            self.tag_vars[suffix] = var

        self.log_widget = tk.Text(self, wrap="word", state="disabled", height=20)
        self.log_widget.pack(fill="both", expand=True, padx=8, pady=4)
        self.status = ttk.Label(self, text="Ready", anchor="w", relief="sunken")
        self.status.pack(fill="x", padx=8, pady=(0, 6))

    def _pick_in(self) -> None:
        d = filedialog.askdirectory(title="Select input folder")
        if d:
            self.in_var.set(d)

    def _pick_out(self) -> None:
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.out_var.set(d)

    def _append(self, msg: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", msg + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "done":
                    self.run_btn.configure(state="normal")
                    self.status.configure(text=payload)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _run(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        inp = self.in_var.get().strip()
        out = self.out_var.get().strip()
        if not inp or not Path(inp).is_dir():
            self._append("[error] Please choose a valid input folder.")
            return
        if not out:
            self._append("[error] Please choose an output folder.")
            return
        skip = self.skip_var.get()
        white_bg = self.white_bg_var.get()
        tags = {sfx for sfx, var in self.tag_vars.items() if var.get()}
        self.run_btn.configure(state="disabled")
        self.status.configure(text="Working...")
        self._append(f"Input:  {inp}")
        self._append(f"Output: {out}")
        self._worker = threading.Thread(
            target=self._work, args=(inp, out, skip, white_bg, tags), daemon=True
        )
        self._worker.start()

    def _work(self, inp: str, out: str, skip: bool, white_bg: bool, tags: set) -> None:
        def log(m: str) -> None:
            self._q.put(("log", m))

        try:
            res = dwfx.run_batch(
                Path(inp), Path(out),
                skip_existing=skip, white_background=white_bg,
                tags=tags, log=log,
            )
            self._q.put((
                "done",
                f"Done. {res.ok} converted, {res.skipped} skipped, "
                f"{res.failed} failed, {res.binary_pending} need manual conversion.",
            ))
        except ValueError as e:
            self._q.put(("log", f"[error] {e}"))
            self._q.put(("done", "Invalid folder selection."))
        except Exception as e:
            self._q.put(("log", f"[error] {e}"))
            self._q.put(("done", "Failed - see log."))


def main() -> None:
    try:
        App().mainloop()
    except Exception:
        exe_dir = Path(sys.argv[0]).resolve().parent
        try:
            (exe_dir / dwfx.CRASH_FILENAME).write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
