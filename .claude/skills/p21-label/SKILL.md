---
name: p21-label
description: Design and print labels on the user's Nelko P21 label printer with the `p21` CLI. Use when the user asks to make, preview or print a label / sticker / tag (ラベル, シール, タグ, 印刷) on the P21.
---

# P21 label design and printing

Follow `AGENTS.md` at the repository root. In short:

1. If you need the format, run `p21 schema` (or `p21 example`).
2. Write the label as `<name>.p21.json`. Canvas for 14x40 mm is 284x96 dots (~8 dots/mm), black and white only.
3. Render with `p21 render <file> -o <file>.png --debug --json`, then **Read the PNG** and check:
   readable size, nothing clipped, balanced margins, no `warnings` in the JSON. Iterate until it looks right.
4. Show the preview to the user. Print only when they asked to print:
   `p21 print <file> --json` (add `--csv rows.csv` for batches, `--var k=v` for fields).
5. Report the result. Exit code 3 = printer not ready (lid open / no labels), 4 = cannot connect
   (suggest powering on, pairing, or `p21 scan` then `p21 use <device>`).

Use `-d mock:` for a dry run that saves the "printed" image instead of using real labels.
