"""`p21` command line interface. Every command accepts --json for machine-readable output."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, api, config, label as labelmod, protocol
from .printer import PrinterError, PrinterNotReady
from .transports import TransportError

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_NOT_READY, EXIT_CONNECTION, EXIT_LABEL = 0, 1, 2, 3, 4, 5


def _out(args, data, human: str | None = None) -> None:
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    elif human is not None:
        print(human)


def _vars(pairs: list[str] | None) -> dict:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise labelmod.LabelError(f"--var must be name=value: {p}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.replace("\\n", "\n")
    return out


def _source(args) -> dict:
    return api.load_source(args.source, text=args.text, image=args.image, media=args.media, font=args.font, vertical=args.vertical, template=args.template)


def _rows(args) -> list[dict] | None:
    return labelmod.read_csv(args.csv) if getattr(args, "csv", None) else None


# --- commands ------------------------------------------------------------------
def cmd_render(args) -> int:
    lab = _source(args)
    labels = api.expand(lab, _vars(args.var), _rows(args))
    src = args.image or args.source
    if args.output:
        out = Path(args.output)
    elif src and src != "-" and Path(src).suffix.lower() in api.IMAGE_EXTS:
        out = Path(src).with_name(Path(src).stem + ".preview.png")
    elif src and src != "-":
        out = Path(src).with_suffix("").with_suffix(".png")
    else:
        out = Path("p21-preview.png")
    if src and src != "-" and Path(src).exists() and out.resolve() == Path(src).resolve():
        raise labelmod.LabelError(f"Output {out} is the same as the input file. Use -o to set another name.")
    results, warnings = [], []
    from .render import render

    for i, l in enumerate(labels):
        r = render(l)
        path = out if len(labels) == 1 else out.with_name(f"{out.stem}-{i + 1:03d}{out.suffix}")
        if args.printer_orientation:
            r.printer_image().save(path)
        else:
            r.preview(args.scale, debug=args.debug).save(path)
        results.append({"output": str(path), "warnings": r.warnings, "errors": r.errors, "elements": r.boxes})
        warnings += r.warnings
    w, h = r.size
    data = {
        "outputs": [x["output"] for x in results],
        "canvas": {"width": w, "height": h},
        "scale": args.scale,
        "warnings": list(dict.fromkeys(warnings)),
        "errors": list(dict.fromkeys(e for x in results for e in x["errors"])),
        "elements": results[0]["elements"],
    }
    if len(results) > 1:
        data["labels"] = results
    human = "\n".join([f"Saved: {x['output']}" for x in results] + [f"Canvas: {w}x{h} dots"] + [f"Warning: {x}" for x in data["warnings"]])
    _out(args, data, human)
    return EXIT_LABEL if data["errors"] else EXIT_OK


def cmd_print(args) -> int:
    lab = _source(args)
    if args.dry_run:
        labels = api.expand(lab, _vars(args.var), _rows(args))
        from .render import render

        total = 0
        warnings = []
        for l in labels:
            r = render(l)
            if r.errors and not args.force:
                raise api.RenderErrors("Not printing: some elements cannot be rendered:\n" + "\n".join(r.errors))
            img = r.printer_image()
            job = protocol.build_print_job(protocol.pack_bitmap(img), img.height, l["media"]["width_mm"], l["media"]["length_mm"], args.density or l["density"], args.copies or l["copies"])
            total += len(job)
            warnings += r.warnings
        _out(args, {"ok": True, "dry_run": True, "labels": len(labels), "bytes": total, "warnings": warnings},
             f"[dry-run] Built job for {len(labels)} label(s) ({total} bytes). Not sent." + "".join(f"\nWarning: {w}" for w in warnings))
        return EXIT_OK
    res = api.print_labels(lab, args.device, args.copies, args.density, _vars(args.var), _rows(args), force=args.force)
    lines = [f"{'Printed' if res['ok'] else 'Print problem'}: {res['printed']}/{res['labels']} labels, {res['sheets']} sheets -> {res['device']}"]
    if res.get("uncertain"):
        lines.append(f"Sheet(s) {', '.join(map(str, res['uncertain']))} may not have printed. Check the labels.")
    if res.get("not_sent"):
        lines.append(f"{res['not_sent']} label(s) not sent. Refill labels and print those again.")
    if res.get("error"):
        lines.append(f"Stopped partway (re-running will duplicate): {res['error']}")
    for r in res["results"]:
        lines += [f"Warning: {w}" for w in r["warnings"]]
        lines += [f"(mock) Saved: {p}" for p in r.get("mock_png", [])]
    lines += [f"Render warning: {w}" for w in res["render_warnings"]]
    _out(args, res, "\n".join(lines))
    return EXIT_OK if res["ok"] else EXIT_ERROR


def cmd_scan(args) -> int:
    from . import discovery

    if not args.json:
        print("Scanning..." + (" (BLE scan {:.0f} s)".format(args.timeout) if not args.no_ble else ""), file=sys.stderr)
    found = discovery.discover(ble=not args.no_ble, ble_timeout=args.timeout, all_devices=args.all)
    lines = []
    if found["suggestions"]:
        lines.append("Printers found (best first):")
        for s in found["suggestions"]:
            lines.append(f"  {s['device']:<34} {s['name']}  [{s['via']}]")
        lines.append(f"\nTo set as default: p21 use {found['suggestions'][0]['device']}")
    else:
        lines.append("No P21 found. Turn it on and pair it in the OS Bluetooth settings (BLE-only models need no pairing).")
    _out(args, found, "\n".join(lines))
    return EXIT_OK


def cmd_use(args) -> int:
    if args.clear:
        config.set_value("device", None)
        _out(args, {"device": None}, "Default printer cleared")
        return EXIT_OK
    if not args.device_spec:
        dev = config.default_device()
        _out(args, {"device": dev, "config": str(config.config_path())}, f"Default printer: {dev or '(not set)'}\nConfig file: {config.config_path()}")
        return EXIT_OK
    from .transports import from_spec

    from_spec(args.device_spec)  # validate
    config.set_value("device", args.device_spec)
    _out(args, {"device": args.device_spec}, f"Default printer set to {args.device_spec}")
    return EXIT_OK


def cmd_info(args) -> int:
    info = api.printer_info(args.device)
    lines = [f"Device: {info['device']}"]
    st, bat, cfg = info.get("status"), info.get("battery"), info.get("config")
    if st:
        lines.append(f"Status: {st['readiness_text']}")
        lines.append(f"Label: {st['label_width_mm']}x{st['label_length_mm']} mm / {st['paper_type']} / {st['label_color']}" if st["has_rfid_tag"] else "Label: cannot read RFID tag (non-genuine roll?)")
    if bat:
        lines.append(f"Battery: {bat['percent']}%" + ("(charging)" if bat["charging"] else ""))
    if cfg:
        lines.append(f"Firmware: {cfg['firmware_version']} / Hardware: {cfg['hardware_version']} / {cfg['dpi']} dpi")
        lines.append(f"Auto power-off: {cfg['auto_off_minutes'] or 'off'}{' min' if cfg['auto_off_minutes'] else ''} / Beep: {'on' if cfg['beep'] else 'off'}")
    for k, v in info.get("errors", {}).items():
        lines.append(f"Failed to get {k}: {v}")
    _out(args, info, "\n".join(lines))
    return EXIT_OK


def cmd_set(args) -> int:
    if args.beep is None and args.auto_off is None:
        print("Give --beep or --auto-off", file=sys.stderr)
        return EXIT_USAGE
    cfg = api.apply_settings(args.device, None if args.beep is None else args.beep == "on", args.auto_off)
    _out(args, cfg, f"Set: beep {'on' if cfg['beep'] else 'off'} / auto power-off {cfg['auto_off_minutes'] or 'off'}")
    return EXIT_OK


def cmd_selftest(args) -> int:
    from .printer import P21

    spec = api.resolve_device(args.device)
    with api.printer_lock, P21(spec) as p:
        p.selftest()
    _out(args, {"ok": True, "device": spec}, "Self-test print sent")
    return EXIT_OK


def cmd_fonts(args) -> int:
    from . import fonts

    items = fonts.list_fonts(refresh=args.refresh)
    if args.query:
        q = args.query.lower()
        items = [f for f in items if q in f["family"].lower() or q in f["path"].lower()]
    data = {"auto_regular": fonts.describe("auto"), "auto_bold": fonts.describe("auto", True), "preferred_available": fonts.available_preferred(), "fonts": items}
    fams = sorted({f["family"] for f in items}, key=str.lower)
    _out(args, data, f"auto (regular): {data['auto_regular']}\nauto (bold): {data['auto_bold']}\n\n" + "\n".join(fams))
    return EXIT_OK


def cmd_schema(args) -> int:
    print(json.dumps(labelmod.json_schema(), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_example(args) -> int:
    print(json.dumps(labelmod.EXAMPLE, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_template(args) -> int:
    from . import templates

    action = args.action
    if action == "list":
        items = templates.list_templates()
        lines = [f"Template folder: {templates.templates_dir()}"]
        for t in items:
            if "error" in t:
                lines.append(f"  {t['name']}  (unreadable: {t['error']})")
                continue
            m = t["media"]
            v = f"  Variables: {', '.join(t['variables'])}" if t["variables"] else ""
            lines.append(f"  {t['name']:<20} {m['width_mm']}x{m['length_mm']} mm {t['orientation']}{v}")
        if not items:
            lines.append("  (none yet. Add with: p21 template save <name> <label.json>)")
        _out(args, {"dir": str(templates.templates_dir()), "templates": items}, "\n".join(lines))
        return EXIT_OK
    if not args.name:
        print("Give a template name", file=sys.stderr)
        return EXIT_USAGE
    if action == "show":
        print(labelmod.dump(templates.load_template(args.name)))
        return EXIT_OK
    if action == "delete":
        templates.delete_template(args.name)
        _out(args, {"ok": True, "deleted": args.name}, f"Template {args.name} deleted")
        return EXIT_OK
    # save
    if not args.file:
        print("Give the label JSON to save: p21 template save <name> <label.json>", file=sys.stderr)
        return EXIT_USAGE
    path = templates.save_template(args.name, labelmod.load(args.file), overwrite=args.force)
    _out(args, {"ok": True, "name": args.name, "path": str(path)}, f"Template {args.name} saved: {path}")
    return EXIT_OK


def cmd_validate(args) -> int:
    lab = labelmod.load(args.source)
    r = api.render_label(lab)
    data = {"ok": not r.warnings, "canvas": dict(zip(("width", "height"), r.size)), "variables": labelmod.find_variables(lab), "warnings": r.warnings, "elements": r.boxes}
    head = "OK" if not r.warnings else f"{len(r.warnings)} warning(s)"
    human = f"{head}: canvas {r.size[0]}x{r.size[1]}, {len(r.boxes)} element(s)" + "".join(f"\nWarning: {w}" for w in r.warnings)
    if data["variables"]:
        human += f"\nVariables: {', '.join(data['variables'])}"
    _out(args, data, human)
    return EXIT_OK if not r.warnings else EXIT_LABEL


# --- parser ------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    common.add_argument("-d", "--device", help="Printer (rfcomm:MAC / serial:COM5 / ble:MAC / mock: / file:job.bin / auto)")

    src = argparse.ArgumentParser(add_help=False)
    src.add_argument("source", nargs="?", help="Label JSON (- for stdin) or image file")
    src.add_argument("-T", "--template", help="Use a saved template (p21 template list)")
    src.add_argument("-t", "--text", help="Make a text-only label (\\n for newline)")
    src.add_argument("-i", "--image", help="Make an image-only label")
    src.add_argument("-m", "--media", choices=list(labelmod.MEDIA_PRESETS), help="Label size (default 14x40)")
    src.add_argument("--font", default="auto", help="Font for --text (family name or path)")
    src.add_argument("--vertical", action="store_true", help="Set --text vertical")
    src.add_argument("--var", action="append", metavar="NAME=VALUE", help="Replace {{NAME}} (repeatable)")
    src.add_argument("--csv", help="One label per CSV row (column name = variable name)")

    p = argparse.ArgumentParser(prog="p21", description="Tool for the Nelko P21 label printer")
    p.add_argument("--version", action="version", version=f"p21studio {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    s = sub.add_parser("render", parents=[common, src], help="Make a preview PNG (does not print)")
    s.add_argument("-o", "--output", help="Output PNG path")
    s.add_argument("--scale", type=int, default=4, choices=range(1, 17), metavar="1-16", help="Scale (default 4)")
    s.add_argument("--debug", action="store_true", help="Draw element boxes and numbers")
    s.add_argument("--printer-orientation", action="store_true", help="Save in printer orientation at 1x")
    s.set_defaults(func=cmd_render)

    s = sub.add_parser("print", parents=[common, src], help="Print")
    s.add_argument("-c", "--copies", type=int, help="Number of copies")
    s.add_argument("--density", type=int, choices=range(1, 16), metavar="1-15", help="Density")
    s.add_argument("--dry-run", action="store_true", help="Build the job but do not send it")
    s.add_argument("--force", action="store_true", help="Print even if some elements cannot be rendered")
    s.set_defaults(func=cmd_print)

    s = sub.add_parser("scan", parents=[common], help="Find printers")
    s.add_argument("--no-ble", action="store_true", help="Skip BLE scan")
    s.add_argument("--timeout", type=float, default=5.0, help="BLE scan seconds")
    s.add_argument("--all", action="store_true", help="Also show non-P21 devices")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("use", parents=[common], help="Set/show the default printer")
    s.add_argument("device_spec", nargs="?", help="e.g. rfcomm:AA:BB:CC:DD:EE:FF")
    s.add_argument("--clear", action="store_true")
    s.set_defaults(func=cmd_use)

    for name in ("info", "status"):
        s = sub.add_parser(name, parents=[common], help="Show status, battery, settings")
        s.set_defaults(func=cmd_info)

    s = sub.add_parser("set", parents=[common], help="Change printer settings")
    s.add_argument("--beep", choices=["on", "off"])
    s.add_argument("--auto-off", type=int, choices=[0, 15, 30, 60], help="Auto power-off (minutes, 0=never)")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("selftest", parents=[common], help="Print a self-test")
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("fonts", parents=[common], help="List available fonts")
    s.add_argument("query", nargs="?")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(func=cmd_fonts)

    s = sub.add_parser("schema", parents=[common], help="Print the label JSON Schema")
    s.set_defaults(func=cmd_schema)
    s = sub.add_parser("example", parents=[common], help="Print an example label JSON")
    s.set_defaults(func=cmd_example)

    s = sub.add_parser("template", parents=[common], help="List, save, delete templates")
    s.add_argument("action", choices=["list", "save", "show", "delete"])
    s.add_argument("name", nargs="?", help="Template name")
    s.add_argument("file", nargs="?", help="save: label JSON to save")
    s.add_argument("--force", action="store_true", help="save: overwrite same name")
    s.set_defaults(func=cmd_template)

    s = sub.add_parser("validate", parents=[common], help="Check a label JSON")
    s.add_argument("source")
    s.set_defaults(func=cmd_validate)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except OSError as e:  # `p21 fonts | head`: EPIPE on POSIX, EINVAL on Windows
        if not isinstance(e, BrokenPipeError) and e.errno != 22:
            raise
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return EXIT_OK


def _main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # Windows consoles default to cp932
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except PrinterNotReady as e:
        return _fail(args, e, EXIT_NOT_READY, "not_ready")
    except (TransportError, PrinterError) as e:
        return _fail(args, e, EXIT_CONNECTION, "connection")
    except (labelmod.LabelError, protocol.ProtocolError, ValueError, TypeError, OSError) as e:
        return _fail(args, e, EXIT_LABEL, "label")
    except KeyboardInterrupt:
        return 130
    except MemoryError as e:
        return _fail(args, e, EXIT_ERROR, "memory")


def _fail(args, e: Exception, code: int, kind: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": kind, "message": str(e)}, ensure_ascii=False, indent=2))
    else:
        print(f"Error: {e}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
