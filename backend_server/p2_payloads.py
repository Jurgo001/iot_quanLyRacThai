"""P2-B reference wire formats. ESP32 demo still transmits HTTP/JSON over Wi-Fi.

The 19-byte packed format is a *proposed LoRaWAN application payload*, not a
LoRaWAN PHY/MAC packet. The network server must bind DevEUI to an authorized
device before forwarding decoded telemetry to the cloud.
"""

from __future__ import annotations

import json
import math
import struct
from typing import Any


PACKET_VERSION = 1
PACKET_FORMAT = ">BBIIHHBI"  # version, bin, seq, captured_at, distance, fill, flags, boot
PACKET_BYTES = struct.calcsize(PACKET_FORMAT)  # 19 bytes
DISTANCE_INVALID = 65535
FLAG_SENSOR_ERROR = 0x01
FLAG_PRIORITY = 0x02
FLAG_TILT = 0x04
FLAG_FIRE = 0x08
KNOWN_FLAGS = FLAG_SENSOR_ERROR | FLAG_PRIORITY | FLAG_TILT | FLAG_FIRE


def minified_json(message: dict[str, Any]) -> bytes:
    """NB-IoT example: no insignificant whitespace, UTF-8, unchanged semantics."""
    return json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _int_in_range(value: Any, lo: int, hi: int, name: str) -> int:
    if type(value) is not int or value < lo or value > hi:
        raise ValueError(f"{name} must be an integer in [{lo}, {hi}]")
    return value


def _scaled(value: Any, upper: float, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not 0 <= value <= upper:
        raise ValueError(f"{name} outside supported range 0..{upper}")
    return int(math.floor(float(value) * 10 + 0.5))


def pack_lorawan_payload(*, bin_number: int, boot_id: int, sequence: int,
                         captured_at_unix: int, distance_cm: float | None,
                         fill_percent: float | None, sensor_error: bool = False,
                         is_priority: bool = False, tilt_detected: bool = False,
                         fire_detected: bool = False) -> bytes:
    """Encode 19 bytes of network-server-independent application data.

    Timestamp zero means no trustworthy UTC clock. A valid timestamp is Unix
    seconds; uptime seconds must never be placed in this field.
    """
    bin_number = _int_in_range(bin_number, 1, 6, "bin_number")
    boot_id = _int_in_range(boot_id, 0, 0xFFFFFFFF, "boot_id")
    sequence = _int_in_range(sequence, 0, 0xFFFFFFFF, "sequence")
    captured_at_unix = _int_in_range(captured_at_unix, 0, 0xFFFFFFFF, "captured_at_unix")
    for name, flag in (("sensor_error", sensor_error), ("is_priority", is_priority),
                       ("tilt_detected", tilt_detected), ("fire_detected", fire_detected)):
        if type(flag) is not bool:
            raise ValueError(f"{name} must be a real boolean")
    if sensor_error:
        distance = DISTANCE_INVALID
        fill = DISTANCE_INVALID
    else:
        distance = _scaled(distance_cm, 400, "distance_cm")
        fill = _scaled(fill_percent, 100, "fill_percent")
    flags = (FLAG_SENSOR_ERROR if sensor_error else 0) | (FLAG_PRIORITY if is_priority else 0)
    flags |= (FLAG_TILT if tilt_detected else 0) | (FLAG_FIRE if fire_detected else 0)
    return struct.pack(PACKET_FORMAT, PACKET_VERSION, bin_number, sequence,
                       captured_at_unix, distance, fill, flags, boot_id)


def unpack_lorawan_payload(payload: bytes) -> dict[str, Any]:
    if len(payload) != PACKET_BYTES:
        raise ValueError(f"expected {PACKET_BYTES} bytes, got {len(payload)}")
    version, bin_no, sequence, captured_at, distance, fill, flags, boot = struct.unpack(PACKET_FORMAT, payload)
    if version != PACKET_VERSION:
        raise ValueError("unsupported packet version")
    _int_in_range(bin_no, 1, 6, "bin_number")
    if flags & ~KNOWN_FLAGS:
        raise ValueError("unknown flag bits")
    sensor_error = bool(flags & FLAG_SENSOR_ERROR)
    if sensor_error != (distance == DISTANCE_INVALID and fill == DISTANCE_INVALID):
        raise ValueError("inconsistent sensor error sentinel")
    if not sensor_error and (distance > 4000 or fill > 1000):
        raise ValueError("measurement out of range")
    bin_id = f"BIN_{bin_no:02d}"
    return {
        "bin_id": bin_id,
        "message_id": f"{bin_id}_MSG_{boot:x}_{sequence}",
        "boot_id": boot,
        "sequence": sequence,
        "captured_at_unix": captured_at or None,
        "distance_cm": -1.0 if sensor_error else distance / 10.0,
        "fill_percent": -1.0 if sensor_error else fill / 10.0,
        "sensor_status": "SENSOR_ERROR" if sensor_error else "OK",
        "is_priority": bool(flags & FLAG_PRIORITY),
        "tilt_detected": bool(flags & FLAG_TILT),
        "fire_detected": bool(flags & FLAG_FIRE),
    }


def lorawan_to_telemetry(payload: bytes, *, device_id: str) -> dict[str, Any]:
    """Map a decoded uplink into the existing /api/telemetry JSON contract.

    Call only inside an authenticated network-server integration. The caller
    MUST bind the sender's DevEUI to its registered device_id beforehand;
    an untrusted uplink may not choose device_id. A LoRaWAN frame in this
    compact format lacks uptime, so measured_at=0 means "not provided".
    """
    if not device_id or not isinstance(device_id, str):
        raise ValueError("network-server-bound device_id is required")
    decoded = unpack_lorawan_payload(payload)
    result = {
        "device_id": device_id,
        "bin_id": decoded["bin_id"],
        "message_id": decoded["message_id"],
        "measured_at": 0,
        "distance_cm": decoded["distance_cm"],
        "fill_percent": decoded["fill_percent"],
        "sensor_status": decoded["sensor_status"],
        "is_priority": decoded["is_priority"],
        "tilt_detected": decoded["tilt_detected"],
        "fire_detected": decoded["fire_detected"],
    }
    if decoded["captured_at_unix"]:
        result["captured_at_unix"] = decoded["captured_at_unix"]
    return result
