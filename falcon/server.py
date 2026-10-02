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
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse

from .ble import WheelLink
from .energy import energy_view
from .guard import Guard, summary
from .health import Health
from .fastpath import BusTracker, CellRegression
from . import __version__
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
guard = Guard(store.get_json("guard_baseline"))
NOMINAL_WH = float(os.environ.get("FALCON_NOMINAL_WH", "1800"))  # Falcon Pro label: 1.8 kWh
health = Health(store.db, NOMINAL_WH)


def log_event(msg: str) -> None:
    events.appendleft({"ts": time.time(), "msg": msg})


link.on_event = log_event


async def sampler() -> None:
    last_status = None
    while True:
        await asyncio.sleep(SAMPLE_EVERY_S)
        if link.status != last_status:
            log_event(f"Bluetooth: {link.status}")
            last_status = link.status
        if link.connected and state.groups:
            store.add(energy_view(state.snapshot(), bus.sag.estimate(), link.info(), store.baseline_sag_ohm()), state.p0, state.p4)


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


@asynccontextmanager
async def lifespan(_app):
    tasks = [asyncio.create_task(link.run()), asyncio.create_task(sampler()),
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
            "wheel_firmware": None,
            "wheel_firmware_note": "kräver läskommandot V – appen skickar inga kommandon än"}


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
