"""Render a label document to a 1-bit printer image.

The same renderer feeds the GUI preview, the CLI preview and the printer, so
what you see is exactly what gets printed.
"""

from __future__ import annotations

import base64
import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageOps

from . import fonts
from .label import canvas_size, new_label, normalize, unknown_keys, unresolved


class SkipElement(Exception):
    """The element cannot be drawn at all (it would be missing from the print)."""


@dataclass
class RenderResult:
    image: Image.Image  # mode "L", values 0 (ink) / 255, design orientation
    label: dict
    warnings: list[str] = field(default_factory=list)  # every problem, including errors
    boxes: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # elements that could not be drawn: printing is refused

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    def printer_image(self) -> Image.Image:
        """Portrait 96 x N image in printer orientation (landscape designs are rotated clockwise)."""
        img = self.image
        if self.label.get("orientation") != "portrait":
            img = img.transpose(Image.Transpose.ROTATE_270)
        return img.convert("1", dither=Image.Dither.NONE)

    def preview(self, scale: int = 1, debug: bool = False, ink: str = "#000000", paper: str = "#ffffff") -> Image.Image:
        rgb = ImageOps.colorize(self.image, black=ink, white=paper)
        if scale > 1:
            rgb = rgb.resize((rgb.width * scale, rgb.height * scale), Image.Resampling.NEAREST)
        if debug:
            d = ImageDraw.Draw(rgb)
            for b in self.boxes:
                x, y, w, h = (v * scale for v in (b["x"], b["y"], b["w"], b["h"]))
                d.rectangle([x, y, x + max(w, 1) - 1, y + max(h, 1) - 1], outline="#e5484d", width=max(1, scale // 2))
                d.text((x + 2, y + 1), str(b["index"]), fill="#e5484d")
        return rgb

    def png_bytes(self, scale: int = 1, debug: bool = False) -> bytes:
        buf = io.BytesIO()
        self.preview(scale, debug).save(buf, "PNG")
        return buf.getvalue()


# ---------------------------------------------------------------------------
def render(label: dict, base_dir: str | Path | None = None) -> RenderResult:
    label = normalize(label)
    base = Path(base_dir or label.get("_base_dir") or ".")
    W, H = canvas_size(label)
    canvas = Image.new("L", (W, H), 255)
    res = RenderResult(canvas, label)

    for ph in dict.fromkeys(unresolved(label)):
        res.warnings.append(f"{ph} に値がありません（そのまま印字されます）")

    for i, el in enumerate(label["elements"]):
        t = el["type"]
        res.boxes.append({"index": i, "type": t, "x": el["x"], "y": el["y"], "w": el["w"], "h": el["h"]})
        if el["x"] < 0 or el["y"] < 0 or el["x"] + el["w"] > W or el["y"] + el["h"] > H:
            res.warnings.append(f"elements[{i}] ({t}) がラベルの印字範囲 {W}x{H} からはみ出しています")
        for k in unknown_keys(el):
            res.warnings.append(f"elements[{i}] ({t}) の {k!r} は使われない項目です（綴り間違い？）")
        if t in ("text", "barcode") and not fonts.exists(el.get("font")):
            res.warnings.append(f"elements[{i}] ({t}) のフォント {el.get('font')!r} が見つからないので標準フォントを使います")
        fill = 0 if el["color"] == "black" else 255
        try:
            if t not in ("rect", "ellipse", "line") and (el["w"] > 4 * W + 100 or el["h"] > 4 * H + 100):
                raise SkipElement(f"枠が大きすぎます（{el['w']}x{el['h']}、ラベルは {W}x{H}）")
            if t in ("rect", "ellipse", "line"):
                mask = Image.new("L", (W, H), 0)
                _draw_shape(ImageDraw.Draw(mask), el)
                canvas.paste(fill, (0, 0), mask)
                continue
            rot = el["rotate"]
            cw, ch = (el["w"], el["h"]) if rot in (0, 180) else (el["h"], el["w"])
            mask = ELEMENT_RENDERERS[t](el, cw, ch, base, res.warnings, i)
            if "_size" in el:
                res.boxes[-1]["font_size"] = el.pop("_size")
            if mask is None:
                continue
            if rot:
                mask = mask.transpose({90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}[rot])
            canvas.paste(fill, (el["x"], el["y"]), mask)
        except SkipElement as e:
            msg = f"elements[{i}] ({t}) を描画できません: {e}"
            res.errors.append(msg)
            res.warnings.append(msg)
        except Exception as e:  # keep rendering the rest of the label
            msg = f"elements[{i}] ({t}) を描画できません: {type(e).__name__}: {e}"
            res.errors.append(msg)
            res.warnings.append(msg)

    canvas = canvas.point(lambda v: 255 if v >= 128 else 0)
    if label.get("invert"):
        canvas = ImageOps.invert(canvas)
    res.image = canvas
    return res


# --- shapes -----------------------------------------------------------------
def _draw_shape(d: ImageDraw.ImageDraw, el: dict) -> None:
    if el["type"] == "line":
        d.line([(el["x1"], el["y1"]), (el["x2"], el["y2"])], fill=255, width=max(1, el["width"]))
        return
    box = [el["x"], el["y"], el["x"] + el["w"] - 1, el["y"] + el["h"] - 1]
    stroke = max(1, el["stroke"])
    if el["type"] == "rect":
        r = max(0, min(el["radius"], min(el["w"], el["h"]) // 2))
        if el["fill"]:
            d.rounded_rectangle(box, r, fill=255)
        else:
            d.rounded_rectangle(box, r, outline=255, width=stroke)
    else:
        if el["fill"]:
            d.ellipse(box, fill=255)
        else:
            d.ellipse(box, outline=255, width=stroke)


# --- text -------------------------------------------------------------------
_REF = "Hgyj漢あ"
_NO_LINE_START = set("・、。，．,.)）]］}｝」』】〉》〕ー～!！?？:：;；ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ々")
_VERTICAL_ROTATE = set("ー－—―‐-〜～…‥()（）「」『』【】[]［］{}｛｝<>＜＞〈〉《》〔〕=＝→←")
_VERTICAL_SHIFT = set("、。，．")
_SMALL_KANA = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ")
_WORD_RE = re.compile(r"[A-Za-z0-9À-ɏ'’\-_.@/#%&+]+\s*|\s+|.", re.S)


class _Face:
    def __init__(self, el: dict, size: int):
        self.font, fake_bold = fonts.load(el["font"], size, el["bold"])
        self.size = size
        self.stroke = el["stroke"] + (max(1, round(size / 26)) if fake_bold else 0)
        self.spacing = el["letter_spacing"]
        top, bottom = self._ref_extent()
        self.top, self.bottom = top - self.stroke, bottom + self.stroke

    def _ref_extent(self) -> tuple[int, int]:
        try:
            _, t, _, b = self.font.getbbox(_REF, anchor="ls")
        except (ValueError, TypeError):
            _, t, _, b = self.font.getbbox(_REF)
        return t, b

    def width(self, s: str) -> float:
        if not s:
            return 0
        if self.spacing:
            return sum(self.font.getlength(c) for c in s) + self.spacing * (len(s) - 1) + 2 * self.stroke
        return self.font.getlength(s) + 2 * self.stroke


def _wrap(text: str, face: _Face, max_w: float, wrap: bool, break_words: bool = True) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        if not wrap or face.width(para) <= max_w:
            lines.append(para)
            continue
        cur = ""
        for tok in _WORD_RE.findall(para):
            if face.width(cur + tok) <= max_w or not cur:
                if face.width(tok) > max_w and len(tok) > 1 and break_words:
                    for c in tok:  # break an over-long word by character
                        if face.width(cur + c) <= max_w or not cur:
                            cur += c
                        else:
                            lines.append(cur.rstrip())
                            cur = c
                else:
                    cur += tok
            elif tok[0] in _NO_LINE_START:
                cur += tok  # kinsoku: keep closing punctuation on the previous line
            else:
                lines.append(cur.rstrip())
                cur = tok.lstrip()
        lines.append(cur.rstrip())
    return lines


def _layout_h(text: str, el: dict, size: int, w: int, h: int, break_words: bool = True):
    face = _Face(el, size)
    lines = _wrap(text, face, w, el["wrap"], break_words)
    pitch = size * el["line_spacing"]
    height = (len(lines) - 1) * pitch + (face.bottom - face.top)
    width = max((face.width(l) for l in lines), default=0)
    return face, lines, pitch, width, height


def _vertical_columns(text: str, rows: int) -> list[str]:
    cols: list[str] = []
    for para in text.split("\n"):
        if not para:
            cols.append("")
            continue
        while para:
            take = max(1, rows)
            cols.append(para[:take])
            para = para[take:]
    return cols


def _layout_v(text: str, el: dict, size: int, w: int, h: int, break_words: bool = True):
    face = _Face(el, size)
    cell = max(1, size + el["letter_spacing"])
    rows = max(1, int((h + el["letter_spacing"]) // cell)) if el["wrap"] else 10**6
    cols = _vertical_columns(text, rows)
    pitch = size * el["line_spacing"]
    width = (len(cols) - 1) * pitch + size
    height = max((len(c) for c in cols), default=0) * cell - el["letter_spacing"]
    return face, cols, pitch, width, height


def _render_text(el: dict, w: int, h: int, base: Path, warnings: list[str], i: int) -> Image.Image | None:
    text = el["text"]
    mask = Image.new("L", (w, h), 0)
    if not text.strip():
        return mask
    base_layout = _layout_v if el["vertical"] else _layout_h
    # prefer keeping words whole; break inside words only when nothing else fits
    break_words = False

    def layout(*a):
        return base_layout(*a, break_words=break_words)

    def fits(size: int) -> bool:
        *_, width, height = layout(text, el, size, w, h)
        return width <= w and height <= h

    if el["size"] in ("auto", None):
        lo, hi = max(1, el["min_size"]), max(el["min_size"], min(el["max_size"], h * 2 if not el["vertical"] else w * 2))
        top = hi

        def first_fit(start: int) -> int | None:
            # "fits" is not monotonic (kinsoku can push punctuation past the edge at one size only),
            # so look a little above the minimum before giving up
            for s_ in range(start, min(top, start + 40) + 1):
                if fits(s_):
                    return s_
            return None

        found = first_fit(lo)
        if found is None:
            break_words = True
            found = first_fit(lo)
        if found is None:
            size = lo
            warnings.append(f"elements[{i}] (text) の文字が最小サイズ {lo}px でも枠に収まりません")
        else:
            lo = found
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if fits(mid):
                    lo = mid
                else:
                    hi = mid - 1
            size = lo
            # wrapping/kinsoku make "fits" non-monotonic: a slightly larger size may still fit
            for s_ in range(min(top, size + 12), size, -1):
                if fits(s_):
                    size = s_
                    break
    else:
        size = int(el["size"])
        break_words = True
        if not fits(size):
            warnings.append(f"elements[{i}] (text) の文字がサイズ {size}px では枠からはみ出します")

    face, lines, pitch, width, height = layout(text, el, size, w, h)
    el["_size"] = size
    if size < 10:
        warnings.append(f"elements[{i}] (text) の文字が {size}px と小さく、印刷すると読みにくい可能性があります")
    missing = fonts.missing_glyphs(face.font, text)
    if missing:
        warnings.append(f"elements[{i}] (text) のフォントに無い文字があり、□ で印字されます: {''.join(missing[:10])}")
    d = ImageDraw.Draw(mask)
    va = el["valign"]
    y0 = 0 if va == "top" else (h - height) if va == "bottom" else (h - height) / 2

    if el["vertical"]:
        cell = max(1, size + el["letter_spacing"])
        al = el["align"]  # columns flow right-to-left; align places the block horizontally
        x_right = w - size if al == "right" else (width - size) if al == "left" else (w + width) / 2 - size
        for ci, col in enumerate(lines):
            cx = x_right - ci * pitch + size / 2
            for ri, ch in enumerate(col):
                cy = y0 + ri * cell + size / 2
                _draw_vertical_char(mask, face, ch, cx, cy, size)
        return mask

    for li, line in enumerate(lines):
        lw = face.width(line)
        al = el["align"]
        x = 0 if al == "left" else (w - lw) if al == "right" else (w - lw) / 2
        baseline = y0 - face.top + li * pitch
        _draw_line(d, face, line, x + face.stroke, baseline)
    return mask


def _draw_line(d: ImageDraw.ImageDraw, face: _Face, line: str, x: float, baseline: float) -> None:
    kw = dict(font=face.font, fill=255, anchor="ls", stroke_width=face.stroke, stroke_fill=255)
    if not face.spacing:
        d.text((x, baseline), line, **kw)
        return
    for c in line:
        d.text((x, baseline), c, **kw)
        x += face.font.getlength(c) + face.spacing


def _draw_vertical_char(mask: Image.Image, face: _Face, ch: str, cx: float, cy: float, size: int) -> None:
    if ch == " " or ch == "　":
        return
    pad = face.stroke + 2
    tile = Image.new("L", (size + 2 * pad, size + 2 * pad), 0)
    td = ImageDraw.Draw(tile)
    td.text((tile.width / 2, tile.height / 2), ch, font=face.font, fill=255, anchor="mm", stroke_width=face.stroke, stroke_fill=255)
    if ch in _VERTICAL_ROTATE:
        tile = tile.transpose(Image.Transpose.ROTATE_270)
    if ch in _VERTICAL_SHIFT:
        cx, cy = cx + size * 0.6, cy - size * 0.6
    elif ch in _SMALL_KANA:
        cx, cy = cx + size * 0.1, cy - size * 0.1
    mask.paste(255, (round(cx - tile.width / 2), round(cy - tile.height / 2)), tile)


# --- QR -----------------------------------------------------------------------
def _render_qr(el: dict, w: int, h: int, base: Path, warnings: list[str], i: int) -> Image.Image | None:
    import segno

    if not el["data"]:
        raise SkipElement("QR の内容 (data) が空です")
    try:
        qr = segno.make(el["data"], error=el["ecc"].lower(), micro=False, boost_error=False)
    except segno.DataOverflowError:
        raise SkipElement(f"QR に入りきらない長さです（{len(el['data'])} 文字）。内容を短くするか誤り訂正を L にしてください")
    rows = [list(r) for r in qr.matrix_iter(scale=1, border=max(0, el["border"]))]
    n = len(rows)
    px = min(w, h) // n
    if px < 1:
        raise SkipElement(f"QR の枠が小さすぎます（{n} モジュールに最低 {n}px、読み取りには {2 * n}px 以上必要）")
    if px < 2:
        warnings.append(f"elements[{i}] (qr) のモジュールが 1px です。読み取れない可能性があります（枠を {2 * n}px 以上に）")
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    ox, oy = (w - n * px) // 2, (h - n * px) // 2
    for y, row in enumerate(rows):
        for x, dark in enumerate(row):
            if dark:
                d.rectangle([ox + x * px, oy + y * px, ox + (x + 1) * px - 1, oy + (y + 1) * px - 1], fill=255)
    return mask


# --- 1D barcodes ----------------------------------------------------------------
def _barcode_pattern(symbology: str, data: str) -> tuple[str, str]:
    import barcode
    from barcode.errors import BarcodeError

    raw = symbology.strip().lower()
    alias = {"jan13": "jan", "ean": "ean13", "upc": "upca", "isbn": "isbn13", "nw7": "codabar", "gs1128": "gs1_128"}
    cls = None
    for name in (raw, alias.get(raw.replace("-", "").replace("_", ""), raw.replace("-", "").replace("_", ""))):
        try:
            cls = barcode.get_barcode_class(name)
            break
        except BarcodeError:
            continue
    if cls is None:
        raise SkipElement(f"未対応のバーコード種類です: {symbology}（{', '.join(barcode.PROVIDED_BARCODES)}）")
    try:
        try:
            bc = cls(data, writer=None)
        except TypeError:
            bc = cls(data)
        modules = "".join(bc.build())
    except Exception as e:
        hint = "数字 12〜13 桁" if "ean13" in cls.__name__.lower() or "jan" in raw else "数字 7〜8 桁" if "8" in raw else "使える文字"
        raise SkipElement(f"{symbology} に使えない内容です（{hint}を確認してください: {e}）")
    return modules, bc.get_fullcode()


def _render_barcode(el: dict, w: int, h: int, base: Path, warnings: list[str], i: int) -> Image.Image | None:
    if not el["data"]:
        raise SkipElement("バーコードの内容 (data) が空です")
    modules, human = _barcode_pattern(el["symbology"], el["data"])
    given = el["data"].strip()
    digits = lambda v: "".join(c for c in str(v) if c.isalnum())  # noqa: E731
    if not digits(human).startswith(digits(given)):
        warnings.append(f"elements[{i}] (barcode) の内容 {given} は {human} として印字されます（チェックデジットや桁数を確認してください）")
    n = len(modules)
    px = w // n
    if px < 1:
        raise SkipElement(f"バーコードの幅が足りません（{n} モジュールに最低 {n}px、推奨 {2 * n}px）")
    if px < 2:
        warnings.append(f"elements[{i}] (barcode) は 1 モジュール 1px です。読み取れない可能性があります（幅 {2 * n}px 以上を推奨）")
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    text_h = 0
    if el["show_text"]:
        tsize = max(6, min(el["text_size"], h // 3))
        font, _ = fonts.load(el.get("font", "auto"), tsize, False)
        while tsize > 6 and font.getlength(human) > w:
            tsize -= 1
            font, _ = fonts.load(el.get("font", "auto"), tsize, False)
        _, t, _, b = font.getbbox(human, anchor="ls")
        text_h = (b - t) + 2
        d.text((w / 2, h - b), human, font=font, fill=255, anchor="ms")
    bar_h = max(1, h - text_h)
    ox = (w - n * px) // 2
    x = 0
    guard_h = min(h, bar_h + max(0, text_h // 2))  # guard bars ("G") extend into the digits
    while x < n:
        kind = modules[x]
        if kind in "1G":
            run = 1
            while x + run < n and modules[x + run] == kind:
                run += 1
            d.rectangle([ox + x * px, 0, ox + (x + run) * px - 1, (guard_h if kind == "G" else bar_h) - 1], fill=255)
            x += run
        else:
            x += 1
    return mask


# --- images -------------------------------------------------------------------
def open_image_source(src: str, base: Path) -> Image.Image:
    try:
        if src.startswith("data:"):
            _, _, payload = src.partition(",")
            img = Image.open(io.BytesIO(base64.b64decode(payload, validate=False)))
        else:
            p = Path(src).expanduser()
            if not p.is_absolute():
                p = base / p
            if not p.is_file():
                raise SkipElement(f"画像ファイルが見つかりません: {p}")
            img = Image.open(p)
        img.load()
        return img
    except SkipElement:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise SkipElement(f"画像を読み込めません: {e}")


def _to_gray_alpha(img: Image.Image) -> tuple[Image.Image, Image.Image | None]:
    """8-bit grey composited on white, plus the alpha channel (if any)."""
    if img.mode.startswith("I;16"):
        return img.convert("I").point(lambda v: v * (1 / 256)).convert("L"), None  # 0..65535 -> 0..255
    if img.mode in ("I", "F"):
        hi = img.getextrema()[1] or 0
        k = 255 if img.mode == "F" and hi <= 1 else (1 / 256 if hi > 255 else 1)
        return img.point(lambda v: v * k).convert("L"), None
    if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
        rgba = img.convert("RGBA")
        gray = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba).convert("L")
        return gray, rgba.getchannel("A")
    return img.convert("L"), None


def _render_image(el: dict, w: int, h: int, base: Path, warnings: list[str], i: int) -> Image.Image | None:
    if not el["src"]:
        raise SkipElement("画像 (src) が指定されていません")
    img = open_image_source(el["src"], base)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    if img.width * img.height > 4_000_000:
        img.thumbnail((2000, 2000))
    img, alpha = _to_gray_alpha(img)
    if el["brightness"] != 1.0:
        img = ImageEnhance.Brightness(img).enhance(el["brightness"])
    if el["contrast"] != 1.0:
        img = ImageEnhance.Contrast(img).enhance(el["contrast"])

    def fit_to_box(im: Image.Image) -> Image.Image:
        if el["fit"] == "stretch":
            return im.resize((w, h), Image.Resampling.LANCZOS)
        if el["fit"] == "cover":
            return ImageOps.fit(im, (w, h), Image.Resampling.LANCZOS)
        scale = min(w / im.width, h / im.height)
        return im.resize((max(1, math.floor(im.width * scale)), max(1, math.floor(im.height * scale))), Image.Resampling.LANCZOS)

    img = fit_to_box(img)

    if el["dither"] == "floyd":
        bw = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG).convert("L")
    else:
        thr = el["threshold"]
        bw = img.point(lambda v: 255 if v >= thr else 0)
    ink = ImageOps.invert(bw)  # ink = 255
    if el["invert"]:
        ink = ImageOps.invert(ink)
    if alpha is not None:  # transparent pixels never get ink, even when inverted
        ink = ImageChops.multiply(ink, fit_to_box(alpha).point(lambda v: 255 if v >= 128 else 0))
    mask = Image.new("L", (w, h), 0)
    mask.paste(ink, ((w - ink.width) // 2, (h - ink.height) // 2))
    return mask


ELEMENT_RENDERERS = {
    "text": _render_text,
    "qr": _render_qr,
    "barcode": _render_barcode,
    "image": _render_image,
}


# --- quick labels -------------------------------------------------------------
def text_label(text: str, media: str | dict = "14x40", font: str = "auto", bold: bool = True, margin: int = 4, vertical: bool = False) -> dict:
    label = new_label(media)
    W, H = canvas_size(label)
    label["elements"] = [{"type": "text", "x": margin, "y": margin // 2, "w": W - 2 * margin, "h": H - margin, "text": text, "font": font, "bold": bold, "vertical": vertical}]
    return normalize(label)


def image_label(src: str, media: str | dict = "14x40", dither: str = "floyd", fit: str = "contain") -> dict:
    label = new_label(media)
    W, H = canvas_size(label)
    p = Path(src)
    if p.is_file():
        src = str(p.resolve())
        try:
            with Image.open(p) as im:  # tall images: design in portrait so they are not shrunk
                if im.height > im.width:
                    label["orientation"] = "portrait"
                    W, H = H, W
        except OSError:
            pass
    label["elements"] = [{"type": "image", "x": 0, "y": 0, "w": W, "h": H, "src": src, "dither": dither, "fit": fit}]
    return normalize(label)
