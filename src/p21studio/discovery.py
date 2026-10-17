"""Find P21 printers: paired Classic devices, Bluetooth serial ports and BLE advertisements."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys

from .transports import BLE_SERVICE, platform_supports_rfcomm

NAME_RE = re.compile(r"p21|nelko", re.I)


def _fmt_mac(hex12: str) -> str:
    h = hex12.upper()
    return ":".join(h[i : i + 2] for i in range(0, 12, 2))


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def paired_classic() -> list[dict]:
    """Paired Bluetooth Classic devices: [{name, address}]."""
    found: dict[str, str] = {}
    if sys.platform == "win32":
        ps = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
            "Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue | "
            "Where-Object { $_.InstanceId -like 'BTHENUM\\DEV_*' } | "
            "Select-Object FriendlyName,InstanceId | ConvertTo-Json -Compress"
        )
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], 20)
        try:
            rows = json.loads(out) if out.strip() else []
        except ValueError:
            rows = []
        for r in [rows] if isinstance(rows, dict) else rows:
            m = re.search(r"DEV_([0-9A-F]{12})", r.get("InstanceId") or "", re.I)
            if m:
                found[_fmt_mac(m.group(1))] = r.get("FriendlyName") or ""
    elif sys.platform == "darwin":
        out = _run(["system_profiler", "SPBluetoothDataType", "-json"], 20)
        try:
            data = json.loads(out)
        except ValueError:
            data = {}
        for ctrl in data.get("SPBluetoothDataType", []):
            for key in ("device_connected", "device_not_connected", "device_paired"):
                for entry in ctrl.get(key, []) or []:
                    for name, info in entry.items():
                        addr = (info or {}).get("device_address")
                        if addr:
                            found[addr.upper().replace("-", ":")] = name
    elif shutil.which("bluetoothctl"):
        out = _run(["bluetoothctl", "devices", "Paired"]) or _run(["bluetoothctl", "paired-devices"])
        for line in out.splitlines():
            m = re.match(r"Device ([0-9A-F:]{17}) (.*)", line.strip(), re.I)
            if m:
                found[m.group(1).upper()] = m.group(2)
    return [{"name": n, "address": a} for a, n in found.items()]


def serial_ports() -> list[dict]:
    from serial.tools import list_ports

    ports = []
    for p in list_ports.comports():
        hwid = p.hwid or ""
        desc = p.description or ""
        mac = None
        m = re.search(r"&([0-9A-F]{12})_", hwid, re.I)  # Windows BTHENUM outgoing port
        if m and m.group(1) != "000000000000":
            mac = _fmt_mac(m.group(1))
        is_bt = "BTHENUM" in hwid.upper() or "bluetooth" in desc.lower() or "rfcomm" in p.device or p.device.startswith("/dev/cu.") and NAME_RE.search(p.device)
        if "BTHENUM" in hwid.upper() and not mac:
            continue  # Windows incoming port: useless for printing
        ports.append({"device": p.device, "description": desc, "hwid": hwid, "bluetooth": bool(is_bt), "address": mac})
    if sys.platform == "darwin":  # list_ports may miss cu.* bluetooth nodes
        import glob

        known = {p["device"] for p in ports}
        for dev in glob.glob("/dev/cu.*"):
            if dev not in known and NAME_RE.search(dev):
                ports.append({"device": dev, "description": "Bluetooth serial", "hwid": "", "bluetooth": True, "address": None})
    return ports


def ble_scan(timeout: float = 5.0, all_devices: bool = False) -> list[dict]:
    try:
        import asyncio

        from bleak import BleakScanner
    except ImportError:
        return []

    async def scan():
        return await BleakScanner.discover(timeout=timeout, return_adv=True)

    try:
        result = asyncio.run(scan())
    except Exception:
        return []
    out = []
    for dev, adv in result.values():
        name = adv.local_name or dev.name or ""
        has_service = BLE_SERVICE in [u.lower() for u in (adv.service_uuids or [])]
        if all_devices or has_service or NAME_RE.search(name):
            out.append({"name": name, "address": dev.address, "rssi": adv.rssi, "p21_service": has_service})
    return sorted(out, key=lambda d: -(d["rssi"] or -999))


def discover(ble: bool = True, ble_timeout: float = 5.0, all_devices: bool = False) -> dict:
    """Return everything we can find plus suggested device specs (best first)."""
    classic = [d for d in paired_classic() if all_devices or NAME_RE.search(d["name"])]
    ports = serial_ports()
    bles = ble_scan(ble_timeout, all_devices) if ble else []

    suggestions: list[dict] = []
    for d in classic:
        if platform_supports_rfcomm():
            suggestions.append({"device": f"rfcomm:{d['address']}", "name": d["name"], "via": "Bluetooth Classic (RFCOMM)"})
    for p in ports:
        if p["bluetooth"] and (all_devices or p["address"] in {c["address"] for c in classic} or NAME_RE.search(p["device"] + p["description"])):
            suggestions.append({"device": f"serial:{p['device']}", "name": p["description"], "via": "Bluetooth serial port"})
    for b in bles:
        suggestions.append({"device": f"ble:{b['address']}", "name": b["name"], "via": f"Bluetooth LE (RSSI {b['rssi']})"})
    return {"suggestions": suggestions, "paired_classic": classic, "serial_ports": ports, "ble": bles}
