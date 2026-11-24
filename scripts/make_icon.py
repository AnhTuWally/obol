"""Build app icon assets (transparent outside the coin) from assets_src/obol_icon_square_512.png.

Requires Pillow and numpy.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
out = root / "src" / "obol" / "gui" / "assets"
out.mkdir(parents=True, exist_ok=True)

src = Image.open(root / "assets_src" / "obol_icon_square_512.png").convert("RGBA")
a = np.array(src).astype(int)

# Locate the gold rim to find the coin's circle.
gold = (a[..., 0] > 150) & (a[..., 2] < 120)
ys, xs = np.where(gold)
cx, cy = (xs.min() + xs.max() + 1) / 2, (ys.min() + ys.max() + 1) / 2
r = ((xs.max() - xs.min() + 1) + (ys.max() - ys.min() + 1)) / 4
print("coin center/radius:", cx, cy, r)

# Antialiased circular mask (4x supersampled), slightly inside the rim edge.
SS = 4
r_mask = r - 0.5
mask = Image.new("L", (src.width * SS, src.height * SS), 0)
ImageDraw.Draw(mask).ellipse(
    [(cx - r_mask) * SS, (cy - r_mask) * SS, (cx + r_mask) * SS, (cy + r_mask) * SS], fill=255
)
mask = mask.resize(src.size, Image.LANCZOS)
src.putalpha(mask)

# Crop tightly to the coin so it fills the icon canvas.
pad = 2
box = tuple(int(round(v)) for v in (cx - r - pad, cy - r - pad, cx + r + pad, cy + r + pad))
icon = src.crop(box)
side = max(icon.size)
canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
canvas.paste(icon, ((side - icon.width) // 2, (side - icon.height) // 2))
canvas = canvas.resize((512, 512), Image.LANCZOS)

canvas.save(out / "obol_icon.png", optimize=True)
canvas.save(
    out / "obol_icon.ico",
    sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)],
)
