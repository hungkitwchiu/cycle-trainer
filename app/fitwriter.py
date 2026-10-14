"""Write a recorded ride as a FIT activity file (accepted by Garmin Connect, Strava, TrainingPeaks...)."""
from __future__ import annotations

from datetime import datetime, timezone

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (Activity, Event, EventType, FileType, Manufacturer,
                                           Sport, SubSport)


def _ms(epoch_s: float) -> int:
    return int(round(epoch_s * 1000))


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _max(vals):
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def write_activity(path, samples: list[dict], laps: list[tuple[int, int]], summary: dict,
                   name: str = "") -> None:
    """samples: [{t (epoch s), power, heart_rate, cadence, speed (m/s), distance (m)}]
    laps: [(first_index, last_index_inclusive)] into samples."""
    if not samples:
        raise ValueError("no samples")
    start = samples[0]["t"]
    end = samples[-1]["t"]
    elapsed = end - start + 1
    timer = len(samples)

    b = FitFileBuilder(auto_define=True, min_string_size=50)

    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = 1
    fid.serial_number = 0x1C7C1E
    fid.time_created = _ms(start)
    b.add(fid)

    dev = DeviceInfoMessage()
    dev.timestamp = _ms(start)
    dev.manufacturer = Manufacturer.DEVELOPMENT.value
    dev.product = 1
    dev.software_version = 0.1
    b.add(dev)

    ev = EventMessage()
    ev.timestamp = _ms(start)
    ev.event = Event.TIMER
    ev.event_type = EventType.START
    b.add(ev)

    for s in samples:
        r = RecordMessage()
        r.timestamp = _ms(s["t"])
        if s.get("power") is not None:
            r.power = int(round(s["power"]))
        if s.get("heart_rate"):
            r.heart_rate = int(round(s["heart_rate"]))
        if s.get("cadence") is not None:
            r.cadence = int(round(s["cadence"]))
        if s.get("speed") is not None:
            r.speed = float(s["speed"])
        if s.get("distance") is not None:
            r.distance = float(s["distance"])
        b.add(r)

    ev = EventMessage()
    ev.timestamp = _ms(end)
    ev.event = Event.TIMER
    ev.event_type = EventType.STOP_ALL
    b.add(ev)

    if not laps:
        laps = [(0, len(samples) - 1)]
    for a, z in laps:
        seg = samples[a:z + 1]
        if not seg:
            continue
        lap = LapMessage()
        lap.timestamp = _ms(seg[-1]["t"])
        lap.start_time = _ms(seg[0]["t"])
        lap.total_elapsed_time = seg[-1]["t"] - seg[0]["t"] + 1
        lap.total_timer_time = len(seg)
        lap.event = Event.LAP
        lap.event_type = EventType.STOP
        lap.sport = Sport.CYCLING
        lap.sub_sport = SubSport.INDOOR_CYCLING
        _fill_stats(lap, seg)
        b.add(lap)

    sess = SessionMessage()
    sess.timestamp = _ms(end)
    sess.start_time = _ms(start)
    sess.total_elapsed_time = elapsed
    sess.total_timer_time = timer
    sess.sport = Sport.CYCLING
    sess.sub_sport = SubSport.INDOOR_CYCLING
    sess.event = Event.SESSION
    sess.event_type = EventType.STOP
    sess.first_lap_index = 0
    sess.num_laps = len(laps)
    _fill_stats(sess, samples)
    if summary.get("np"):
        sess.normalized_power = int(round(summary["np"]))
    if summary.get("tss") is not None:
        sess.training_stress_score = float(summary["tss"])
    if summary.get("if"):
        sess.intensity_factor = float(summary["if"])
    if summary.get("ftp"):
        sess.threshold_power = int(summary["ftp"])
    b.add(sess)

    act = ActivityMessage()
    act.timestamp = _ms(end)
    act.total_timer_time = timer
    act.num_sessions = 1
    act.type = Activity.MANUAL
    act.event = Event.ACTIVITY
    act.event_type = EventType.STOP
    offset = datetime.fromtimestamp(end, timezone.utc).astimezone().utcoffset()
    # local_timestamp is raw seconds since the FIT epoch (1989-12-31), in local time
    act.local_timestamp = int(end + (offset.total_seconds() if offset else 0)) - 631065600
    b.add(act)

    b.build().to_file(str(path))


def _fill_stats(msg, seg):
    pw = [s.get("power") for s in seg]
    hr = [s.get("heart_rate") or None for s in seg]
    cad = [s.get("cadence") for s in seg]
    spd = [s.get("speed") for s in seg]
    a = _avg(pw)
    if a is not None:
        msg.avg_power = int(round(a))
        msg.max_power = int(round(_max(pw)))
        msg.total_calories = int(round(sum(p or 0 for p in pw) / 1000.0))  # kJ ~= kcal
    if _avg(hr) is not None:
        msg.avg_heart_rate = int(round(_avg(hr)))
        msg.max_heart_rate = int(round(_max(hr)))
    if _avg(cad) is not None:
        msg.avg_cadence = int(round(_avg(cad)))
        msg.max_cadence = int(round(_max(cad)))
    if _avg(spd) is not None:
        msg.avg_speed = float(_avg(spd))
        msg.max_speed = float(_max(spd))
    d0 = seg[0].get("distance") or 0.0
    d1 = seg[-1].get("distance") or 0.0
    msg.total_distance = max(0.0, d1 - d0 + (seg[0].get("speed") or 0.0))
