# 03. OS ごとの接続方法

P21 は RFCOMM チャンネル **1** で SPP（シリアルポートプロファイル）を公開している。BLE 専用機は GATT。

## Windows

1. 設定 → Bluetooth とデバイス → デバイスの追加 → 「P21」をペアリング。
2. 方法 A（推奨・p21studio の `rfcomm:` 方式）: **Python 標準の `socket.AF_BLUETOOTH` / `BTPROTO_RFCOMM`** で MAC アドレスに直接接続できる（CPython 3.9+ で Windows 対応、この環境の Python 3.12 で `AF_BLUETOOTH=32, BTPROTO_RFCOMM=3` を確認）。COM ポート不要。
3. 方法 B（`serial:` 方式）: ペアリングすると「Bluetooth リンク経由の標準シリアル」COM ポートが 2 つできる（送信用と受信用）。**送信 (Outgoing)** 側を開く。
   - COM ポートができない場合: その他の Bluetooth オプション → COM ポート → 追加 → 発信 → P21。
   - COM10 以上は `\\.\COM10` 形式（pyserial は自動で処理）。
4. ペア済みデバイスの MAC は PnP のインスタンス ID（`BTHENUM\...DEV_AABBCCDDEEFF...`）から取れる（PowerShell `Get-PnpDevice -Class Bluetooth`）。
5. BLE 専用機: WinRT 経由（Python なら bleak）。ペアリング不要。

## Linux（BlueZ）

1. `bluetoothctl` → `scan on` → `pair <MAC>` → `trust <MAC>`。
2. 方法 A: Python の `socket.AF_BLUETOOTH` で直接 RFCOMM 接続（root 不要。BlueZ がペアリング済みなら OK）。
3. 方法 B: `sudo rfcomm bind /dev/rfcomm0 <MAC> 1` → `/dev/rfcomm0` をシリアルとして開く（`dialout` グループが必要）。
4. ペア済み一覧: `bluetoothctl devices Paired`。
5. BLE 専用機: BlueZ D-Bus（bleak）。
6. BLE 専用機に `rfcomm connect` すると "Host is down" になる。

## macOS

- Python 標準の socket は macOS で `AF_BLUETOOTH` をサポートしない。
- ペアリングすると `/dev/cu.P21` のようなシリアルノードができる。
  - Fyne-P21-Print は「これで動く」としている。
  - KalebCole の検証では **`/dev/cu.P21` への書き込みは応答なし（RFCOMM セッションが張られない）**、IOBluetooth の `openRFCOMMChannelSync` を使うと動いた。
  - → 環境により差がある。まず `serial:/dev/cu.P21` を試し、ダメなら BLE を試す。IOBluetooth（PyObjC）対応は将来の課題（[06-open-questions.md](06-open-questions.md)）。
- BLE: CoreBluetooth（bleak）。macOS では MAC アドレスではなく UUID でデバイスが識別される。

## USB（有線）— 使えない

2026-10-17 に Windows で実機を USB 接続して確認した。

- 仮想 COM ポート（USB CDC）として認識される: `USB VID:PID=4C4A:4155`（VID `4C4A` は JieLi = "JL"）。
- 送った内容に関係なく、受け取るたびに **`ERROR0`（改行なし）** を返す。試したのは `ESC ! ?` / `ESC ! o` / `BATTERY?` / `CONFIG?`（CRLF あり・なし）、TSPL の `~!T` などの問い合わせ、ESC/POS、`AT`、1 バイトだけの `a`、適当なバイナリ。
  → 中身を解釈していない固定応答なので、USB 側は印刷用の経路ではないと判断した（merlinschumacher の報告と一致）。
- 印刷ジョブは送っていない（応答が固定なので通る見込みが薄く、ラベルを消費するため）。
- USB は充電用と割り切り、印刷は Bluetooth（Classic / BLE）で行う。

## BLE の詳細（全 OS 共通）

| 項目 | 値 |
|---|---|
| サービス | `0000ff00-0000-1000-8000-00805f9b34fb`（16bit: `ff00`） |
| 書き込み特性 | `ff02`（Write Without Response） |
| 通知特性 | `ff01`（プリンタからの応答がここに来る） |
| チャンク | `min(MTU-3, 200)` バイト |
| チャンク間隔 | 15 ms |
| デバイス名 | 「P21」を含む（スキャンで名前フィルタ or サービス UUID で判別） |
