# Nelko P21 調査資料

P21 を PC から使うソフト（p21studio）を作るために調べた内容をまとめたものです。
調査日: 2026-10-17

| ファイル | 内容 |
|---|---|
| [01-hardware.md](01-hardware.md) | 本体スペック、ラベルサイズ、RFID、BLE専用機の存在 |
| [02-protocol.md](02-protocol.md) | 通信プロトコル（コマンド・応答フォーマット・ビットマップ形式・CRC） |
| [03-connection-by-os.md](03-connection-by-os.md) | OS ごとの接続方法（Windows / Linux / macOS、Classic / BLE） |
| [04-prior-art.md](04-prior-art.md) | 既存の OSS 実装（何が分かっていて、何をしているか） |
| [05-design-decisions.md](05-design-decisions.md) | p21studio の技術選定とその理由 |
| [06-open-questions.md](06-open-questions.md) | 未検証・不明点（実機で確認すべきこと） |
| [sources.md](sources.md) | 参照したURL一覧 |

## 実装との対応

| 調査内容 | 実装 |
|---|---|
| コマンド・応答・CRC・ビットマップ | [src/p21studio/protocol.py](../src/p21studio/protocol.py)（テストベクタは [tests/test_protocol.py](../tests/test_protocol.py)） |
| RFCOMM / シリアル / BLE | [src/p21studio/transports.py](../src/p21studio/transports.py) |
| OS ごとのペア済み機器検出 | [src/p21studio/discovery.py](../src/p21studio/discovery.py) |
| 時計回り回転・印字範囲 | [src/p21studio/render.py](../src/p21studio/render.py) の `printer_image()` |

> 2026-10-17: **Windows 11 + BLE 専用機（FW 0.0.1）で実機印刷に成功**（14×40、向き・欠け・QR・濃さ OK）。Classic 接続・macOS/Linux・40mm 以外のラベルは未検証。詳細は [06-open-questions.md](06-open-questions.md)。

## 要点（3行で）

1. P21 は **Bluetooth Classic SPP (RFCOMM ch.1)** で **TSPL2 風テキストコマンド**を受け付ける。ただし **BLE専用の個体**もあり、その場合は GATT `ff00` サービス（write `ff02` / notify `ff01`）で同じバイト列を流す。
2. 印刷内容は常に **96×284 ドット・1bit のビットマップ**（3408 バイト, MSB-first, **0=黒 / 1=白**）として送る。プリンタ側にフォントや QR 生成機能は実質ない → **PC 側で全部ラスタライズ**する必要がある。
3. デザインは横長 284×96 で作り、**時計回りに 90° 回転**して送ると正しい向きで出る（実機検証済みの報告あり）。
