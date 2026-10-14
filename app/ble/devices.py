"""Bluetooth LE sensors (via bleak), a hub that merges their readings, and simulators.

Roles:
  trainer  - smart trainer (FTMS preferred, Wahoo proprietary fallback). Supplies
             power (+ cadence/speed if the trainer reports them) and takes ERG/grade.
  hr       - heart-rate strap.
  cadence  - optional standalone cadence sensor (CSC) or power meter (CPS).
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Callable

from . import parsers as P

log = logging.getLogger("ble")

ROLES = ("trainer", "hr", "cadence")
FRESH_SECONDS = 3.0
# Which source wins for each metric, highest priority first.
PRIORITY = {
    "power": ["ftms", "cps", "wahoo", "sim"],
    "cadence": ["csc", "cad_cps", "ftms", "cps", "sim"],
    "heart_rate": ["hr", "ftms", "sim_hr"],
    "speed_kmh": ["ftms"],
}


class DeviceHub:
    def __init__(self):
        self.sensors: dict[str, "BaseSensor"] = {}
        self._readings: dict[str, dict[str, tuple[float, float]]] = {}
        self.listeners: list[Callable[[], None]] = []

    # ---- readings -------------------------------------------------------
    def report(self, source: str, **values) -> None:
        now = time.monotonic()
        for k, v in values.items():
            if v is None:
                continue
            self._readings.setdefault(k, {})[source] = (float(v), now)

    def current(self) -> dict:
        now = time.monotonic()
        out = {}
        for metric, order in PRIORITY.items():
            srcs = self._readings.get(metric, {})
            val = None
            for s in order:
                if s in srcs and now - srcs[s][1] <= FRESH_SECONDS:
                    val = srcs[s][0]
                    break
            out[metric] = val
        return out

    # ---- sensors ----------------------------------------------------------
    async def attach(self, role: str, sensor: "BaseSensor") -> None:
        await self.detach(role)
        self.sensors[role] = sensor
        await sensor.start()

    async def detach(self, role: str) -> None:
        old = self.sensors.pop(role, None)
        if old:
            await old.stop()

    async def detach_all(self) -> None:
        for role in list(self.sensors):
            await self.detach(role)

    @property
    def trainer(self):
        return self.sensors.get("trainer")

    async def set_erg(self, watts: int) -> None:
        t = self.trainer
        if t and t.connected:
            await t.set_erg(watts)

    async def set_grade(self, grade: float) -> None:
        t = self.trainer
        if t and t.connected:
            await t.set_grade(grade)

    def status(self) -> dict:
        return {role: s.describe() for role, s in self.sensors.items()}


# =========================================================================== base
class BaseSensor:
    kind = "ble"

    def __init__(self, hub: DeviceHub, address: str, name: str):
        self.hub = hub
        self.address = address
        self.name = name or address
        self.state = "disconnected"
        self.error = ""
        self.info = ""
        self._want = False
        self._task: asyncio.Task | None = None
        self.client = None

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def describe(self) -> dict:
        return {"name": self.name, "address": self.address, "state": self.state,
                "error": self.error, "info": self.info, "kind": self.kind}

    async def start(self) -> None:
        self._want = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._want = False
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.state = "disconnected"

    async def _run(self) -> None:
        from bleak import BleakClient  # imported lazily so the simulator works without it

        while self._want:
            self.state = "connecting"
            try:
                async with BleakClient(self.address, timeout=20.0) as client:
                    self.client = client
                    await self.setup(client)
                    self.state = "connected"
                    self.error = ""
                    log.info("%s connected", self.name)
                    while self._want and client.is_connected:
                        await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.error = f"{type(e).__name__}: {e}"
                log.warning("%s: %s", self.name, self.error)
            finally:
                self.client = None
            if self._want:
                self.state = "reconnecting"
                await asyncio.sleep(3.0)
        self.state = "disconnected"

    async def setup(self, client) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    @staticmethod
    def _has(client, uuid: str) -> bool:
        return client.services.get_characteristic(uuid) is not None


# ======================================================================= sensors
class HeartRateSensor(BaseSensor):
    async def setup(self, client) -> None:
        def on_hr(_, data: bytearray):
            self.hub.report("hr", heart_rate=P.parse_heart_rate(bytes(data))["heart_rate"])

        await client.start_notify(P.HR_MEASUREMENT, on_hr)
        self.info = "Heart Rate service"


class CadenceSensor(BaseSensor):
    """Standalone cadence sensor (CSC) or a pedal/crank power meter (CPS)."""

    async def setup(self, client) -> None:
        crank = P.CrankCadence()
        if self._has(client, P.CSC_MEASUREMENT):
            def on_csc(_, data):
                d = P.parse_csc(bytes(data))
                if "crank_revs" in d:
                    self.hub.report("csc", cadence=crank.update(d["crank_revs"], d["crank_time"]))
            await client.start_notify(P.CSC_MEASUREMENT, on_csc)
            self.info = "Speed/Cadence sensor"
        elif self._has(client, P.CPS_MEASUREMENT):
            def on_cps(_, data):
                d = P.parse_cycling_power(bytes(data))
                if "crank_revs" in d:
                    self.hub.report("cad_cps", cadence=crank.update(d["crank_revs"], d["crank_time"]))
            await client.start_notify(P.CPS_MEASUREMENT, on_cps)
            self.info = "Power meter (cadence only)"
        else:
            raise RuntimeError("No cadence characteristic found")


class TrainerSensor(BaseSensor):
    """Smart trainer. KICKR v6 exposes FTMS; older Wahoo firmware only the
    proprietary control characteristic, which we fall back to."""

    def __init__(self, hub, address, name, weight_kg: float = 75.0):
        super().__init__(hub, address, name)
        self.weight_kg = weight_kg
        self.protocol = None  # "ftms" | "wahoo"
        self._cp_lock = asyncio.Lock()
        self._cp_future: asyncio.Future | None = None
        self._mode: tuple[str, float] | None = None  # last command, re-sent on reconnect
        self._wahoo_sim_ready = False

    async def setup(self, client) -> None:
        crank = P.CrankCadence()
        has_ftms_data = self._has(client, P.FTMS_INDOOR_BIKE_DATA)

        if has_ftms_data:
            def on_bike(_, data):
                d = P.parse_indoor_bike_data(bytes(data))
                self.hub.report("ftms", power=d.get("power"), cadence=d.get("cadence"),
                                speed_kmh=d.get("speed_kmh"), heart_rate=d.get("heart_rate") or None)
            await client.start_notify(P.FTMS_INDOOR_BIKE_DATA, on_bike)

        if self._has(client, P.CPS_MEASUREMENT):
            def on_cps(_, data):
                d = P.parse_cycling_power(bytes(data))
                vals = {"power": d["power"]}
                if "crank_revs" in d:
                    vals["cadence"] = crank.update(d["crank_revs"], d["crank_time"])
                self.hub.report("cps", **vals)
            await client.start_notify(P.CPS_MEASUREMENT, on_cps)

        loop = asyncio.get_running_loop()
        self._wahoo_sim_ready = False
        if self._has(client, P.FTMS_CONTROL_POINT):
            self.protocol = "ftms"

            def _resolve(fut, resp):
                if fut is not None and not fut.done():
                    fut.set_result(resp)

            def on_cp(_, data):
                resp = P.parse_ftms_response(bytes(data))
                if resp:
                    loop.call_soon_threadsafe(_resolve, self._cp_future, resp)
            await client.start_notify(P.FTMS_CONTROL_POINT, on_cp)
            res = await self._ftms_write(P.ftms_request_control())
            if res and res[1] != 1:
                raise RuntimeError("Trainer refused control (" + P.FTMS_RESULTS.get(res[1], "?") +
                                   "). Close Wahoo/Zwift/TrainerRoad apps that may hold it.")
            await self._ftms_write(P.ftms_start())
        elif self._has(client, P.WAHOO_TRAINER_CP):
            self.protocol = "wahoo"
            await client.start_notify(P.WAHOO_TRAINER_CP, lambda *_: None)
            await client.write_gatt_char(P.WAHOO_TRAINER_CP, P.WAHOO_UNLOCK, response=True)
        else:
            self.protocol = None
        self.info = {"ftms": "FTMS control", "wahoo": "Wahoo control",
                     None: "read-only (no control)"}[self.protocol]
        if self._mode:  # restore ERG/grade after a reconnect
            kind, value = self._mode
            self._mode = None
            asyncio.get_running_loop().call_later(
                0.5, lambda: asyncio.ensure_future(
                    self.set_erg(int(value)) if kind == "erg" else self.set_grade(value)))

    async def _ftms_write(self, payload: bytes):
        async with self._cp_lock:
            self._cp_future = asyncio.get_running_loop().create_future()
            await self.client.write_gatt_char(P.FTMS_CONTROL_POINT, payload, response=True)
            try:
                return await asyncio.wait_for(self._cp_future, 2.0)
            except asyncio.TimeoutError:
                return None

    async def set_erg(self, watts: int) -> None:
        if not self.client or self._mode == ("erg", watts):
            return
        try:
            if self.protocol == "ftms":
                await self._ftms_write(P.ftms_set_target_power(watts))
            elif self.protocol == "wahoo":
                await self.client.write_gatt_char(P.WAHOO_TRAINER_CP, P.wahoo_set_erg(watts), response=True)
            self._mode = ("erg", watts)
        except Exception as e:  # noqa: BLE001
            self.error = f"ERG command failed: {e}"

    async def set_grade(self, grade: float) -> None:
        if not self.client or self._mode == ("grade", grade):
            return
        try:
            if self.protocol == "ftms":
                await self._ftms_write(P.ftms_set_simulation(grade))
            elif self.protocol == "wahoo":
                if not self._wahoo_sim_ready:
                    await self.client.write_gatt_char(P.WAHOO_TRAINER_CP, P.wahoo_init_sim(self.weight_kg + 9),
                                                      response=True)
                    self._wahoo_sim_ready = True
                await self.client.write_gatt_char(P.WAHOO_TRAINER_CP, P.wahoo_set_grade(grade), response=True)
            self._mode = ("grade", grade)
        except Exception as e:  # noqa: BLE001
            self.error = f"Grade command failed: {e}"


SENSOR_CLASSES = {"trainer": TrainerSensor, "hr": HeartRateSensor, "cadence": CadenceSensor}


# ====================================================================== scanning
async def scan(timeout: float = 6.0) -> list[dict]:
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for addr, (dev, adv) in found.items():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        name = adv.local_name or dev.name or ""
        tags = []
        if P.FTMS_SERVICE in uuids:
            tags.append("trainer")
        if P.CPS_SERVICE in uuids:
            tags.append("power")
        if P.HR_SERVICE in uuids:
            tags.append("hr")
        if P.CSC_SERVICE in uuids:
            tags.append("cadence")
        if "kickr" in name.lower() and "trainer" not in tags:
            tags.append("trainer")
        if not tags and not name:
            continue
        guess = ("trainer" if "trainer" in tags else "hr" if "hr" in tags
                 else "cadence" if ("cadence" in tags or "power" in tags) else None)
        out.append({"address": addr, "name": name or addr, "rssi": adv.rssi,
                    "tags": tags, "suggested_role": guess})
    out.sort(key=lambda d: (d["suggested_role"] is None, -(d["rssi"] or -999)))
    return out


# ===================================================================== simulator
class SimTrainer(BaseSensor):
    """Fake trainer + HR so the whole app can be tried without hardware."""
    kind = "sim"

    def __init__(self, hub, ftp: int = 200):
        super().__init__(hub, "SIM-TRAINER", "Simulated trainer")
        self.ftp = ftp
        self.target = 0.6 * ftp
        self.grade = None
        self.info = "simulator"
        self._hr = 70.0

    async def _run(self) -> None:
        self.state = "connected"
        power = 0.0
        t0 = time.monotonic()
        while self._want:
            t = time.monotonic() - t0
            if self.grade is not None:
                goal = self.ftp * (0.65 + 0.06 * self.grade)
            else:
                goal = self.target
            power += (goal - power) * 0.35
            p = max(0.0, power + random.gauss(0, 4))
            cad = 88 + 4 * math.sin(t / 20) + random.gauss(0, 1)
            hr_goal = 60 + 0.45 * p
            self._hr += (hr_goal - self._hr) * 0.02
            self.hub.report("sim", power=round(p), cadence=round(cad))
            self.hub.report("sim_hr", heart_rate=round(self._hr))
            await asyncio.sleep(0.25)

    async def set_erg(self, watts: int) -> None:
        self.target, self.grade = watts, None

    async def set_grade(self, grade: float) -> None:
        self.grade = grade
