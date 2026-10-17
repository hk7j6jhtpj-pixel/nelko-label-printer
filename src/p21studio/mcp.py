"""Minimal MCP (Model Context Protocol) server over stdio, no SDK dependency.

Lets AI agents (Claude Code, Claude Desktop, other MCP clients) design labels,
look at the rendered preview image, and print.
"""

from __future__ import annotations

import base64
import json
import sys
import traceback
from pathlib import Path

from . import __version__, api, discovery, fonts, label as labelmod
from .render import text_label

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PROTOCOL_VERSION = "2025-06-18"

DESIGN_GUIDE = """\
# Designing Nelko P21 labels

- Units are printer dots (203 dpi, ~8 dots = 1 mm). Output is pure black/white (no grey).
- Default media 14x40 mm, landscape: canvas 284 (x, along the tape) x 96 (y, across the tape) dots.
  Other lengths: width = round(length_mm * 7.1). orientation="portrait" swaps the axes (96 wide).
- Elements are drawn in order (later = on top). Every element except "line" has a box x, y, w, h.
- text: size "auto" (default) picks the largest font that fits the box; wrap=true wraps to the box width.
  Use bold=true for small text. Text under ~12 dots tall gets hard to read. "\\n" = new line.
  vertical=true gives Japanese vertical writing. Fonts: "auto" picks an installed Japanese-capable font.
- qr: needs at least 2 dots per module. A short URL (25 modules) needs a box >= 50 dots; use 76-90 on 14 mm tape.
- barcode: code128 needs roughly (11 * chars + 35) dots of width at 1 dot per module; prefer 2+ dots per module.
- rect/ellipse: stroke width in dots, fill=true for solid. color="white" draws white on top of black (knock-out).
- Variables: "{{name}}" in text/data is replaced from `variables`; also {{date}}, {{date:%m/%d}}, {{time}}, {{n}}.
- Keep ~3 dots of margin from the canvas edges; the printer may clip the outermost dots.
- Always call p21_render and look at the image before printing. Fix any warnings it reports.
- Printing uses real labels: print only when the user asked for it, and print 1 copy unless told otherwise.
"""


class InvalidParams(ValueError):
    """Bad tool name or arguments: reported as JSON-RPC -32602."""


def _int(args: dict, key: str, default: int | None, lo: int, hi: int) -> int | None:
    v = args.get(key)
    if v is None:
        return default
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise InvalidParams(f"{key} must be an integer between {lo} and {hi}")
    if not lo <= v <= hi:
        raise InvalidParams(f"{key} must be between {lo} and {hi} (got {v})")
    return v


def _label_arg(args: dict) -> dict:
    given = [k for k in ("label", "text", "template") if args.get(k) not in (None, "")]
    if len(given) > 1:
        raise InvalidParams(f"pass only one of label / text / template (got {', '.join(given)})")
    if args.get("template"):
        from .templates import load_template

        return load_template(str(args["template"]))
    if args.get("text") is not None:
        media = args.get("media") or "14x40"
        if media not in labelmod.MEDIA_PRESETS:
            raise InvalidParams(f"media must be one of {', '.join(labelmod.MEDIA_PRESETS)}")
        return text_label(str(args["text"]), media)
    lab = args.get("label")
    if isinstance(lab, str):
        try:
            lab = json.loads(lab)
        except ValueError as e:
            raise InvalidParams(f"label is not valid JSON: {e}")
    if not isinstance(lab, dict):
        raise InvalidParams("pass `label` (a label JSON object, see p21_label_format) or `text`")
    return labelmod.normalize(lab)


def _label_schema() -> dict:
    s = labelmod.json_schema()
    s.pop("$schema", None)
    return s


LABEL_PROP = _label_schema()
TEXT_PROP = {"type": "string", "description": "Shortcut: a plain text label (auto-sized, bold) instead of `label`. Use `\\n` for line breaks."}
TEMPLATE_PROP = {"type": "string", "description": "Name of a saved template (see p21_list_templates) instead of `label`; fill its {{variables}} with `variables`/`rows`"}
MEDIA_PROP = {"enum": list(labelmod.MEDIA_PRESETS), "default": "14x40", "description": "Label roll size for `text` (ignored with `label`)"}
VARS_PROP = {"type": "object", "additionalProperties": {"type": "string"}, "description": "Values for {{name}} placeholders"}
DEVICE_PROP = {"type": "string", "description": "Printer spec, e.g. ble:AA:BB:CC:DD:EE:FF, rfcomm:AA:BB:CC:DD:EE:FF, serial:COM5, mock: (simulated). Omit to use the server's default."}

TOOLS = [
    {
        "name": "p21_label_format",
        "title": "Label format and design guide",
        "description": "Get the P21 label JSON format (JSON Schema) and design guidelines. Call this first.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "p21_render",
        "title": "Preview a label",
        "description": "Render a label to a preview PNG exactly as it will print, without printing. Returns the image, warnings (fix them) and each element's box and chosen font size.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": LABEL_PROP,
                "text": TEXT_PROP,
                "template": TEMPLATE_PROP,
                "media": MEDIA_PROP,
                "variables": VARS_PROP,
                "scale": {"type": "integer", "default": 3, "minimum": 1, "maximum": 8, "description": "Preview magnification"},
                "debug": {"type": "boolean", "default": False, "description": "Outline element boxes with their index"},
            },
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "p21_print",
        "title": "Print on the P21",
        "description": (
            "Print a label on the Nelko P21. This uses real labels: call it only when the user asked to print, "
            "with 1 copy unless told otherwise, after checking p21_render. `rows` prints one label per item. "
            "With device mock: the result contains mock_png, the image the simulated printer received."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": LABEL_PROP,
                "text": TEXT_PROP,
                "template": TEMPLATE_PROP,
                "media": MEDIA_PROP,
                "variables": VARS_PROP,
                "rows": {"type": "array", "items": VARS_PROP, "maxItems": 500, "description": "Batch: one label per item"},
                "copies": {"type": "integer", "minimum": 1, "maximum": 99, "default": 1},
                "density": {"type": "integer", "minimum": 1, "maximum": 15, "description": "Darkness (default 15)"},
                "device": DEVICE_PROP,
            },
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
    },
    {
        "name": "p21_status",
        "title": "Printer status",
        "description": "Printer readiness, loaded label size (from the roll's RFID tag), battery and firmware.",
        "inputSchema": {"type": "object", "properties": {"device": DEVICE_PROP}},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "p21_scan",
        "title": "Find printers",
        "description": "Search for P21 printers (paired Bluetooth Classic devices, serial ports, BLE). Takes ~5 s.",
        "inputSchema": {"type": "object", "properties": {"ble": {"type": "boolean", "default": True}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "p21_fonts",
        "title": "List fonts",
        "description": "List installed font families usable in the `font` field of text elements.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "description": "Case-insensitive filter"}}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "p21_list_templates",
        "title": "List saved templates",
        "description": "List the user's saved label templates (name, media, {{variables}}). Use one with `template` in p21_render / p21_print.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "p21_save_template",
        "title": "Save a template",
        "description": "Save a label design as a reusable template (also shown in the GUI). Put {{placeholders}} where values change. Refuses to replace an existing template unless overwrite=true.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "maxLength": 60}, "label": LABEL_PROP, "overwrite": {"type": "boolean", "default": False}},
            "required": ["name", "label"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
    },
    {
        "name": "p21_save_label",
        "title": "Save a label file",
        "description": "Save a label as a .p21.json file (relative paths are relative to the server's working directory) so the user can open it in the GUI or print it with the CLI. Returns the absolute path.",
        "inputSchema": {"type": "object", "properties": {"label": LABEL_PROP, "path": {"type": "string", "description": "Must end in .json"}}, "required": ["label", "path"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "p21_load_label",
        "title": "Load a label file",
        "description": "Load a .p21.json label file. Returns the label as written in the file plus validation warnings.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def _text(obj) -> dict:
    return {"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2, default=str)}


def _absolutize_images(label: dict) -> dict:
    """Make relative image paths absolute so a saved file still finds its images."""
    base = Path(label.get("_base_dir") or ".")
    for el in label.get("elements", []):
        src = el.get("src")
        if el.get("type") == "image" and isinstance(src, str) and src and not src.startswith("data:") and not Path(src).is_absolute():
            el["src"] = str((base / src).resolve())
    return label


class Server:
    def __init__(self, default_device: str | None = None):
        self.default_device = default_device

    def _device(self, args: dict) -> str | None:
        dev = args.get("device")
        if not dev or dev == "auto":  # never let "auto" bypass the server's configured device
            return self.default_device
        return dev

    def call(self, name: str, args: dict) -> list[dict]:
        if not isinstance(name, str) or name not in TOOL_NAMES:
            raise InvalidParams(f"unknown tool: {name!r}")
        if not isinstance(args, dict):
            raise InvalidParams("arguments must be an object")
        if name == "p21_label_format":
            return [_text(DESIGN_GUIDE), _text(labelmod.json_schema())]
        if name == "p21_render":
            lab = _label_arg(args)
            scale = _int(args, "scale", 3, 1, 8)
            r = api.render_label(lab, args.get("variables"))
            png = base64.b64encode(r.png_bytes(scale, bool(args.get("debug")))).decode()
            info = {"canvas": {"width": r.size[0], "height": r.size[1]}, "warnings": r.warnings, "elements": r.boxes, "variables": labelmod.find_variables(lab)}
            return [{"type": "image", "data": png, "mimeType": "image/png"}, _text(info)]
        if name == "p21_print":
            lab = _label_arg(args)
            copies = _int(args, "copies", None, 1, 99)
            density = _int(args, "density", None, 1, 15)
            rows = args.get("rows")
            if rows is not None and (not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows)):
                raise InvalidParams("rows must be an array of objects")
            return [_text(api.print_labels(lab, self._device(args), copies, density, args.get("variables"), rows))]
        if name == "p21_status":
            return [_text(api.printer_info(self._device(args)))]
        if name == "p21_scan":
            return [_text(discovery.discover(ble=bool(args.get("ble", True))))]
        if name == "p21_fonts":
            q = str(args.get("query") or "").lower()
            return [_text({"auto": fonts.describe("auto"), "preferred": fonts.available_preferred(), "families": [f for f in fonts.families() if q in f.lower()]})]
        if name == "p21_list_templates":
            from . import templates

            return [_text({"dir": str(templates.templates_dir()), "templates": [{k: v for k, v in t.items() if k != "path"} for t in templates.list_templates()]})]
        if name == "p21_save_template":
            from . import templates

            if not isinstance(args.get("name"), str):
                raise InvalidParams("name must be a string")
            path = templates.save_template(args["name"], _label_arg({"label": args.get("label")}), overwrite=bool(args.get("overwrite")))
            return [_text({"ok": True, "name": args["name"], "path": str(path)})]
        if name == "p21_save_label":
            path = Path(str(args.get("path") or "")).expanduser()
            if path.suffix.lower() != ".json":
                raise InvalidParams("path must end in .json (e.g. name.p21.json)")
            lab = _absolutize_images(labelmod.normalize(args.get("label") or {}))
            path.write_text(labelmod.dump(lab), encoding="utf-8")
            return [_text({"ok": True, "path": str(path.resolve())})]
        if name == "p21_load_label":
            raw = str(args.get("path") or "")
            path = Path(raw).expanduser()
            if raw in ("", "-") or not path.is_file():
                raise InvalidParams(f"file not found: {raw!r}")
            data = json.loads(path.read_text(encoding="utf-8"))
            lab = labelmod.load(path)  # validates; raises LabelError on bad content
            warnings = api.render_label(lab).warnings
            return [_text({"path": str(path.resolve()), "label": data, "warnings": warnings})]
        raise InvalidParams(f"unknown tool: {name!r}")

    @staticmethod
    def _error(mid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    def handle(self, msg) -> dict | None:
        if not isinstance(msg, dict):
            return self._error(None, -32600, "invalid request: expected a JSON object")
        if "method" not in msg:
            return None  # a response from the client (we never send requests): ignore
        mid = msg.get("id")
        if "id" not in msg:
            return None  # notification
        if msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str) or not isinstance(mid, (str, int)) or isinstance(mid, bool):
            return self._error(mid if isinstance(mid, (str, int)) else None, -32600, "invalid request")
        method = msg["method"]
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return self._error(mid, -32602, "params must be an object")
        try:
            if method == "initialize":
                requested = params.get("protocolVersion")
                result = {
                    "protocolVersion": requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "p21studio", "title": "P21 Studio", "version": __version__},
                    "instructions": DESIGN_GUIDE,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    content = self.call(params.get("name"), params.get("arguments") or {})
                    result = {"content": content, "isError": False}
                except InvalidParams as e:
                    return self._error(mid, -32602, str(e))
                except Exception as e:  # tool failures are reported to the model, not as protocol errors
                    result = {"content": [_text(str(e) if isinstance(e, (labelmod.LabelError, OSError)) else f"{type(e).__name__}: {e}")], "isError": True}
            else:
                return self._error(mid, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return self._error(mid, -32603, str(e))


def serve_stdio(default_device: str | None = None) -> None:
    server = Server(default_device)
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            resp = Server._error(None, -32700, "parse error")
        else:
            resp = server.handle(msg)  # batches (arrays) were removed in 2025-06-18 and are rejected
        if resp is not None:
            out.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
            out.flush()
