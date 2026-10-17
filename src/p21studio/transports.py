"""Byte transports to the printer.

Device spec strings (see research/05-design-decisions.md):

    rfcomm:AA:BB:CC:DD:EE:FF[@channel]   Classic Bluetooth socket (Windows, Linux)
    serial:COM5 | serial:/dev/rfcomm0     Serial / virtual COM port (all OSes)
    ble:AA:BB:CC:DD:EE:FF | ble:<uuid>    Bluetooth LE GATT (all OSes, needs bleak)
    mock:[output-dir]                     Simulated printer, saves received labels as PNG
    file:path.bin                         Write the raw job bytes to a file
"""

from __future__ import annotations

import re
import socket
import sys
import threading
import time
from pathlib import Path

from . import protocol

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


class TransportError(Exception):
    pass


class Transport:
    """Base class: subclasses implement _open, _close, _write and _read_some."""

    name = "transport"
    #: whether responses can be expected (file transport has none)
    interactive = True

    def __init__(self) -> None:
        self._buf = bytearray()
        self.is_open = False

    # --- subclass hooks -------------------------------------------------
    def _open(self) -> None: ...
    def _close(self) -> None: ...
    def _write(self, data: bytes) -> None:
        raise NotImplementedError

    def _read_some(self, timeout: float) -> bytes:
        """Return whatever bytes arrive within timeout (may be empty)."""
        raise NotImplementedError

    # --- public API -----------------------------------------------------
    def open(self) -> "Transport":
        if not self.is_open:
            self._open()
            self.is_open = True
        return self

    def close(self) -> None:
        if self.is_open:
            try:
                self._close()
            finally:
                self.is_open = False

    def __enter__(self) -> "Transport":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def write(self, data: bytes) -> None:
        self._write(data)

    def reset_input(self) -> None:
        self._buf.clear()
        while self._read_some(0.05):
            pass

    def _fill(self, deadline: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        chunk = self._read_some(min(remaining, 0.25))
        if chunk:
            self._buf.extend(chunk)
        return True

    def read_exact(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._buf) < n and self._fill(deadline):
            pass
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def read_line(self, timeout: float, max_len: int = 256) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            idx = self._buf.find(b"\r\n")
            if idx >= 0:
                out = bytes(self._buf[: idx + 2])
                del self._buf[: idx + 2]
                return out
            if len(self._buf) >= max_len or not self._fill(deadline):
                out = bytes(self._buf)
                self._buf.clear()
                return out

    def describe(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
class SerialTransport(Transport):
    def __init__(self, port: str, baudrate: int = 115200) -> None:
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.name = f"serial:{port}"
        self._ser = None

    def _open(self) -> None:
        import serial

        try:
            self._ser = serial.Serial(self.port, self.baudrate, timeout=0.1, write_timeout=10)
        except serial.SerialException as e:
            raise TransportError(f"シリアルポート {self.port} を開けません: {e}") from e

    def _close(self) -> None:
        if self._ser:
            self._ser.close()

    def _write(self, data: bytes) -> None:
        try:
            for i in range(0, len(data), 512):
                self._ser.write(data[i : i + 512])
            self._ser.flush()
        except Exception as e:  # serial.SerialTimeoutException etc.
            raise TransportError(f"書き込みに失敗しました: {e}") from e

    def _read_some(self, timeout: float) -> bytes:
        self._ser.timeout = timeout
        first = self._ser.read(1)
        if not first:
            return b""
        waiting = self._ser.in_waiting
        return first + (self._ser.read(waiting) if waiting else b"")


# ---------------------------------------------------------------------------
class RfcommTransport(Transport):
    """Classic Bluetooth RFCOMM via the Python standard library (Linux, Windows)."""

    def __init__(self, address: str, channel: int = 1, connect_timeout: float = 10.0) -> None:
        super().__init__()
        if not MAC_RE.match(address):
            raise TransportError(f"Bluetooth アドレスの形式が不正です: {address}")
        self.address = address.upper()
        self.channel = channel
        self.connect_timeout = connect_timeout
        self.name = f"rfcomm:{self.address}" + (f"@{channel}" if channel != 1 else "")
        self._sock: socket.socket | None = None

    def _open(self) -> None:
        if not hasattr(socket, "AF_BLUETOOTH") or not hasattr(socket, "BTPROTO_RFCOMM"):
            raise TransportError(
                "この OS の Python は Bluetooth RFCOMM ソケットに対応していません"
                "（macOS など）。serial: か ble: を使ってください。"
            )
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(self.connect_timeout)
        try:
            sock.connect((self.address, self.channel))
        except OSError as e:
            sock.close()
            raise TransportError(
                f"{self.address} に接続できません: {e}\n"
                "プリンタの電源・ペアリングを確認してください。BLE 専用機の場合は ble: を使います。"
            ) from e
        self._sock = sock

    def _close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _write(self, data: bytes) -> None:
        try:
            self._sock.settimeout(15)
            for i in range(0, len(data), 512):
                self._sock.sendall(data[i : i + 512])
        except OSError as e:
            raise TransportError(f"書き込みに失敗しました: {e}") from e

    def _read_some(self, timeout: float) -> bytes:
        self._sock.settimeout(max(timeout, 0.001))
        try:
            data = self._sock.recv(1024)
            if data == b"":
                raise TransportError("プリンタとの接続が切れました")
            return data
        except (socket.timeout, TimeoutError, BlockingIOError):
            return b""
        except OSError as e:
            raise TransportError(f"読み込みに失敗しました: {e}") from e


# ---------------------------------------------------------------------------
BLE_SERVICE = "0000ff00-0000-1000-8000-00805f9b34fb"
BLE_WRITE = "0000ff02-0000-1000-8000-00805f9b34fb"
BLE_NOTIFY = "0000ff01-0000-1000-8000-00805f9b34fb"
BLE_MAX_CHUNK = 200
BLE_CHUNK_PAUSE = 0.015


class _AsyncRunner:
    """A private asyncio loop on a daemon thread so bleak can be used synchronously."""

    def __init__(self) -> None:
        import asyncio

        self._asyncio = asyncio
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True, name="p21-ble")
        self.thread.start()

    def run(self, coro, timeout: float | None = None):
        import concurrent.futures

        fut = self._asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()  # stop the coroutine instead of leaving it running in the background
            raise TimeoutError("BLE 操作がタイムアウトしました")

    def stop(self) -> None:
        if self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        if not self.thread.is_alive():
            self.loop.close()


class BleTransport(Transport):
    def __init__(self, address: str, connect_timeout: float = 15.0) -> None:
        super().__init__()
        self.address = address
        self.connect_timeout = connect_timeout
        self.name = f"ble:{address}"
        self._runner: _AsyncRunner | None = None
        self._client = None
        self._chunk = 20
        self._cv = threading.Condition()
        self._rx = bytearray()
        self._lost = False

    def _on_notify(self, _char, data: bytearray) -> None:
        with self._cv:
            self._rx.extend(data)
            self._cv.notify_all()

    def _on_disconnect(self, _client) -> None:
        with self._cv:
            self._lost = True
            self._cv.notify_all()

    def _open(self) -> None:
        try:
            from bleak import BleakClient
        except ImportError as e:
            raise TransportError("BLE には bleak が必要です: pip install bleak") from e
        self._runner = _AsyncRunner()
        client = BleakClient(self.address, timeout=self.connect_timeout, disconnected_callback=self._on_disconnect)

        async def connect():
            await client.connect()
            try:
                await client.start_notify(BLE_NOTIFY, self._on_notify)
            except BaseException:
                await client.disconnect()  # do not leave a half-open link behind
                raise

        try:
            self._runner.run(connect(), self.connect_timeout + 5)
        except BaseException as e:
            try:
                if client.is_connected:
                    self._runner.run(client.disconnect(), 5)
            except BaseException:
                pass
            self._runner.stop()
            self._runner = None
            raise TransportError(f"BLE で {self.address} に接続できません: {e or type(e).__name__}") from e
        self._client = client
        mtu = getattr(client, "mtu_size", 23) or 23
        self._chunk = max(20, min(mtu - 3, BLE_MAX_CHUNK))

    def _close(self) -> None:
        try:
            if self._client is not None and self._runner is not None:
                self._runner.run(self._client.disconnect(), 10)
        except BaseException:
            pass
        finally:
            if self._runner:
                self._runner.stop()
            self._client = None
            self._runner = None

    def _write(self, data: bytes) -> None:
        if self._lost:
            raise TransportError("プリンタとの BLE 接続が切れました")

        async def send():
            for i in range(0, len(data), self._chunk):
                await self._client.write_gatt_char(BLE_WRITE, data[i : i + self._chunk], response=False)
                await self._runner._asyncio.sleep(BLE_CHUNK_PAUSE)

        # generous timeout: ~3.5 KB at 200 B / 15 ms takes well under a second
        try:
            self._runner.run(send(), 30 + len(data) / 1000)
        except BaseException as e:
            raise TransportError(f"BLE 書き込みに失敗しました: {e or type(e).__name__}") from e

    def _read_some(self, timeout: float) -> bytes:
        with self._cv:
            if not self._rx and not self._lost:
                self._cv.wait(timeout)
            if not self._rx and self._lost:
                raise TransportError("プリンタとの BLE 接続が切れました")
            out = bytes(self._rx)
            self._rx.clear()
            return out


# ---------------------------------------------------------------------------
class FileTransport(Transport):
    interactive = False

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = Path(path)
        self.name = f"file:{path}"
        self._fh = None

    def _open(self) -> None:
        self._fh = open(self.path, "wb")

    def _close(self) -> None:
        if self._fh:
            self._fh.close()

    def _write(self, data: bytes) -> None:
        self._fh.write(data)

    def _read_some(self, timeout: float) -> bytes:
        return b""


# ---------------------------------------------------------------------------
class MockTransport(Transport):
    """A simulated P21. Responds like the real printer and saves printed labels as PNG."""

    def __init__(self, outdir: str | None = None, battery: int = 75, print_seconds: float = 0.3) -> None:
        super().__init__()
        from .config import mock_dir

        self.outdir = Path(outdir) if outdir else mock_dir()
        self.name = f"mock:{self.outdir}" if outdir else "mock:"
        self.battery = battery
        self.print_seconds = print_seconds
        self._in = bytearray()
        self._out = bytearray()
        self._busy_until = 0.0
        self._job: dict = {}
        self.printed: list[Path] = []
        self.design_orientation = "landscape"
        self.jobs: list[dict] = []

    def _readiness(self) -> int:
        return protocol.Readiness.BUSY if time.monotonic() < self._busy_until else protocol.Readiness.READY

    def _write(self, data: bytes) -> None:
        self._in.extend(data)
        self._process()

    def _process(self) -> None:
        buf = self._in
        while buf:
            if buf.startswith(b"BITMAP "):
                comma_end = -1
                commas = 0
                for i, b in enumerate(buf):
                    if b == 0x2C:
                        commas += 1
                        if commas == 5:
                            comma_end = i
                            break
                if comma_end < 0:
                    return
                x, y, wb, h, mode = (int(v) for v in buf[7:comma_end].decode().split(","))
                need = comma_end + 1 + wb * h
                if len(buf) < need + 2:
                    return
                self._job["bitmap"] = (wb, h, bytes(buf[comma_end + 1 : need]))
                del buf[: need + 2]
                continue
            idx = buf.find(b"\r\n")
            if idx < 0:
                return
            line = bytes(buf[:idx])
            del buf[: idx + 2]
            self._command(line)

    def _command(self, line: bytes) -> None:
        if line == b"\x1b!?":
            self._out.append(self._readiness())
        elif line == b"\x1b!o":
            self._out.extend(protocol.build_status(self._readiness()))
        elif line == b"BATTERY?":
            bcd = ((self.battery // 10) << 4) | (self.battery % 10)
            self._out.extend(b"BATTERY " + bytes([bcd, 0]) + b"\r\n")
        elif line == b"CONFIG?":
            self._out.extend(b"CONFIG " + bytes([0, 0xCB, 0, 0, 3, 4, 2, 4, 2, 1]) + b"\r\n")
        elif line.startswith(b"PRINT "):
            self._finish_job(int(line[6:]))
        elif line.split(b" ")[0] in (b"SIZE", b"GAP", b"DIRECTION", b"DENSITY"):
            self._job[line.split(b" ")[0].decode().lower()] = line.decode(errors="replace")
        elif line == b"CLS":
            self._job.pop("bitmap", None)

    def _finish_job(self, copies: int) -> None:
        from PIL import Image

        job = dict(self._job, copies=copies)
        if "bitmap" in job:
            wb, h, data = job.pop("bitmap")
            img = Image.frombytes("1", (wb * 8, h), data)
            self.outdir.mkdir(parents=True, exist_ok=True)
            # save in the design orientation (undo the clockwise rotation of landscape designs)
            stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() // 1_000_000 % 1000:03d}"
            path = self.outdir / f"label-{stamp}.png"
            (img if self.design_orientation == "portrait" else img.transpose(Image.Transpose.ROTATE_90)).save(path)
            self.printed.append(path)
            job["png"] = str(path)
        self.jobs.append(job)
        self._busy_until = time.monotonic() + self.print_seconds * copies

    def _read_some(self, timeout: float) -> bytes:
        if not self._out:
            time.sleep(min(timeout, 0.02))
            return b""
        out = bytes(self._out)
        self._out.clear()
        return out


# ---------------------------------------------------------------------------
def from_spec(spec: str) -> Transport:
    """Create a transport from a device spec string (without opening it)."""
    spec = spec.strip()
    # bare values: MAC address -> rfcomm (or BLE where RFCOMM sockets are missing), COMx or /dev/... -> serial
    if MAC_RE.match(spec):
        return RfcommTransport(spec) if platform_supports_rfcomm() else BleTransport(spec)
    if re.match(r"^COM\d+$", spec, re.I) or spec.startswith("/dev/"):
        return SerialTransport(spec)
    kind, _, rest = spec.partition(":")
    kind = kind.lower()
    if kind == "rfcomm":
        addr, _, ch = rest.partition("@")
        if ch and not ch.isdigit():
            raise TransportError(f"RFCOMM チャンネルは数字で指定してください: {spec!r}")
        return RfcommTransport(addr, int(ch) if ch else 1)
    if kind == "serial":
        return SerialTransport(rest)
    if kind == "ble":
        return BleTransport(rest)
    if kind == "mock":
        return MockTransport(rest or None)
    if kind == "file":
        return FileTransport(rest or "p21-job.bin")
    raise TransportError(
        f"デバイス指定が不正です: {spec!r}\n"
        "例: rfcomm:AA:BB:CC:DD:EE:FF / serial:COM5 / ble:AA:BB:CC:DD:EE:FF / mock: / file:job.bin"
    )


def platform_supports_rfcomm() -> bool:
    return hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_RFCOMM") and sys.platform != "darwin"


__all__ = [
    "Transport",
    "TransportError",
    "SerialTransport",
    "RfcommTransport",
    "BleTransport",
    "MockTransport",
    "FileTransport",
    "from_spec",
    "platform_supports_rfcomm",
]
