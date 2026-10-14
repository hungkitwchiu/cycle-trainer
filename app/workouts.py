"""Workout model, library storage, importers and exporters.

A workout is a list of segments. Each segment has a duration (s) and a start/end
target as a fraction of FTP (start == end for steady blocks, different for ramps).
A segment with ``free: true`` has no target: the trainer goes to resistance mode.

Native file format (JSON, stored in data/workouts/<id>.json):
{
  "name": "Sweet Spot 3x10", "description": "...",
  "segments": [{"duration": 600, "start": 0.5, "end": 0.75, "cadence": null,
                "text": "Warm up", "free": false}, ...]
}

Import: .json (native), .zwo (Zwift), .mrc / .erg (TrainerRoad/Golden Cheetah style),
        .fit (a past ride, converted to a workout template from its power data).
Export: .zwo, .mrc, .json
"""
from __future__ import annotations

import io
import json
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import SAMPLE_WORKOUTS_DIR, WORKOUTS_DIR


@dataclass
class Segment:
    duration: int
    start: float
    end: float
    cadence: int | None = None
    text: str = ""
    free: bool = False

    def target_at(self, t: float) -> float | None:
        if self.free:
            return None
        if self.duration <= 0:
            return self.start
        f = max(0.0, min(1.0, t / self.duration))
        return self.start + (self.end - self.start) * f


@dataclass
class Workout:
    name: str
    segments: list[Segment] = field(default_factory=list)
    description: str = ""
    id: str = ""
    source: str = ""

    # ---- queries ------------------------------------------------------------
    @property
    def duration(self) -> int:
        return sum(s.duration for s in self.segments)

    def locate(self, t: float) -> tuple[int, float]:
        """Return (segment index, seconds into that segment). index == len => finished."""
        acc = 0
        for i, s in enumerate(self.segments):
            if t < acc + s.duration:
                return i, t - acc
            acc += s.duration
        return len(self.segments), 0.0

    def segment_start(self, idx: int) -> int:
        return sum(s.duration for s in self.segments[:idx])

    def target_at(self, t: float) -> float | None:
        i, into = self.locate(t)
        if i >= len(self.segments):
            return None
        return self.segments[i].target_at(into)

    def stats(self, ftp: float = 1.0) -> dict:
        """Estimated TSS / IF from the target profile (1 s resolution)."""
        vals = []
        for s in self.segments:
            for k in range(int(s.duration)):
                v = s.target_at(k + 0.5)
                vals.append(0.5 if v is None else v)
        if not vals:
            return {"duration": 0, "tss": 0, "if": 0}
        np_ = normalized(vals)
        hrs = len(vals) / 3600
        return {"duration": len(vals), "if": round(np_, 2), "tss": round(hrs * np_ * np_ * 100)}

    # ---- (de)serialisation ------------------------------------------------
    def to_dict(self) -> dict:
        d = {"id": self.id, "name": self.name, "description": self.description,
             "source": self.source, "segments": [asdict(s) for s in self.segments]}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Workout":
        segs = []
        for s in d.get("segments", []):
            segs.append(Segment(
                duration=int(round(float(s.get("duration", 0)))),
                start=float(s.get("start", s.get("power", 0.5)) or 0),
                end=float(s.get("end", s.get("start", s.get("power", 0.5))) or 0),
                cadence=int(s["cadence"]) if s.get("cadence") not in (None, "") else None,
                text=s.get("text", "") or "",
                free=bool(s.get("free", False)),
            ))
        return cls(name=d.get("name") or "Untitled", segments=[s for s in segs if s.duration > 0],
                   description=d.get("description", ""), id=d.get("id", ""),
                   source=d.get("source", ""))


def normalized(vals: list[float], window: int = 30) -> float:
    if not vals:
        return 0.0
    if len(vals) < window:
        return (sum(v ** 4 for v in vals) / len(vals)) ** 0.25
    s = sum(vals[:window])
    rolls = [s / window]
    for i in range(window, len(vals)):
        s += vals[i] - vals[i - window]
        rolls.append(s / window)
    return (sum(r ** 4 for r in rolls) / len(rolls)) ** 0.25


# ============================================================== library storage
def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()[:40]
    return f"{s or 'workout'}-{uuid.uuid4().hex[:6]}"


def list_workouts(ftp: float = 200) -> list[dict]:
    out = []
    for p in sorted(WORKOUTS_DIR.glob("*.json")):
        try:
            w = load_workout(p.stem)
        except Exception:  # noqa: BLE001
            continue
        st = w.stats()
        out.append({"id": w.id, "name": w.name, "description": w.description,
                    "duration": w.duration, "tss": st["tss"], "if": st["if"], "source": w.source,
                    "segments": [asdict(s) for s in w.segments]})
    out.sort(key=lambda x: x["name"].lower())
    return out


def load_workout(wid: str) -> Workout:
    p = WORKOUTS_DIR / f"{Path(wid).name}.json"
    w = Workout.from_dict(json.loads(p.read_text(encoding="utf-8")))
    w.id = p.stem
    return w


def save_workout(w: Workout) -> Workout:
    if not w.id:
        w.id = _slug(w.name)
    w.id = Path(w.id).name
    (WORKOUTS_DIR / f"{w.id}.json").write_text(json.dumps(w.to_dict(), indent=2), encoding="utf-8")
    return w


def delete_workout(wid: str) -> None:
    p = WORKOUTS_DIR / f"{Path(wid).name}.json"
    if p.exists():
        p.unlink()


def install_samples() -> None:
    """Copy bundled sample workouts into an empty library on first run."""
    if any(WORKOUTS_DIR.glob("*.json")) or not SAMPLE_WORKOUTS_DIR.exists():
        return
    for p in SAMPLE_WORKOUTS_DIR.iterdir():
        try:
            w = import_bytes(p.name, p.read_bytes())
            w.id = ""
            save_workout(w)
        except Exception:  # noqa: BLE001
            pass


# ===================================================================== importers
def import_bytes(filename: str, data: bytes, ftp: float = 200) -> Workout:
    ext = Path(filename).suffix.lower()
    stem = Path(filename).stem
    if ext == ".json":
        w = Workout.from_dict(json.loads(data.decode("utf-8-sig")))
        w.id = ""
    elif ext == ".zwo":
        w = parse_zwo(data.decode("utf-8-sig", errors="replace"))
    elif ext in (".mrc", ".erg"):
        w = parse_mrc(data.decode("utf-8-sig", errors="replace"), ftp)
    elif ext == ".fit":
        w = fit_to_workout(data, ftp)
    else:
        raise ValueError(f"Unsupported file type '{ext}'. Use .zwo, .mrc, .erg, .fit or .json")
    if not w.name or w.name == "Untitled":
        w.name = stem
    w.source = w.source or f"imported from {filename}"
    if not w.segments:
        raise ValueError("No workout steps found in file")
    return w


def _f(el, *names, default=None):
    for n in names:
        v = el.get(n)
        if v is not None and v != "":
            return float(v)
    return default


def parse_zwo(text: str) -> Workout:
    root = ET.fromstring(text)
    name = (root.findtext("name") or "").strip()
    desc = (root.findtext("description") or "").strip()
    wk = root.find("workout")
    segs: list[Segment] = []
    if wk is None:
        raise ValueError("ZWO file has no <workout> element")
    for el in wk:
        tag = el.tag.lower()
        dur = int(_f(el, "Duration", default=0) or 0)
        cad = _f(el, "Cadence")
        cad = int(cad) if cad else None
        texts = [t.get("message", "") for t in el.iter() if t.tag.lower() == "textevent"]
        text = texts[0] if texts else ""
        if tag in ("warmup", "cooldown", "ramp"):
            lo = _f(el, "PowerLow", "Power", default=0.5)
            hi = _f(el, "PowerHigh", "Power", default=lo)
            segs.append(Segment(dur, lo, hi, cad, text))
        elif tag == "steadystate":
            p = _f(el, "Power", default=None)
            if p is None:
                lo, hi = _f(el, "PowerLow", default=0.5), _f(el, "PowerHigh", default=0.5)
                p = (lo + hi) / 2
            segs.append(Segment(dur, p, p, cad, text))
        elif tag == "intervalst":
            rep = int(_f(el, "Repeat", default=1))
            on_d, off_d = int(_f(el, "OnDuration", default=0)), int(_f(el, "OffDuration", default=0))
            on_p = _f(el, "OnPower", "PowerOnHigh", "PowerOnLow", default=1.0)
            off_p = _f(el, "OffPower", "PowerOffLow", "PowerOffHigh", default=0.5)
            cad_on = _f(el, "Cadence")
            cad_off = _f(el, "CadenceResting")
            for r in range(rep):
                segs.append(Segment(on_d, on_p, on_p, int(cad_on) if cad_on else None,
                                    text if r == 0 else f"Interval {r + 1}/{rep}"))
                if off_d:
                    segs.append(Segment(off_d, off_p, off_p, int(cad_off) if cad_off else None, "Recover"))
        elif tag in ("freeride", "maxeffort"):
            segs.append(Segment(dur, 0.5, 0.5, cad, text or ("Max effort" if tag == "maxeffort" else "Free ride"),
                                free=True))
    return Workout(name=name, segments=[s for s in segs if s.duration > 0], description=desc)


def parse_mrc(text: str, ftp: float = 200) -> Workout:
    name, desc, unit, file_ftp = "", "", "percent", None
    pts: list[tuple[float, float]] = []
    in_data = False
    for raw in text.splitlines():
        line = raw.strip()
        up = line.upper()
        if not line:
            continue
        if up.startswith("[COURSE DATA]"):
            in_data = True
            continue
        if up.startswith("[END COURSE DATA]"):
            in_data = False
            continue
        if up.startswith("["):
            continue
        if in_data:
            parts = re.split(r"[\s,]+", line)
            try:
                pts.append((float(parts[0]), float(parts[1])))
            except (ValueError, IndexError):
                pass
            continue
        if "=" in line:
            k, v = [x.strip() for x in line.split("=", 1)]
            ku = k.upper()
            if ku == "DESCRIPTION":
                desc = v
            elif ku in ("FILE NAME", "FILENAME", "NAME"):
                name = Path(v).stem
            elif ku == "FTP":
                try:
                    file_ftp = float(v)
                except ValueError:
                    pass
        elif "MINUTES" in up:
            unit = "watts" if "WATTS" in up else "percent"
    base = file_ftp or ftp
    segs = []
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        dur = int(round((t1 - t0) * 60))
        if dur <= 0:
            continue
        conv = (lambda p: p / 100.0) if unit == "percent" else (lambda p: p / base)
        segs.append(Segment(dur, round(conv(p0), 4), round(conv(p1), 4)))
    return Workout(name=name, segments=_merge(segs), description=desc)


def _merge(segs: list[Segment]) -> list[Segment]:
    """Merge consecutive steady segments with identical targets."""
    out: list[Segment] = []
    for s in segs:
        if (out and not s.free and not out[-1].free and out[-1].start == out[-1].end == s.start == s.end
                and out[-1].cadence == s.cadence and not s.text):
            out[-1].duration += s.duration
        else:
            out.append(s)
    return out


def fit_to_workout(data: bytes, ftp: float = 200, step: float = 0.05) -> Workout:
    """Turn a recorded ride into a workout template.

    Power is smoothed (30 s), split where it shifts by more than ~one zone, each
    block is set to its average power rounded to 5% FTP, and tiny blocks merged.
    """
    import fitdecode

    times, power = [], []
    name = ""
    file_ftp = None
    with fitdecode.FitReader(io.BytesIO(data)) as fr:
        for frame in fr:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "record" and frame.has_field("timestamp"):
                p = frame.get_value("power", fallback=None)
                times.append(frame.get_value("timestamp"))
                power.append(float(p) if p is not None else None)
            elif frame.name == "session" and frame.has_field("threshold_power"):
                file_ftp = frame.get_value("threshold_power") or None
            elif frame.name == "workout" and frame.has_field("wkt_name"):
                name = frame.get_value("wkt_name") or ""
    if not times or all(p is None for p in power):
        raise ValueError("This FIT file has no power data to build a workout from")
    # resample to 1 Hz (fill gaps with last value, treat pauses > 10 s as removed)
    t0 = times[0]
    series: list[float] = []
    last_t, last_p = None, 0.0
    for t, p in zip(times, power):
        sec = (t - t0).total_seconds()
        p = last_p if p is None else p
        if last_t is not None:
            gap = int(sec - last_t)
            if 1 < gap <= 10:
                series.extend([last_p] * (gap - 1))
        series.append(p)
        last_t, last_p = sec, p
    ref = ftp or file_ftp or 200
    # smooth (10 s centred moving average: removes pedal noise without lagging the steps)
    half = 5
    pre = [0.0]
    for v in series:
        pre.append(pre[-1] + v)
    sm = []
    for i in range(len(series)):
        a, b = max(0, i - half), min(len(series), i + half)
        sm.append((pre[b] - pre[a]) / (b - a))
    # segment
    blocks: list[list[int]] = [[0, 0]]
    cur_vals = [sm[0]]
    threshold = 0.08 * ref
    pending = 0
    for i in range(1, len(sm)):
        mean = sum(cur_vals) / len(cur_vals)
        if abs(sm[i] - mean) > threshold:
            pending += 1
            if pending >= 15:
                split = i - pending + 1
                blocks[-1][1] = split - 1
                blocks.append([split, i])
                cur_vals = sm[split:i + 1]
                pending = 0
                continue
        else:
            pending = 0
        cur_vals.append(sm[i])
        blocks[-1][1] = i
    blocks[-1][1] = len(series) - 1
    segs: list[Segment] = []
    for a, b in blocks:
        raw = series[a:b + 1]
        if not raw:
            continue
        avg = sum(raw) / len(raw)
        frac = max(step, round(avg / ref / step) * step)
        segs.append(Segment(len(raw), round(frac, 3), round(frac, 3)))
    # absorb blocks shorter than 30 s into their neighbour
    merged: list[Segment] = []
    for s in segs:
        if merged and (s.duration < 30 or merged[-1].start == s.start):
            merged[-1].duration += s.duration  # keep the previous block's level
        else:
            merged.append(s)
    return Workout(name=name or "Imported ride", segments=_merge(merged),
                   description=f"Built from a recorded ride (FTP {int(ref)} W).")


# ===================================================================== exporters
def to_zwo(w: Workout) -> str:
    root = ET.Element("workout_file")
    ET.SubElement(root, "author").text = "CycleTrainer"
    ET.SubElement(root, "name").text = w.name
    ET.SubElement(root, "description").text = w.description
    ET.SubElement(root, "sportType").text = "bike"
    wk = ET.SubElement(root, "workout")
    for s in w.segments:
        attrs = {"Duration": str(s.duration)}
        if s.cadence:
            attrs["Cadence"] = str(s.cadence)
        if s.free:
            el = ET.SubElement(wk, "FreeRide", attrs)
        elif abs(s.start - s.end) < 1e-6:
            attrs["Power"] = f"{s.start:.3f}"
            el = ET.SubElement(wk, "SteadyState", attrs)
        else:
            attrs.update(PowerLow=f"{s.start:.3f}", PowerHigh=f"{s.end:.3f}")
            el = ET.SubElement(wk, "Ramp", attrs)
        if s.text:
            ET.SubElement(el, "textevent", {"timeoffset": "0", "message": s.text})
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def to_mrc(w: Workout) -> str:
    lines = ["[COURSE HEADER]", "VERSION = 2", "UNITS = ENGLISH",
             f"DESCRIPTION = {w.description}", f"FILE NAME = {w.name}",
             "MINUTES PERCENT", "[END COURSE HEADER]", "[COURSE DATA]"]
    t = 0.0
    for s in w.segments:
        lines.append(f"{t / 60:.2f}\t{(s.start if not s.free else 0.5) * 100:.0f}")
        t += s.duration
        lines.append(f"{t / 60:.2f}\t{(s.end if not s.free else 0.5) * 100:.0f}")
    lines.append("[END COURSE DATA]")
    return "\r\n".join(lines) + "\r\n"
