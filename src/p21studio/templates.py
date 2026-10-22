"""User templates: saved label designs, shared by the desktop app and the CLI.

Templates are ordinary label files kept in one folder (config dir /templates, or
$P21_TEMPLATES_DIR to share a folder between machines, e.g. via a synced drive).
Images are embedded as data: URIs when saving so a template is self-contained.
"""

from __future__ import annotations

import base64
import copy
import json
import mimetypes
import os
import re
import time
from pathlib import Path

from . import label as labelmod
from .config import config_dir

SUFFIX = ".p21.json"
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class TemplateError(labelmod.LabelError):
    pass


def templates_dir() -> Path:
    env = os.environ.get("P21_TEMPLATES_DIR")
    return Path(env).expanduser() if env else config_dir() / "templates"


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 60 or _BAD_CHARS.search(name) or name in (".", "..") or name.startswith("."):
        raise TemplateError("Template name must be 1-60 characters and must not contain \\ / : * ? \" < > |")
    return name


def _path(name: str) -> Path:
    return templates_dir() / (_check_name(name) + SUFFIX)


def assets_dir(name: str) -> Path:
    return templates_dir() / "assets" / _check_name(name)


def is_template_asset(path: str | Path) -> bool:
    """True if the path points inside the templates folder (safe for the GUI to read)."""
    try:
        Path(path).resolve().relative_to(templates_dir().resolve())
        return True
    except (ValueError, OSError):
        return False


def _embed_images(label: dict, name: str) -> dict:
    """Make the template self-contained.

    Fixed image paths become data: URIs. Paths that depend on a variable
    (e.g. icons/{{icon}}.png) cannot be embedded, so every file matching the
    pattern is copied into the template's asset folder and the path points there.
    """
    import shutil

    out = copy.deepcopy(label)
    base = Path(out.get("_base_dir") or ".")
    for el in out.get("elements", []):
        src = el.get("src")
        if el.get("type") != "image" or not isinstance(src, str) or not src or src.startswith("data:"):
            continue
        p = Path(src).expanduser()
        if not p.is_absolute():
            p = base / p
        if "{{" in src:
            pattern = labelmod.VAR_RE.sub("*", p.name)
            files = sorted(p.parent.glob(pattern)) if "{{" not in str(p.parent) else []
            if not files:
                raise TemplateError(f"No image matching {src} in {p.parent}")
            dest = assets_dir(name)
            dest.mkdir(parents=True, exist_ok=True)
            for f in files:
                if f.is_file():
                    shutil.copy2(f, dest / f.name)
            el["src"] = str(dest / p.name)
            continue
        if not p.is_file():
            raise TemplateError(f"Image file not found: {p}")
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        el["src"] = f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()
    return out


def list_templates() -> list[dict]:
    d = templates_dir()
    out = []
    for p in sorted(d.glob("*" + SUFFIX), key=lambda x: x.name.lower()) if d.is_dir() else []:
        name = p.name[: -len(SUFFIX)]
        try:
            lab = labelmod.normalize(json.loads(p.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as e:
            out.append({"name": name, "path": str(p), "error": str(e)})
            continue
        out.append({
            "name": name,
            "path": str(p),
            "media": lab["media"],
            "orientation": lab["orientation"],
            "variables": labelmod.find_variables(lab),
            "elements": len(lab["elements"]),
            "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
        })
    return out


def exists(name: str) -> bool:
    return _path(name).is_file()


def load_template(name: str) -> dict:
    p = _path(name)
    if not p.is_file():
        known = ", ".join(t["name"] for t in list_templates()) or "(none yet)"
        raise TemplateError(f"Template {name!r} not found. Saved: {known}")
    return labelmod.load(p)


def save_template(name: str, label: dict, overwrite: bool = False) -> Path:
    p = _path(name)
    if p.exists() and not overwrite:
        raise TemplateError(f"Template {name!r} already exists (use overwrite / --force to replace it)")
    lab = labelmod.normalize(_embed_images(labelmod.normalize(label), name))
    lab["name"] = lab.get("name") or name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(labelmod.dump(lab), encoding="utf-8")
    tmp.replace(p)
    return p


def delete_template(name: str) -> None:
    p = _path(name)
    if not p.is_file():
        raise TemplateError(f"Template {name!r} not found")
    p.unlink()
    import shutil

    shutil.rmtree(templates_dir() / "assets" / _check_name(name), ignore_errors=True)
