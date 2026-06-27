from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

import converter
from converter import OdaNotFoundError


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DWG -> PDF (A3 landscape)")
        self.geometry("720x500")
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
        ttk.Label(opt, text="Color:").pack(side="left")
        self.color_var = tk.StringVar(value=converter.DEFAULT_COLOR_LABEL)
        ttk.Combobox(
            opt, textvariable=self.color_var, state="readonly",
            values=list(converter.COLOR_CHOICES), width=18,
        ).pack(side="left", padx=6)
        self.skip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Skip existing PDFs", variable=self.skip_var).pack(
            side="left", padx=12
        )
        self.run_btn = ttk.Button(opt, text="Run", command=self._run)
        self.run_btn.pack(side="right")

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
                elif kind == "status":
                    self.status.configure(text=payload)
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
        color = converter.COLOR_CHOICES[self.color_var.get()]
        skip = self.skip_var.get()
        self.run_btn.configure(state="disabled")
        self.status.configure(text="Working...")
        self._append(f"Input:  {inp}")
        self._append(f"Output: {out}")
        self._worker = threading.Thread(
            target=self._work, args=(inp, out, color, skip), daemon=True
        )
        self._worker.start()

    def _work(self, inp: str, out: str, color, skip: bool) -> None:
        def log(m: str) -> None:
            self._q.put(("log", m))

        try:
            res = converter.run_batch(
                Path(inp), Path(out), color=color, skip_existing=skip, log=log
            )
            self._q.put((
                "done",
                f"Done. {res.ok} converted, {res.skipped} skipped, {res.failed} failed.",
            ))
        except OdaNotFoundError as e:
            self._q.put(("log", f"[error] {e}"))
            self._q.put(("done", "ODA File Converter not found."))
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
            (exe_dir / converter.CRASH_FILENAME).write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
