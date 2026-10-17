"""Draw the part icons for the bag labels at exactly the printer's resolution (80 x 80 dots)."""

import math
from pathlib import Path

from PIL import Image, ImageDraw

S = 80  # dots
OUT = Path(__file__).parent / "icons"


def canvas():
    img = Image.new("L", (S, S), 255)
    return img, ImageDraw.Draw(img)


def screw():
    img, d = canvas()
    # pan head with a slot (side view) + threaded shank, as large as the square allows
    d.rounded_rectangle([1, 14, 27, 66], radius=9, fill=0)
    d.line([(14, 21), (14, 59)], fill=255, width=5)
    d.rectangle([27, 25, 72, 55], fill=0)
    d.polygon([(72, 25), (79, 32), (79, 48), (72, 55)], fill=0)  # chamfered tip
    for x in range(32, 76, 8):  # thread grooves
        d.line([(x, 25), (x + 8, 55)], fill=255, width=3)
    return img


def nut():
    img, d = canvas()
    c, r = S / 2, 36
    hexagon = [(c + r * math.cos(math.radians(a)), c + r * math.sin(math.radians(a))) for a in range(0, 360, 60)]
    d.polygon(hexagon, fill=0)
    d.ellipse([c - 15, c - 15, c + 15, c + 15], fill=255)
    for k in range(0, 360, 45):  # hint of the thread inside the hole
        a = math.radians(k)
        d.line([(c + 10 * math.cos(a), c + 10 * math.sin(a)), (c + 15 * math.cos(a), c + 15 * math.sin(a))], fill=0, width=2)
    return img


def washer():
    img, d = canvas()
    c = S / 2
    d.ellipse([c - 37, c - 37, c + 37, c + 37], fill=0)
    d.ellipse([c - 16, c - 16, c + 16, c + 16], fill=255)
    return img


def tnut():
    img, d = canvas()
    # T-slot nut for aluminium extrusions, front view: wide flange + neck, threaded hole
    d.polygon([(4, 32), (76, 32), (76, 46), (62, 60), (18, 60), (4, 46)], fill=0)  # body
    d.rectangle([24, 14, 56, 32], fill=0)  # neck that sits in the slot opening
    d.rectangle([34, 14, 46, 60], fill=255)  # threaded hole (section)
    for y in range(16, 60, 5):  # thread teeth on both walls
        d.polygon([(34, y), (37, y + 2), (34, y + 4)], fill=0)
        d.polygon([(46, y), (43, y + 2), (46, y + 4)], fill=0)
    return img


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, fn in [("screw", screw), ("nut", nut), ("washer", washer), ("tnut", tnut)]:
        fn().save(OUT / f"{name}.png")
        print("saved", OUT / f"{name}.png")
