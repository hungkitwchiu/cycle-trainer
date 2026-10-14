"""Run with:  python -m pytest tests   (no Bluetooth hardware needed)"""
import math
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ble import parsers as P  # noqa: E402
from app import workouts as W  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_heart_rate_uint8_and_uint16():
    assert P.parse_heart_rate(bytes([0x00, 72]))["heart_rate"] == 72
    assert P.parse_heart_rate(bytes([0x01]) + struct.pack("<H", 181))["heart_rate"] == 181
    d = P.parse_heart_rate(bytes([0x10, 60]) + struct.pack("<HH", 1024, 512))
    assert d["rr"] == [1.0, 0.5]


def test_indoor_bike_data():
    # flags: speed present (bit0=0), cadence (bit2), power (bit6)
    flags = 0x0004 | 0x0040
    pkt = struct.pack("<HHHh", flags, 3250, 180, 245)
    d = P.parse_indoor_bike_data(pkt)
    assert d == {"speed_kmh": 32.5, "cadence": 90.0, "power": 245}
    # "more data" set -> no speed; power only
    d = P.parse_indoor_bike_data(struct.pack("<Hh", 0x0041, 300))
    assert d == {"power": 300}


def test_cycling_power_with_crank():
    pkt = struct.pack("<HhHH", 0x0020, 210, 100, 2048)
    d = P.parse_cycling_power(pkt)
    assert d["power"] == 210 and d["crank_revs"] == 100
    c = P.CrankCadence()
    c.update(100, 0, now=0)
    rpm = c.update(101, 683, now=0.67)  # one rev in 0.667 s => 90 rpm
    assert abs(rpm - 90) < 0.5
    rpm = c.update(0, 1366, now=1.3)  # wrap-around of the 16-bit counter (65535 -> 0)
    assert rpm > 0


def test_control_point_encoding():
    assert P.ftms_set_target_power(250) == bytes([0x05, 250, 0])
    assert P.ftms_set_target_power(300) == bytes([0x05]) + struct.pack("<h", 300)
    sim = P.ftms_set_simulation(5.0)
    assert sim[0] == 0x11 and struct.unpack_from("<h", sim, 3)[0] == 500
    assert P.parse_ftms_response(bytes([0x80, 0x05, 0x01])) == (5, 1)
    assert P.wahoo_set_erg(200) == bytes([0x42, 200, 0])


def test_zwo_import_and_roundtrip():
    w = W.import_bytes("VO2max 5x3.zwo", (ROOT / "sample_workouts" / "VO2max 5x3.zwo").read_bytes())
    assert w.name == "VO2max 5x3"
    on = [s for s in w.segments if abs(s.start - 1.15) < 1e-9]
    assert len(on) == 5 and all(s.cadence == 100 for s in on)
    assert w.segments[0].start == 0.45 and w.segments[0].end == 0.75
    w2 = W.parse_zwo(W.to_zwo(w))
    assert w2.duration == w.duration and len(w2.segments) == len(w.segments)


def test_mrc_import():
    w = W.import_bytes("Endurance 60.mrc", (ROOT / "sample_workouts" / "Endurance 60.mrc").read_bytes())
    assert w.duration == 3600
    assert w.segments[1].start == 0.68
    erg = "[COURSE HEADER]\nFTP = 250\nMINUTES WATTS\n[END COURSE HEADER]\n[COURSE DATA]\n0 125\n10 125\n10 250\n15 250\n[END COURSE DATA]\n"
    w = W.import_bytes("x.erg", erg.encode())
    assert [round(s.start, 2) for s in w.segments] == [0.5, 1.0]


def test_target_and_locate():
    w = W.Workout("t", [W.Segment(100, 0.5, 1.0), W.Segment(50, 0.8, 0.8)])
    assert w.target_at(0) == 0.5
    assert abs(w.target_at(50) - 0.75) < 1e-9
    assert w.target_at(120) == 0.8
    assert w.target_at(150) is None
    assert w.locate(149) == (1, 49)


def test_fit_write_and_import_as_template(tmp_path):
    from app.fitwriter import write_activity

    t0 = time.time() - 4000
    samples, d = [], 0.0
    plan = [(600, 120), (300, 260), (180, 120), (300, 260), (300, 110)]
    for dur, pw in plan:
        for _ in range(dur):
            d += 9
            samples.append({"t": t0 + len(samples), "power": pw + 5 * math.sin(len(samples)), "heart_rate": 130,
                            "cadence": 90, "speed": 9.0, "distance": d})
    f = tmp_path / "ride.fit"
    write_activity(f, samples, [(0, 599), (600, len(samples) - 1)], {"np": 200, "tss": 40, "if": 0.8, "ftp": 250})
    w = W.import_bytes("ride.fit", f.read_bytes(), ftp=250)
    levels = [round(s.start * 250) for s in w.segments]
    assert len(w.segments) == 5, levels
    assert abs(w.duration - len(samples)) <= 2
    assert levels[1] in (250, 262) and levels[0] in (112, 125)
