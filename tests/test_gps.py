"""NMEA parsing. GSV/GSA and the rollover date are real H5321 gw output; the position is
synthetic (Greenwich) so no real location is published."""
import asyncio

import pytest

from falcon.gps import GpsReader, Nmea, fix_date, nmea_ok

REAL = [
    "$GPGSV,1,1,04,04,45,202,42,01,35,156,41,02,14,156,34,25,10,017,*7C",
    "$GPGSA,A,2,04,01,02,,,,,,,,,,12.79,12.76,1.00*0A",
    "$GPRMC,060057.000,A,5128.667200,N,00000.005000,W,1.113,7.6,170207,,,A*76",
    "$GPGGA,060057.000,5128.667200,N,00000.005000,W,1,3,12.76,46.000,M,,,,*14",
]


def test_checksums_of_real_sentences():
    assert all(nmea_ok(s) for s in REAL)
    assert not nmea_ok(REAL[2][:-2] + "00")


def test_parse_fix():
    n = Nmea()
    for s in REAL:
        n.feed(s)
    st = n.state
    assert n.has_fix and st["fix_type"] == "2D" and st["sats_used"] == 3
    assert st["lat"] == pytest.approx(51.477787, abs=1e-5) and st["lon"] == pytest.approx(-0.0000833, abs=1e-6)
    assert st["speed_kmh"] == pytest.approx(1.113 * 1.852, abs=0.01)
    assert [x["prn"] for x in n.sats][:3] == [4, 1, 2] and n.sats[0]["snr"] == 42


def test_week_rollover_corrected():
    t, corrected = fix_date("170207", "060057.000")
    assert corrected and t.date().isoformat() == "2026-10-03" and t.hour == 6
    t, corrected = fix_date("031026", "060057.000")
    assert not corrected and t.year == 2026


def test_no_fix_sentences():
    n = Nmea()
    n.feed("$GPRMC,000005.000,V,,,,,,,,,,N*48")
    n.feed("$GPGGA,,,,,,0,0,,,M,,,,*1B")
    assert not n.has_fix and "lat" not in n.state


def test_reader_parses_mmcli_output():
    text = "  GPS | nmea: " + REAL[0] + "\n      |       " + "\n      |       ".join(REAL[1:]) + "\n"
    r = GpsReader(modem="0", interval_s=0.01)
    got = []
    r.on_fix = got.append

    async def fake_get():
        return text
    r._get = fake_get

    async def go():
        t = asyncio.ensure_future(r.run())
        await asyncio.sleep(0.05)
        t.cancel()
    asyncio.run(go())
    rep = r.report()
    assert rep["fix"] and rep["status"] == "position" and got and rep["stats"]["first_fix_s"] is not None
    assert rep["state"]["rollover_corrected"] is True
