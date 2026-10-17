# 04. 既存の OSS 実装

コードは流用せず（AGPL のものもあるため）、プロトコルの事実だけを参照した。

## merlinschumacher/nelko-p21-print（Python, AGPL-3.0）

- https://github.com/merlinschumacher/nelko-p21-print （同内容の `nelko-p21-capture` もあり）
- **リバースエンジニアリングの原典**。Wireshark の BT キャプチャ (`nelko_bt_capture.pcapng`) 付き。
- pyserial で `/dev/rfcomm0` に書く CLI。画像印刷、状態/電池/設定取得、ビープ・タイムアウト設定、セルフテスト。
- 状態応答の CRC16、BCD の電池残量、CONFIG のフィールドを解読。
- 画像は `rotate(90)`（反時計回り）でフィットさせているが、向きの検証はしていない。

## julienvincent/nelko-p21-rs（Rust, MIT）

- https://github.com/julienvincent/nelko-p21-rs
- protocol / serial / rfcomm（Linux ソケット直）/ graphics（SVG テンプレート, テキスト）/ cli のクレート構成。
- `pack_bitmap`: 輝度 >127 を 1（白）にする MSB-first。テストベクタあり（CRC、CONFIG、状態）。
- 横長 284×96 で描いて **時計回り回転**。
- 応答の読み取り: CRLF が来るか、16 バイト溜まって CRC が合ったら終了。

## TylerCode/Fyne-P21-Print（Go + Fyne GUI, MIT）

- https://github.com/TylerCode/Fyne-P21-Print
- Linux / Windows / macOS 向けの GUI。デザイナーモード（テキスト、画像、QR/Aztec/DataMatrix/Code128、図形、ドラッグ移動・リサイズ、45°回転、`.nlbl` 保存）。
- Linux では `pkexec rfcomm connect` を裏で起動、Windows は COM ポート、macOS は `/dev/cu.*`。
- ラベルサイズ定義: 12x40 / 14x40 / 14x50 / 14x75 / 15x30（ドット数は比例計算）。
- **giray フォークの `feat/ble-transport` ブランチ（PR #12）で BLE 対応**: サービス `ff00`, write `ff02`, notify `ff01`, 200 バイト/15ms。

## CzJLee/nelko-p21（Python）

- https://github.com/CzJLee/nelko-p21
- テキスト / 画像を印刷する CLI。`--preview-only`。Linux の `rfcomm bind` 前提。
- テキストは横長で描いて `rotate(-90)`（時計回り）。`BITMAP` のモードに 0 を使用。

## KalebCole/nelko-p21-cli（設計中, macOS）

- https://github.com/KalebCole/nelko-p21-cli
- コードより **実機検証ログが価値大**（`docs/live-transport-investigation.md`, `docs/p21-layout-calibration-2026-09-14.md`）。
  - macOS の `/dev/cu.P21` は RFCOMM を張らない → IOBluetooth で channel 1 を直接開くと成功、MTU 666。
  - `BATTERY?` → `BATTERY 0x75 0x00`、印刷ジョブ 3515 バイト → 16 バイト応答。
  - 時計回り回転が pixel-perfect、14 枚連続印刷に成功。
  - 「送信開始後は自動リトライしない」「各ジョブ前に readiness を確認」という安全原則。

## このプロジェクト（p21studio）との違い

| | 既存 | p21studio |
|---|---|---|
| 接続 | 各 OS で手作業セットアップが多い | RFCOMM ソケット直 / シリアル / BLE を同じ指定方法で扱う、自動検出 |
| GUI | Fyne ネイティブ（ビルドが必要） | ブラウザ GUI（Python だけで動く。描画はサーバ側の同じレンダラで WYSIWYG） |
| 日本語 | 考慮なし | 日本語 UI、OS の日本語フォント自動検出、縦書き |
| AI 連携 | なし | JSON ラベル形式 + JSON 出力 CLI + プレビュー PNG + MCP サーバ |
| 一括印刷 | なし | 変数 `{{name}}` 置換 + CSV 一括印刷 |
