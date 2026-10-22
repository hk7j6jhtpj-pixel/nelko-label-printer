# Instructions for AI agents: printing labels on a Nelko P21

This repository ships `p21`, a CLI for the Nelko P21 thermal label printer.
You can design a label as JSON, render it to PNG to check it visually, and print it.

## Workflow

1. Learn the format: `p21 schema` (JSON Schema) or `p21 example`.
2. Write a label file, e.g. `label.p21.json`.
3. Render and **look at the image**: `p21 render label.p21.json -o preview.png --debug --json`
   - `--debug` outlines each element box with its index.
   - The JSON output lists `warnings` (text that does not fit, QR too small, out of bounds). Fix them.
   - `errors` (elements that could not be drawn at all) make `p21 print` refuse to print; fix them.
4. Print only when the user asked for it: `p21 print label.p21.json --json` (1 copy unless told otherwise).
   - Check the printer first if unsure: `p21 info --json` (readiness, loaded label size from RFID, battery).
   - Not ready (exit code 3): tell the user (lid open / no labels). Connection error (exit 4): ask them to power on / pair, or run `p21 scan`.
   - Use `-d mock:` to test without a printer; the "printed" PNG path is in `results[].mock_png`.

Saved templates: `p21 template list --json` shows the user's templates and their `{{variables}}`;
print one with `p21 print -T <name> --var key=value` (or `--csv`). Save a reusable design with
`p21 template save <name> <file>` (refuses to overwrite unless `--force`; ask before overwriting).

Quick one-offs: `p21 print -t "text\nsecond line"` or `p21 print image.png`.

## Canvas and units

- Units are printer dots: 203 dpi, ~8 dots per mm. Output is strictly black and white.
- 14x40 mm label, landscape (default): canvas **284 wide x 96 tall**. Other lengths: width = round(length_mm x 7.1).
- `"orientation": "portrait"` gives a 96-wide, 284-tall canvas (for vertical designs).
- Keep ~3 dots of margin from the edges.

## Design tips

- `text` with `"size": "auto"` (default) finds the largest font that fits its box, so control size via the box.
  Text shorter than ~12 dots is hard to read; use `"bold": true` for anything small.
- `"vertical": true` for Japanese vertical writing.
- Cable flag: [text | blank wrap zone | same text], all upright (no rotate). The middle wraps the cable and the
  two ends are stuck back to back, so both faces read normally. Wrap zone ≈ π × cable diameter (mm) × 8 dots;
  a 6 mm cable needs ~150 dots, i.e. use a 14x50 or longer label.
- `qr` needs >= 2 dots per module: give it a square box of 70-90 dots on 14 mm tape.
- `barcode` (`code128`, `ean13`/`jan`, `code39`, `itf`, `codabar`, ...): give it most of the label length.
- Knock-out text: a filled `rect`, then `text` with `"color": "white"` on top (later elements draw on top).
- Variables: `{{name}}` with `"variables": {...}` or `--var name=value`; batch with `--csv file.csv`;
  built-ins `{{date}}`, `{{date:%m/%d}}`, `{{time}}`, `{{n}}` (row number).
- Fonts: `"auto"` picks an installed Japanese-capable font; list others with `p21 fonts --json`. A family name
  or a font file path works; an unknown name produces a warning and falls back to auto.
- `size` is the font em size, so glyphs are slightly smaller than the number (and the reported size can exceed h a bit).

## Code map (for changing the tool itself)

- `src/p21studio/protocol.py` wire format (see `research/02-protocol.md`)
- `src/p21studio/transports.py` RFCOMM / serial / BLE / mock / file
- `src/p21studio/printer.py` readiness check, print, status
- `src/p21studio/label.py` JSON model, validation, variables, schema
- `src/p21studio/render.py` renderer shared by the desktop app, CLI and printing
- `src/p21studio/cli.py`, `desktop.py` (tkinter app)
- Tests: `pytest`
