"""Label document model (.p21.json): defaults, validation, variables and JSON schema."""

from __future__ import annotations

import copy
import csv
import datetime as _dt
import io
import json
import math
import re
import sys
from pathlib import Path

from . import protocol

FORMAT_VERSION = 1

MEDIA_PRESETS = {
    "14x40": {"width_mm": 14, "length_mm": 40},
    "12x40": {"width_mm": 12, "length_mm": 40},
    "15x30": {"width_mm": 15, "length_mm": 30},
    "15x40": {"width_mm": 15, "length_mm": 40},
    "15x50": {"width_mm": 15, "length_mm": 50},
    "14x50": {"width_mm": 14, "length_mm": 50},
    "14x75": {"width_mm": 14, "length_mm": 75},
}

ELEMENT_TYPES = ("text", "qr", "barcode", "image", "rect", "ellipse", "line")

COMMON_DEFAULTS = {"x": 0, "y": 0, "w": 50, "h": 30, "rotate": 0, "color": "black"}
TYPE_DEFAULTS: dict[str, dict] = {
    "text": {
        "text": "",
        "font": "auto",
        "size": "auto",
        "min_size": 8,
        "max_size": 200,
        "bold": False,
        "italic": False,
        "underline": False,
        "align": "center",
        "valign": "middle",
        "line_spacing": 1.15,
        "letter_spacing": 0,
        "wrap": True,
        "vertical": False,
        "stroke": 0,
    },
    "qr": {"data": "", "ecc": "M", "border": 0},
    "barcode": {"data": "", "symbology": "code128", "show_text": True, "text_size": 14, "font": "auto"},
    "image": {"src": "", "fit": "contain", "dither": "floyd", "threshold": 128, "invert": False, "brightness": 1.0, "contrast": 1.0},
    "rect": {"fill": False, "stroke": 2, "radius": 0},
    "ellipse": {"fill": False, "stroke": 2},
    "line": {"x1": 0, "y1": 0, "x2": 50, "y2": 0, "width": 2},
}


class LabelError(ValueError):
    pass


def canvas_size(label: dict) -> tuple[int, int]:
    """(width, height) of the design canvas in dots."""
    media = label["media"]
    length = int(media.get("length_dots") or protocol.length_dots(media["length_mm"]))
    if label.get("orientation") == "portrait":
        return protocol.HEAD_DOTS, length
    return length, protocol.HEAD_DOTS


def new_label(media: str | dict = "14x40", elements: list | None = None, **kw) -> dict:
    m = dict(MEDIA_PRESETS[media]) if isinstance(media, str) else dict(media)
    return normalize({"version": FORMAT_VERSION, "media": m, "elements": elements or [], **kw})


def _num(v, path: str, errors: list[str], integer: bool = False, lo: float | None = None, hi: float | None = None):
    fallback = lo if lo is not None and lo > 0 else 0
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(v)
        except (TypeError, ValueError):
            errors.append(f"{path}: must be a number (got {v!r})")
            return fallback
    if not math.isfinite(v):
        errors.append(f"{path}: must be a finite number (got {v!r})")
        return fallback
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        errors.append(f"{path}: must be between {lo} and {hi} (got {v!r})")
        if lo is not None:
            v = max(v, lo)
        if hi is not None:
            v = min(v, hi)
    return int(round(v)) if integer else v


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off", ""}


def _bool(v, path: str, errors: list[str]) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in _TRUE | _FALSE:
        return v.strip().lower() in _TRUE
    errors.append(f"{path}: must be true or false (got {v!r})")
    return False


BOOL_FIELDS = ("bold", "italic", "underline", "wrap", "vertical", "fill", "show_text", "invert")
COORD_MAX = 10000
KNOWN_EXTRA_KEYS = {"type", "id", "name", "locked", "hidden", "note"}


def unknown_keys(el: dict) -> list[str]:
    """Keys of an element that the renderer does not know (likely typos)."""
    known = set(COMMON_DEFAULTS) | set(TYPE_DEFAULTS.get(el.get("type"), {})) | KNOWN_EXTRA_KEYS
    return [k for k in el if k not in known and not k.startswith("_")]


def normalize(label: dict) -> dict:
    """Return a copy of the label with defaults filled in. Raises LabelError on invalid input."""
    if not isinstance(label, dict):
        raise LabelError("Label must be a JSON object")
    errors: list[str] = []
    out = copy.deepcopy(label)
    out.setdefault("version", FORMAT_VERSION)
    media = out.get("media") or "14x40"
    if isinstance(media, str):
        if media not in MEDIA_PRESETS:
            errors.append(f"media: unknown preset {media!r} ({', '.join(MEDIA_PRESETS)})")
            media = "14x40"
        media = dict(MEDIA_PRESETS[media])
    elif not isinstance(media, dict):
        errors.append("media: must be a preset name or an object")
        media = dict(MEDIA_PRESETS["14x40"])
    media.setdefault("width_mm", 14)
    media.setdefault("length_mm", 40)
    media["width_mm"] = _num(media["width_mm"], "media.width_mm", errors, lo=5, hi=30)
    media["length_mm"] = _num(media["length_mm"], "media.length_mm", errors, lo=5, hi=300)
    if media.get("length_dots") is not None:
        media["length_dots"] = _num(media["length_dots"], "media.length_dots", errors, integer=True, lo=8, hi=2400)
    out["media"] = media
    out.setdefault("orientation", "landscape")
    if out["orientation"] not in ("landscape", "portrait"):
        errors.append("orientation: must be landscape or portrait")
    out["invert"] = _bool(out.get("invert", False), "invert", errors)
    out["density"] = _num(out.get("density", 15), "density", errors, integer=True, lo=1, hi=15)
    out["copies"] = _num(out.get("copies", 1), "copies", errors, integer=True, lo=1, hi=99)
    variables = out.get("variables") or {}
    if not isinstance(variables, dict):
        errors.append('variables: must be an object like {"name": "value"}')
        variables = {}
    out["variables"] = {str(k): "" if v is None else str(v) for k, v in variables.items()}
    elements = out.get("elements", [])
    if not isinstance(elements, list):
        errors.append("elements: must be an array")
        elements = []
    norm = []
    for i, el in enumerate(elements):
        p = f"elements[{i}]"
        if not isinstance(el, dict):
            errors.append(f"{p}: must be an object")
            continue
        t = el.get("type")
        if t not in ELEMENT_TYPES:
            errors.append(f"{p}.type: {t!r} is unknown ({', '.join(ELEMENT_TYPES)})")
            continue
        e = {**COMMON_DEFAULTS, **TYPE_DEFAULTS[t], **el}
        if t == "line":
            for k in ("x1", "y1", "x2", "y2"):
                e[k] = _num(e[k], f"{p}.{k}", errors, integer=True, lo=-COORD_MAX, hi=COORD_MAX)
            e["width"] = _num(e["width"], f"{p}.width", errors, integer=True, lo=1, hi=200)
            e["x"], e["y"] = min(e["x1"], e["x2"]), min(e["y1"], e["y2"])
            e["w"], e["h"] = abs(e["x2"] - e["x1"]), abs(e["y2"] - e["y1"])
        for k in ("x", "y"):
            e[k] = _num(e[k], f"{p}.{k}", errors, integer=True, lo=-COORD_MAX, hi=COORD_MAX)
        for k in ("w", "h"):
            e[k] = _num(e[k], f"{p}.{k}", errors, integer=True, lo=0 if t == "line" else 1, hi=COORD_MAX)
        try:
            e["rotate"] = int(e["rotate"])
        except (TypeError, ValueError):
            pass
        if e["rotate"] not in (0, 90, 180, 270):
            errors.append(f"{p}.rotate: must be 0/90/180/270")
        if e["color"] not in ("black", "white"):
            errors.append(f"{p}.color: must be black or white")
        for k in BOOL_FIELDS:
            if k in e:
                e[k] = _bool(e[k], f"{p}.{k}", errors)
        for k in ("text", "data", "src"):
            if k in e:
                e[k] = "" if e[k] is None else str(e[k])
        if t == "text":
            if e["size"] not in ("auto", None):
                e["size"] = _num(e["size"], f"{p}.size", errors, integer=True, lo=1, hi=1000)
            e["min_size"] = _num(e["min_size"], f"{p}.min_size", errors, integer=True, lo=1, hi=1000)
            e["max_size"] = _num(e["max_size"], f"{p}.max_size", errors, integer=True, lo=1, hi=1000)
            if e["min_size"] > e["max_size"]:
                errors.append(f"{p}: min_size must not exceed max_size")
            e["line_spacing"] = _num(e["line_spacing"], f"{p}.line_spacing", errors, lo=0.5, hi=5)
            e["letter_spacing"] = _num(e["letter_spacing"], f"{p}.letter_spacing", errors, integer=True, lo=-50, hi=200)
            e["stroke"] = _num(e["stroke"], f"{p}.stroke", errors, integer=True, lo=0, hi=20)
            if not isinstance(e["font"], str) or not e["font"]:
                e["font"] = "auto"
            if e["align"] not in ("left", "center", "right"):
                errors.append(f"{p}.align: left/center/right")
            if e["valign"] not in ("top", "middle", "bottom"):
                errors.append(f"{p}.valign: top/middle/bottom")
        if t == "qr":
            if e["ecc"] not in ("L", "M", "Q", "H"):
                errors.append(f"{p}.ecc: L/M/Q/H")
            e["border"] = _num(e["border"], f"{p}.border", errors, integer=True, lo=0, hi=10)
        if t == "barcode":
            e["text_size"] = _num(e["text_size"], f"{p}.text_size", errors, integer=True, lo=4, hi=200)
        if t in ("rect", "ellipse"):
            e["stroke"] = _num(e["stroke"], f"{p}.stroke", errors, integer=True, lo=1, hi=200)
        if t == "rect":
            e["radius"] = _num(e["radius"], f"{p}.radius", errors, integer=True, lo=0, hi=COORD_MAX)
        if t == "image":
            if e["fit"] not in ("contain", "cover", "stretch"):
                errors.append(f"{p}.fit: contain/cover/stretch")
            if e["dither"] not in ("floyd", "threshold"):
                errors.append(f"{p}.dither: floyd/threshold")
            e["threshold"] = _num(e["threshold"], f"{p}.threshold", errors, integer=True, lo=0, hi=255)
            e["brightness"] = _num(e["brightness"], f"{p}.brightness", errors, lo=0, hi=10)
            e["contrast"] = _num(e["contrast"], f"{p}.contrast", errors, lo=0, hi=10)
        norm.append(e)
    out["elements"] = norm
    if errors:
        raise LabelError("\n".join(errors))
    return out


def load(path: str | Path) -> dict:
    """Load a label from a file path, or '-' for stdin."""
    try:
        if str(path) == "-":
            data = json.loads(sys.stdin.read())
        else:
            p = Path(path)
            if p.is_dir():
                raise LabelError(f"{path} is a folder. Give a label JSON file.")
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                data.setdefault("_base_dir", str(p.resolve().parent))
    except UnicodeDecodeError:
        raise LabelError(f"{path} is not UTF-8 text")
    except ValueError as e:
        if isinstance(e, LabelError):
            raise
        raise LabelError(f"{path} is not valid JSON: {e}")
    return normalize(data)


def dump(label: dict) -> str:
    clean = {k: v for k, v in label.items() if not k.startswith("_")}
    return json.dumps(clean, ensure_ascii=False, indent=2)


# --- variables --------------------------------------------------------------
VAR_RE = re.compile(r"\{\{\s*([^{}\s:]+)\s*(?::([^}]*))?\}\}")
SUBST_FIELDS = ("text", "data", "src")


def find_variables(label: dict) -> list[str]:
    names: list[str] = []
    for el in label.get("elements", []):
        for f in SUBST_FIELDS:
            for m in VAR_RE.finditer(str(el.get(f, ""))):
                if m.group(1) not in names and m.group(1) not in ("date", "time", "datetime", "n"):
                    names.append(m.group(1))
    return names


def substitute(label: dict, values: dict | None = None, n: int = 1) -> dict:
    """Replace {{name}}, {{date}}, {{date:%m/%d}}, {{time}}, {{n}}, {{n:03}} in text/data/src fields."""
    vals = {**(label.get("variables") or {}), **{k: v for k, v in (values or {}).items() if v is not None}}
    now = _dt.datetime.now()

    def rep(m: re.Match) -> str:
        name, fmt = m.group(1), m.group(2)
        if name in vals:
            return str(vals[name])
        try:
            if name == "date":
                return _strftime(now, fmt or "%Y-%m-%d")
            if name == "time":
                return _strftime(now, fmt or "%H:%M")
            if name == "datetime":
                return _strftime(now, fmt or "%Y-%m-%d %H:%M")
            if name == "n":
                return format(n, fmt or "")
        except ValueError:
            raise LabelError(f"Invalid format in {m.group(0)}")
        return m.group(0)

    out = copy.deepcopy(label)
    for el in out["elements"]:
        for f in SUBST_FIELDS:
            if isinstance(el.get(f), str) and "{{" in el[f]:
                el[f] = VAR_RE.sub(rep, el[f])
    return out


_STRFTIME_OK = set("aAbBcdHIjmMpSUwWyYzZGuV%")


def _strftime(dt: _dt.datetime, fmt: str) -> str:
    codes = re.findall(r"%-?(.)", fmt.replace("%%", ""))
    if any(c not in _STRFTIME_OK for c in codes):
        raise ValueError(fmt)
    if sys.platform == "win32":
        fmt = fmt.replace("%-", "%#")  # no-padding flag differs on Windows
    return dt.strftime(fmt)


def unresolved(label: dict) -> list[str]:
    """Placeholders still present after substitution."""
    out: list[str] = []
    for el in label.get("elements", []):
        for f in SUBST_FIELDS:
            out += [m.group(0) for m in VAR_RE.finditer(str(el.get(f, "")))]
    return out


def read_csv(path_or_text: str, is_text: bool = False) -> list[dict]:
    if is_text:
        text = path_or_text
    else:
        data = Path(path_or_text).read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp932")  # Excel on Japanese Windows saves Shift_JIS
            except UnicodeDecodeError:
                raise LabelError(f"Cannot detect encoding of {path_or_text} (save as UTF-8)")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        row = {k.strip(): ("" if v is None else str(v)) for k, v in r.items() if isinstance(k, str)}
        if any(v.strip() for v in row.values()):
            rows.append(row)
    return rows


# --- JSON schema (for AI tools) --------------------------------------------
def json_schema() -> dict:
    box = {
        "x": {"type": "integer", "description": "Top-left X (dots)"},
        "y": {"type": "integer", "description": "Top-left Y (dots)"},
        "w": {"type": "integer", "minimum": 1},
        "h": {"type": "integer", "minimum": 1},
        "rotate": {"enum": [0, 90, 180, 270], "default": 0, "description": "Rotate content within the box (clockwise)"},
        "color": {"enum": ["black", "white"], "default": "black", "description": "white knocks out white over black shapes"},
    }

    def el(t: str, props: dict, required: list[str]) -> dict:
        return {
            "type": "object",
            "properties": {"type": {"const": t}, **box, **props},
            "required": ["type", *required],
        }

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "P21 label (.p21.json)",
        "description": (
            "Nelko P21 label. Coordinates are printer dots (203 dpi, ~8 dots/mm) on the design canvas. "
            "Landscape 14x40 mm canvas = 284 x 96 dots (x across the label length, y across the 12 mm printable tape width). "
            "Canvas width = round(length_mm * 7.1) for other lengths. Output is pure black/white."
        ),
        "type": "object",
        "properties": {
            "version": {"const": 1},
            "name": {"type": "string"},
            "media": {
                "description": "Label roll size. Preset name (e.g. 14x40) or object",
                "oneOf": [
                    {"enum": list(MEDIA_PRESETS)},
                    {
                        "type": "object",
                        "properties": {
                            "width_mm": {"type": "number", "default": 14},
                            "length_mm": {"type": "number", "default": 40},
                            "length_dots": {"type": "integer", "description": "Override print length in dots (usually not needed)"},
                        },
                    },
                ],
            },
            "orientation": {"enum": ["landscape", "portrait"], "default": "landscape"},
            "invert": {"type": "boolean", "default": False, "description": "Invert black and white"},
            "density": {"type": "integer", "minimum": 1, "maximum": 15, "default": 15},
            "copies": {"type": "integer", "minimum": 1, "default": 1},
            "variables": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Default values for {{name}}"},
            "elements": {
                "type": "array",
                "description": "Later elements are drawn on top",
                "items": {
                    "oneOf": [
                        el("text", {
                            "text": {"type": "string", "description": "\\n for newline. Supports {{var}} {{date}} {{date:%m/%d}} {{time}} {{n}}"},
                            "font": {"type": "string", "default": "auto", "description": "auto / family name / font file path"},
                            "size": {"oneOf": [{"const": "auto"}, {"type": "integer"}], "default": "auto", "description": "Font size (px, em units, so glyphs are slightly smaller). auto = largest size that fits the box"},
                            "min_size": {"type": "integer", "default": 8},
                            "max_size": {"type": "integer", "default": 200},
                            "bold": {"type": "boolean", "default": False},
                            "italic": {"type": "boolean", "default": False, "description": "slanted (synthetic oblique)"},
                            "underline": {"type": "boolean", "default": False},
                            "align": {"enum": ["left", "center", "right"], "default": "center"},
                            "valign": {"enum": ["top", "middle", "bottom"], "default": "middle"},
                            "line_spacing": {"type": "number", "default": 1.15},
                            "letter_spacing": {"type": "integer", "default": 0},
                            "wrap": {"type": "boolean", "default": True, "description": "Wrap at box width"},
                            "vertical": {"type": "boolean", "default": False, "description": "Vertical text"},
                            "stroke": {"type": "integer", "default": 0, "description": "Thicken text (px). Under 20px, even 1 can blur the glyphs"},
                        }, ["text"]),
                        el("qr", {
                            "data": {"type": "string"},
                            "ecc": {"enum": ["L", "M", "Q", "H"], "default": "M"},
                            "border": {"type": "integer", "default": 0, "description": "Quiet zone (modules)"},
                        }, ["data"]),
                        el("barcode", {
                            "data": {"type": "string"},
                            "symbology": {"type": "string", "default": "code128", "description": "code128, code39, ean13, ean8, jan, upca, itf, codabar, isbn13 ..."},
                            "show_text": {"type": "boolean", "default": True},
                            "text_size": {"type": "integer", "default": 14},
                        }, ["data"]),
                        el("image", {
                            "src": {"type": "string", "description": "File path (relative to the label file allowed) or data: URI"},
                            "fit": {"enum": ["contain", "cover", "stretch"], "default": "contain"},
                            "dither": {"enum": ["floyd", "threshold"], "default": "floyd", "description": "floyd for photos, threshold for logos/line art"},
                            "threshold": {"type": "integer", "default": 128},
                            "invert": {"type": "boolean", "default": False},
                            "brightness": {"type": "number", "default": 1.0},
                            "contrast": {"type": "number", "default": 1.0},
                        }, ["src"]),
                        el("rect", {
                            "fill": {"type": "boolean", "default": False},
                            "stroke": {"type": "integer", "default": 2},
                            "radius": {"type": "integer", "default": 0},
                        }, ["x", "y", "w", "h"]),
                        el("ellipse", {
                            "fill": {"type": "boolean", "default": False},
                            "stroke": {"type": "integer", "default": 2},
                        }, ["x", "y", "w", "h"]),
                        {
                            "type": "object",
                            "properties": {
                                "type": {"const": "line"},
                                "x1": {"type": "integer"}, "y1": {"type": "integer"},
                                "x2": {"type": "integer"}, "y2": {"type": "integer"},
                                "width": {"type": "integer", "default": 2},
                                "color": {"enum": ["black", "white"], "default": "black"},
                            },
                            "required": ["type", "x1", "y1", "x2", "y2"],
                        },
                    ]
                },
            },
        },
        "required": ["elements"],
    }


EXAMPLE = {
    "version": 1,
    "name": "example",
    "media": {"width_mm": 14, "length_mm": 40},
    "elements": [
        {"type": "rect", "x": 0, "y": 0, "w": 284, "h": 96, "stroke": 3, "radius": 10},
        {"type": "qr", "x": 8, "y": 10, "w": 76, "h": 76, "data": "https://example.com"},
        {"type": "text", "x": 92, "y": 8, "w": 184, "h": 52, "text": "{{title}}", "bold": True},
        {"type": "text", "x": 92, "y": 62, "w": 184, "h": 26, "text": "{{date}}"},
    ],
    "variables": {"title": "Sample label"},
}
