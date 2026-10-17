"""High-level printer API on top of a transport."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PIL import Image

from . import protocol
from .protocol import Readiness
from .transports import Transport, TransportError, from_spec


class PrinterError(Exception):
    """Printer reachable but not able to do what was asked."""


class PrinterNotReady(PrinterError):
    def __init__(self, code: int):
        self.code = code
        super().__init__(f"プリンタが印刷できる状態ではありません: {protocol.readiness_text_ja(code)} ({protocol.readiness_name(code)})")


@dataclass
class PrintResult:
    ok: bool
    device: str
    copies: int
    bytes_sent: int
    acknowledged: bool
    finished: bool | None
    status: dict | None = None
    warnings: list[str] = field(default_factory=list)
    mock_png: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class P21:
    def __init__(self, transport: Transport | str):
        self.transport = from_spec(transport) if isinstance(transport, str) else transport

    # context manager ------------------------------------------------------
    def open(self) -> "P21":
        self.transport.open()
        return self

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "P21":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def device(self) -> str:
        return self.transport.describe()

    def _require_interactive(self) -> None:
        if not self.transport.interactive:
            raise PrinterError(f"{self.device} はプリンタから応答を受け取れません")

    # queries ---------------------------------------------------------------
    def readiness(self, timeout: float = 3.0) -> int:
        self._require_interactive()
        self.transport.reset_input()
        self.transport.write(protocol.CMD_READINESS)
        resp = self.transport.read_exact(1, timeout)
        if not resp:
            raise TransportError("プリンタから応答がありません（電源・接続を確認してください）")
        return resp[0]

    def status(self, timeout: float = 3.0) -> protocol.Status:
        self._require_interactive()
        self.transport.reset_input()
        self.transport.write(protocol.CMD_STATUS)
        resp = self.transport.read_exact(protocol.STATUS_LEN, timeout)
        if not resp:
            raise TransportError("プリンタから応答がありません（電源・接続を確認してください）")
        return protocol.parse_status(resp)

    def battery(self, timeout: float = 3.0) -> protocol.Battery:
        self._require_interactive()
        self.transport.reset_input()
        self.transport.write(protocol.CMD_BATTERY)
        return protocol.parse_battery(self.transport.read_line(timeout))

    def config(self, timeout: float = 3.0) -> protocol.Config:
        self._require_interactive()
        self.transport.reset_input()
        self.transport.write(protocol.CMD_CONFIG)
        return protocol.parse_config(self.transport.read_line(timeout))

    def info(self) -> dict:
        out: dict = {"device": self.device}
        for key, fn in (("status", self.status), ("battery", self.battery), ("config", self.config)):
            try:
                out[key] = fn().to_dict()
            except (protocol.ProtocolError, TransportError) as e:
                out[key] = None
                out.setdefault("errors", {})[key] = str(e)
        return out

    # settings --------------------------------------------------------------
    def set_beep(self, on: bool) -> None:
        self.transport.write(protocol.beep_command(on))
        self.transport.read_line(1.0)

    def set_auto_off(self, minutes: int) -> None:
        self.transport.write(protocol.timeout_command(minutes))
        self.transport.read_line(1.0)

    def selftest(self) -> None:
        self.transport.write(protocol.CMD_SELFTEST)

    # printing --------------------------------------------------------------
    def wait_ready(self, timeout: float = 30.0, poll: float = 0.5) -> int:
        deadline = time.monotonic() + timeout
        while True:
            code = self.readiness()
            if code != Readiness.BUSY or time.monotonic() > deadline:
                return code
            time.sleep(poll)

    def print_image(
        self,
        portrait: Image.Image,
        width_mm: float = 14.0,
        length_mm: float = 40.0,
        density: int = 15,
        copies: int = 1,
        wait: bool = True,
    ) -> PrintResult:
        """Print a portrait (96 x N dots) image.

        Safety: the readiness check happens before any job byte is sent; once
        sending has started the job is never resent automatically.
        """
        bitmap = protocol.pack_bitmap(portrait)
        job = protocol.build_print_job(bitmap, portrait.height, width_mm, length_mm, density, copies)
        warnings: list[str] = []

        if self.transport.interactive:
            code = self.wait_ready()
            if code != Readiness.READY:
                raise PrinterNotReady(code)
            self.transport.reset_input()

        self.transport.write(job)  # no retry beyond this point

        result = PrintResult(
            ok=True,
            device=self.device,
            copies=copies,
            bytes_sent=len(job),
            acknowledged=False,
            finished=None,
            warnings=warnings,
        )
        if not self.transport.interactive:
            return result

        resp = self.transport.read_exact(protocol.STATUS_LEN, 8.0)
        if len(resp) == protocol.STATUS_LEN:
            try:
                st = protocol.parse_status(resp)
                result.status = st.to_dict()
                result.acknowledged = True
            except protocol.ProtocolError as e:
                warnings.append(f"応答を解釈できませんでした: {e}")
        else:
            warnings.append("ジョブ送信後にプリンタの応答がありませんでした（印刷されたかは実物を確認してください）")

        if wait:
            try:
                code = self.wait_ready(timeout=15.0 + 5.0 * copies)
                result.finished = code == Readiness.READY
                if code not in (Readiness.READY, Readiness.BUSY):
                    result.ok = False
                    warnings.append(f"印刷後の状態: {protocol.readiness_text_ja(code)}")
            except TransportError as e:
                warnings.append(f"印刷完了の確認に失敗: {e}")

        mock_printed = getattr(self.transport, "printed", None)
        if mock_printed:
            result.mock_png = [str(mock_printed[-1])]
        return result
