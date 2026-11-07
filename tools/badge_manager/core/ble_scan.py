"""BLE scan for Maker Detector badges, run in a CHILD process (never import bleak in the GUI).

    python -m core.ble_scan [seconds]
        prints one JSON line: [{"id", "name", "rssi", "address"}, ...] sorted by signal strength,
        or {"error": "..."} with exit code 2 on a Python-level failure.

Why a subprocess: macOS (TCC) kills any process that touches CoreBluetooth without Bluetooth
permission with SIGABRT: no exception, no traceback, the whole app just disappears. Python started
from a terminal / IDE has no NSBluetoothAlwaysUsageDescription, so the GUI must never do this
itself. The caller (tabs/maker.py) treats a signal exit / code 134 / no output as "blocked".

parse_maker_payload is pure and also used by the GUI.
"""

from __future__ import annotations

import json
import sys

COMPANY_ID = 0xFFFF
MAGIC = b"SC26"
VERSION = 0x01
MAX_TEXT = 16


def parse_maker_payload(data: bytes) -> dict | None:
    """Parse manufacturer data *after* the company id (as bleak gives it):
    b"SC26" + version(1) + id(3) + name(0-16). Returns {"version","id","name"} or None."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 8 or bytes(data[:4]) != MAGIC:
        return None
    version = data[4]
    if version != VERSION:
        return None
    raw_name = bytes(data[8:8 + MAX_TEXT])
    name = "".join(chr(b) if 0x20 <= b <= 0x7E else "?" for b in raw_name)
    return {"version": version, "id": bytes(data[5:8]).hex(), "name": name}


def scan(seconds: float) -> list[dict]:
    import asyncio

    from bleak import BleakScanner

    async def go():
        return await BleakScanner.discover(timeout=seconds, return_adv=True)

    found = {}
    for addr, (_dev, adv) in asyncio.run(go()).items():
        payload = (adv.manufacturer_data or {}).get(COMPANY_ID)
        p = parse_maker_payload(payload) if payload else None
        if p:
            found[p["id"]] = {"id": p["id"], "name": p["name"], "rssi": adv.rssi, "address": addr}
    return sorted(found.values(), key=lambda d: -(d["rssi"] if d["rssi"] is not None else -999))


def main(argv: list[str]) -> int:
    try:
        seconds = float(argv[1]) if len(argv) > 1 else 10.0
        rows = scan(seconds)
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}), flush=True)
        return 2
    print(json.dumps(rows), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
