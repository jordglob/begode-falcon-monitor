"""Falcon dashboard: read-only BLE listener + web UI (Live / Energilager / Logg).

Run:  .venv/bin/python -m falcon.server   (port 8096, LAN)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse, StreamingResponse

from .ble import WheelLink
from .energy import energy_view
from .guard import Guard, summary
from .health import Health
from .fastpath import BusTracker, CellRegression
from . import __version__
from . import control as ctl
from .verify import Verifier
from .backup import Backups, RESTORABLE
from .gps import GpsReader
from . import rides as rides_mod
from .protocol import ascii_replies
from .protocol import FIELD_INFO, UNCERTAIN, decode_p0, decode_p1, decode_p4, decode_p7, Frame
import json
from .protocol import WheelState
from .store import Store

ADDRESS = os.environ.get("FALCON_ADDR") or None   # None = first wheel named GotWay*/Begode*
PORT = int(os.environ.get("FALCON_PORT", "8096"))
ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("FALCON_DB", Path.home() / ".local/share/begode-falcon/history.db"))
SAMPLE_EVERY_S = 5

state = WheelState()
bus = BusTracker()
cellreg = CellRegression()
events: deque = deque(maxlen=200)
subscribers: set = set()
_last_group_ts = 0.0


def _publish(msg: dict) -> None:
    data = json.dumps(msg)
    for q in list(subscribers):
        if q.qsize() < 50:
            q.put_nowait(data)


def _on_frame(f) -> None:
    """Called for every decoded frame (state already updated)."""
    global _last_group_ts
    ts = time.time()
    if f.type == 1:
        _last_group_ts = ts
    elif f.type == 0:
        _publish({"bus": bus.on_p0(state.p0, state.p7, ts, store.baseline_sag_ohm())})
    elif f.type in (2, 3):
        cellreg.on_bank(ts, "A" if f.type == 2 else "B", f.sub, list(f.u16()),
                        bus.current(state.p7))


link = WheelLink(ADDRESS, state, on_frame=_on_frame)
store = Store(DB)
gate = ctl.ControlGate()          # OFF at every start
GPS_ON = os.environ.get("FALCON_GPS", "auto") != "0"
gps = GpsReader()
_last_gps_store = 0.0


def _on_gps_fix(s: dict) -> None:
    """Store a GPS point every 5 s together with the wheel's own speed (future: rides on a map)."""
    global _last_gps_store
    now = time.time()
    if now - _last_gps_store >= 5:
        _last_gps_store = now
        fresh = link.connected and time.time() - link.last_frame_ts < 3
        store.add_gps(s, (state.p0 or {}).get("speed_kmh") if fresh else None)


gps.on_fix = _on_gps_fix
backups = Backups(Path(os.environ.get("FALCON_BACKUPS",
                                      Path.home() / ".local/share/begode-falcon/backups")))
guard = Guard(store.get_json("guard_baseline"))
NOMINAL_WH = float(os.environ.get("FALCON_NOMINAL_WH", "1800"))  # Falcon Pro label: 1.8 kWh
health = Health(store.db, NOMINAL_WH)


def log_event(msg: str) -> None:
    events.appendleft({"ts": time.time(), "msg": msg})


link.on_event = log_event
_wf = store.get_json("wheel_firmware")
if _wf:
    link.device_info["wheel_firmware"] = _wf["value"]


async def sampler() -> None:
    last_status = None
    while True:
        await asyncio.sleep(SAMPLE_EVERY_S)
        if link.status != last_status:
            log_event(f"Bluetooth: {link.status}")
            last_status = link.status
        if link.connected and state.groups:
            store.add(energy_view(state.snapshot(), bus.sag.estimate(), link.info(), store.baseline_sag_ohm()), state.p0, state.p4)


def _auto_backup() -> None:
    """Daily snapshot, plus one whenever a setting differs from the latest backup
    (e.g. changed in the phone app)."""
    if not state.p4 or len(state.groups) < 4:
        return
    try:
        items = backups.list()
        reason = None
        if not items or backups.latest_age_s() > 86400:
            reason = "automatisk (dygnsvis)"
        else:
            ch = Backups.changes_vs_now(backups.load(items[0]["name"]), state.snapshot())
            if ch:
                reason = "automatisk (inställning ändrad: " + ", ".join(ch) + ")"
        if reason:
            b = backups.make(state.snapshot(), state.raw, link.device_info, __version__, reason)
            log_event(f"💾 Inställningsbackup {b['name']} – {reason}")
    except Exception as e:
        log_event(f"Backup: fel {e}")


async def guard_loop() -> None:
    """1 Hz: run the imbalance guard, log every change of finding, persist baseline."""
    active: dict = {}
    n = 0
    while True:
        await asyncio.sleep(1)
        fresh = link.connected and state.groups and time.time() - link.last_frame_ts < 5
        try:
            health.tick(time.time(), state.snapshot() if fresh else None, bool(fresh))
        except Exception as e:  # health must never stop the guard
            log_event(f"Batterihälsa: fel {e}")
        if not fresh:
            continue
        found = {(f.code, f.where): f for f in guard.update(state.snapshot())
                 if f.level in ("warn", "alarm")}
        for key, f in found.items():
            if key not in active or active[key] != f.level:
                log_event(f"{'⛔' if f.level == 'alarm' else '⚠️'} {f.text}")
                store.add_guard_event(f.level, f.code, f.where, f.text)
        for key in set(active) - set(found):
            log_event(f"✅ Upphört: {key[0]} {key[1]}")
        active = {k: f.level for k, f in found.items()}
        n += 1
        if n % 60 == 0:
            store.set_json("guard_baseline", guard.export_baseline())
            health.save()
            _auto_backup()


@asynccontextmanager
async def lifespan(_app):
    ble_on = os.environ.get("FALCON_BLE", "1") != "0"
    tasks = [*([asyncio.create_task(link.run())] if ble_on else []), asyncio.create_task(sampler()),
             *([asyncio.create_task(gps.run())] if GPS_ON else []),
             asyncio.create_task(guard_loop())]
    log_event("Tjänsten startad (endast läsning, inga kommandon skickas)")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/api/state")
def api_state():
    return {"link": link.info(), "snapshot": state.snapshot(), "read_only": True}


@app.get("/api/energy")
def api_energy():
    return energy_view(state.snapshot(), bus.sag.estimate(), link.info(), store.baseline_sag_ohm())


@app.get("/api/sag_points")
def api_sag_points():
    return {"points": [[i, v] for _, i, v in bus.sag.samples], "fit": bus.sag.estimate()}


@app.get("/api/bus")
def api_bus():
    return bus.last


@app.get("/api/cells_fast")
def api_cells_fast():
    return cellreg.report()


@app.get("/api/stream")
async def api_stream():
    """Server-sent events: one message per p0 packet (every ~0.3 s)."""
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)

    async def gen():
        try:
            while True:
                yield f"data: {await q.get()}\n\n"
        finally:
            subscribers.discard(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/api/guard")
def api_guard():
    return {**summary(guard.findings), "learned": guard.learned,
            "baseline": guard.export_baseline(), "events": store.guard_events(50)}


@app.get("/api/health")
def api_health():
    return health.report()


@app.get("/api/version")
def api_version():
    di = link.device_info
    return {"app": __version__,
            "ble_module": {"firmware": di.get("firmware"), "hardware": di.get("hardware"),
                           "manufacturer": di.get("manufacturer")},
            "wheel_firmware": di.get("wheel_firmware"),
            "wheel_firmware_note": "läses med \"Läs hjulets firmware (V)\" under Styrning"}


_DECODERS = {0: decode_p0, 1: decode_p1, 4: decode_p4, 7: decode_p7}


@app.get("/api/all")
def api_all():
    """Every decoded field of every packet, with metadata, raw words and update rate."""
    now = time.time()
    packets = []
    for key in sorted(state.raw, key=lambda k: tuple(int(x) for x in k.split("."))):
        r = state.raw[key]
        t, sub = (int(x) for x in key.split("."))
        span = max(r["ts"] - r["first_ts"], 1e-6)
        period = span / (r["count"] - 1) if r["count"] > 1 else None
        fields = []
        dec = _DECODERS.get(t)
        if dec:
            d = dec(Frame(t, sub, bytes.fromhex(r["hex"])))
            for name, val in d.items():
                desc, unit, status = FIELD_INFO.get(f"p{t}.{name}", (name, "", "unknown"))
                fields.append({"field": name, "value": val, "desc": desc, "unit": unit,
                               "status": status, "note": UNCERTAIN.get(f"p{t}.{name}")})
        elif t in (2, 3):
            for j, mv in enumerate(r["u16"]):
                fields.append({"field": f"{'A' if t == 2 else 'B'}{sub * 8 + j + 1}", "value": mv,
                               "desc": f"Cellspänning sträng {'A' if t == 2 else 'B'}", "unit": "mV",
                               "status": "ok", "note": None})
        packets.append({"packet": t, "sub": sub, "age_s": round(now - r["ts"], 2),
                        "count": r["count"], "period_s": round(period, 2) if period else None,
                        "raw_hex": r["hex"], "u16": r["u16"], "s16": r["s16"], "fields": fields})
    return {"packets": packets, "link": link.info(), "version": __version__}


def _host(req: Request) -> str:
    return req.client.host if req.client else ""


def _err(e: Exception, code: int = 403):
    return JSONResponse({"ok": False, "error": str(e)}, status_code=code)


@app.get("/api/control")
def api_control(req: Request):
    snap = state.snapshot()
    settings = []
    for sid, s in ctl.SETTINGS.items():
        cur = s["read"](state)[1] if s.get("read") else None
        settings.append({"id": sid, "label": s["label"], "kind": s["kind"],
                         "min": s.get("min"), "max": s.get("max"), "step": s.get("step", 1),
                         "choices": s.get("choices"), "verifiable": s.get("read") is not None,
                         "current": cur})
    age = time.time() - link.last_frame_ts if link.last_frame_ts else None
    return {**gate.status(_host(req)), "connected": link.connected,
            "motion_block": ctl.motion_block(snap, age), "settings": settings,
            "log": store.control_log(30)}


@app.post("/api/control/enable")
def api_control_enable(req: Request):
    try:
        return {"ok": True, "token": gate.request_enable(_host(req)), "window_s": ctl.CONFIRM_WINDOW_S}
    except PermissionError as e:
        return _err(e)


@app.post("/api/control/confirm")
async def api_control_confirm(req: Request):
    body = await req.json()
    try:
        gate.confirm_enable(_host(req), body.get("token", ""))
    except PermissionError as e:
        return _err(e)
    log_event("⚠️ STYRNING PÅSLAGEN (från " + _host(req) + ")")
    return {"ok": True}


@app.post("/api/control/disable")
def api_control_disable(req: Request):
    gate.disable("avstängd av användaren")
    log_event("Styrning avstängd")
    return {"ok": True}


_send_lock = asyncio.Lock()


class Busy(Exception):
    pass


async def _send(host: str, setting: str, value) -> dict:
    """Gate -> whitelist -> motion check -> write twice / read once -> log. Raises
    PermissionError / ValueError / Busy."""
    gate.check_use(host)
    cmd = ctl.build(setting, value)
    if _send_lock.locked():
        raise Busy("ett kommando pågår redan")
    async with _send_lock:
        spec = ctl.SETTINGS[setting]

        def moving():
            age = time.time() - link.last_frame_ts if link.last_frame_ts else None
            return ctl.motion_block(state.snapshot(), age) is not None

        reader = spec["read"] or (lambda st: (st.counts.get(4, 0), None))
        expected = spec["expected"](value) if spec.get("expected") else None
        before = ctl.flat(state.snapshot())
        t0 = time.time()
        err = None
        try:
            res = await Verifier(link.write, lambda: reader(state), moving).apply(cmd, expected)
        except Exception as e:
            res, err = None, str(e)
        await asyncio.sleep(1.5)          # let every packet row refresh before the diff
        after = ctl.flat(state.snapshot())
        reply = ascii_replies(link.notifications_since(t0)) if spec.get("ascii_reply") else None
        row = {"ts": t0, "host": host, "setting": setting, "value": str(value),
               "payload": cmd.payload.decode(errors="replace"),
               "status": res.status if res else "fel", "sends": res.sends if res else 0,
               "before": str(res.before) if res else None, "after": str(res.after) if res else None,
               "detail": res.detail if res else err, "changes": ctl.diff(before, after), "reply": reply}
        store.add_control(row)
        icon = {"verified": "✅", "mismatch": "❌", "unverifiable": "⚠️", "refused": "⛔"}.get(row["status"], "❓")
        log_event(f"{icon} Kommando {spec['label']} = {value} ({row['payload']}): {row['status']}"
                  + (f" – {row['detail']}" if row["detail"] else ""))
        if setting == "req_version" and reply:
            link.device_info["wheel_firmware"] = " / ".join(reply)
            store.set_json("wheel_firmware", {"value": link.device_info["wheel_firmware"], "ts": t0})
        return row


@app.post("/api/control/send")
async def api_control_send(req: Request):
    body = await req.json()
    try:
        row = await _send(_host(req), body.get("setting"), body.get("value"))
    except PermissionError as e:
        return _err(e)
    except (ValueError, TypeError) as e:
        return _err(e, 400)
    except Busy as e:
        return _err(e, 409)
    return {"ok": True, **row}


@app.post("/api/backup")
def api_backup_make():
    if not state.p4:
        return _err(Exception("inga data från hjulet ännu"), 409)
    b = backups.make(state.snapshot(), state.raw, link.device_info, __version__, "manuell")
    log_event(f"💾 Inställningsbackup {b['name']} – manuell")
    return {"ok": True, "name": b["name"]}


@app.get("/api/backup")
def api_backup_list():
    snap = state.snapshot()
    items = backups.list()
    for it in items[:20]:
        try:
            it["differs_now"] = Backups.changes_vs_now(backups.load(it["name"]), snap) if snap.get("p4") else None
        except Exception:
            it["differs_now"] = None
    return {"items": items, "restorable_fields": RESTORABLE, "folder": str(backups.folder)}


@app.get("/api/backup/file/{name}")
def api_backup_file(name: str):
    try:
        backups.load(name)
    except ValueError as e:
        return _err(e, 404)
    return FileResponse(backups.folder / name, media_type="application/json", filename=name)


@app.post("/api/backup/restore")
async def api_backup_restore(req: Request):
    body = await req.json()
    try:
        doc = backups.load(body.get("name", ""))
        gate.check_use(_host(req))
    except ValueError as e:
        return _err(e, 404)
    except PermissionError as e:
        return _err(e)
    results = []
    for field, value in (doc.get("restorable") or {}).items():
        current = (state.p4 or {}).get(field.split(".", 1)[1])
        if current == value:
            results.append({"field": field, "status": "redan rätt", "value": value})
            continue
        try:
            row = await _send(_host(req), RESTORABLE[field], value)
            results.append({"field": field, "status": row["status"], "value": value})
        except Exception as e:
            results.append({"field": field, "status": "fel", "detail": str(e), "value": value})
    log_event(f"♻️ Återställning från {body.get('name')}: " +
              ", ".join(f"{r['field']}={r['value']} {r['status']}" for r in results))
    return {"ok": True, "results": results}


@app.get("/api/gps")
def api_gps():
    if not GPS_ON:
        return {"status": "avstängd (FALCON_GPS=0)", "fix": False, "state": {}, "satellites": [],
                "stats": {}, "stored_points": 0}
    return {**gps.report(), "stored_points": store.gps_count()}


@app.get("/api/rides")
def api_rides(days: float = 90):
    pts = rides_mod.load_points(store.db, time.time() - days * 86400)
    return {"rides": [rides_mod.summary(r) for r in reversed(rides_mod.segment(pts))]}


@app.get("/api/rides/{ride_id}")
def api_ride(ride_id: int):
    pts = rides_mod.load_points(store.db, ride_id - 1)
    for r in rides_mod.segment(pts):
        if int(r[0]["ts"]) == ride_id:
            return {**rides_mod.summary(r), "track": rides_mod.track(r)}
    return _err(Exception("turen finns inte"), 404)


@app.get("/api/history")
def api_history(hours: float = 24):
    return store.history(hours)


@app.get("/api/log")
def api_log():
    return list(events)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # open SSE streams would otherwise block a Ctrl+C shutdown forever
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", timeout_graceful_shutdown=2)
