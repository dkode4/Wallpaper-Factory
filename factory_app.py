"""
Wallpaper Factory - small desktop app for running and tracking pack processing.

Launch with:  Wallpaper Factory.bat  (same folder)

- Pick the pack folder (must contain an 'original' subfolder)
- Live counts + progress bar update every 2s by watching the output folders,
  so it also tracks runs started elsewhere (e.g. by Claude)
- Start / Stop the processing run, live log below
"""

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps

SCRIPT = Path(__file__).parent / "process_pack.py"
SETTINGS_FILE = Path(__file__).parent / ".factory_settings.json"
DEFAULT_PACK = str(Path.home() / "Desktop" / "pack1")


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


def pid_alive(pid: str) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        return pid in out
    except OSError:
        return False


def upscaler_running() -> bool:
    """True if a Real-ESRGAN process is running anywhere on this machine."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {UPSCALER_EXE}", "/NH"],
            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
        ).stdout
        return UPSCALER_EXE.lower() in out.lower()
    except OSError:
        return False


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


class FactoryApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        root.title("Wallpaper Factory")
        root.geometry("720x520")
        root.minsize(600, 420)

        pad = {"padx": 10, "pady": 5}

        # -- pack folder row
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Pack folder:").pack(side="left")
        self.pack_var = tk.StringVar(value=load_last_pack())
        self.pack_var.trace_add("write", lambda *_: save_last_pack(self.pack_var.get()))
        ttk.Entry(top, textvariable=self.pack_var).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(top, text="Browse...", command=self.browse).pack(side="left")

        # -- status row
        status = ttk.Frame(root)
        status.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).pack(side="left")
        # resolution QC counters (originals big enough for sharp 4K output?)
        self._res_cache: dict[str, tuple[float, int, int]] = {}
        self.res_bad_var = tk.StringVar(value="")
        self.res_ok_var = tk.StringVar(value="")
        ttk.Button(status, text="Details", command=self.show_res_details).pack(side="right")
        tk.Label(status, textvariable=self.res_bad_var, fg="#c62828",
                 font=("Segoe UI", 10, "bold")).pack(side="right", padx=(4, 10))
        tk.Label(status, textvariable=self.res_ok_var, fg="#2e7d32",
                 font=("Segoe UI", 10, "bold")).pack(side="right")

        self.progress = ttk.Progressbar(root, mode="determinate")
        self.progress.pack(fill="x", **pad)

        # -- buttons row
        btns = ttk.Frame(root)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="Start processing", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(btns, text="Open original", command=lambda: self.open_folder("original")).pack(side="right")
        ttk.Button(btns, text="Open desktop", command=lambda: self.open_folder("desktop")).pack(side="right", padx=6)
        ttk.Button(btns, text="Open mobile", command=lambda: self.open_folder("mobile")).pack(side="right")

        # -- collage section (orientation grouped)
        collage = ttk.LabelFrame(root, text="Create collage")
        collage.pack(fill="x", padx=10, pady=5)
        prow = ttk.Frame(collage); prow.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(prow, text="Portrait", width=9).pack(side="left")
        ttk.Button(prow, text="Strips (1 per row)",
                   command=lambda: self.create_collage("strips")).pack(side="left", padx=4)
        ttk.Button(prow, text="Split (2 per row)",
                   command=lambda: self.create_collage("split")).pack(side="left", padx=4)
        ttk.Button(prow, text="Split full (whole images)",
                   command=lambda: self.create_collage("splitfull")).pack(side="left", padx=4)
        lrow = ttk.Frame(collage); lrow.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Label(lrow, text="Landscape", width=9).pack(side="left")
        ttk.Button(lrow, text="Grid (wide, for banners)",
                   command=lambda: self.create_collage("landscape")).pack(side="left", padx=4)

        # -- log box
        self.log = tk.Text(root, height=14, state="disabled", bg="#111", fg="#ddd",
                           font=("Consolas", 9), wrap="word")
        self.log.pack(fill="both", expand=True, padx=10, pady=(5, 10))

        self.refresh()
        self.poll_log()

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
        done = sum(
            1 for s in stems
            if (pack / "desktop" / f"{s}_desktop_4k.png").exists()
            and (pack / "mobile" / f"{s}_mobile_4k.png").exists()
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
        tree.tag_configure("ok", foreground="#2e7d32")
        tree.tag_configure("bad", foreground="#c62828")
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
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        ttk.Label(win, text="Rule of thumb: originals need to be at least 960 px tall "
                            "(Midjourney's upscaled 2944x1648 downloads always pass).").pack(pady=(0, 8))

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- status loop ----------
    def refresh(self) -> None:
        total, done = self.counts()
        own_run = self.proc is not None and self.proc.poll() is None
        external_run = not own_run and (lock_pid(self.pack_dir()) is not None or upscaler_running())
        self.progress["maximum"] = max(total, 1)
        self.progress["value"] = done
        if total and done == total:
            state = "all done"
        elif own_run:
            state = "processing (this app)..."
        elif external_run:
            state = "processing (background run)..."
        else:
            state = "idle"
        self.status_var.set(f"{done} / {total} images finished  —  {state}")
        rows = self.scan_originals()
        ok = sum(1 for r in rows if r[3] and r[4])
        bad = len(rows) - ok
        self.res_ok_var.set(f"✓ {ok}")
        self.res_bad_var.set(f"✗ {bad}" if bad else "")
        if own_run or external_run:
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
        else:
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        self.root.after(2000, self.refresh)

    def poll_log(self) -> None:
        try:
            while True:
                self.append_log(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(300, self.poll_log)

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
        self.append_log(f"\n=== starting: {pack} ===\n")
        self.proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), str(pack)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        threading.Thread(target=self.reader, daemon=True).start()

    def reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.log_queue.put(line)
        code = self.proc.wait()
        self.log_queue.put(f"=== finished (exit {code}) ===\n")
        self.root.after(0, self.run_ended)

    def run_ended(self) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def stop(self) -> None:
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
            self.append_log("=== stopped by user (processes killed, partial temp files cleaned) ===\n")
        self.run_ended()


def main() -> None:
    # own AppUserModelID so Windows shows our icon in the taskbar
    # instead of grouping the window under a generic Python icon
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WallpaperFactory.App.1")
    except Exception:
        pass
    root = tk.Tk()
    icon = Path(__file__).parent / "tools" / "factory.ico"
    if icon.exists():
        try:
            root.iconbitmap(default=str(icon))
        except tk.TclError:
            pass
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    FactoryApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
