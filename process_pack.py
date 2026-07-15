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
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

REALESRGAN = Path(__file__).parent / "tools" / "realesrgan" / "realesrgan-ncnn-vulkan.exe"
DESKTOP_SIZE = (3840, 2160)   # 16:9 4K
MOBILE_SIZE = (2160, 3840)    # 9:16 4K
TMP_NAME = ".factory_tmp"     # scratch for the upscaled master, inside the pack


def output_ok(p: Path) -> bool:
    """True if p exists and is a complete PNG.

    A PNG ends with a 12-byte IEND chunk, so a truncated file is detectable by
    reading the tail - no decode, cheap enough to call on every poll. Existence
    alone isn't enough: a half-written file would otherwise count as finished
    and never be redone. Covers files cut short by a killed run and part-copied
    files arriving from another machine."""
    try:
        if p.stat().st_size < 24:
            return False
        with open(p, "rb") as f:
            f.seek(-12, os.SEEK_END)
            return f.read(12)[4:8] == b"IEND"
    except OSError:
        return False


def save_atomic(img: Image.Image, dst: Path) -> None:
    """Write to a .part file, then rename into place.

    A run can be killed mid-write (the Stop button uses taskkill /F, which gives
    the process no chance to tidy up). Renaming is atomic within a volume, so
    dst is only ever absent or complete, never half-written. Pairs with
    output_ok: this stops bad files being created, that one catches any which
    already exist."""
    tmp = dst.with_name(dst.name + ".part")
    # PIL picks the format from the extension, and ".part" means nothing to it,
    # so it has to be passed explicitly
    fmt = Image.registered_extensions().get(dst.suffix.lower(), "PNG")
    img.save(tmp, format=fmt)
    os.replace(tmp, dst)


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

    # a killed run can't clean up after itself, so sweep its leftovers now:
    # scratch dir, plus any .part file from a write that was cut short
    tmp_dir = pack_dir / TMP_NAME
    swept = 0
    if tmp_dir.is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)
        swept += 1
    for folder in (desktop, mobile):
        for part in folder.glob("*.part"):
            try:
                part.unlink()
                swept += 1
            except OSError:
                pass
    if swept:
        print(f"[cleanup] cleared {swept} leftover(s) from a previous run")
    tmp_dir.mkdir(exist_ok=True)

    # lock file tells the Factory app (or anyone) that this pack is being processed
    lock = pack_dir / ".factory.lock"
    lock.write_text(str(os.getpid()))
    try:
        run_all(images, desktop, mobile, offsets, tmp_dir)
    finally:
        lock.unlink(missing_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print("done.")


def run_all(images: list[Path], desktop: Path, mobile: Path, offsets: dict[str, int],
            tmp_dir: Path) -> None:
    for src in images:
        d_out = desktop / f"{src.stem}_desktop_4k.png"
        m_out = mobile / f"{src.stem}_mobile_4k.png"
        if output_ok(d_out) and output_ok(m_out):
            print(f"[skip] {src.name} (already processed)")
            continue

        print(f"[upscale 4x] {src.name} ...", flush=True)
        # scratch lives in the pack rather than %TEMP%: a killed run used to
        # abandon its temp dir there with no way to know which one was ours
        big_path = tmp_dir / "big.png"
        big_path.unlink(missing_ok=True)
        upscale(src, big_path)
        big = Image.open(big_path)
        big.load()                      # fully in memory, so the file can go now
        big_path.unlink(missing_ok=True)

        if not output_ok(d_out):
            save_atomic(ratio_crop(big, *DESKTOP_SIZE).resize(DESKTOP_SIZE, Image.LANCZOS), d_out)
            print(f"  -> {d_out.name}")
        if not output_ok(m_out):
            off = offsets.get(src.stem, 0)
            save_atomic(ratio_crop(big, *MOBILE_SIZE, x_offset=off).resize(MOBILE_SIZE, Image.LANCZOS), m_out)
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
