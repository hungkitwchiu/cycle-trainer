"""Pure parsing/encoding of Bluetooth LE fitness packets (no Bluetooth needed).

References: Bluetooth SIG GATT specs for Heart Rate (0x180D), Cycling Power (0x1818),
Cycling Speed & Cadence (0x1816) and Fitness Machine (0x1826).
"""
from __future__ import annotations

import struct
import time


def uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


# Services
HR_SERVICE = uuid16(0x180D)
CPS_SERVICE = uuid16(0x1818)
CSC_SERVICE = uuid16(0x1816)
FTMS_SERVICE = uuid16(0x1826)
# Characteristics
HR_MEASUREMENT = uuid16(0x2A37)
CPS_MEASUREMENT = uuid16(0x2A63)
CSC_MEASUREMENT = uuid16(0x2A5B)
FTMS_INDOOR_BIKE_DATA = uuid16(0x2AD2)
FTMS_CONTROL_POINT = uuid16(0x2AD9)
FTMS_STATUS = uuid16(0x2ADA)
# Wahoo proprietary trainer control (lives inside the Cycling Power service on KICKRs)
WAHOO_TRAINER_CP = "a026e005-0a7d-4ab3-97fa-f1500f9feb8b"


# --------------------------------------------------------------------------- HR
def parse_heart_rate(data: bytes) -> dict:
    flags = data[0]
    i = 1
    if flags & 0x01:
        hr = struct.unpack_from("<H", data, i)[0]
        i += 2
    else:
        hr = data[i]
        i += 1
    out = {"heart_rate": hr}
    if flags & 0x08:  # energy expended
        i += 2
    if flags & 0x10:  # RR intervals, 1/1024 s
        rr = []
        while i + 1 < len(data):
            rr.append(struct.unpack_from("<H", data, i)[0] / 1024.0)
            i += 2
        out["rr"] = rr
    return out


# ------------------------------------------------------------- crank cadence
class CrankCadence:
    """Turns cumulative crank revolutions + event time (1/1024 s) into rpm."""

    def __init__(self, stale_after: float = 2.5):
        self._last = None  # (revs, event_time)
        self._rpm = 0.0
        self._last_change = 0.0
        self.stale_after = stale_after

    def update(self, revs: int, event_time: int, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        if self._last is not None:
            d_revs = (revs - self._last[0]) & 0xFFFF
            d_t = ((event_time - self._last[1]) & 0xFFFF) / 1024.0
            if d_revs > 0 and d_t > 0:
                rpm = d_revs / d_t * 60.0
                if rpm < 250:
                    self._rpm = rpm
                    self._last_change = now
            elif now - self._last_change > self.stale_after:
                self._rpm = 0.0
        else:
            self._last_change = now
        self._last = (revs, event_time)
        return self._rpm


# ------------------------------------------------------------ cycling power
def parse_cycling_power(data: bytes) -> dict:
    flags, power = struct.unpack_from("<Hh", data, 0)
    i = 4
    out: dict = {"power": power}
    if flags & 0x0001:  # pedal power balance
        i += 1
    if flags & 0x0004:  # accumulated torque
        i += 2
    if flags & 0x0010:  # wheel revolution data
        i += 6
    if flags & 0x0020 and i + 4 <= len(data):  # crank revolution data
        revs, t = struct.unpack_from("<HH", data, i)
        out["crank_revs"] = revs
        out["crank_time"] = t
    return out


def parse_csc(data: bytes) -> dict:
    flags = data[0]
    i = 1
    out: dict = {}
    if flags & 0x01:
        i += 6
    if flags & 0x02 and i + 4 <= len(data):
        revs, t = struct.unpack_from("<HH", data, i)
        out["crank_revs"] = revs
        out["crank_time"] = t
    return out


# ------------------------------------------------------- FTMS indoor bike data
def parse_indoor_bike_data(data: bytes) -> dict:
    flags = struct.unpack_from("<H", data, 0)[0]
    i = 2
    out: dict = {}

    def take(fmt):
        nonlocal i
        v = struct.unpack_from(fmt, data, i)[0]
        i += struct.calcsize(fmt)
        return v

    try:
        if not flags & 0x0001:  # "More data" bit is inverted: 0 => speed present
            out["speed_kmh"] = take("<H") / 100.0
        if flags & 0x0002:
            take("<H")  # average speed
        if flags & 0x0004:
            out["cadence"] = take("<H") / 2.0
        if flags & 0x0008:
            take("<H")  # average cadence
        if flags & 0x0010:
            lo, hi = struct.unpack_from("<HB", data, i)
            i += 3
            out["distance_m"] = lo | (hi << 16)
        if flags & 0x0020:
            out["resistance"] = take("<h")
        if flags & 0x0040:
            out["power"] = take("<h")
        if flags & 0x0080:
            take("<h")  # average power
        if flags & 0x0100:
            i += 5  # total energy, energy/hour, energy/minute
        if flags & 0x0200:
            out["heart_rate"] = take("<B")
    except struct.error:
        pass  # truncated packet: return what we have
    return out


# ---------------------------------------------------------- FTMS control point
FTMS_OP_REQUEST_CONTROL = 0x00
FTMS_OP_RESET = 0x01
FTMS_OP_SET_TARGET_RESISTANCE = 0x04
FTMS_OP_SET_TARGET_POWER = 0x05
FTMS_OP_START_RESUME = 0x07
FTMS_OP_STOP_PAUSE = 0x08
FTMS_OP_SET_SIMULATION = 0x11
FTMS_OP_RESPONSE = 0x80

FTMS_RESULTS = {1: "success", 2: "not supported", 3: "invalid parameter",
                4: "operation failed", 5: "control not permitted"}


def ftms_request_control() -> bytes:
    return bytes([FTMS_OP_REQUEST_CONTROL])


def ftms_start() -> bytes:
    return bytes([FTMS_OP_START_RESUME])


def ftms_set_target_power(watts: int) -> bytes:
    return struct.pack("<Bh", FTMS_OP_SET_TARGET_POWER, max(0, min(int(round(watts)), 4000)))


def ftms_set_simulation(grade_pct: float, wind_mps: float = 0.0,
                        crr: float = 0.004, cw: float = 0.51) -> bytes:
    return struct.pack("<BhhBB", FTMS_OP_SET_SIMULATION,
                       int(round(wind_mps * 1000)),
                       int(round(max(-40, min(40, grade_pct)) * 100)),
                       int(round(crr * 10000)) & 0xFF,
                       int(round(cw * 100)) & 0xFF)


def parse_ftms_response(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 3 and data[0] == FTMS_OP_RESPONSE:
        return data[1], data[2]
    return None


# ------------------------------------------------- Wahoo proprietary control
WAHOO_UNLOCK = bytes([0x20, 0xEE, 0xFC])


def wahoo_set_erg(watts: int) -> bytes:
    return struct.pack("<BH", 0x42, max(0, min(int(round(watts)), 4000)))


def wahoo_init_sim(weight_kg: float, crr: float = 0.004, cw: float = 0.51) -> bytes:
    return struct.pack("<BHHH", 0x43, int(weight_kg * 100), int(crr * 10000), int(cw * 1000))


def wahoo_set_grade(grade_pct: float) -> bytes:
    g = max(-1.0, min(1.0, grade_pct / 100.0))
    return struct.pack("<BH", 0x46, int(round((g + 1.0) * 65535 / 2.0)))
