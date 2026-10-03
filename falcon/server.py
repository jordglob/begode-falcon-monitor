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
from fastapi.staticfiles import StaticFiles

from .ble import WheelLink
from .energy import energy_view
from .guard import Guard, summary
from .health import Health
from .fastpath import BusTracker, CellRegression, EnergyCounter
from . import settings as settings_mod
from . import rideanalysis
from .elevation import Dem
from .beeps import BeepWatch
from . import tripmax
from . import __version__
from . import control as ctl
from .verify import Verifier
from .backup import Backups, RESTORABLE
from .gps import GpsReader
from . import rides as rides_mod
from . import export as export_mod
from .alarms import RideAlarms
from .charging import ChargeController, Plug
from .health import wheel_percent
from fastapi.responses import PlainTextResponse
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
energy = EnergyCounter()
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
    beepwatch.frame(ts, f.type, f.sub, f.payload.hex())
    if f.type == 0:
        try:
            if beepwatch.check(ts, state.snapshot(), state.battery_current()):
                _publish({"beeps": beepwatch.report()["active"]})
        except Exception as e:
            log_event(f"Pip-vakt: fel {e}")
    if f.type == 0:
        _publish({"bus": bus.on_p0(state.p0, state.p7, ts, store.baseline_sag_ohm())})
        v = bus.bus_v(state.p0)
        energy.add(ts, v, (state.p7 or {}).get("battery_current_a"), (state.p7 or {}).get("pwm_pct"))
    elif f.type in (2, 3):
        cellreg.on_bank(ts, "A" if f.type == 2 else "B", f.sub, list(f.u16()),
                        bus.current(state.p7))


link = WheelLink(ADDRESS, state, on_frame=_on_frame)
store = Store(DB)
gate = ctl.ControlGate()          # OFF at every start
beepwatch = BeepWatch(Path(os.environ.get("FALCON_BLACKBOX", Path.home() / ".local/share/begode-falcon/blackbox")),
                      on_event=lambda m: log_event(m), on_record=lambda e: store.add_beep_event(e))
SETTINGS = {**settings_mod.defaults(), **(store.get_json("settings") or {})}
dem = Dem(SETTINGS.get("dem_dir"))
GPS_ON = os.environ.get("FALCON_GPS", "auto") != "0"
gps = GpsReader()
_last_gps_store = 0.0


def _on_gps_fix(s: dict) -> None:
    """Store a GPS point (every 2 s while moving, 5 s standing – see settings) with the wheel's
    speed and the packet-rate energy counters, so energy between any two points is exact."""
    global _last_gps_store
    now = time.time()
    fresh = link.connected and now - link.last_frame_ts < 3
    wheel = (state.p0 or {}).get("speed_kmh") if fresh else None
    moving = max(wheel or 0, s.get("speed_kmh") or 0) > 2.0
    cfg = SETTINGS
    every = cfg["gps_ride_interval_s"] if moving else cfg["gps_idle_interval_s"]
    if now - _last_gps_store >= every:
        _last_gps_store = now
        store.add_gps(s, wheel, energy.take_interval(now) if fresh else None)


gps.on_fix = _on_gps_fix
alarms = RideAlarms(store.get_json("alarm_config"))
charger = ChargeController(Plug(os.environ.get("FALCON_PLUG_URL"), os.environ.get("FALCON_PLUG_TYPE", "shelly2")),
                           store.get_json("charge_config"), on_event=lambda m: log_event(m))
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
            snap_now = state.snapshot()
            charger.tick(snap_now, bool(fresh))
            if fresh:
                before = {(a.code, a.level) for a in alarms.active}
                found = alarms.update(snap_now, wheel_percent((state.p0 or {}).get("voltage_raw")))
                for a in found:
                    if (a.code, a.level) not in before:
                        log_event(f"{'⛔' if a.level == 'alarm' else '⚠️'} {a.text}")
                if found:
                    _publish({"alarms": [a.__dict__ for a in found]})
        except Exception as e:
            log_event(f"Larm/laddning: fel {e}")
        try:
            health.tick(time.time(), state.snapshot() if fresh else None, bool(fresh))
        except Exception as e:  # health must never stop the guard
            log_event(f"Batterihälsa: fel {e}")
        try:
            _check_watches()
        except Exception as e:
            log_event(f"Bevakning: fel {e}")
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
app.mount("/vendor", StaticFiles(directory=ROOT / "web" / "vendor"), name="vendor")


@app.get("/")
def index():
    # never cache the page itself: after an update the browser must load the new version
    return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-store"})


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
        if s.get("hidden"):
            continue
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
DIFF_WINDOW_S = 5.0          # how long field changes are collected after a command
SETTLE_S = 5.0               # how long the wheel gets to show a new value
_watches: list = []          # settings watched for late changes after a command


def _check_watches() -> None:
    """A setting that changes AFTER its command was judged (the wheel can be slow, or a
    value can be changed elsewhere) is logged – nothing may change unnoticed."""
    now = time.time()
    for w in list(_watches):
        cur = w["reader"](state)[1]
        if cur != w["value"]:
            ok = cur == w["expected"]
            row = {"ts": now, "host": "server", "setting": w["setting"], "value": str(cur), "payload": "",
                   "status": "late_verified" if ok else "late_change", "sends": 0,
                   "before": str(w["value"]), "after": str(cur),
                   "detail": ("hjulet visade det beställda värdet först nu" if ok else
                              f"värdet ändrades till {cur} (beställt {w['expected']})"),
                   "changes": {}, "reply": None}
            store.add_control(row)
            log_event(f"{'✅' if ok else '⚠️'} {w['label']}: hjulet visar nu {cur} (sen ändring, beställt {w['expected']})")
            w["value"] = cur
        if now > w["until"]:
            _watches.remove(w)


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
            return ctl.motion_block(state.snapshot(), age)          # reason text or None

        reader = spec["read"] or (lambda st: (st.counts.get(4, 0), None))
        expected = spec["expected"](value) if spec.get("expected") else None
        before = ctl.flat(state.snapshot())
        t0 = time.time()
        err = None
        try:
            res = await Verifier(link.write, lambda: reader(state), moving,
                                 settle_timeout=SETTLE_S).apply(cmd, expected)
        except Exception as e:
            res, err = None, str(e)
        # collect every field that changes during the next 5 s (the wheel can be slow to
        # report a new value) – also maps settings that cannot be read back directly
        changes: dict = {}
        steps = max(1, int(DIFF_WINDOW_S / 0.5))
        for _ in range(steps):
            await asyncio.sleep(DIFF_WINDOW_S / steps)
            for k, v in ctl.diff(before, ctl.flat(state.snapshot())).items():
                changes[k] = v
        after = ctl.flat(state.snapshot())
        reply = ascii_replies(link.notifications_since(t0)) if spec.get("ascii_reply") else None
        row = {"ts": t0, "host": host, "setting": setting, "value": str(value),
               "payload": cmd.payload.decode(errors="replace"),
               "status": res.status if res else "fel", "sends": res.sends if res else 0,
               "before": str(res.before) if res else None, "after": str(res.after) if res else None,
               "detail": res.detail if res else err,
               "changes": {k: [before.get(k), after.get(k)] for k in changes}, "reply": reply}
        store.add_control(row)
        if spec.get("read") and res and res.status in ("mismatch", "unknown", "verified"):
            _watches.append({"setting": setting, "label": spec["label"], "reader": spec["read"],
                             "value": reader(state)[1], "expected": expected, "until": time.time() + 60})
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
    rep = gps.report()
    fresh = link.connected and time.time() - link.last_frame_ts < 3
    w = (state.p0 or {}).get("speed_kmh") if fresh else None
    st = rep.get("state") or {}
    live = {"wheel_speed_kmh": w, "speed_kmh": st.get("speed_kmh"), "sats": st.get("sats_used"),
            "hdop": st.get("hdop")}
    return {**rep, "stored_points": store.gps_count(),
            "plausibility_now": {**live, "flag": rides_mod.plausibility(live) if rep.get("fix") else None}}


@app.get("/api/rides")
def api_rides(days: float = 90):
    pts = rides_mod.load_points(store.db, time.time() - days * 86400)
    return {"rides": [rides_mod.summary(r) for r in reversed(rides_mod.segment(pts))]}


@app.get("/api/rides/{ride_id:int}")
def api_ride(ride_id: int):
    pts = rides_mod.load_points(store.db, ride_id - 1)
    for r in rides_mod.segment(pts):
        if int(r[0]["ts"]) == ride_id:
            return {**rides_mod.summary(r), "track": rides_mod.track(r)}
    return _err(Exception("turen finns inte"), 404)


@app.post("/api/link/release")
async def api_link_release(req: Request):
    """Free the wheel for the phone app (allowed from any device – it only disconnects)."""
    body = await req.json() if (await req.body()) else {}
    minutes = max(1, min(int(body.get("minutes", 15)), 240))
    link.release(minutes * 60)
    return {"ok": True, "minutes": minutes}


@app.post("/api/link/resume")
def api_link_resume():
    link.resume()
    return {"ok": True}


@app.get("/api/alarms")
def api_alarms():
    return alarms.report()


@app.post("/api/alarms/config")
async def api_alarms_config(req: Request):
    if not gate.is_local(_host(req)):
        return _err(Exception("larmgränser ändras bara från datorn som kör servern"))
    body = await req.json()
    for k, v in body.items():
        if k in alarms.cfg:
            alarms.cfg[k] = None if v in (None, "") else float(v)
    store.set_json("alarm_config", alarms.cfg)
    return {"ok": True, "config": alarms.cfg}


@app.get("/api/charge")
def api_charge():
    return {**charger.report(), "plug_on": charger.plug.state()}


@app.post("/api/charge/config")
async def api_charge_config(req: Request):
    if not gate.is_local(_host(req)):
        return _err(Exception("laddningsstyrning ändras bara från datorn som kör servern"))
    body = await req.json()
    if "enabled" in body:
        charger.enabled = bool(body["enabled"])
    if "target_cell_v" in body:
        v = float(body["target_cell_v"])
        if not 3.80 <= v <= 4.20:
            return _err(Exception("gränsen måste vara 3,80–4,20 V per cell"), 400)
        charger.target_cell_v = v
    store.set_json("charge_config", charger.config())
    log_event(f"🔌 Laddningsgräns: {'på' if charger.enabled else 'av'}, {charger.target_cell_v:.2f} V/cell")
    return {"ok": True, **charger.config()}


@app.post("/api/charge/plug")
async def api_charge_plug(req: Request):
    body = await req.json()
    on = bool(body.get("on"))
    if on and not gate.is_local(_host(req)):
        return _err(Exception("kontakten slås på bara från datorn som kör servern"))
    try:
        charger.plug.set(on)
    except Exception as e:
        return _err(e, 409)
    log_event(f"🔌 Kontakten {'på' if on else 'av'} (manuellt)")
    return {"ok": True}


@app.get("/api/rides/{ride_id}.gpx")
def api_ride_gpx(ride_id: int):
    r = api_ride(ride_id)
    if not isinstance(r, dict):
        return r
    name = "falcon-" + time.strftime("%Y%m%d-%H%M", time.localtime(r["start"]))
    return PlainTextResponse(export_mod.ride_gpx(r, r["track"], name), media_type="application/gpx+xml",
                             headers={"Content-Disposition": f'attachment; filename="{name}.gpx"'})


@app.get("/api/rides/{ride_id}.csv")
def api_ride_csv(ride_id: int):
    r = api_ride(ride_id)
    if not isinstance(r, dict):
        return r
    name = "falcon-" + time.strftime("%Y%m%d-%H%M", time.localtime(r["start"]))
    return PlainTextResponse(export_mod.ride_csv(r["track"]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


@app.get("/api/export/samples.csv")
def api_export_samples(hours: float = 24):
    rows = store.history(hours, limit=200000)
    return PlainTextResponse(export_mod.samples_csv(rows), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="falcon-telemetri.csv"'})


@app.get("/api/settings")
def api_settings(req: Request):
    return {"values": SETTINGS, "fields": settings_mod.describe(),
            "total_mass_kg": settings_mod.total_mass(SETTINGS), "local_client": gate.is_local(_host(req))}


@app.post("/api/settings")
async def api_settings_save(req: Request):
    if not gate.is_local(_host(req)):
        return _err(Exception("inställningar ändras bara från datorn som kör servern"))
    try:
        upd = settings_mod.validate(await req.json())
    except (ValueError, TypeError) as e:
        return _err(e, 400)
    SETTINGS.update(upd)
    store.set_json("settings", SETTINGS)
    global dem
    if "dem_dir" in upd:
        dem = Dem(SETTINGS.get("dem_dir"))
    log_event("⚙️ Inställningar sparade: " + ", ".join(f"{k}={v}" for k, v in upd.items()))
    return {"ok": True, "values": SETTINGS, "total_mass_kg": settings_mod.total_mass(SETTINGS)}


def _analysis_key() -> str:
    return "|".join(str(x) for x in (SETTINGS.get("elevation_source"), SETTINGS.get("climb_threshold_dem_m"),
                                      SETTINGS.get("climb_threshold_gps_m"), settings_mod.total_mass(SETTINGS),
                                      len(dem.tiles)))


def _ride_points(ride_id: int):
    pts = rides_mod.load_points(store.db, ride_id - 1)
    for r in rides_mod.segment(pts):
        if int(r[0]["ts"]) == ride_id:
            return r
    return None


def _analysis(ride_id: int, use_cache: bool = True):
    r = _ride_points(ride_id)
    if r is None:
        return None
    # the key includes the ride's length and end: a ride analysed while it was still going on
    # must be recomputed when it has grown (seen live: a 0.6 km ride showed 0.15 km)
    key = _analysis_key() + f"|{len(r)}|{int(r[-1]['ts'])}"
    if use_cache:
        row = store.db.execute("SELECT result FROM ride_analysis WHERE ride_id=? AND version=? AND source=?",
                               (ride_id, rideanalysis.ANALYSIS_VERSION, key)).fetchone()
        if row:
            return json.loads(row[0])
    res = rideanalysis.analyze(r, SETTINGS, dem, settings_mod.total_mass(SETTINGS))
    store.db.execute("INSERT OR REPLACE INTO ride_analysis VALUES (?,?,?,?,?)",
                     (ride_id, rideanalysis.ANALYSIS_VERSION, key, time.time(), json.dumps(res)))
    store.db.commit()
    return res


@app.get("/api/rides/{ride_id:int}/analysis")
def api_ride_analysis(ride_id: int, fresh: bool = False):
    res = _analysis(ride_id, use_cache=not fresh)
    if res is None:
        return _err(Exception("turen finns inte"), 404)
    return res


LIVE_GAP_S = 60


@app.get("/api/rides/live")
def api_ride_live():
    """The ride going on right now (last GPS point < 60 s old), with running totals and the
    max values so far – for the live view."""
    pts = rides_mod.load_points(store.db, time.time() - 6 * 3600)
    segs = rides_mod.segment(pts)
    if not pts or not segs or time.time() - pts[-1]["ts"] > LIVE_GAP_S:
        return {"live": False}
    r = segs[-1]
    if time.time() - r[-1]["ts"] > LIVE_GAP_S:
        return {"live": False}
    # include the points after the segment's trimmed end (standing still now)
    r = [p for p in pts if p["ts"] >= r[0]["ts"]]
    summ = rides_mod.summary(r)
    a = rideanalysis.analyze(r, SETTINGS, dem, settings_mod.total_mass(SETTINGS))
    mx = tripmax.compute(r, store.samples_between(r[0]["ts"], r[-1]["ts"]), a,
                         store.beep_events_between(r[0]["ts"], r[-1]["ts"]))
    tail = rides_mod.track(r)[-900:]
    return {"live": True, "summary": summ, "totals": a.get("totals") if a.get("ok") else None,
            "max": mx["items"], "alarms": mx["alarms"]["count"], "track": tail,
            "now": {"speed_kmh": (state.p0 or {}).get("speed_kmh"), "margin": alarms.report().get("safety_margin_pct"),
                    "power_w": round(energy_power_now()) if energy_power_now() is not None else None}}


def energy_power_now():
    v = bus.bus_v(state.p0 or {})
    i = (state.p7 or {}).get("battery_current_a")
    return v * i if v is not None and i is not None else None


@app.get("/api/rides/{ride_id:int}/max")
def api_ride_max(ride_id: int):
    r = _ride_points(ride_id)
    if r is None:
        return _err(Exception("turen finns inte"), 404)
    t0, t1 = r[0]["ts"], r[-1]["ts"]
    return tripmax.compute(r, store.samples_between(t0, t1), _analysis(ride_id),
                           store.beep_events_between(t0, t1))


@app.get("/api/analysis/grade-energy")
def api_grade_energy(days: float = 365):
    pts = rides_mod.load_points(store.db, time.time() - days * 86400)
    analyses = [a for a in (_analysis(int(r[0]["ts"])) for r in rides_mod.segment(pts)) if a and a.get("ok")]
    return {"rides": len(analyses), "bins": rideanalysis.grade_energy_bins(analyses)}


@app.get("/api/dem")
def api_dem():
    return dem.describe()


@app.get("/api/beeps")
def api_beeps():
    return {**beepwatch.report(), "connected": link.connected}


@app.post("/api/beeps/mark")
async def api_beeps_mark(req: Request):
    """'Jag hör pip nu!' – allowed from any device (it only records)."""
    body = await req.json() if (await req.body()) else {}
    m = beepwatch.mark(time.time(), str(body.get("note", ""))[:200])
    return {"ok": True, **m}


@app.get("/api/beeps/box/{name}")
def api_beeps_box(name: str):
    import re as _re
    if not _re.match(r"^blackbox-\d{8}-\d{6}\.json$", name):
        return _err(Exception("ogiltigt namn"), 404)
    p = beepwatch.folder / name
    if not p.exists():
        return _err(Exception("finns inte"), 404)
    return FileResponse(p, media_type="application/json", filename=name)


@app.get("/api/history")
def api_history(hours: float = 24):
    return store.history_bucketed(min(hours, 24 * 366))


@app.get("/api/log")
def api_log():
    return list(events)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # open SSE streams would otherwise block a Ctrl+C shutdown forever
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", timeout_graceful_shutdown=2)
