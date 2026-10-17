"""Locate system fonts (with Japanese support) without third-party dependencies."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

from .config import cache_dir

FONT_EXTS = {".ttf", ".otf", ".ttc", ".otc"}

# (family, regular file candidates, bold file candidates) in preference order.
# File names are matched case-insensitively inside the system font directories;
# "file.ttc#1" selects a face inside a font collection.
PREFERRED = [
    ("BIZ UDPGothic", ["BIZ-UDGothicR.ttc#1"], ["BIZ-UDGothicB.ttc#1"]),
    ("BIZ UDGothic", ["BIZ-UDGothicR.ttc"], ["BIZ-UDGothicB.ttc"]),
    ("Yu Gothic", ["YuGothM.ttc", "YuGothR.ttc"], ["YuGothB.ttc"]),
    ("Meiryo", ["meiryo.ttc"], ["meiryob.ttc"]),
    ("Hiragino Sans", ["ヒラギノ角ゴシック W4.ttc", "ヒラギノ角ゴシック W3.ttc", "Hiragino Sans GB.ttc"], ["ヒラギノ角ゴシック W7.ttc", "ヒラギノ角ゴシック W6.ttc"]),
    ("Noto Sans CJK JP", ["NotoSansCJK-Regular.ttc", "NotoSansCJKjp-Regular.otf", "NotoSansJP-Regular.ttf", "NotoSansJP-Regular.otf", "NotoSansJP[wght].ttf"], ["NotoSansCJK-Bold.ttc", "NotoSansCJKjp-Bold.otf", "NotoSansJP-Bold.ttf", "NotoSansJP-Bold.otf"]),
    ("IPAexGothic", ["ipaexg.ttf"], []),
    ("IPAGothic", ["ipag.ttf"], []),
    ("MS Gothic", ["msgothic.ttc"], []),
    ("DejaVu Sans", ["DejaVuSans.ttf"], ["DejaVuSans-Bold.ttf"]),
    ("Arial", ["arial.ttf", "Arial.ttf"], ["arialbd.ttf", "Arial Bold.ttf"]),
    ("Helvetica", ["Helvetica.ttc"], []),
]


def font_dirs() -> list[Path]:
    dirs: list[Path] = []
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        dirs.append(Path(windir) / "Fonts")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    else:
        dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".local" / "share" / "fonts", Path.home() / ".fonts"]
    return [d for d in dirs if d.is_dir()]


@lru_cache(maxsize=1)
def _file_index() -> dict[str, Path]:
    """lower-case file name -> path for every font file in the system dirs."""
    index: dict[str, Path] = {}
    for d in font_dirs():
        for root, _dirs, files in os.walk(d):
            for f in files:
                if Path(f).suffix.lower() in FONT_EXTS:
                    index.setdefault(_key(f), Path(root) / f)
    return index


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).lower()


def _find_file(names: list[str]) -> str | None:
    idx = _file_index()
    for n in names:
        name, _, face = n.partition("#")
        p = idx.get(_key(name))
        if p:
            return str(p) + (f"#{face}" if face else "")
    return None


def _fc_match(pattern: str) -> Path | None:
    if not shutil.which("fc-match"):
        return None
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(out) if out and Path(out).is_file() else None


@lru_cache(maxsize=256)
def lookup(font: str, bold: bool = False) -> tuple[str, bool] | None:
    """Find a font by path or family name without falling back. Returns (path, is_real_bold) or None."""
    font = font.strip()
    p = Path(font).expanduser()
    if p.suffix.lower() in FONT_EXTS:
        if not p.is_file():
            return None
        try:
            style = ImageFont.truetype(str(p), 12).getname()[1] or ""
        except OSError:
            return None
        return str(p), "bold" in style.lower() or "black" in style.lower() or "heavy" in style.lower()
    for family, regular, bolds in PREFERRED:
        if family.lower() != font.lower():
            continue
        if bold and bolds:
            b = _find_file(bolds)
            if b:
                return b, True
        r = _find_file(regular)
        if r:
            return r, False
    entries = [e for e in list_fonts() if e["family"].lower() == font.lower()]
    if bold:
        for e in entries:
            if "bold" in e["style"].lower():
                return e["path"] + (f"#{e['index']}" if e["index"] else ""), True
    for e in sorted(entries, key=lambda e: e["style"].lower() not in ("regular", "normal", "標準")):
        return e["path"] + (f"#{e['index']}" if e["index"] else ""), "bold" in e["style"].lower()
    m = _fc_match(font + (":bold" if bold else ""))
    if m and m.stem.lower().replace(" ", "").startswith(font.lower().replace(" ", "")[:4]):
        return str(m), bold
    return None


@lru_cache(maxsize=256)
def resolve(font: str | None = "auto", bold: bool = False) -> tuple[str | None, bool]:
    """Return (font file path or None for Pillow's built-in, whether the file is a real bold face)."""
    font = (font or "auto").strip()
    if font != "auto":
        found = lookup(font, bold)
        if found:
            return found
    for _family, regular, bolds in PREFERRED:
        if bold and bolds:
            b = _find_file(bolds)
            if b:
                return b, True
        r = _find_file(regular)
        if r:
            return r, False
    m = _fc_match("sans-serif:lang=ja" + (":bold" if bold else ""))
    return (str(m), bold) if m else (None, False)


def exists(font: str | None) -> bool:
    return not font or font.strip() == "auto" or lookup(font.strip(), False) is not None


@lru_cache(maxsize=512)
def load(font: str | None, size: int, bold: bool = False) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, bool]:
    """Load a font at a pixel size. Returns (font, needs_fake_bold)."""
    path, real_bold = resolve(font, bold)
    if real_bold:
        bold = False  # the face is already bold: do not thicken it again
    size = max(1, int(size))
    if path is None:
        return ImageFont.load_default(size), bold
    index = 0
    if "#" in path:
        path, _, idx = path.rpartition("#")
        index = int(idx)
    try:
        return ImageFont.truetype(path, size, index=index), bold
    except OSError:
        return ImageFont.load_default(size), bold


def describe(font: str | None, bold: bool = False) -> str:
    path, _ = resolve(font, bold)
    return path or "Pillow built-in"


# ---------------------------------------------------------------------------
def _catalogue_path() -> Path:
    return cache_dir() / "fonts.json"


def list_fonts(refresh: bool = False) -> list[dict]:
    """All installed font faces: [{family, style, path, index}] (cached on disk)."""
    cache = _catalogue_path()
    if not refresh:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("dirs") == [str(d) for d in font_dirs()] and data.get("count") == len(_file_index()):
                return data["fonts"]
        except (OSError, ValueError, KeyError):
            pass
    fonts: list[dict] = []
    for path in sorted(set(_file_index().values())):
        for index in range(32):
            try:
                f = ImageFont.truetype(str(path), 12, index=index)
            except OSError:
                break
            family, style = f.getname()
            fonts.append({"family": family or path.stem, "style": style or "Regular", "path": str(path), "index": index})
            if path.suffix.lower() not in (".ttc", ".otc"):
                break
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"dirs": [str(d) for d in font_dirs()], "count": len(_file_index()), "fonts": fonts}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return fonts


def families(refresh: bool = False) -> list[str]:
    return sorted({f["family"] for f in list_fonts(refresh)}, key=str.lower)


def available_preferred() -> list[str]:
    """Preferred (mostly Japanese-capable) families that exist on this machine."""
    return [fam for fam, regular, _ in PREFERRED if _find_file(regular)]


@lru_cache(maxsize=64)
def _notdef(font: ImageFont.FreeTypeFont) -> bytes | None:
    try:
        m = font.getmask("􏿽")
        return bytes(m) + bytes(str(m.size), "ascii")
    except Exception:
        return None


def missing_glyphs(font, text: str) -> list[str]:
    """Characters the font cannot draw (rendered as the .notdef 'tofu' box)."""
    if not isinstance(font, ImageFont.FreeTypeFont):
        return []
    ref = _notdef(font)
    out: list[str] = []
    for ch in dict.fromkeys(text):
        if ch.isspace() or ord(ch) < 0x20 or ch in out:
            continue
        try:
            m = font.getmask(ch)
            sig = bytes(m) + bytes(str(m.size), "ascii")
        except Exception:
            continue
        if ref is not None and sig == ref:
            out.append(ch)
    return out
