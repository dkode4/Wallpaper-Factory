"""
Wallpaper Factory - small desktop app for running and tracking pack processing.

Launch with:  Wallpaper Factory.bat  (same folder)

- Pick the pack folder (must contain an 'original' subfolder)
- Live counts + progress bar update every 2s by watching the output folders,
  so it also tracks runs started elsewhere (e.g. by Claude)
- Timing KPIs (last image / current elapsed) come from the same folder watch,
  so they work for background runs too
- Start / Stop the processing run, live log below

A run writes its output to <pack>\\.factory.log and the app tails that file,
rather than reading the child's stdout pipe. That means a run can outlive the
app (closing the window offers to leave it going), and the log also shows runs
started from the command line, which a pipe never could.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps

from process_pack import TMP_NAME, output_ok   # shared: app and script must agree

SCRIPT = Path(__file__).parent / "process_pack.py"
SETTINGS_FILE = Path(__file__).parent / ".factory_settings.json"
DEFAULT_PACK = str(Path.home() / "Desktop" / "pack1")

# -- palette
BG = "#eef0f3"
CARD = "#ffffff"
BORDER = "#dcdfe4"
INK = "#16191d"
MUTED = "#6b7280"
ACCENT = "#9a7b2f"        # Quadretta gold
OK = "#2e7d32"
BAD = "#c62828"
LOG_BG = "#0f1115"
LOG_FG = "#d7dae0"

UI = "Segoe UI"
MONO = "Consolas"


def load_last_pack() -> str:
    try:
        saved = json.loads(SETTINGS_FILE.read_text())["last_pack"]
        if Path(saved).is_dir():
            return saved
    except (OSError, KeyError, ValueError):
        pass
    return DEFAULT_PACK


def save_last_pack(path: str) -> None:
    try:
        SETTINGS_FILE.write_text(json.dumps({"last_pack": path}))
    except OSError:
        pass
IMAGE_EXTS = (".png", ".jpg", ".jpeg")
UPSCALER_EXE = "realesrgan-ncnn-vulkan.exe"
LOG_NAME = ".factory.log"     # per-pack, written by the run, tailed by the app

# outputs are cut from a 4x upscaled master, so originals must be big enough
# that nothing gets stretched: desktop 3840x2160 needs w>=960 h>=540,
# mobile 2160x3840 (9:16 crop of the master) needs h>=960 w>=540
DESKTOP_MIN = (960, 540)
MOBILE_MIN = (540, 960)


def res_check(w: int, h: int) -> tuple[bool, bool]:
    """(desktop_ok, mobile_ok) for an original of size w x h."""
    desktop_ok = w >= DESKTOP_MIN[0] and h >= DESKTOP_MIN[1]
    mobile_ok = w >= MOBILE_MIN[0] and h >= MOBILE_MIN[1]
    return desktop_ok, mobile_ok


def fmt_duration(seconds: float | None) -> str:
    """Short human duration: 47s / 6m 52s / 1h 04m. None -> em dash."""
    if seconds is None:
        return "—"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _tasklist(filter_expr: str) -> str:
    """tasklist output for a filter, as CSV.

    CSV matters: the default table format truncates the image name column to 25
    chars, so "realesrgan-ncnn-vulkan.exe" (26) came back as
    "realesrgan-ncnn-vulkan.ex" and never matched."""
    try:
        return subprocess.run(
            ["tasklist", "/FI", filter_expr, "/NH", "/FO", "CSV"],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
    except OSError:
        return ""


def pid_alive(pid: str) -> bool:
    return f'"{pid}"' in _tasklist(f"PID eq {pid}")


def upscaler_running() -> bool:
    """True if a Real-ESRGAN process is running anywhere on this machine."""
    return UPSCALER_EXE.lower() in _tasklist(f"IMAGENAME eq {UPSCALER_EXE}").lower()


def lock_pid(pack: Path) -> str | None:
    """PID from the pack's lock file if that process is still alive.
    A processing run holds this lock from first image to last, so it has no
    blind spots between images. Stale locks (crashed runs) are cleaned up."""
    lock = pack / ".factory.lock"
    if not lock.exists():
        return None
    try:
        pid = lock.read_text().strip()
    except OSError:
        return None
    if pid.isdigit() and pid_alive(pid):
        return pid
    try:
        lock.unlink()
    except OSError:
        pass
    return None


def apply_theme(root: tk.Tk) -> None:
    """Flat modern styling. 'clam' is the only stock theme that lets ttk
    widget colours actually be overridden."""
    root.configure(bg=BG)
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        return

    style.configure(".", background=BG, foreground=INK, font=(UI, 9))
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=INK)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Card.TLabel", background=CARD, foreground=INK)
    style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
    style.configure("Title.TLabel", background=BG, foreground=INK, font=(UI, 13, "bold"))
    style.configure("Field.TLabel", background=BG, foreground=MUTED)

    style.configure("TButton", padding=(12, 7), relief="flat", background=CARD,
                    bordercolor=BORDER, foreground=INK, focuscolor=BG)
    style.map("TButton",
              background=[("active", "#f0f1f4"), ("disabled", "#f2f3f5")],
              foreground=[("disabled", "#a3a8ae")])
    # sits on a white card, so it needs a fill to read as a button at all
    style.configure("Chip.TButton", background="#e7eaee", bordercolor="#c9ced6",
                    foreground=INK, padding=(12, 5))
    style.map("Chip.TButton",
              background=[("active", "#dbe0e6")],
              bordercolor=[("active", "#b6bdc7")])
    style.configure("Primary.TButton", background=INK, foreground="#ffffff",
                    bordercolor=INK)
    style.map("Primary.TButton",
              background=[("active", "#2c3138"), ("disabled", "#c7cbd1")],
              bordercolor=[("disabled", "#c7cbd1")],
              foreground=[("disabled", "#eef0f3")])

    style.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=6)
    style.configure("Horizontal.TProgressbar", background=INK, troughcolor="#e4e7eb",
                    bordercolor="#e4e7eb", lightcolor=INK, darkcolor=INK,
                    thickness=8)
    style.configure("TLabelframe", background=BG, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=(UI, 9))
    style.configure("Treeview", fieldbackground=CARD, background=CARD,
                    bordercolor=BORDER, rowheight=22)
    style.configure("Treeview.Heading", background="#f0f1f4", foreground=INK,
                    relief="flat", font=(UI, 9, "bold"))


def card(parent: tk.Misc) -> tk.Frame:
    """White panel with a 1px border."""
    return tk.Frame(parent, bg=CARD, highlightbackground=BORDER,
                    highlightcolor=BORDER, highlightthickness=1, bd=0)


class FactoryApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        # -- log tail state (see drain_log_file)
        self._tail_path: str | None = None
        self._tail_pos = 0

        # -- timing state (see update_timing)
        self._timed_pack: str | None = None
        self._last_done: int | None = None
        self._img_start: float | None = None
        self._last_dur: float | None = None
        self._durations: list[float] = []      # this run's images, for the ETA
        self._total = 0
        self._done = 0
        self._was_running = False

        root.title("Wallpaper Factory")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        # tk sizes windows in raw pixels, so on a scaled display the layout has
        # to be scaled by hand or it comes out cramped around the larger text
        scale = root.winfo_fpixels("1i") / 96.0
        root.geometry(f"{int(780 * scale)}x{int(700 * scale)}")
        root.minsize(int(660 * scale), int(620 * scale))

        outer = tk.Frame(root, bg=BG)
        outer.pack(fill="both", expand=True, padx=14, pady=12)

        # -- header + pack folder
        ttk.Label(outer, text="Wallpaper Factory", style="Title.TLabel").pack(anchor="w")

        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(10, 0))
        ttk.Label(top, text="Pack folder", style="Field.TLabel").pack(side="left")
        self.pack_var = tk.StringVar(value=load_last_pack())
        self.pack_var.trace_add("write", lambda *_: save_last_pack(self.pack_var.get()))
        ttk.Entry(top, textvariable=self.pack_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(top, text="Browse...", command=self.browse).pack(side="left")

        # -- status card: counts, state, progress, resolution QC
        status = card(outer)
        status.pack(fill="x", pady=(12, 0))
        srow = tk.Frame(status, bg=CARD)
        srow.pack(fill="x", padx=14, pady=(12, 0))
        self.status_var = tk.StringVar(value="")
        tk.Label(srow, textvariable=self.status_var, bg=CARD, fg=INK,
                 font=(UI, 10, "bold")).pack(side="left")
        self.state_var = tk.StringVar(value="")
        self.state_lbl = tk.Label(srow, textvariable=self.state_var, bg=CARD, fg=MUTED,
                                  font=(UI, 9))
        self.state_lbl.pack(side="left", padx=(8, 0))

        self._res_cache: dict[str, tuple[float, int, int]] = {}
        self.res_bad_var = tk.StringVar(value="")
        self.res_ok_var = tk.StringVar(value="")
        ttk.Button(srow, text="Details", style="Chip.TButton",
                   command=self.show_res_details).pack(side="right")
        tk.Label(srow, textvariable=self.res_bad_var, bg=CARD, fg=BAD,
                 font=(UI, 10, "bold")).pack(side="right", padx=(4, 10))
        tk.Label(srow, textvariable=self.res_ok_var, bg=CARD, fg=OK,
                 font=(UI, 10, "bold")).pack(side="right")

        self.progress = ttk.Progressbar(status, mode="determinate")
        self.progress.pack(fill="x", padx=14, pady=(10, 14))

        # -- KPI cards
        kpis = tk.Frame(outer, bg=BG)
        kpis.pack(fill="x", pady=(10, 0))
        self.last_var = tk.StringVar(value="—")
        self.elapsed_var = tk.StringVar(value="—")
        self.eta_var = tk.StringVar(value="—")
        self._kpi(kpis, "LAST IMAGE TOOK", self.last_var, INK).pack(
            side="left", fill="x", expand=True)
        self._kpi(kpis, "CURRENT IMAGE", self.elapsed_var, ACCENT).pack(
            side="left", fill="x", expand=True, padx=(10, 0))
        self._kpi(kpis, "PACK TIME LEFT", self.eta_var, INK).pack(
            side="left", fill="x", expand=True, padx=(10, 0))

        # -- run controls
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(12, 0))
        self.start_btn = ttk.Button(btns, text="Start processing", style="Primary.TButton",
                                    command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        # packed in reverse so they read left-to-right in pipeline order
        ttk.Button(btns, text="Mobile", command=lambda: self.open_folder("mobile")).pack(side="right")
        ttk.Button(btns, text="Desktop", command=lambda: self.open_folder("desktop")).pack(side="right", padx=6)
        ttk.Button(btns, text="Original", command=lambda: self.open_folder("original")).pack(side="right")

        # -- collage section (orientation grouped)
        collage = ttk.LabelFrame(outer, text="Create collage")
        collage.pack(fill="x", pady=(12, 0))
        prow = ttk.Frame(collage); prow.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(prow, text="Portrait", width=9, style="Field.TLabel").pack(side="left")
        ttk.Button(prow, text="Strips (1 per row)",
                   command=lambda: self.create_collage("strips")).pack(side="left", padx=4)
        ttk.Button(prow, text="Split (2 per row)",
                   command=lambda: self.create_collage("split")).pack(side="left", padx=4)
        ttk.Button(prow, text="Split full (whole images)",
                   command=lambda: self.create_collage("splitfull")).pack(side="left", padx=4)
        lrow = ttk.Frame(collage); lrow.pack(fill="x", padx=10, pady=(2, 10))
        ttk.Label(lrow, text="Landscape", width=9, style="Field.TLabel").pack(side="left")
        ttk.Button(lrow, text="Grid (wide, for banners)",
                   command=lambda: self.create_collage("landscape")).pack(side="left", padx=4)

        # -- log box
        logwrap = tk.Frame(outer, bg=LOG_BG, highlightbackground=BORDER,
                           highlightcolor=BORDER, highlightthickness=1, bd=0)
        logwrap.pack(fill="both", expand=True, pady=(12, 0))
        self.log = tk.Text(logwrap, height=10, state="disabled", bg=LOG_BG, fg=LOG_FG,
                           font=(MONO, 9), wrap="word", relief="flat", bd=0,
                           padx=10, pady=8, insertbackground=LOG_FG)
        self.log.pack(fill="both", expand=True)
        # line types, coloured as they're appended (see append_log)
        self.log.tag_configure("ts", foreground="#59616e")
        self.log.tag_configure("skip", foreground="#6b7280")
        self.log.tag_configure("work", foreground="#d6b45f")
        self.log.tag_configure("ok", foreground="#7bc47f")
        self.log.tag_configure("head", foreground="#e6e9ee", font=(MONO, 9, "bold"))
        self.log.tag_configure("bad", foreground="#ef6b6b")

        self.refresh()
        self.poll_log()
        self.tick_elapsed()

    def _kpi(self, parent: tk.Misc, label: str, var: tk.StringVar, colour: str) -> tk.Frame:
        box = card(parent)
        tk.Label(box, text=label, bg=CARD, fg=MUTED, font=(UI, 8, "bold")).pack(
            anchor="w", padx=14, pady=(10, 0))
        tk.Label(box, textvariable=var, bg=CARD, fg=colour, font=(UI, 19, "bold")).pack(
            anchor="w", padx=14, pady=(0, 10))
        return box

    # ---------- helpers ----------
    def pack_dir(self) -> Path:
        return Path(self.pack_var.get().strip('" '))

    def browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=str(self.pack_dir()))
        if chosen:
            self.pack_var.set(os.path.normpath(chosen))

    def open_folder(self, sub: str) -> None:
        target = self.pack_dir() / sub
        if target.is_dir():
            os.startfile(target)
        else:
            messagebox.showinfo("Not found", f"{target} doesn't exist yet.")

    def counts(self) -> tuple[int, int]:
        pack = self.pack_dir()
        original = pack / "original"
        if not original.is_dir():
            return 0, 0
        stems = [p.stem for p in original.iterdir() if p.suffix.lower() in IMAGE_EXTS]
        # output_ok, not exists(): must agree with what process_pack considers
        # finished, or a half-written file would show as done here while the
        # next run quietly redoes it
        done = sum(
            1 for s in stems
            if output_ok(pack / "desktop" / f"{s}_desktop_4k.png")
            and output_ok(pack / "mobile" / f"{s}_mobile_4k.png")
        )
        return len(stems), done

    def scan_originals(self) -> list[tuple[str, int, int, bool, bool]]:
        """(name, w, h, desktop_ok, mobile_ok) for every image in original/.
        Sizes are cached by file modification time - only headers are read."""
        original = self.pack_dir() / "original"
        results = []
        if not original.is_dir():
            return results
        for p in sorted(original.iterdir(), key=lambda p: (len(p.stem), p.stem)):
            if p.suffix.lower() not in IMAGE_EXTS:
                continue
            key = str(p)
            mtime = p.stat().st_mtime
            cached = self._res_cache.get(key)
            if cached and cached[0] == mtime:
                w, h = cached[1], cached[2]
            else:
                try:
                    with Image.open(p) as im:
                        w, h = im.size
                except OSError:
                    continue
                self._res_cache[key] = (mtime, w, h)
            results.append((p.name, w, h, *res_check(w, h)))
        return results

    def show_res_details(self) -> None:
        rows = self.scan_originals()
        win = tk.Toplevel(self.root)
        win.title("Original resolutions — 4K readiness")
        win.geometry("640x420")
        win.configure(bg=BG)
        cols = ("file", "resolution", "desktop", "mobile")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        for col, txt, width, anchor in (
            ("file", "File", 280, "w"),
            ("resolution", "Resolution", 120, "center"),
            ("desktop", "Desktop 4K", 100, "center"),
            ("mobile", "Mobile 4K", 100, "center"),
        ):
            tree.heading(col, text=txt)
            tree.column(col, width=width, anchor=anchor)
        tree.tag_configure("ok", foreground=OK)
        tree.tag_configure("bad", foreground=BAD)
        for name, w, h, d_ok, m_ok in rows:
            tag = "ok" if (d_ok and m_ok) else "bad"
            tree.insert("", "end", tags=(tag,), values=(
                name, f"{w} x {h}",
                "✓" if d_ok else "✗ too small",
                "✓" if m_ok else "✗ will be soft",
            ))
        if not rows:
            tree.insert("", "end", values=("no images found in original/", "", "", ""))
        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True, padx=10, pady=10)
        ttk.Label(win, text="Rule of thumb: originals need to be at least 960 px tall "
                            "(Midjourney's upscaled 2944x1648 downloads always pass).",
                  style="Field.TLabel").pack(pady=(0, 10))

    def _log_tag(self, line: str) -> str | None:
        stripped = line.strip()
        if not stripped:
            return None
        if stripped.startswith("==="):
            return "head"
        if stripped.startswith("[skip]"):
            return "skip"
        if stripped.startswith("[upscale"):
            return "work"
        if stripped.startswith("->"):
            return "ok"
        low = stripped.lower()
        if "failed" in low or "error" in low or "traceback" in low:
            return "bad"
        return None

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        for line in text.splitlines(keepends=True):
            if line.strip():
                self.log.insert("end", time.strftime("%H:%M:%S  "), "ts")
            tag = self._log_tag(line)
            self.log.insert("end", line, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def drain_log_file(self) -> None:
        """Tail <pack>\\.factory.log into the box.

        Reading the file rather than the child's stdout is what lets a run
        outlive the app, and it means CLI-started runs get a log too."""
        path = self.pack_dir() / LOG_NAME
        key = str(path)
        if key != self._tail_path:
            # switched pack: show that pack's run, not the last one's
            self._tail_path = key
            self.clear_log()
            # skip whatever is already in the file - lines are timestamped as
            # they're read, so replaying history would date it all to now
            self._tail_pos = path.stat().st_size if path.exists() else 0
            if path.exists() and self._run_active():
                self.append_log("=== attached to a run already in progress ===\n")
        if not path.exists():
            return
        try:
            if path.stat().st_size < self._tail_pos:   # truncated: new run
                self._tail_pos = 0
                self.clear_log()
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._tail_pos)
                data = fh.read()
                self._tail_pos = fh.tell()
        except OSError:
            return
        if data:
            self.append_log(data)

    # ---------- status loop ----------
    def update_timing(self, done: int, running: bool) -> None:
        """Derive the two KPIs from the output-folder count.

        Watching the folders (rather than parsing the log) means the timings also
        cover runs started outside this app, which the log never sees. Cost is
        2s granularity - irrelevant against ~7min/image."""
        pack = str(self.pack_dir())
        if pack != self._timed_pack:      # different pack: nothing carries over
            self._timed_pack = pack
            self._last_done = None
            self._img_start = None
            self._last_dur = None
            self._durations = []
            self._was_running = False

        now = time.monotonic()
        if running and not self._was_running:   # run just started
            self._img_start = now
            self._last_dur = None
            self._durations = []       # a previous run's pace isn't evidence
        elif not running:
            self._img_start = None
        self._was_running = running

        if self._last_done is None:
            self._last_done = done              # first look: no baseline to time against
        elif done > self._last_done:
            finished = done - self._last_done
            if self._img_start is not None:
                # >1 in a tick (e.g. a burst of skips) averages out rather than
                # crediting the whole span to one image
                self._last_dur = (now - self._img_start) / finished
                self._durations.extend([self._last_dur] * finished)
            self._img_start = now
            self._last_done = done
        elif done < self._last_done:            # outputs deleted underneath us
            self._last_done = done

        self.last_var.set(fmt_duration(self._last_dur))

    def eta(self) -> float | None:
        """Seconds left for the whole pack, or None if not estimable yet.

        Averages the images finished so far this run rather than using the last
        one, since a single image is a poor predictor. Subtracts the current
        image's elapsed time so the estimate counts down smoothly instead of
        stepping only when an image lands."""
        if self._img_start is None or not self._durations:
            return None
        remaining = self._total - self._done
        if remaining <= 0:
            return None
        avg = sum(self._durations) / len(self._durations)
        return max(0.0, avg * remaining - (time.monotonic() - self._img_start))

    def tick_elapsed(self) -> None:
        """Separate from refresh() so the running clock ticks every second
        instead of every two."""
        if self._img_start is not None:
            self.elapsed_var.set(fmt_duration(time.monotonic() - self._img_start))
        else:
            self.elapsed_var.set("—")
        left = self.eta()
        if left is None:
            # no finished image yet this run, so any number would be invented
            self.eta_var.set("—" if self._img_start is None else "…")
        else:
            self.eta_var.set(fmt_duration(left))
        self.root.after(500, self.tick_elapsed)

    def refresh(self) -> None:
        total, done = self.counts()
        own_run = self.proc is not None and self.proc.poll() is None
        # the lock file is per-pack, so it's the only thing that proves THIS
        # pack is being worked on
        pack_run = not own_run and lock_pid(self.pack_dir()) is not None
        running = own_run or pack_run
        # the upscaler check is machine-wide: it means the GPU is busy, not that
        # this pack is being processed. Kept apart, or an idle pack would report
        # another pack's run as its own.
        elsewhere = not running and upscaler_running()
        busy = running or elsewhere
        self._total, self._done = total, done
        self.progress["maximum"] = max(total, 1)
        self.progress["value"] = done
        if total and done == total and not running:
            state, colour = "all done", OK
        elif own_run:
            state, colour = "processing (this app)", ACCENT
        elif pack_run:
            state, colour = "processing (background run)", ACCENT
        elif elsewhere:
            state, colour = "waiting — upscaler busy on another pack", MUTED
        else:
            state, colour = "idle", MUTED
        self.status_var.set(f"{done} / {total} images finished")
        self.state_var.set(f"· {state}")
        self.state_lbl.configure(fg=colour)
        self.update_timing(done, running)
        rows = self.scan_originals()
        ok = sum(1 for r in rows if r[3] and r[4])
        bad = len(rows) - ok
        self.res_ok_var.set(f"✓ {ok}")
        self.res_bad_var.set(f"✗ {bad}" if bad else "")
        # Start is gated on the GPU being free at all (two runs would just fight
        # over it). Stop stays available whenever anything is running so an
        # orphaned upscaler can still be cleared - stop() confirms first if the
        # run isn't this pack's.
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        self.root.after(2000, self.refresh)

    def poll_log(self) -> None:
        self.drain_log_file()
        try:
            while True:
                self.append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(300, self.poll_log)

    def _run_active(self) -> bool:
        own = self.proc is not None and self.proc.poll() is None
        return own or lock_pid(self.pack_dir()) is not None or upscaler_running()

    def on_close(self) -> None:
        if not self._run_active():
            self.root.destroy()
            return
        answer = messagebox.askyesnocancel(
            "Still processing",
            "A pack is still being processed.\n\n"
            "Yes  —  stop the run, then close\n"
            "No  —  leave it running in the background and close\n"
            "Cancel  —  don't close",
            icon="warning",
        )
        if answer is None:          # cancel
            return
        if answer:
            self.stop()
        # else: the run writes to the pack's log file rather than a pipe to us,
        # so it survives this window closing and finishes the pack on its own
        self.root.destroy()

    # ---------- collage ----------
    PORTRAIT = (2160, 2700)   # 4:5 portrait canvas (product / social preview)
    LS_CELL = (480, 270)      # 16:9 cell for the landscape grid

    def create_collage(self, mode: str = "strips") -> None:
        labels = {
            "strips": "collage (1 per row)",
            "split": "split collage (2 per row)",
            "splitfull": "split collage, whole images (2 per row)",
            "landscape": "landscape collage (wide grid)",
        }
        default_name = {
            "strips": "collage_preview.png",
            "split": "collage_split.png",
            "splitfull": "collage_split_full.png",
            "landscape": "collage_landscape.png",
        }
        paths = filedialog.askopenfilenames(
            title=f"Select images for the {labels[mode]} (order = file order)",
            initialdir=str(self.pack_dir() / "desktop"),
            filetypes=[("Images", "*.png *.jpg *.jpeg")],
        )
        if not paths:
            return
        if mode == "split" and len(paths) % 2 != 0:
            messagebox.showwarning(
                "Even number required",
                f"A split collage places 2 images per row, so you need an even number "
                f"of images.\n\nYou selected {len(paths)} — add or remove one.",
            )
            return
        out = filedialog.asksaveasfilename(
            title="Save collage as",
            initialdir=str(self.pack_dir()),
            initialfile=default_name[mode],
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("JPEG image", "*.jpg")],
        )
        if not out:
            return

        n = len(paths)
        # work out the grid + cell size for the chosen mode
        if mode == "strips":
            cols, rows = 1, n
            cell_w, cell_h = self.PORTRAIT[0], self.PORTRAIT[1] // rows
        elif mode == "split":
            cols, rows = 2, n // 2
            cell_w, cell_h = self.PORTRAIT[0] // 2, self.PORTRAIT[1] // rows
        elif mode == "splitfull":
            # 2 per row, whole images (no cropping): cell matches the source aspect,
            # so the canvas grows downward with however many rows you need
            cols = 2
            rows = -(-n // cols)  # ceil
            cell_w = self.PORTRAIT[0] // cols
            with Image.open(paths[0]) as im0:
                cell_h = max(1, round(cell_w * im0.height / im0.width))
        else:  # landscape: roughly square grid biased wide (cols >= rows)
            rows = max(1, round((n / 2) ** 0.5))
            cols = -(-n // rows)  # ceil division
            cell_w, cell_h = self.LS_CELL
        canvas_w, canvas_h = cell_w * cols, cell_h * rows

        popup = tk.Toplevel(self.root)
        popup.title("Creating collage")
        popup.geometry("380x110")
        popup.resizable(False, False)
        popup.transient(self.root)
        popup.configure(bg=BG)
        popup.grab_set()
        msg = tk.StringVar(value=f"Adding image 1 / {n} ...")
        ttk.Label(popup, textvariable=msg).pack(pady=(18, 6))
        bar = ttk.Progressbar(popup, maximum=n + 1, length=330)
        bar.pack(pady=4)

        state: dict = {"done": 0, "error": None, "finished": False}

        def work() -> None:
            try:
                canvas = Image.new("RGB", (canvas_w, canvas_h), (20, 20, 20))
                for i, p in enumerate(paths):
                    r, c = divmod(i, cols)
                    # centre the final row if it isn't full (landscape grids)
                    row_count = min(cols, n - r * cols)
                    x_off = (cols - row_count) * cell_w // 2 if r == rows - 1 else 0
                    if mode == "splitfull":
                        # whole image, never cropped; centred if aspects differ
                        img = ImageOps.contain(Image.open(p), (cell_w, cell_h), Image.LANCZOS)
                        cell = Image.new("RGB", (cell_w, cell_h), (20, 20, 20))
                        cell.paste(img, ((cell_w - img.width) // 2, (cell_h - img.height) // 2))
                    else:
                        cell = ImageOps.fit(Image.open(p), (cell_w, cell_h),
                                            Image.LANCZOS, centering=(0.5, 0.5))
                    canvas.paste(cell, (c * cell_w + x_off, r * cell_h))
                    state["done"] = i + 1
                if out.lower().endswith((".jpg", ".jpeg")):
                    canvas.save(out, quality=92)
                else:
                    canvas.save(out)
                state["done"] = n + 1
            except Exception as e:  # surfaced in the popup poller
                state["error"] = str(e)
            state["finished"] = True

        def poll() -> None:
            done = state["done"]
            bar["value"] = done
            msg.set("Saving ..." if done > n else f"Adding image {min(done + 1, n)} / {n} ...")
            if state["finished"]:
                popup.grab_release()
                popup.destroy()
                if state["error"]:
                    self.append_log(f"collage FAILED: {state['error']}\n")
                    messagebox.showerror("Collage failed", state["error"])
                else:
                    self.append_log(f"collage saved: {out}  ({n} images, {canvas_w}x{canvas_h})\n")
                    messagebox.showinfo("Collage saved",
                                        f"Saved {n}-image collage ({canvas_w}x{canvas_h}):\n{out}")
                return
            popup.after(100, poll)

        threading.Thread(target=work, daemon=True).start()
        poll()

    # ---------- run control ----------
    def start(self) -> None:
        pack = self.pack_dir()
        if not (pack / "original").is_dir():
            messagebox.showerror("No originals", f"{pack}\\original doesn't exist.")
            return
        if self.proc is not None and self.proc.poll() is None:
            return
        if lock_pid(pack) is not None or upscaler_running():
            messagebox.showwarning(
                "Already processing",
                "An upscaler run is already active on this machine. "
                "Wait for it to finish (or press Stop) before starting a new one.",
            )
            return
        log_path = pack / LOG_NAME
        try:
            # "w" truncates: one log per run, so the box shows this run only
            fh = open(log_path, "w", encoding="utf-8", errors="replace")
        except OSError as e:
            messagebox.showerror("Cannot write log", f"{log_path}\n\n{e}")
            return
        fh.write(f"=== starting: {pack} ===\n")
        fh.flush()
        # start the tail at the top of the file we just made
        self.clear_log()
        self._tail_path = str(log_path)
        self._tail_pos = 0
        # -u: unbuffered, or output to a file would arrive in 8KB lumps and the
        # log would sit empty for minutes at a time
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(SCRIPT), str(pack)],
            stdout=fh, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        fh.close()      # the child owns the handle now; it keeps writing if we exit
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self.waiter, daemon=True).start()

    def waiter(self) -> None:
        assert self.proc
        code = self.proc.wait()
        self.log_queue.put(f"=== finished (exit {code}) ===\n")
        try:
            self.root.after(0, self.run_ended)
        except (RuntimeError, tk.TclError):
            # closing with "stop the run" kills the child and destroys the
            # window, so this thread can wake to find there's no window left
            pass

    def run_ended(self) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def stop(self) -> None:
        own = self.proc is not None and self.proc.poll() is None
        if not own and lock_pid(self.pack_dir()) is None and upscaler_running():
            # nothing here to stop, yet the GPU is busy: either another pack's
            # run or a leftover process. Killing the upscaler is machine-wide,
            # so never do it to someone else's run without asking.
            if not messagebox.askyesno(
                "Upscaler busy elsewhere",
                "No run is active for this pack, but an upscaler is running on "
                "this machine — either another pack's run, or a leftover "
                "process from a crash.\n\nStop it anyway?",
                icon="warning",
            ):
                return
        stopped_something = False
        # kill this app's own run, including its child processes
        if self.proc is not None and self.proc.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            stopped_something = True
        # kill an external run via its lock-file PID (whole process tree)
        ext_pid = lock_pid(self.pack_dir())
        if ext_pid is not None:
            subprocess.run(
                ["taskkill", "/PID", ext_pid, "/T", "/F"],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            (self.pack_dir() / ".factory.lock").unlink(missing_ok=True)
            stopped_something = True
        # belt and braces: kill any orphaned upscaler exe
        if upscaler_running():
            subprocess.run(
                ["taskkill", "/IM", UPSCALER_EXE, "/F"],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            stopped_something = True
        if stopped_something:
            swept = self.cleanup_partials()
            note = f", {swept} leftover(s) cleaned" if swept else ", nothing to clean up"
            self.append_log(f"=== stopped by user (processes killed{note}) ===\n")
        self.run_ended()

    def cleanup_partials(self) -> int:
        """Clear scratch a killed run left behind.

        taskkill /F gives the run no chance to tidy up, so its scratch dir and
        any half-written .part file are ours to remove. The next run sweeps
        these too, but doing it here means Stop leaves the pack genuinely clean."""
        pack = self.pack_dir()
        swept = 0
        tmp_dir = pack / TMP_NAME
        if tmp_dir.is_dir():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            swept += 1
        for sub in ("desktop", "mobile"):
            folder = pack / sub
            if not folder.is_dir():
                continue
            for part in folder.glob("*.part"):
                try:
                    part.unlink()
                    swept += 1
                except OSError:
                    pass
        return swept


def main() -> None:
    # own AppUserModelID so Windows shows our icon in the taskbar
    # instead of grouping the window under a generic Python icon
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WallpaperFactory.App.1")
    except Exception:
        pass
    # Draw at the display's real resolution. Without this Windows renders the
    # window at 96dpi and bitmap-stretches it on a scaled display, which is what
    # makes tk apps look soft.
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # system DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()    # pre-8.1 fallback
        except Exception:
            pass
    root = tk.Tk()
    # tk measures font sizes in points, so it needs the true pixels-per-inch or
    # every label comes out too small once we're DPI aware
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except tk.TclError:
        pass
    icon = Path(__file__).parent / "tools" / "factory.ico"
    if icon.exists():
        try:
            root.iconbitmap(default=str(icon))
        except tk.TclError:
            pass
    apply_theme(root)
    FactoryApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
