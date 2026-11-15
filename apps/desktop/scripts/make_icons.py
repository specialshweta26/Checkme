"""Build Lumio's app icons from the mascot (apps/web/public/agents/01-wave.png).

    python apps/desktop/scripts/make_icons.py            # needs Pillow (the API image has it)
    iconutil -c icns apps/desktop/src-tauri/icons/icon.iconset -o apps/desktop/src-tauri/icons/icon.icns   # macOS

Writes:
  apps/web/public/icons/   apple-touch-icon.png (180, iPhone/iPad home screen), icon-192.png, icon-512.png and
                           maskable-512.png (Android home screen / installed app), favicon-32.png, favicon.ico
  apps/desktop/src-tauri/icons/   32x32.png, 128x128.png, 256x256.png, 512x512.png, icon.png (1024), icon.ico
                           (Windows) and icon.iconset/ (turned into icon.icns for macOS by iconutil)

The design: the orange mascot on a deep navy tile with a soft warm glow. Home-screen icons are full-bleed squares
(iOS and Android round the corners themselves); the desktop icon follows the macOS grid (an 824px rounded tile on a
1024px canvas with a soft shadow). The mascot only exists at 256px, so large sizes are upscaled with Lanczos and a
light sharpen; swap in a larger original (same name) and rerun for crisper big icons.
"""

from __future__ import annotations

import math
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[3]
MASCOT = ROOT / "apps/web/public/agents/01-wave.png"
WEB = ROOT / "apps/web/public/icons"
DESKTOP = ROOT / "apps/desktop/src-tauri/icons"

TOP = (31, 38, 66)  # navy, lighter at the top
BOTTOM = (11, 13, 20)  # the app's dark background (#0b0d12)
GLOW = (255, 140, 40)


def mascot(size: int) -> Image.Image:
    """The mascot cropped to its own outline and scaled to ``size`` on its longer side."""
    src = Image.open(MASCOT).convert("RGBA")
    src = src.crop(src.getbbox())
    w, h = src.size
    scale = size / max(w, h)
    out = src.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    if scale > 1.4:  # upscaled: bring back a little edge definition
        rgb, alpha = out.convert("RGB").filter(ImageFilter.UnsharpMask(radius=2, percent=55, threshold=2)), out.getchannel("A")
        out = rgb.convert("RGBA")
        out.putalpha(alpha)
    return out


def tile(size: int) -> Image.Image:
    """A square navy tile: vertical gradient plus a warm glow behind where the mascot sits."""
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(1, size - 1)
        grad.putpixel((0, y), tuple(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)))
    img = grad.resize((size, size)).convert("RGBA")
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    r = size * 0.36
    cx, cy = size * 0.5, size * 0.56
    ImageDraw.Draw(glow).ellipse((cx - r, cy - r, cx + r, cy + r), fill=GLOW + (95,))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.12))
    return Image.alpha_composite(img, glow)


def place(canvas: Image.Image, fraction: float, drop: float = 0.02) -> Image.Image:
    """Centre the mascot on ``canvas`` at ``fraction`` of its width, nudged down a touch (it waves upward)."""
    size = canvas.size[0]
    m = mascot(round(size * fraction))
    x = (size - m.size[0]) // 2
    y = (size - m.size[1]) // 2 + round(size * drop)
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 110), (x, y + round(size * 0.02)), m.getchannel("A"))
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(size * 0.02)))
    canvas.alpha_composite(m, (x, y))
    return canvas


def rounded_mask(size: int, box: tuple[int, int, int, int], radius: float) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)
    return mask


def home_screen(size: int, fraction: float = 0.74) -> Image.Image:
    """Full-bleed square (the OS rounds it). Opaque: iOS fills transparency with black."""
    return place(tile(size), fraction).convert("RGB")


def desktop(size: int = 1024) -> Image.Image:
    """macOS grid: an 824/1024 rounded tile, centred, with a soft shadow; transparent around it."""
    body = round(size * 824 / 1024)
    off = (size - body) // 2
    radius = body * 0.2237
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 120), (0, 0), rounded_mask(size, (off, off + round(size * 0.012), off + body, off + body + round(size * 0.012)), radius))
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(size * 0.014)))
    face = place(tile(body), 0.74)
    canvas.paste(face, (off, off), rounded_mask(body, (0, 0, body - 1, body - 1), radius))
    return canvas


def favicon(size: int) -> Image.Image:
    """Small sizes: the mascot alone, transparent, as large as it fits."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    m = mascot(size)
    canvas.alpha_composite(m, ((size - m.size[0]) // 2, (size - m.size[1]) // 2))
    return canvas


def main() -> int:
    if not MASCOT.exists():
        print(f"missing {MASCOT}", file=sys.stderr)
        return 1
    WEB.mkdir(parents=True, exist_ok=True)
    home_screen(180).save(WEB / "apple-touch-icon.png", optimize=True)
    home_screen(192).save(WEB / "icon-192.png", optimize=True)
    home_screen(512).save(WEB / "icon-512.png", optimize=True)
    # Android may crop to a circle: keep the mascot inside the central 80% safe zone.
    home_screen(512, fraction=0.58).save(WEB / "maskable-512.png", optimize=True)
    favicon(32).save(WEB / "favicon-32.png", optimize=True)
    favicon(64).save(WEB / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])

    big = desktop(1024)
    for name, px in {"32x32.png": 32, "128x128.png": 128, "256x256.png": 256, "512x512.png": 512, "icon.png": 1024}.items():
        big.resize((px, px), Image.LANCZOS).save(DESKTOP / name, optimize=True)
    big.resize((256, 256), Image.LANCZOS).save(DESKTOP / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    iconset = DESKTOP / "icon.iconset"
    shutil.rmtree(iconset, ignore_errors=True)
    iconset.mkdir()
    for base in (16, 32, 128, 256, 512):
        big.resize((base, base), Image.LANCZOS).save(iconset / f"icon_{base}x{base}.png")
        big.resize((base * 2, base * 2), Image.LANCZOS).save(iconset / f"icon_{base}x{base}@2x.png")
    print("icons written; on macOS run: iconutil -c icns", iconset, "-o", DESKTOP / "icon.icns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
