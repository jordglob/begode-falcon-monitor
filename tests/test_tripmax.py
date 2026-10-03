"""Max values per ride, with time and place."""
from falcon import tripmax


def pts():
    out = []
    for i in range(50):
        out.append({"ts": 100 + i * 2, "lat": 51 + i * 1e-4, "lon": 0.0, "speed_kmh": 10 + i % 7,
                    "wheel_speed_kmh": 10 + i % 7, "sats": 9, "hdop": 1.0,
                    "pwm_max": 40 + (35 if i == 20 else 0), "current_max": 10 + (30 if i == 30 else 0),
                    "power_max_w": 900 + (2000 if i == 30 else 0), "regen_max_w": 100 if i == 40 else None,
                    "volt_min": 95 - (6 if i == 30 else 0)})
    return out


def test_extremes_with_time_and_place():
    samples = [{"ts": 100 + i * 5, "cell_min_mv": 3900 - (700 if i == 10 else 0), "cell_spread_mv": 8,
                "motor_temp_c": 40 + i, "board_temp_c": 35, "temp2_c": 30} for i in range(20)]
    events = [{"ts": 160, "key": "larmbit 0x01", "old": "False", "new": "True", "level": "alarm", "text": "x"}]
    m = tripmax.compute(pts(), samples, None, events)
    it = {x["key"]: x for x in m["items"]}
    assert it["speed"]["value"] == 16
    assert it["margin"]["value"] == 25 and it["margin"]["ts"] == 140 and it["margin"]["level"] == "warn"
    assert it["current"]["value"] == 40 and it["current"]["lat"] == 51 + 30 * 1e-4
    assert it["power"]["value"] == 2900 and it["regen"]["value"] == 100 and it["volt_min"]["value"] == 89
    assert it["cell_min"]["value"] == 3.2 and it["cell_min"]["level"] == "warn"
    assert it["motor_temp_c"]["value"] == 59
    assert m["alarms"]["count"] == 1 and m["alarms"]["kinds"] == ["larmbit 0x01"]
    assert m["notes"] == []


def test_old_rides_without_new_columns_get_a_note():
    p = [{k: v for k, v in x.items() if k not in ("current_max", "power_max_w", "regen_max_w", "volt_min")}
         for x in pts()]
    m = tripmax.compute(p, [], None, [])
    assert "speed" in {x["key"] for x in m["items"]} and m["notes"]


def test_energy_counter_interval_extremes():
    from falcon.fastpath import EnergyCounter
    e = EnergyCounter()
    for i, (v, a) in enumerate([(96, 10), (90, 40), (97, -5), (95, 12)]):
        e.add(i * 0.3, v, a, 50)
    iv = e.take_interval(1.2)
    assert iv["current_max"] == 40 and iv["power_max_w"] == 3600 and iv["regen_max_w"] == 485 and iv["volt_min"] == 90
    assert e.take_interval(2.0)["current_max"] is None          # reset per interval
