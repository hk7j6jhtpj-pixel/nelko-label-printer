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
            raise labelmod.LabelError(f"--var は name=value の形式です: {p}")
        k, v = p.split("=", 1)
        out[k.strip()] = v.replace("\\n", "\n")
    return out


def _source(args) -> dict:
    return api.load_source(args.source, text=args.text, image=args.image, media=args.media, font=args.font, vertical=args.vertical)


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
        raise labelmod.LabelError(f"出力先 {out} が入力ファイルと同じです。-o で別の名前を指定してください")
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
    human = "\n".join([f"保存しました: {x['output']}" for x in results] + [f"キャンバス: {w}x{h} ドット"] + [f"警告: {x}" for x in data["warnings"]])
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
                raise api.RenderErrors("描画できない要素があるので印刷しません:\n" + "\n".join(r.errors))
            img = r.printer_image()
            job = protocol.build_print_job(protocol.pack_bitmap(img), img.height, l["media"]["width_mm"], l["media"]["length_mm"], args.density or l["density"], args.copies or l["copies"])
            total += len(job)
            warnings += r.warnings
        _out(args, {"ok": True, "dry_run": True, "labels": len(labels), "bytes": total, "warnings": warnings},
             f"[dry-run] {len(labels)} 枚分のジョブ ({total} バイト) を作成しました（送信していません）" + "".join(f"\n警告: {w}" for w in warnings))
        return EXIT_OK
    res = api.print_labels(lab, args.device, args.copies, args.density, _vars(args.var), _rows(args), force=args.force)
    lines = [f"{'印刷しました' if res['ok'] else '印刷に問題がありました'}: {res['printed']}/{res['labels']} 種類・{res['sheets']} 枚 → {res['device']}"]
    if res.get("error"):
        lines.append(f"途中で止まりました（{res['printed']} 種類目まで印刷済み。再実行すると重複します）: {res['error']}")
    for r in res["results"]:
        lines += [f"警告: {w}" for w in r["warnings"]]
        lines += [f"(mock) 保存: {p}" for p in r.get("mock_png", [])]
    lines += [f"描画警告: {w}" for w in res["render_warnings"]]
    _out(args, res, "\n".join(lines))
    return EXIT_OK if res["ok"] else EXIT_ERROR


def cmd_scan(args) -> int:
    from . import discovery

    if not args.json:
        print("検索中..." + ("（BLE スキャン {:.0f} 秒）".format(args.timeout) if not args.no_ble else ""), file=sys.stderr)
    found = discovery.discover(ble=not args.no_ble, ble_timeout=args.timeout, all_devices=args.all)
    lines = []
    if found["suggestions"]:
        lines.append("見つかったプリンタ（上ほどおすすめ）:")
        for s in found["suggestions"]:
            lines.append(f"  {s['device']:<34} {s['name']}  [{s['via']}]")
        lines.append(f"\n既定に設定するには: p21 use {found['suggestions'][0]['device']}")
    else:
        lines.append("P21 が見つかりませんでした。電源を入れ、OS の Bluetooth 設定でペアリングしてください（BLE 専用機はペアリング不要）。")
    _out(args, found, "\n".join(lines))
    return EXIT_OK


def cmd_use(args) -> int:
    if args.clear:
        config.set_value("device", None)
        _out(args, {"device": None}, "既定のプリンタ設定を削除しました")
        return EXIT_OK
    if not args.device_spec:
        dev = config.default_device()
        _out(args, {"device": dev, "config": str(config.config_path())}, f"既定のプリンタ: {dev or '(未設定)'}\n設定ファイル: {config.config_path()}")
        return EXIT_OK
    from .transports import from_spec

    from_spec(args.device_spec)  # validate
    config.set_value("device", args.device_spec)
    _out(args, {"device": args.device_spec}, f"既定のプリンタを {args.device_spec} にしました")
    return EXIT_OK


def cmd_info(args) -> int:
    info = api.printer_info(args.device)
    lines = [f"デバイス: {info['device']}"]
    st, bat, cfg = info.get("status"), info.get("battery"), info.get("config")
    if st:
        lines.append(f"状態: {st['readiness_text']}")
        lines.append(f"ラベル: {st['label_width_mm']}x{st['label_length_mm']} mm / {st['paper_type']} / {st['label_color']}" if st["has_rfid_tag"] else "ラベル: RFID タグを読めません（非純正ロール？）")
    if bat:
        lines.append(f"電池: {bat['percent']}%" + ("（充電中）" if bat["charging"] else ""))
    if cfg:
        lines.append(f"ファームウェア: {cfg['firmware_version']} / ハードウェア: {cfg['hardware_version']} / {cfg['dpi']} dpi")
        lines.append(f"自動電源オフ: {cfg['auto_off_minutes'] or 'なし'}{'分' if cfg['auto_off_minutes'] else ''} / ビープ: {'オン' if cfg['beep'] else 'オフ'}")
    for k, v in info.get("errors", {}).items():
        lines.append(f"{k} の取得に失敗: {v}")
    _out(args, info, "\n".join(lines))
    return EXIT_OK


def cmd_set(args) -> int:
    if args.beep is None and args.auto_off is None:
        print("--beep か --auto-off を指定してください", file=sys.stderr)
        return EXIT_USAGE
    cfg = api.apply_settings(args.device, None if args.beep is None else args.beep == "on", args.auto_off)
    _out(args, cfg, f"設定しました: ビープ {'オン' if cfg['beep'] else 'オフ'} / 自動電源オフ {cfg['auto_off_minutes'] or 'なし'}")
    return EXIT_OK


def cmd_selftest(args) -> int:
    from .printer import P21

    spec = api.resolve_device(args.device)
    with api.printer_lock, P21(spec) as p:
        p.selftest()
    _out(args, {"ok": True, "device": spec}, "セルフテスト印刷を送信しました")
    return EXIT_OK


def cmd_fonts(args) -> int:
    from . import fonts

    items = fonts.list_fonts(refresh=args.refresh)
    if args.query:
        q = args.query.lower()
        items = [f for f in items if q in f["family"].lower() or q in f["path"].lower()]
    data = {"auto_regular": fonts.describe("auto"), "auto_bold": fonts.describe("auto", True), "preferred_available": fonts.available_preferred(), "fonts": items}
    fams = sorted({f["family"] for f in items}, key=str.lower)
    _out(args, data, f"auto（標準）: {data['auto_regular']}\nauto（太字）: {data['auto_bold']}\n\n" + "\n".join(fams))
    return EXIT_OK


def cmd_schema(args) -> int:
    print(json.dumps(labelmod.json_schema(), ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_example(args) -> int:
    print(json.dumps(labelmod.EXAMPLE, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_validate(args) -> int:
    lab = labelmod.load(args.source)
    r = api.render_label(lab)
    data = {"ok": not r.warnings, "canvas": dict(zip(("width", "height"), r.size)), "variables": labelmod.find_variables(lab), "warnings": r.warnings, "elements": r.boxes}
    head = "OK" if not r.warnings else f"警告 {len(r.warnings)} 件"
    human = f"{head}: キャンバス {r.size[0]}x{r.size[1]}、要素 {len(r.boxes)} 個" + "".join(f"\n警告: {w}" for w in r.warnings)
    if data["variables"]:
        human += f"\n変数: {', '.join(data['variables'])}"
    _out(args, data, human)
    return EXIT_OK if not r.warnings else EXIT_LABEL


def cmd_gui(args) -> int:
    from .server import serve

    serve(args.host, args.port, open_browser=not args.no_browser, device=args.device)
    return EXIT_OK


def cmd_mcp(args) -> int:
    from .mcp import serve_stdio

    serve_stdio(default_device=args.device)
    return EXIT_OK


# --- parser ------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="機械可読な JSON で出力")
    common.add_argument("-d", "--device", help="プリンタ指定 (rfcomm:MAC / serial:COM5 / ble:MAC / mock: / file:job.bin / auto)")

    src = argparse.ArgumentParser(add_help=False)
    src.add_argument("source", nargs="?", help="ラベル JSON（- で標準入力）または画像ファイル")
    src.add_argument("-t", "--text", help="テキストだけのラベルを作る（\\n で改行）")
    src.add_argument("-i", "--image", help="画像だけのラベルを作る")
    src.add_argument("-m", "--media", choices=list(labelmod.MEDIA_PRESETS), help="ラベルサイズ（既定 14x40）")
    src.add_argument("--font", default="auto", help="--text 用フォント（ファミリ名かパス）")
    src.add_argument("--vertical", action="store_true", help="--text を縦書きにする")
    src.add_argument("--var", action="append", metavar="NAME=VALUE", help="{{NAME}} を置換（複数可）")
    src.add_argument("--csv", help="CSV の各行で 1 枚ずつ（列名 = 変数名）")

    p = argparse.ArgumentParser(prog="p21", description="Nelko P21 ラベルプリンタ用ツール")
    p.add_argument("--version", action="version", version=f"p21studio {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    s = sub.add_parser("render", parents=[common, src], help="プレビュー PNG を作る（印刷しない）")
    s.add_argument("-o", "--output", help="出力 PNG パス")
    s.add_argument("--scale", type=int, default=4, choices=range(1, 17), metavar="1-16", help="拡大率（既定 4）")
    s.add_argument("--debug", action="store_true", help="要素の枠と番号を描く")
    s.add_argument("--printer-orientation", action="store_true", help="プリンタに送る向き・等倍で保存")
    s.set_defaults(func=cmd_render)

    s = sub.add_parser("print", parents=[common, src], help="印刷する")
    s.add_argument("-c", "--copies", type=int, help="枚数")
    s.add_argument("--density", type=int, choices=range(1, 16), metavar="1-15", help="濃度")
    s.add_argument("--dry-run", action="store_true", help="ジョブを作るだけで送信しない")
    s.add_argument("--force", action="store_true", help="描画できない要素があっても印刷する")
    s.set_defaults(func=cmd_print)

    s = sub.add_parser("scan", parents=[common], help="プリンタを探す")
    s.add_argument("--no-ble", action="store_true", help="BLE スキャンをしない")
    s.add_argument("--timeout", type=float, default=5.0, help="BLE スキャン秒数")
    s.add_argument("--all", action="store_true", help="P21 以外の機器も表示")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("use", parents=[common], help="既定のプリンタを設定/表示")
    s.add_argument("device_spec", nargs="?", help="例: rfcomm:AA:BB:CC:DD:EE:FF")
    s.add_argument("--clear", action="store_true")
    s.set_defaults(func=cmd_use)

    for name in ("info", "status"):
        s = sub.add_parser(name, parents=[common], help="状態・電池・設定を表示")
        s.set_defaults(func=cmd_info)

    s = sub.add_parser("set", parents=[common], help="プリンタ設定を変更")
    s.add_argument("--beep", choices=["on", "off"])
    s.add_argument("--auto-off", type=int, choices=[0, 15, 30, 60], help="自動電源オフ（分, 0=しない）")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("selftest", parents=[common], help="セルフテスト印刷")
    s.set_defaults(func=cmd_selftest)

    s = sub.add_parser("fonts", parents=[common], help="使えるフォント一覧")
    s.add_argument("query", nargs="?")
    s.add_argument("--refresh", action="store_true")
    s.set_defaults(func=cmd_fonts)

    s = sub.add_parser("schema", parents=[common], help="ラベル JSON の JSON Schema を出力")
    s.set_defaults(func=cmd_schema)
    s = sub.add_parser("example", parents=[common], help="ラベル JSON の例を出力")
    s.set_defaults(func=cmd_example)

    s = sub.add_parser("validate", parents=[common], help="ラベル JSON を検査")
    s.add_argument("source")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("gui", parents=[common], help="GUI（ブラウザ）を起動")
    s.add_argument("--host", default="127.0.0.1", help="0.0.0.0 にすると他の PC からも使える")
    s.add_argument("--port", type=int, default=8721)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_gui)

    s = sub.add_parser("mcp", parents=[common], help="MCP サーバ (stdio) として起動（AI エージェント用）")
    s.set_defaults(func=cmd_mcp)
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
        print(f"エラー: {e}", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
