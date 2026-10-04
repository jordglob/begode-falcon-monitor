"""Half-pack seam, steady-current gate, rest timer, repeated frames."""
from falcon.sampling import CurrentTrace, NewFrames, half_of, segments, simultaneous_spread


def test_segments_split_bank_1_at_the_seam():
    assert segments(0, list(range(8))) == [(1, 1, list(range(8)))]
    assert segments(1, list(range(8))) == [(1, 9, [0, 1, 2, 3]), (2, 13, [4, 5, 6, 7])]
    assert segments(2, list(range(8))) == [(2, 17, list(range(8)))]
    assert [half_of(n) for n in (1, 12, 13, 24)] == [1, 1, 2, 2]


def test_simultaneous_spread_ignores_the_step_between_halves():
    cells = [3860] * 11 + [3824] + [4042] * 12            # 218 mV across the seam
    assert max(cells) - min(cells) == 218 and simultaneous_spread(cells) == 36


def test_steady_needs_history_and_a_narrow_range():
    tr = CurrentTrace()
    for k in range(5):
        tr.add(k * 0.3, 20.0)
    assert tr.steady(1.2) is None                         # only 1.2 s known
    for k in range(5, 16):
        tr.add(k * 0.3, 20.0 + k % 2)
    assert abs(tr.steady(4.5) - 20.5) < 0.6
    tr.add(4.8, 35.0)
    assert tr.steady(4.8) is None                         # a jump inside the window
    assert tr.peak(4.8) == 35.0


def test_steady_is_none_when_the_data_stopped():
    tr = CurrentTrace()
    for k in range(20):
        tr.add(k * 0.3, 20.0)
    assert tr.steady(5.7) is not None and tr.steady(9.0) is None


def test_rest_timer():
    tr = CurrentTrace()
    assert tr.rest_s(0) is None
    tr.add(0.0, 25.0)
    for k in range(1, 31):
        tr.add(k * 0.3, 0.3)
    assert tr.rest_s(0.0) == 0.0 and abs(tr.rest_s(9.0) - 8.7) < 1e-6


def test_new_frames():
    nf = NewFrames()
    assert nf.is_new(("A", 0), [1, 2]) and not nf.is_new(("A", 0), [1, 2])
    assert nf.is_new(("A", 1), [1, 2]) and nf.is_new(("A", 0), [1, 3])
