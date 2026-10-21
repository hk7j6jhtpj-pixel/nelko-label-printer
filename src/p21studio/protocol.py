"""Nelko P21 wire protocol: command encoding, bitmap packing and response parsing.

See research/02-protocol.md for the reverse-engineered specification.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import IntEnum

from PIL import Image

DPI = 203
HEAD_DOTS = 96  # printable dots across the tape
WIDTH_BYTES = HEAD_DOTS // 8
DOTS_PER_MM_LENGTH = 284 / 40  # 40 mm label -> 284 printable dots (verified on hardware)

CRLF = b"\r\n"
CMD_READINESS = b"\x1b!?" + CRLF
CMD_STATUS = b"\x1b!o" + CRLF
CMD_BATTERY = b"BATTERY?" + CRLF
CMD_CONFIG = b"CONFIG?" + CRLF
CMD_SELFTEST = b"SELFTEST" + CRLF

STATUS_LEN = 16
TIMEOUT_MINUTES = {0: 0, 15: 1, 30: 2, 60: 3}


def length_dots(length_mm: float) -> int:
    """Printable dots along the tape for a label of the given length.

    Only 40 mm (284 dots) is verified; other lengths are scaled proportionally.
    """
    if abs(length_mm - 40) < 1e-6:
        return 284
    return max(8, math.floor(length_mm * DOTS_PER_MM_LENGTH + 0.5))


def crc16(data: bytes) -> bytes:
    """CRC-16/MODBUS, returned big-endian as the printer appends it."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc.to_bytes(2, "big")


def pack_bitmap(image: Image.Image) -> bytes:
    """Pack a portrait image (96 dots wide) into the P21 1-bit format.

    Rows are MSB-first, bit 0 = black (ink), bit 1 = white. This is exactly
    Pillow's mode "1" byte layout, so we convert and dump.
    """
    if image.width != HEAD_DOTS:
        raise ValueError(f"bitmap must be {HEAD_DOTS} dots wide, got {image.width}")
    if image.mode != "1":
        image = image.convert("L").point(lambda v: 255 if v > 127 else 0).convert("1", dither=Image.Dither.NONE)
    data = image.tobytes()
    expected = WIDTH_BYTES * image.height
    if len(data) < expected:  # never let the printer fill missing rows with black
        data = data.ljust(expected, b"\xff")
    return data[:expected]


def build_print_job(
    bitmap: bytes,
    height_dots: int,
    width_mm: float = 14.0,
    length_mm: float = 40.0,
    density: int = 15,
    copies: int = 1,
    gap_mm: float = 5.0,
) -> bytes:
    if len(bitmap) != WIDTH_BYTES * height_dots:
        raise ValueError(f"bitmap is {len(bitmap)} bytes, expected {WIDTH_BYTES * height_dots}")
    density = max(1, min(15, int(density)))
    copies = max(1, int(copies))
    head = (
        CMD_STATUS
        + f"SIZE {width_mm:.1f} mm,{length_mm:.1f} mm\r\n".encode()
        + f"GAP {gap_mm:.1f} mm,0 mm\r\n".encode()
        + b"DIRECTION 1,1\r\n"
        + f"DENSITY {density}\r\n".encode()
        + b"CLS\r\n"
        + f"BITMAP 0,0,{WIDTH_BYTES},{height_dots},1,".encode()
    )
    return head + bitmap + f"\r\nPRINT {copies}\r\n".encode()


def beep_command(on: bool) -> bytes:
    return b"BEEP " + bytes([1 if on else 0]) + CRLF


def timeout_command(minutes: int) -> bytes:
    if minutes not in TIMEOUT_MINUTES:
        raise ValueError("timeout must be one of 0, 15, 30, 60 minutes")
    return b"TIMEOUT " + bytes([TIMEOUT_MINUTES[minutes]]) + CRLF


class Readiness(IntEnum):
    READY = 0
    LID_OPEN = 1
    OUT_OF_PAPER = 4
    BUSY = 32


READINESS_TEXT = {
    0: ("ready", "Ready"),
    1: ("lid_open", "Cover is open"),
    4: ("out_of_paper", "Out of labels"),
    32: ("busy", "Printing"),
}

PAPER_COLORS = {0: "unknown", 2: "transparent", 3: "white", 4: "pink", 5: "blue", 6: "yellow"}
PAPER_TYPES = {0: "continuous", 1: "gapped", 2: "blackmark"}


def readiness_name(code: int) -> str:
    return READINESS_TEXT.get(code, (f"unknown_{code}", ""))[0]


def readiness_text_ja(code: int) -> str:
    return READINESS_TEXT.get(code, ("", f"Unknown status ({code})"))[1]


class ProtocolError(Exception):
    pass


@dataclass
class Status:
    readiness: int
    readiness_name: str
    label_color: str
    paper_type: str
    label_width_mm: int
    label_length_mm: int
    max_label_width_mm: int
    raw: str = field(repr=False)

    @property
    def has_tag(self) -> bool:
        return bool(self.label_width_mm or self.label_length_mm)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["has_rfid_tag"] = self.has_tag
        d["readiness_text"] = readiness_text_ja(self.readiness)
        return d


def parse_status(resp: bytes) -> Status:
    if len(resp) != STATUS_LEN:
        raise ProtocolError(f"status response must be {STATUS_LEN} bytes, got {len(resp)}: {resp.hex()}")
    if crc16(resp[:-2]) != resp[-2:]:
        raise ProtocolError(f"status checksum mismatch: {resp.hex()}")
    return Status(
        readiness=resp[0],
        readiness_name=readiness_name(resp[0]),
        label_color=PAPER_COLORS.get(resp[4], f"unknown_{resp[4]}"),
        paper_type=PAPER_TYPES.get(resp[7], f"unknown_{resp[7]}"),
        label_length_mm=resp[11],
        max_label_width_mm=resp[12],
        label_width_mm=resp[13],
        raw=resp.hex(),
    )


def build_status(readiness: int = 0, width_mm: int = 14, length_mm: int = 40, color: int = 3, paper_type: int = 1) -> bytes:
    """Build a status frame (used by the mock printer and tests)."""
    body = bytes([readiness, 0x0C, 0x01, 0x12, color, 0x00, 0x03, paper_type, 0x12, 0x12, 0x15, length_mm, 0x0F, width_mm])
    return body + crc16(body)


def _strip_prefixed(resp: bytes, prefix: bytes, n: int) -> bytes:
    if not resp.startswith(prefix) or not resp.endswith(CRLF) or len(resp) != len(prefix) + n + 2:
        raise ProtocolError(f"unexpected response to {prefix.decode().strip()}: {resp.hex()}")
    return resp[len(prefix) : len(prefix) + n]


@dataclass
class Battery:
    percent: int
    charging: bool

    def to_dict(self) -> dict:
        return asdict(self)


def parse_battery(resp: bytes) -> Battery:
    data = _strip_prefixed(resp, b"BATTERY ", 2)
    return Battery(percent=((data[0] >> 4) & 0xF) * 10 + (data[0] & 0xF), charging=data[1] != 0)


@dataclass
class Config:
    protocol_type: int
    dpi: int
    hardware_version: str
    firmware_version: str
    auto_off_minutes: int | None
    beep: bool

    def to_dict(self) -> dict:
        return asdict(self)


def parse_config(resp: bytes) -> Config:
    d = _strip_prefixed(resp, b"CONFIG ", 10)
    minutes = {v: k for k, v in TIMEOUT_MINUTES.items()}.get(d[8])
    return Config(
        protocol_type=d[0],
        dpi=d[1],
        hardware_version=f"{d[2]}.{d[3]}.{d[4]}",
        firmware_version=f"{d[5]}.{d[6]}.{d[7]}",
        auto_off_minutes=minutes,
        beep=bool(d[9]),
    )
