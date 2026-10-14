"""Ride session: plays a workout on the trainer and records data once per second."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import config
from .ble.devices import DeviceHub
from .fitwriter import write_activity
from .workouts import Workout, normalized

log = logging.getLogger("session")


def virtual_speed(power: float, mass_kg: float) -> float:
    """Flat-road speed (m/s) for a given power - same idea as most trainer apps."""
    if not power or power <= 0:
        return 0.0
    crr, cda, rho, g = 0.004, 0.32, 1.225, 9.81
    a, c = 0.5 * rho * cda, crr * mass_kg * g
    v = 8.0
    for _ in range(30):  # Newton's method on a v^3 + c v - P = 0
        f = a * v ** 3 + c * v - power
        v -= f / (3 * a * v ** 2 + c)
        v = max(v, 0.1)
    return v


class RideSession:
    def __init__(self, hub: DeviceHub):
        self.hub = hub
        self.reset(None)

    # ------------------------------------------------------------------ state
    def reset(self, workout: Workout | None) -> None:
        self.workout = workout
        self.state = "ready"  # ready | running | paused | finished
        self.elapsed = 0  # seconds of riding (timer time)
        self.offset = 0  # workout clock offset (skip / extend)
        self.samples: list[dict] = []
        self.lap_starts: list[int] = [0]
        self.intensity = 1.0
        self.erg = True
        self.grade = 0.0
        self.manual_watts = 150
        self.distance = 0.0
        self.paused_by = None  # "user" | "auto"
        self.zero_power_secs = 0
        self._last_seg_idx = 0
        self._power3 = deque(maxlen=3)
        self.saved = None
        self.started_at = None
        self.workout_done = False

    @property
    def settings(self) -> dict:
        return config.load_settings()

    @property
    def ftp(self) -> int:
        return int(self.settings.get("ftp", 200))

    def workout_time(self) -> float:
        return self.elapsed + self.offset

    def target_watts(self) -> int | None:
        if self.workout is None:
            return self.manual_watts if self.erg else None
        frac = self.workout.target_at(self.workout_time())
        if frac is None:
            return None
        return int(round(frac * self.ftp * self.intensity))

    # ----------------------------------------------------------------- control
    def start(self) -> None:
        if self.state in ("ready", "paused"):
            if self.started_at is None:
                self.started_at = time.time()
            self.state = "running"
            self.paused_by = None

    def pause(self, by: str = "user") -> None:
        if self.state == "running":
            self.state = "paused"
            self.paused_by = by

    def skip(self) -> None:
        if not self.workout:
            return
        idx, into = self.workout.locate(self.workout_time())
        if idx < len(self.workout.segments):
            self.offset += self.workout.segments[idx].duration - into

    def extend(self, seconds: int) -> None:
        """Repeat part of the current interval (negative offset)."""
        if self.workout:
            self.offset -= seconds

    def adjust_intensity(self, delta: float) -> None:
        self.intensity = max(0.5, min(1.5, round(self.intensity + delta, 2)))

    # -------------------------------------------------------------------- tick
    async def tick(self) -> None:
        """Called once per second by the app loop."""
        m = self.hub.current()
        power = m.get("power")
        self._power3.append(power or 0)

        # auto start / pause / resume
        pedaling = (power or 0) > 5 or (m.get("cadence") or 0) > 10
        if self.state == "running" and self.settings.get("auto_pause", True):
            self.zero_power_secs = 0 if pedaling else self.zero_power_secs + 1
            if self.zero_power_secs >= 5:
                self.pause("auto")
        elif self.state == "paused" and self.paused_by == "auto" and pedaling:
            self.start()

        # trainer command
        await self._command_trainer()

        if self.state != "running":
            return

        mass = float(self.settings.get("weight_kg", 75)) + 9.0
        speed = virtual_speed(power or 0, mass)
        self.distance += speed
        target = self.target_watts()
        self.samples.append({
            "t": int(time.time()),
            "power": power, "heart_rate": m.get("heart_rate"), "cadence": m.get("cadence"),
            "speed": speed, "distance": self.distance, "target": target,
            "wt": self.workout_time(),
        })
        self.elapsed += 1

        if self.workout:
            idx, _ = self.workout.locate(self.workout_time())
            if idx != self._last_seg_idx:
                self.lap_starts.append(len(self.samples))
                self._last_seg_idx = idx
            if idx >= len(self.workout.segments) and not self.workout_done:
                self.finish_workout()

    async def _command_trainer(self) -> None:
        t = self.target_watts() if self.state != "finished" else None
        if self.erg and t is not None:
            await self.hub.set_erg(t)
        else:
            await self.hub.set_grade(self.grade)

    def finish_workout(self) -> None:
        """Workout reached the end: drop to free ride so the user can keep spinning/cool down."""
        self.workout_done = True

    # ------------------------------------------------------------- summarise
    def summary(self) -> dict:
        pw = [s["power"] or 0 for s in self.samples]
        hr = [s["heart_rate"] for s in self.samples if s["heart_rate"]]
        cad = [s["cadence"] for s in self.samples if s["cadence"]]
        ftp = self.ftp
        np_ = normalized(pw) if pw else 0
        if_ = np_ / ftp if ftp else 0
        secs = len(pw)
        return {
            "duration": secs,
            "avg_power": round(sum(pw) / secs) if secs else 0,
            "max_power": max(pw) if pw else 0,
            "np": round(np_),
            "if": round(if_, 2),
            "tss": round(secs * np_ * if_ / (ftp * 3600) * 100, 1) if ftp else 0,
            "avg_hr": round(sum(hr) / len(hr)) if hr else None,
            "max_hr": max(hr) if hr else None,
            "avg_cadence": round(sum(cad) / len(cad)) if cad else None,
            "kj": round(sum(pw) / 1000),
            "distance_km": round(self.distance / 1000, 2),
            "ftp": ftp,
        }

    def save(self, name: str) -> dict:
        """Write FIT + JSON summary. Returns activity record."""
        if len(self.samples) < 10:
            raise ValueError("Ride too short to save (less than 10 seconds recorded)")
        start = datetime.fromtimestamp(self.samples[0]["t"])
        aid = start.strftime("%Y%m%d-%H%M%S")
        folder = config.ACTIVITIES_DIR
        fit_path = folder / f"{aid}.fit"
        n = len(self.samples)
        bounds = sorted(set(i for i in self.lap_starts if 0 <= i < n))
        laps = [(a, (bounds[k + 1] - 1) if k + 1 < len(bounds) else n - 1) for k, a in enumerate(bounds)]
        summ = self.summary()
        write_activity(fit_path, self.samples, laps, summ, name)
        rec = {"id": aid, "name": name, "start": start.isoformat(timespec="seconds"),
               "summary": summ, "fit": fit_path.name, "uploads": {},
               "power_trace": _downsample([s["power"] or 0 for s in self.samples], 600),
               "hr_trace": _downsample([s["heart_rate"] or 0 for s in self.samples], 600)}
        (folder / f"{aid}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        self.saved = rec
        return rec

    # ------------------------------------------------------------------- view
    def view(self) -> dict:
        m = self.hub.current()
        w = self.workout
        seg_info = None
        if w:
            wt = self.workout_time()
            idx, into = w.locate(wt)
            if idx < len(w.segments):
                seg = w.segments[idx]
                nxt = w.segments[idx + 1] if idx + 1 < len(w.segments) else None
                seg_info = {
                    "index": idx, "remaining": int(math.ceil(seg.duration - into)),
                    "text": seg.text, "cadence": seg.cadence, "free": seg.free,
                    "next": ({"start": nxt.start, "end": nxt.end, "duration": nxt.duration,
                              "free": nxt.free} if nxt else None),
                }
        p3 = round(sum(self._power3) / len(self._power3)) if self._power3 else None
        return {
            "state": self.state, "paused_by": self.paused_by, "workout_done": self.workout_done,
            "elapsed": self.elapsed, "workout_time": self.workout_time() if w else None,
            "workout": ({"id": w.id, "name": w.name, "duration": w.duration} if w else None),
            "segment": seg_info,
            "target": self.target_watts(), "intensity": self.intensity, "erg": self.erg,
            "grade": self.grade, "manual_watts": self.manual_watts, "ftp": self.ftp,
            "metrics": {**m, "power3s": p3 if m.get("power") is not None else None},
            "distance_km": round(self.distance / 1000, 2),
            "recorded": len(self.samples),
        }

    def trace(self, since: int = 0) -> list:
        return [[i, s["power"], s["heart_rate"], s["target"], s["wt"]]
                for i, s in enumerate(self.samples[since:], start=since)]


def _downsample(vals: list[float], n: int) -> list[int]:
    if len(vals) <= n:
        return [round(v) for v in vals]
    step = len(vals) / n
    return [round(sum(vals[int(i * step):int((i + 1) * step)]) / max(1, int((i + 1) * step) - int(i * step)))
            for i in range(n)]


def list_activities() -> list[dict]:
    out = []
    for p in sorted(config.ACTIVITIES_DIR.glob("*.json"), reverse=True):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            out.append(rec)
        except (OSError, ValueError):
            pass
    return out


def load_activity(aid: str) -> dict:
    return json.loads((config.ACTIVITIES_DIR / f"{Path(aid).name}.json").read_text(encoding="utf-8"))


def update_activity(rec: dict) -> None:
    (config.ACTIVITIES_DIR / f"{rec['id']}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
