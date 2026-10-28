"""FastAPI app: dashboard, websocket stream and REST API."""
import asyncio
import json
import time

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import ENVIRONMENTS, LABELS, MODELS_DIR, ORIENTATIONS, STATIC_DIR
from .hub import Hub
from .model import train as train_model

hub = Hub()
app = FastAPI(title="ExamGuard Rover")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
training = {"running": False, "log": [], "error": None}


@app.middleware("http")
async def no_cache_static(request, call_next):
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-cache"  # always pick up dashboard edits
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    q = asyncio.Queue()
    hub.clients.add(q)
    hub.loop = asyncio.get_running_loop()

    async def pump():
        while True:
            await sock.send_text(await q.get())

    task = asyncio.create_task(pump())
    try:
        await sock.send_text(json.dumps({"type": "hello", "state": hub.state()}, default=float))
        while True:
            msg = json.loads(await sock.receive_text())
            kind = msg.get("type")
            if kind == "drive":
                hub.set_drive(msg.get("cmd", "S"), msg.get("speed", 60))
            elif kind == "alert_ack":
                hub.alert_ack(msg.get("id"))
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
        pass
    finally:
        task.cancel()
        hub.clients.discard(q)


# -- state & settings --------------------------------------------------------
@app.get("/api/state")
def state():
    return hub.state() | {"labels": LABELS, "orientations": ORIENTATIONS, "environments": ENVIRONMENTS,
                          "training": training}


@app.post("/api/settings")
def settings(patch: dict = Body(...)):
    hub.update_settings(patch)
    return hub.settings


@app.post("/api/command")
def command(body: dict = Body(...)):
    cmd = str(body.get("cmd", "")).strip()
    if cmd not in ("C", "A 0", "A 1", "Z 0", "Z 1"):
        raise HTTPException(400, "unsupported command")
    hub.pending.append(cmd)
    return {"queued": cmd}


# -- calibration -------------------------------------------------------------
@app.post("/api/calibrate")
def calibrate(body: dict = Body(default={})):
    if not hub.connected:
        raise HTTPException(409, "car not connected")
    hub.start_calibration(float(body.get("seconds", 4)), float(body.get("k", 5)), body.get("note", ""))
    return {"started": True}


# -- BLE -----------------------------------------------------------------------
@app.post("/api/ble/ignore")
def ble_ignore(body: dict = Body(...)):
    mac = body["mac"]
    (hub.ignored_macs.add if body.get("ignore", True) else hub.ignored_macs.discard)(mac)
    hub.save_settings()
    return {"ignored": sorted(hub.ignored_macs)}


# -- localization --------------------------------------------------------------
@app.post("/api/pose")
def pose(body: dict = Body(...)):
    if body.get("reset_map"):
        hub.loc.reset(body.get("x", 0.0), body.get("y", 0.0), body.get("th", 0.0))
    else:
        hub.loc.set_pose(float(body["x"]), float(body["y"]), body.get("th"))
    return hub.loc.snapshot(hub.settings)


@app.get("/api/map")
def map_():
    return hub.loc.snapshot(hub.settings)


# -- alerts --------------------------------------------------------------------
@app.get("/api/alerts")
def alerts():
    return hub.detector.history[::-1]


@app.post("/api/alerts/{aid}/feedback")
def alert_feedback(aid: int, body: dict = Body(...)):
    try:
        return hub.alert_feedback(aid, body.get("verdict"))
    except KeyError:
        raise HTTPException(404, "no such alert")


@app.delete("/api/alerts")
def clear_alerts():
    hub.detector.history = [a for a in hub.detector.history if a is hub.detector.active]
    hub.save_alerts()
    return {"ok": True}


# -- dataset -------------------------------------------------------------------
@app.get("/api/dataset")
def dataset():
    d = hub.dataset
    return {"sessions": d.sessions[::-1], "stats": d.stats(), "active": d.active}


@app.post("/api/dataset/record/start")
def record_start(meta: dict = Body(...)):
    if not hub.connected:
        raise HTTPException(409, "car not connected")
    try:
        return hub.dataset.start(meta, hub.baseline())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/dataset/record/stop")
def record_stop():
    return hub.dataset.stop() or {}


@app.delete("/api/dataset/{sid}")
def dataset_delete(sid: str):
    if sid == "synthetic":
        hub.dataset.delete_synthetic()
    else:
        hub.dataset.delete(sid)
    return {"ok": True}


@app.post("/api/dataset/synthetic")
def dataset_synthetic(body: dict = Body(default={})):
    n = hub.dataset.generate_synthetic(int(body.get("sessions_per_class", 30)), int(body.get("frames", 60)))
    return {"created_sessions": n}


@app.get("/api/dataset/export.csv")
def dataset_export(include_synthetic: bool = True):
    return Response(hub.dataset.export_csv(include_synthetic), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=examguard_dataset.csv"})


# -- models --------------------------------------------------------------------
@app.get("/api/models")
def models():
    return {"models": hub.models.list(), "active": hub.models.meta, "training": training}


@app.get("/api/models/{version}")
def model_meta(version: str):
    try:
        return hub.models.get_meta(version)
    except FileNotFoundError:
        raise HTTPException(404, "no such model")


@app.post("/api/models/train")
async def models_train(body: dict = Body(default={})):
    if training["running"]:
        raise HTTPException(409, "training already running")
    loop = asyncio.get_running_loop()
    training.update(running=True, log=[], error=None)

    def progress(m):
        training["log"].append(m)
        loop.call_soon_threadsafe(hub.broadcast, {"type": "train_progress", "message": m})

    def job():
        X, y, g, sessions = hub.dataset.build_matrix(bool(body.get("include_synthetic", True)),
                                                     frozenset(hub.ignored_macs))
        progress(f"Built feature matrix: {len(X)} frames from {len(sessions)} sessions")
        return train_model(X, y, g, sessions, body.get("algorithms"), progress)

    try:
        meta = await loop.run_in_executor(None, job)
        if body.get("activate", True):
            hub.models.activate(meta["version"])
        hub.broadcast({"type": "train_done", "meta": meta})
        return meta
    except Exception as e:  # surface to the dashboard
        training["error"] = str(e)
        hub.broadcast({"type": "train_error", "message": str(e)})
        raise HTTPException(400, str(e))
    finally:
        training["running"] = False


@app.post("/api/models/{version}/activate")
def models_activate(version: str):
    try:
        hub.models.activate(version)
    except FileNotFoundError:
        raise HTTPException(404, "no such model")
    return hub.models.meta


@app.post("/api/models/deactivate")
def models_deactivate():
    hub.models.deactivate()
    return {"ok": True}


@app.get("/api/models/{version}/pico_model.py")
def models_pico(version: str):
    p = MODELS_DIR / version / "pico_model.py"
    if not p.exists():
        raise HTTPException(404, "no such model")
    return PlainTextResponse(p.read_text(), headers={"Content-Disposition": "attachment; filename=pico_model.py"})


# -- evaluation ----------------------------------------------------------------
@app.get("/api/evaluation")
def evaluation():
    return hub.evaluation_summary() | {"active": hub.trial}


@app.post("/api/evaluation/start")
def evaluation_start(meta: dict = Body(...)):
    return hub.start_trial(meta)


@app.post("/api/evaluation/stop")
def evaluation_stop():
    return hub.stop_trial() or {}


@app.delete("/api/evaluation/{tid}")
def evaluation_delete(tid: int):
    hub.delete_trial(tid)
    return {"ok": True}


@app.get("/api/evaluation/report.json")
def evaluation_report():
    rep = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "evaluation": hub.evaluation_summary(),
           "model": hub.models.meta, "calibrations": hub.calibrations, "dataset": hub.dataset.stats(),
           "settings": hub.settings}
    return Response(json.dumps(rep, indent=1, default=float), media_type="application/json",
                    headers={"Content-Disposition": "attachment; filename=examguard_report.json"})
