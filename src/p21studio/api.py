"""Operations shared by the CLI, the GUI server and the MCP server."""

from __future__ import annotations

import threading
from pathlib import Path

from . import config, discovery, label as labelmod
from .printer import P21, PrintResult
from .render import RenderResult, image_label, render, text_label
from .transports import TransportError

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}

# Bluetooth links do not like concurrent sessions: serialise all printer access.
printer_lock = threading.RLock()


class NoDeviceError(TransportError):
    pass


def resolve_device(spec: str | None = None, scan_ble: bool = True) -> str:
    if spec and spec != "auto":
        return spec
    saved = config.default_device()
    if saved:
        return saved
    found = discovery.discover(ble=scan_ble)
    if found["suggestions"]:
        return found["suggestions"][0]["device"]
    raise NoDeviceError(
        "プリンタが見つかりません。電源を入れてペアリングするか、`p21 scan` で探して `p21 use <device>` で指定してください。"
    )


def load_source(source: str | None = None, text: str | None = None, image: str | None = None, media: str | None = None, font: str = "auto", vertical: bool = False) -> dict:
    """Build a label from a .json file, an image file, or plain text."""
    if text is not None:
        return text_label(text.replace("\\n", "\n"), media or "14x40", font=font, vertical=vertical)
    if image is not None or (source and Path(source).suffix.lower() in IMAGE_EXTS):
        return image_label(image or source, media or "14x40")
    if not source:
        raise labelmod.LabelError("ラベルファイル、--text、--image のいずれかを指定してください")
    lab = labelmod.load(source)
    if media:
        lab["media"] = dict(labelmod.MEDIA_PRESETS[media])
    return lab


def _check_values(values, rows=None) -> None:
    if values is not None and not isinstance(values, dict):
        raise labelmod.LabelError("values は {\"名前\": \"値\"} のオブジェクトにしてください")
    if rows is not None and (not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows)):
        raise labelmod.LabelError("rows はオブジェクトの配列にしてください")


def expand(label: dict, values: dict | None = None, rows: list[dict] | None = None) -> list[dict]:
    """One label per CSV row (or one label), with variables substituted."""
    _check_values(values, rows)
    if rows:
        return [labelmod.substitute(label, {**(values or {}), **row}, n=i + 1) for i, row in enumerate(rows)]
    return [labelmod.substitute(label, values or {}, n=1)]


def render_label(label: dict, values: dict | None = None) -> RenderResult:
    _check_values(values)
    return render(labelmod.substitute(labelmod.normalize(label), values or {}))


class RenderErrors(labelmod.LabelError):
    """Some elements could not be drawn; printing would produce a wrong label."""


def print_labels(
    label: dict,
    device: str | None = None,
    copies: int | None = None,
    density: int | None = None,
    values: dict | None = None,
    rows: list[dict] | None = None,
    force: bool = False,
) -> dict:
    """Render and print. Never resends a job; on a mid-batch failure returns what was printed."""
    label = labelmod.normalize(label)
    results: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    rendered = []
    for lab in expand(label, values, rows):
        r = render(lab)
        warnings += r.warnings
        errors += r.errors
        rendered.append(r)
    if errors and not force:
        raise RenderErrors("描画できない要素があるので印刷しません:\n" + "\n".join(dict.fromkeys(errors)))
    spec = resolve_device(device)
    media = label["media"]
    copies = copies if copies is not None else label.get("copies", 1)
    copies = max(1, min(99, int(copies)))
    density = density if density is not None else label.get("density", 15)
    error = None
    with printer_lock, P21(spec) as p:
        if hasattr(p.transport, "design_orientation"):  # mock: save the image the way it was designed
            p.transport.design_orientation = label.get("orientation", "landscape")
        for r in rendered:
            try:
                res: PrintResult = p.print_image(r.printer_image(), width_mm=media["width_mm"], length_mm=media["length_mm"], density=density, copies=copies)
            except Exception as e:
                if not results:
                    raise  # nothing printed yet: let the caller map it to an exit code
                error = f"{type(e).__name__}: {e}"
                break
            results.append(res.to_dict())
            if not res.ok:
                break
    return {
        "ok": error is None and all(r["ok"] for r in results) and len(results) == len(rendered),
        "device": spec,
        "labels": len(rendered),
        "printed": len(results),
        "sheets": len(results) * copies,
        "error": error,
        "results": results,
        "render_warnings": list(dict.fromkeys(warnings)),
    }


def printer_info(device: str | None = None) -> dict:
    spec = resolve_device(device)
    with printer_lock, P21(spec) as p:
        return p.info()


def apply_settings(device: str | None, beep: bool | None = None, auto_off: int | None = None) -> dict:
    spec = resolve_device(device)
    with printer_lock, P21(spec) as p:
        if beep is not None:
            p.set_beep(beep)
        if auto_off is not None:
            p.set_auto_off(auto_off)
        return p.config().to_dict()
