# 05. 技術選定

## 目標

- Windows / macOS / Linux（＋ Raspberry Pi 等）で同じように動く。
- 人間向けのまともな GUI。
- AI がヘッドレスでデザイン・プレビュー確認・印刷できる。

## 言語: Python 3.10+

- **Bluetooth が標準ライブラリで扱える**: `socket.AF_BLUETOOTH` + `BTPROTO_RFCOMM` が Linux と Windows で使える → ドライバや外部コマンド不要で Classic SPP に直接つながる。
- BLE は **bleak**（Windows=WinRT, macOS=CoreBluetooth, Linux=BlueZ）で 3 OS 共通。
- 画像処理は **Pillow**（フォント描画・回転・ディザ）。ビットマップの 0=黒/1=白 も Pillow の 1bit 画像と同じ。
- `pip install` / `pipx` / `uv tool install` だけで入る。AI エージェントからも扱いやすい。
- Go/Rust で単一バイナリにする案もあったが、BLE+Classic+フォント+GUI を全 OS で揃える手間と、AI/スクリプトからの拡張性で Python を選んだ。単一実行ファイルが必要なら PyInstaller で作れる。

## GUI: ローカル Web アプリ（標準ライブラリの HTTP サーバ + 素の JS）

- `p21 gui` でローカルサーバを起動しブラウザを開く。Qt/Tk などの GUI ツールキットに依存しない → どの OS でも、ヘッドレス機（Raspberry Pi）にプリンタを繋いで別 PC のブラウザから操作することもできる。
- **プレビューはサーバ側の印刷用レンダラで作った PNG をそのまま表示**し、その上に編集用の枠（ドラッグ・リサイズ）を重ねる → 画面と印刷結果が必ず一致する（WYSIWYG）。
- フロントエンドはビルド不要の素の JS（CDN 非依存、オフラインで動く）。
- 依存を増やさないため Web フレームワークは使わない（`http.server.ThreadingHTTPServer`）。

## ラベル形式: JSON（`.p21.json`）

- 人も AI も読み書きしやすい。座標は**横長キャンバスのドット単位**（14×40 なら 284×96、1mm ≒ 8 ドット）。
- 要素: `text` / `qr` / `barcode` / `image` / `rect` / `ellipse` / `line`。
- テキストは「枠に収まるよう自動縮小」(fit) を既定にして、AI がフォントサイズで悩まなくて済むようにする。
- `{{変数}}` 置換と CSV 一括印刷。
- `p21 schema` で JSON Schema を出力（AI 向け）。

## バーコード / QR

- プリンタの `BARCODE` / `QRCODE` は使えないので PC 側で描画。
- QR: **segno**（純 Python）。モジュール単位でドットに整数倍で描き、ぼやけさせない。
- 1 次元バーコード: **python-barcode** でバーパターンだけ取り出し、自前でドット整列して描画（203dpi でもにじまない）。

## フォント

- 日本語ラベルが前提なので、OS の日本語フォントを自動検出（Windows: 游ゴシック / メイリオ / MS ゴシック、macOS: ヒラギノ、Linux: Noto Sans CJK / IPA）。
- フォントファイルのパス指定も可能。同梱はしない（サイズとライセンスのため）。

## 接続（デバイス指定文字列）

| 指定 | 意味 | 対応 OS |
|---|---|---|
| `rfcomm:AA:BB:CC:DD:EE:FF` | Classic RFCOMM ソケット直（ch.1。`@2` で変更） | Windows, Linux |
| `serial:COM5` / `serial:/dev/rfcomm0` / `serial:/dev/cu.P21` | シリアルポート | 全 OS |
| `ble:AA:BB:...` / `ble:<UUID>` | BLE GATT | 全 OS |
| `mock:` | 仮想プリンタ（実機なしで動作確認、受け取ったジョブを PNG 保存） | 全 OS |
| `file:job.bin` | ジョブのバイト列をファイルに書く | 全 OS |
| `auto` | 保存済み既定 → 自動検出 | 全 OS |

## AI 連携

- CLI は全コマンド `--json` で機械可読な出力、終了コードも定義。
- `p21 render` で人が見るのと同じプレビュー PNG を出力 → マルチモーダル AI が見て修正できる。
- **MCP サーバ**（`p21 mcp`, stdio）: render（画像を返す）/ print / status / schema などをツールとして公開。追加依存なしの最小実装。
- Claude Code 用のスキル（`.claude/skills/`）と `AGENTS.md` を同梱。

## 安全

- 印刷前に readiness (`ESC ! ?`) を確認。Busy なら待つ、カバー開・紙なしならエラー。
- ジョブ送信開始後は自動リトライしない（二重印刷防止）。
- `INITIALPRINTER`（工場リセット）は実装しない。
