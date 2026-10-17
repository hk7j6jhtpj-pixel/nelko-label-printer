# P21 Studio

Nelko P21（Bluetooth ラベルプリンタ）を **スマホアプリなしで PC から使う**ためのツールです。

- **GUI**: ブラウザで動くラベルエディタ（ドラッグ配置、テンプレート、差し込み印刷、CSV 一括印刷）
- **CLI**: `p21 print -t "ラベル"` のようにコマンド 1 行で印刷。全コマンドに `--json` があります
- **AI 連携**: JSON のラベル形式、プレビュー PNG 出力、**MCP サーバ**（`p21 mcp`）
- Windows / macOS / Linux（Raspberry Pi も可）。Python 3.10 以上

プロトコルの調査結果は [research/](research/README.md) にまとめてあります。

![例](examples/asset-tag.png)

## インストール

```bash
# どれか 1 つ
uv tool install .            # おすすめ（uv: https://docs.astral.sh/uv/）
pipx install .
pip install .
```

開発用: `uv venv && uv pip install -e ".[dev]"` → `.venv` の `p21` を使います。

## 最初の設定（プリンタをつなぐ）

1. P21 の電源を入れ、**OS の Bluetooth 設定で「P21」をペアリング**します。
2. `p21 scan` で探し、表示されたものを既定にします。

```bash
p21 scan
p21 use rfcomm:AA:BB:CC:DD:EE:FF   # scan が出したものをそのまま
p21 info                            # 状態・入っているラベルのサイズ・電池
```

| OS | おすすめの指定 | 補足 |
|---|---|---|
| Windows | `rfcomm:<MAC>` | COM ポートでも可: `serial:COM5`（「送信」側のポート） |
| Linux | `rfcomm:<MAC>` | `bluetoothctl` でペアリング・trust。`sudo rfcomm bind` で作った `serial:/dev/rfcomm0` でも可 |
| macOS | `serial:/dev/cu.P21` | うまくいかなければ BLE（`ble:<UUID>`）を試す |
| BLE 専用の個体 | `ble:<アドレス>` | ペアリング不要（OS の Bluetooth 設定に出てこない個体はこれ）。`p21 scan` で出てきます |

実機なしで試すには `mock:`（仮想プリンタ。「印刷」した画像が保存されます）を使えます。

## GUI

```bash
p21 gui            # ブラウザが開きます（http://127.0.0.1:8721/）
p21 gui --host 0.0.0.0   # LAN の他端末から使う（表示されるトークン付き URL を開く）
```

- 左: 要素の追加（テキスト / QR / バーコード / 画像 / 四角 / 円 / 線 / 日付）とテンプレート
- 中央: プレビュー。**印刷と同じ描画エンジンの出力をそのまま表示**しているので、見たまま印刷されます
- 右: 選んだ要素の設定。文字は「枠に合わせる」で自動的に最大サイズになります
- `{{名前}}` を書くと差し込み変数の欄が出ます。CSV を読み込むと 1 行 1 枚で一括印刷
- 画像はドラッグ＆ドロップ / 貼り付け（Ctrl+V）でも追加できます
- ショートカット: Del 削除 / Ctrl+D 複製 / 矢印で 1 ドット移動（Shift で 8）/ Ctrl+Z / Ctrl+S / Ctrl+P

## CLI

```bash
p21 print -t "調味料\n（塩）"                 # テキストだけのラベル
p21 print photo.png                            # 画像をそのまま
p21 print examples/asset-tag.p21.json --var id=PC-0042 --var name=ノートPC
p21 print examples/asset-tag.p21.json --csv examples/assets.csv   # 3 枚出る
p21 render examples/food-date.p21.json -o preview.png             # 印刷せずに確認
p21 print label.p21.json --dry-run            # ジョブを作るだけ
p21 set --beep off --auto-off 30
p21 fonts ゴシック                             # 使えるフォント
```

`-d/--device` で都度プリンタを指定できます（環境変数 `P21_DEVICE` も可）。

| 終了コード | 意味 |
|---|---|
| 0 | 成功 |
| 1 | 印刷を始めたが途中で失敗した（`printed` に印刷済みの数） |
| 2 | コマンドの使い方の誤り |
| 3 | プリンタが印刷できない状態（カバー開・ラベルなし） |
| 4 | 接続できない |
| 5 | ラベル JSON の誤り・描画できない要素がある（`--force` で無視） / validate で警告あり |

## テンプレート

よく使うデザインはテンプレートとして保存できます。GUI・CLI・AI（MCP）で共通です。

- **GUI**: 左の「＋ 今のデザインを保存」で登録し、「マイテンプレート」からクリックで呼び出します（✕ で削除）
- **CLI**:

  ```bash
  p21 template save 作り置き label.p21.json     # 登録（同名は --force で上書き）
  p21 template list                             # 一覧
  p21 print -T 作り置き --var 品名=カレー        # テンプレートで印刷
  p21 print -T 備品タグ --csv assets.csv         # CSV で一括
  p21 template delete 作り置き
  ```

- **AI**: MCP の `p21_list_templates` / `p21_save_template`、`p21_render` / `p21_print` の `template` 引数

テンプレートの中身はふつうのラベル JSON で、設定フォルダの `templates/` に入ります（`p21 template list` で場所を表示）。
環境変数 `P21_TEMPLATES_DIR` で別のフォルダ（クラウド同期フォルダなど）を指定すると、複数の PC で共有できます。
画像は保存時にファイルへ埋め込まれるので、元の画像を消しても使えます。

## ラベル形式（.p21.json）

座標は**ドット**（203 dpi、約 8 ドット = 1 mm）。14×40 mm の横長キャンバスは **284×96**。

```json
{
  "version": 1,
  "media": {"width_mm": 14, "length_mm": 40},
  "elements": [
    {"type": "qr",   "x": 8,  "y": 8,  "w": 80,  "h": 80, "data": "https://example.com"},
    {"type": "text", "x": 96, "y": 8,  "w": 180, "h": 50, "text": "{{title}}", "bold": true},
    {"type": "text", "x": 96, "y": 62, "w": 180, "h": 26, "text": "{{date:%Y/%m/%d}}"}
  ],
  "variables": {"title": "見本"}
}
```

要素: `text`（自動サイズ・自動改行・縦書き）/ `qr` / `barcode`（code128, ean13/jan, code39, itf, codabar…）/ `image`（ディザ / しきい値）/ `rect` / `ellipse` / `line`。
`color: "white"` で白抜き、`rotate` で 90° 単位の回転。完全な仕様は `p21 schema`。

## AI から使う

- **CLI**: `p21 schema` → JSON を書く → `p21 render --json`（PNG を見て直す）→ `p21 print`
- **MCP**: Claude Code なら

  ```bash
  claude mcp add p21 -- p21 mcp
  ```

  ツール: `p21_label_format` / `p21_render`（プレビュー画像を返す）/ `p21_print` / `p21_status` / `p21_scan` / `p21_fonts` / `p21_list_templates` / `p21_save_template` / `p21_save_label` / `p21_load_label`
- AI 向けの説明は [AGENTS.md](AGENTS.md)、Claude Code 用スキルは [.claude/skills/p21-label/](.claude/skills/p21-label/SKILL.md)

## 仕組み

```
ラベル JSON ──► render.py (Pillow) ──► 1bit 画像 284×96 ──► 時計回りに回転 96×284
                                                       └─► プレビュー PNG（GUI / CLI / MCP）
96×284 ──► protocol.py（TSPL: SIZE/GAP/DENSITY/BITMAP/PRINT）──► transports.py
                                                     RFCOMM ソケット / シリアル / BLE / mock
```

- 印刷前に準備状態（`ESC ! ?`）を確認し、カバー開・ラベルなしなら送りません
- 送信を始めたジョブは自動で再送しません（二重印刷防止）
- 工場出荷リセット（`INITIALPRINTER`）は実装していません

## 未検証の点

Windows 11 + BLE 接続（ペアリング不要の個体）で、14×40 mm ラベルの印刷を実機確認済みです。
Bluetooth Classic 接続、macOS / Linux、40 mm 以外のラベルの印字ドット数はまだ実機で確認していません。
詳しくは [research/06-open-questions.md](research/06-open-questions.md)。

## テスト

```bash
uv pip install -e ".[dev]" && pytest
```

## ライセンス

MIT。プロトコル情報は [research/04-prior-art.md](research/04-prior-art.md) に挙げた先行プロジェクトの解析に基づきます（コードは流用していません）。
