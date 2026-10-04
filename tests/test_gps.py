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


def test_reader_reads_a_serial_gps_receiver():
    """A USB/serial receiver just sends NMEA lines; a pty stands in for it."""
    import os
    master, slave = os.openpty()
    r = GpsReader(interval_s=0.01, device=os.ttyname(slave))
    got = []
    r.on_fix = got.append

    async def go():
        t = asyncio.ensure_future(r.run())
        await asyncio.sleep(0.03)
        assert got == []                                   # nothing sent yet: nothing stored
        os.write(master, ("\r\n".join(REAL) + "\r\n").encode())
        await asyncio.sleep(0.06)
        n = len(got)
        await asyncio.sleep(0.06)                          # silence: the old position is not stored again
        t.cancel()
        return n, len(got)
    n, n_later = asyncio.run(go())
    os.close(master)
    os.close(slave)
    assert n == 1 and n_later == 1
    assert got[0]["lat"] is not None and r.report()["source"].startswith("USB-GPS") and r.status == "position"


def test_pushed_phone_position_is_stored_and_hides_the_modem_for_a_while():
    text = "  GPS | nmea: " + REAL[0] + "\n      |       " + "\n      |       ".join(REAL[1:]) + "\n"
    r = GpsReader(modem="0", interval_s=0.01)
    got = []
    r.on_fix = got.append

    async def fake_get():
        return text
    r._get = fake_get
    r.push(51.5, 0.25, speed_kmh=21.6, alt_m=30.0, accuracy_m=6.0)
    assert got[-1]["lat"] == 51.5 and got[-1]["fix_type"] == "telefon" and got[-1]["hdop"] == 1.2
    rep = r.report()
    assert rep["source"] == "telefonens GPS" and rep["fix"] and rep["state"]["speed_kmh"] == 21.6

    async def go():
        t = asyncio.ensure_future(r.run())
        await asyncio.sleep(0.05)
        t.cancel()
    asyncio.run(go())
    assert len(got) == 1                                   # the modem's own fixes were not stored meanwhile
    r.ext_until = 0.0                                      # the phone stopped sending
    asyncio.run(go())
    assert len(got) > 1 and got[-1]["fix_type"] != "telefon"


def test_phone_positions_count_as_good_fix_by_accuracy():
    from falcon.rides import good_fix
    assert good_fix({"sats": None, "hdop": 1.2}) and not good_fix({"sats": None, "hdop": 6.0})
    assert good_fix({"sats": 9, "hdop": 1.0}) and not good_fix({"sats": 4, "hdop": 1.0})
    assert not good_fix({"sats": None, "hdop": None})
