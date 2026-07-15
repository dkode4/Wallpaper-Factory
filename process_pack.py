"""
process_pack.py - wallpaper pack factory.

Drop Midjourney originals (16:9-ish PNG/JPG) into <pack>\\original, then run:

    python process_pack.py "C:\\Users\\User\\Desktop\\pack1"

For every image it:
    1. AI-upscales 4x with Real-ESRGAN (paint texture preserved)
    2. Exports exact 4K desktop version (3840x2160) -> <pack>\\desktop
    3. Exports 4K mobile version (2160x3840, center 9:16 crop) -> <pack>\\mobile

Already-processed images are skipped, so the folder works as an inbox:
add new files, re-run, only the new ones get processed.

Mobile crop is centered by default. To shift a specific image's crop, add
--offset entries like:  --offset 3=-800 --offset 7=500
(negative = crop window moves left, positive = right, in upscaled pixels)
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

REALESRGAN = Path(__file__).parent / "tools" / "realesrgan" / "realesrgan-ncnn-vulkan.exe"
DESKTOP_SIZE = (3840, 2160)   # 16:9 4K
MOBILE_SIZE = (2160, 3840)    # 9:16 4K


def ratio_crop(img: Image.Image, target_w: int, target_h: int, x_offset: int = 0) -> Image.Image:
    """Largest centered crop matching target aspect ratio, optionally shifted horizontally."""
    w, h = img.size
    target_ratio = target_w / target_h
    if w / h > target_ratio:
        crop_w, crop_h = int(h * target_ratio), h
    else:
        crop_w, crop_h = w, int(w / target_ratio)
    x = (w - crop_w) // 2 + x_offset
    x = max(0, min(x, w - crop_w))
    y = (h - crop_h) // 2
    return img.crop((x, y, x + crop_w, y + crop_h))


def upscale(src: Path, dst: Path) -> None:
    result = subprocess.run(
        [str(REALESRGAN), "-i", str(src), "-o", str(dst), "-s", "4", "-n", "realesrgan-x4plus"],
        capture_output=True, text=True,
        # stop Windows popping a console window for the upscaler on every image
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not dst.exists():
        raise RuntimeError(f"Real-ESRGAN failed on {src.name}: {result.stderr[-500:]}")


def process(pack_dir: Path, offsets: dict[str, int]) -> None:
    original = pack_dir / "original"
    desktop = pack_dir / "desktop"
    mobile = pack_dir / "mobile"
    if not original.is_dir():
        sys.exit(f"no 'original' folder in {pack_dir}")
    desktop.mkdir(exist_ok=True)
    mobile.mkdir(exist_ok=True)

    images = sorted(
        [p for p in original.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")],
        key=lambda p: (len(p.stem), p.stem),
    )
    if not images:
        sys.exit(f"no images in {original}")

    # lock file tells the Factory app (or anyone) that this pack is being processed
    lock = pack_dir / ".factory.lock"
    lock.write_text(str(os.getpid()))
    try:
        run_all(images, desktop, mobile, offsets)
    finally:
        lock.unlink(missing_ok=True)

    print("done.")


def run_all(images: list[Path], desktop: Path, mobile: Path, offsets: dict[str, int]) -> None:
    for src in images:
        d_out = desktop / f"{src.stem}_desktop_4k.png"
        m_out = mobile / f"{src.stem}_mobile_4k.png"
        if d_out.exists() and m_out.exists():
            print(f"[skip] {src.name} (already processed)")
            continue

        print(f"[upscale 4x] {src.name} ...", flush=True)
        with tempfile.TemporaryDirectory() as td:
            big_path = Path(td) / "big.png"
            upscale(src, big_path)
            big = Image.open(big_path)
            big.load()

        if not d_out.exists():
            ratio_crop(big, *DESKTOP_SIZE).resize(DESKTOP_SIZE, Image.LANCZOS).save(d_out)
            print(f"  -> {d_out.name}")
        if not m_out.exists():
            off = offsets.get(src.stem, 0)
            ratio_crop(big, *MOBILE_SIZE, x_offset=off).resize(MOBILE_SIZE, Image.LANCZOS).save(m_out)
            print(f"  -> {m_out.name}" + (f" (offset {off})" if off else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description="Upscale + export wallpaper pack")
    ap.add_argument("pack", help="pack folder containing an 'original' subfolder")
    ap.add_argument("--offset", action="append", default=[],
                    help="per-image mobile crop shift, e.g. 3=-800 (image stem = pixels)")
    args = ap.parse_args()
    offsets = {}
    for item in args.offset:
        stem, _, val = item.partition("=")
        offsets[stem] = int(val)
    process(Path(args.pack), offsets)


if __name__ == "__main__":
    main()
