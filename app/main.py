"""HTTP/WebSocket server tying everything together. Run via run.py."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import config, sync
from . import workouts as W
from .ble import devices as D
from .session import RideSession, list_activities, load_activity, update_activity

log = logging.getLogger("app")

hub = D.DeviceHub()
session = RideSession(hub)
_sockets: set[WebSocket] = set()
_upload_jobs: dict[str, str] = {}  # "<activity>:<target>" -> status


# ============================================================== lifecycle
async def _ticker():
    loop = asyncio.get_running_loop()
    next_t = loop.time()
    while True:
        next_t += 1.0
        try:
            await session.tick()
        except Exception:  # noqa: BLE001
            log.exception("tick failed")
        await asyncio.sleep(max(0.0, next_t - loop.time()))


async def _broadcaster():
    while True:
        await asyncio.sleep(0.5)
        if not _sockets:
            continue
        msg = {"type": "state", "session": session.view(), "devices": hub.status()}
        for ws in list(_sockets):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                _sockets.discard(ws)


async def _autoconnect():
    s = config.load_settings()
    for role, dev in (s.get("devices") or {}).items():
        if role in D.SENSOR_CLASSES and dev.get("address"):
            await _connect(role, dev["address"], dev.get("name", ""), remember=False)


@contextlib.asynccontextmanager
async def lifespan(_app):
    config.ensure_dirs()
    W.install_samples()
    tasks = [asyncio.create_task(_ticker()), asyncio.create_task(_broadcaster())]
    if not config.load_settings().get("simulate"):
        asyncio.create_task(_autoconnect())
    else:
        await hub.attach("trainer", D.SimTrainer(hub, config.load_settings()["ftp"]))
    yield
    for t in tasks:
        t.cancel()
    await hub.detach_all()


app = FastAPI(title="CycleTrainer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _sockets.discard(ws)


# ============================================================== settings
@app.get("/api/settings")
async def get_settings():
    return {"settings": config.load_settings(), "garmin": sync.garmin.status(),
            "data_dir": str(config.DATA_DIR)}


@app.put("/api/settings")
async def put_settings(body: dict = Body(...)):
    allowed = {"ftp", "weight_kg", "auto_upload_garmin", "auto_pause"}
    changes = {k: v for k, v in body.items() if k in allowed}
    if "ftp" in changes:
        changes["ftp"] = max(50, min(600, int(changes["ftp"])))
    if "weight_kg" in changes:
        changes["weight_kg"] = max(30.0, min(200.0, float(changes["weight_kg"])))
    return config.update_settings(**changes)


# ============================================================== devices
async def _connect(role: str, address: str, name: str, remember: bool = True):
    cls = D.SENSOR_CLASSES[role]
    if role == "trainer":
        sensor = cls(hub, address, name, weight_kg=config.load_settings().get("weight_kg", 75))
    else:
        sensor = cls(hub, address, name)
    await hub.attach(role, sensor)
    if remember:
        s = config.load_settings()
        s.setdefault("devices", {})[role] = {"address": address, "name": name}
        config.save_settings(s)


@app.post("/api/devices/scan")
async def devices_scan():
    try:
        return {"devices": await D.scan(6.0)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Bluetooth scan failed: {e}. Is Bluetooth turned on?") from e


@app.post("/api/devices/connect")
async def devices_connect(body: dict = Body(...)):
    role = body.get("role")
    if role not in D.SENSOR_CLASSES:
        raise HTTPException(400, "role must be trainer, hr or cadence")
    if config.load_settings().get("simulate"):
        await _set_simulate(False)
    await _connect(role, body["address"], body.get("name", ""))
    return hub.status()


@app.post("/api/devices/disconnect")
async def devices_disconnect(body: dict = Body(...)):
    role = body.get("role")
    await hub.detach(role)
    if body.get("forget"):
        s = config.load_settings()
        s.get("devices", {}).pop(role, None)
        config.save_settings(s)
    return hub.status()


async def _set_simulate(on: bool):
    config.update_settings(simulate=on)
    await hub.detach_all()
    if on:
        await hub.attach("trainer", D.SimTrainer(hub, config.load_settings()["ftp"]))
    else:
        await _autoconnect()


@app.post("/api/devices/simulate")
async def devices_simulate(body: dict = Body(...)):
    await _set_simulate(bool(body.get("enabled")))
    return hub.status()


# ============================================================== workouts
@app.get("/api/workouts")
async def workouts_list():
    return W.list_workouts()


@app.get("/api/workouts/{wid}")
async def workouts_get(wid: str):
    try:
        w = W.load_workout(wid)
    except FileNotFoundError as e:
        raise HTTPException(404, "not found") from e
    return {**w.to_dict(), "duration": w.duration, **{"stats": w.stats()}}


@app.post("/api/workouts")
async def workouts_save(body: dict = Body(...)):
    w = W.Workout.from_dict(body)
    if not w.segments:
        raise HTTPException(400, "Add at least one step")
    w = W.save_workout(w)
    return w.to_dict()


@app.delete("/api/workouts/{wid}")
async def workouts_delete(wid: str):
    W.delete_workout(wid)
    return {"ok": True}


@app.post("/api/workouts/import")
async def workouts_import(file: UploadFile = File(...)):
    data = await file.read()
    try:
        w = await asyncio.to_thread(W.import_bytes, file.filename or "workout", data,
                                    config.load_settings()["ftp"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Could not import: {e}") from e
    w = W.save_workout(w)
    return w.to_dict()


@app.post("/api/activities/{aid}/as_workout")
async def activity_to_workout(aid: str):
    rec = load_activity(aid)
    data = (config.ACTIVITIES_DIR / rec["fit"]).read_bytes()
    w = await asyncio.to_thread(W.fit_to_workout, data, config.load_settings()["ftp"])
    w.name = f"{rec['name']} (template)"
    w.source = f"from ride {rec['start']}"
    return W.save_workout(w).to_dict()


@app.get("/api/workouts/{wid}/export")
async def workouts_export(wid: str, fmt: str = "zwo"):
    w = W.load_workout(wid)
    safe = "".join(c for c in w.name if c.isalnum() or c in " -_").strip() or "workout"
    if fmt == "zwo":
        body, media = W.to_zwo(w), "application/xml"
    elif fmt == "mrc":
        body, media = W.to_mrc(w), "text/plain"
    else:
        import json
        body, media, fmt = json.dumps(w.to_dict(), indent=2), "application/json", "json"
    return PlainTextResponse(body, media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="{safe}.{fmt}"'})


# ============================================================== ride session
@app.post("/api/session/load")
async def session_load(body: dict = Body(default={})):
    if session.state in ("running", "paused") and session.samples and not body.get("force"):
        raise HTTPException(409, "A ride is in progress. End or discard it first.")
    wid = body.get("workout_id")
    session.reset(W.load_workout(wid) if wid else None)
    return session.view()


@app.get("/api/session")
async def session_get(since: int = 0):
    v = session.view()
    w = session.workout
    v["profile"] = [W.asdict(s) for s in w.segments] if w else []
    v["trace"] = session.trace(since)
    return v


@app.post("/api/session/{action}")
async def session_action(action: str, body: dict = Body(default={})):
    if action == "start":
        session.start()
    elif action == "pause":
        session.pause()
    elif action == "skip":
        session.skip()
    elif action == "extend":
        session.extend(int(body.get("seconds", 60)))
    elif action == "intensity":
        session.adjust_intensity(float(body.get("delta", 0)))
    elif action == "erg":
        session.erg = bool(body.get("enabled"))
    elif action == "grade":
        session.grade = max(-10.0, min(20.0, float(body.get("grade", 0))))
    elif action == "watts":
        session.manual_watts = max(0, min(1500, int(body.get("watts", 150))))
    elif action == "summary":
        return session.summary()
    elif action == "discard":
        session.reset(session.workout)
    elif action == "save":
        session.pause()
        name = body.get("name") or (session.workout.name if session.workout else "Indoor ride")
        try:
            rec = await asyncio.to_thread(session.save, name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        session.state = "finished"
        if config.load_settings().get("auto_upload_garmin"):
            asyncio.create_task(_do_upload(rec["id"]))
        return rec
    else:
        raise HTTPException(404, "unknown action")
    return session.view()


# ============================================================== activities & sync
@app.get("/api/activities")
async def activities():
    out = list_activities()
    for a in out:
        job = _upload_jobs.get(a["id"])
        if job:
            a["job"] = job
    return out


@app.get("/api/activities/{aid}/fit")
async def activity_fit(aid: str):
    rec = load_activity(aid)
    return FileResponse(config.ACTIVITIES_DIR / rec["fit"], filename=rec["fit"],
                        media_type="application/octet-stream")


@app.delete("/api/activities/{aid}")
async def activity_delete(aid: str):
    rec = load_activity(aid)
    for f in (rec["fit"], f"{aid}.json"):
        p = config.ACTIVITIES_DIR / Path(f).name
        if p.exists():
            p.unlink()
    return {"ok": True}


async def _do_upload(aid: str):
    _upload_jobs[aid] = "uploading"
    try:
        rec = load_activity(aid)
        res = await asyncio.to_thread(sync.garmin.upload, config.ACTIVITIES_DIR / rec["fit"])
        rec = load_activity(aid)
        rec.setdefault("uploads", {})["garmin"] = res
        update_activity(rec)
        _upload_jobs.pop(aid, None)
    except Exception as e:  # noqa: BLE001
        log.warning("upload %s failed: %s", aid, e)
        _upload_jobs[aid] = f"error: {e}"


@app.post("/api/activities/{aid}/upload")
async def activity_upload(aid: str):
    asyncio.create_task(_do_upload(aid))
    return {"ok": True, "status": "uploading"}


# Garmin -------------------------------------------------------------------------
@app.post("/api/garmin/login")
async def garmin_login(body: dict = Body(...)):
    try:
        return await asyncio.to_thread(sync.garmin.login, body.get("email", ""), body.get("password", ""))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Garmin sign-in failed: {e}") from e


@app.post("/api/garmin/mfa")
async def garmin_mfa(body: dict = Body(...)):
    try:
        return await asyncio.to_thread(sync.garmin.submit_mfa, body.get("code", ""))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Garmin code rejected: {e}") from e


@app.post("/api/garmin/logout")
async def garmin_logout():
    sync.garmin.logout()
    return sync.garmin.status()
