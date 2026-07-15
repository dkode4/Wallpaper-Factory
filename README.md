# Wallpaper Factory

Small Windows toolchain for producing sellable 4K wallpaper packs from
AI-generated images (Midjourney etc.): AI-upscales originals 4x with
Real-ESRGAN, exports exact **3840x2160 desktop** and **2160x3840 mobile**
versions, and builds stacked-strip **preview collages** for product pages.

## Requirements

- Windows 10/11
- Python 3.10+ with `pip install pillow`
- Real-ESRGAN portable build (free, ~40 MB). See setup below.
- Any GPU with Vulkan support (integrated Intel graphics works; a dedicated
  GPU is just faster, and output quality is identical)

## Setup

1. Clone this repo.
2. Download `realesrgan-ncnn-vulkan-20220424-windows.zip` from the official
   Real-ESRGAN releases page:
   <https://github.com/xinntao/Real-ESRGAN/releases/tag/v0.2.5.0>
3. Extract it so these four files sit at:
   ```
   tools\realesrgan\realesrgan-ncnn-vulkan.exe
   tools\realesrgan\vcomp140.dll
   tools\realesrgan\models\realesrgan-x4plus.bin
   tools\realesrgan\models\realesrgan-x4plus.param
   ```
   All four are required. `vcomp140.dll` is not optional: the upscaler cannot
   start without it, and Windows does not ship it by default. Most machines
   have a system-wide copy from the Visual C++ redistributable, but on one that
   doesn't, deleting it gives a "VCOMP140.dll was not found" error instead of a
   working upscaler.

   Everything else in the zip (the other models, the sample images, the demo
   video) can be deleted.

## Usage

### The app

Double-click **`Wallpaper Factory.bat`**.

- **Pack folder**: any folder containing an `original` subfolder with your
  source images (PNG/JPG, roughly 16:9). Finished files land in `desktop`
  and `mobile` subfolders, created automatically.
- **Start processing**: runs the pipeline; progress bar + live log.
  Already-processed images are skipped, so the folder works as an inbox:
  drop new files in `original`, press Start again.
- **Timings**: *Last image took* and *Current image* show how long each 4K
  export takes, so a long pack has a visible pace. Both are read from the
  output folders, so they also track runs started from the command line.
- **Closing during a run**: you're asked whether to stop it or leave it
  running in the background. A run left running finishes the pack on its own;
  reopen the app and it reattaches to the live log.
- **Resolution check (✓ / ✗ + Details)**: flags any original too small to
  make a sharp 4K export. Rule of thumb: originals need to be at least
  **960 px tall** (Midjourney's upscaled 2944x1648 downloads always pass).
- **Create collage**: multi-select images, pick a save location. Four modes:

  | Mode | Output |
  | --- | --- |
  | Portrait: Strips (1 per row) | 2160x2700, one wide band per image |
  | Portrait: Split (2 per row) | 2160x2700, two cropped bands per row (needs an even count) |
  | Portrait: Split full (whole images) | 2 per row, **nothing cropped**; canvas grows with the count |
  | Landscape: Grid (wide, for banners) | auto grid biased wide, 16:9 cells, for page backgrounds |

  Images are ordered by file name.

### Command line

```
python process_pack.py "C:\path\to\pack1"
```

Per-image horizontal shift of the mobile (9:16) center crop:

```
python process_pack.py "C:\path\to\pack1" --offset 3=-800 --offset 7=500
```

## How it works

`process_pack.py` upscales each original 4x via the Real-ESRGAN executable
(a neural upscaler that preserves texture instead of blurring), then cuts an
exact 16:9 4K frame and a centered 9:16 4K frame from the oversized master
and saves them with Lanczos resampling. A `.factory.lock` file in the pack
folder (containing the runner's PID) coordinates between the GUI and any
externally started runs, so the app always shows the true processing state.

Exports are written to a `.part` file and renamed into place, so an interrupted
run can never leave a half-written wallpaper that later looks finished. An
image counts as done only when both outputs are complete PNGs, checked by their
trailing IEND chunk, which also catches files part-copied from another machine.

Each pack holds its own working files, all disposable:

| File | Purpose |
| --- | --- |
| `.factory.log` | The current run's output. The app tails it, which is why a run can outlive the window and why CLI runs still show a log. |
| `.factory.lock` | The running PID, so the app knows a run is active. |
| `.factory_tmp/` | Scratch for the upscaled master (large). |

Stopping a run deletes its scratch, and every run starts by sweeping anything a
previous one left behind, so an interrupted run can't accumulate on disk.

## Files

| Path | Purpose |
| --- | --- |
| `factory_app.py` | Tkinter GUI (progress, start/stop, collage maker) |
| `process_pack.py` | The actual pipeline; usable standalone via CLI |
| `Wallpaper Factory.bat` | Launcher (no console window) |
| `tools/factory.ico` / `factory_icon.png` | App icon |
| `tools/realesrgan/` | Real-ESRGAN portable build (downloaded in setup, not committed) |
