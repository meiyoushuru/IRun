# SPDX-License-Identifier: MPL-2.0
import math
import pytest
from vrun.routes import LapCounter, load_route, pace_text

def test_routes_close_and_have_valid_duration(varied_route):
    route = varied_route
    assert route.points[0] == route.points[-1]
    assert 50 < route.length_m < 10000
    samples = list(route.samples(3.3))
    assert samples[-1][0] == route.points[0]
    assert sum(delta for _, delta in samples) == pytest.approx(route.length_m / 3.3)
    assert all(0 < delta <= 0.25 for _, delta in samples)


def test_legacy_txt_still_converts_and_closes(tmp_path):
    path = tmp_path / "custom.txt"
    path.write_text("[{'lat': 30, 'lng': 120}, {'lat': 30.001, 'lng': 120}]", encoding="utf-8")
    route = load_route(path)
    assert route.points[0] == route.points[-1]
    assert route.points[0].lat != 30
    assert 210 < route.length_m < 235


@pytest.mark.parametrize("contents", [
    "", "[]", "[{'lat': 30, 'lng': 120}]", "[{'lat': 99, 'lng': 120}, {'lat': 30, 'lng': 120}]",
    "[{'lat': 30, 'lng': 120}, {'lat': 30, 'lng': 120}]", "__import__('os').getcwd()",
    "[{'lat': True, 'lng': 120}, {'lat': 30, 'lng': 120}]",
])
def test_malformed_routes_are_rejected(tmp_path, contents):
    path = tmp_path / "bad.txt"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        load_route(path)


def test_distance_reminder_fires_once_after_completed_lap():
    counter = LapCounter(400, 3000)
    assert [counter.complete_lap() for _ in range(9)] == [False]*7 + [True, False]
    assert counter.laps == 9
    assert counter.distance_m == 3600


def test_disabled_reminder():
    counter = LapCounter(400)
    assert not any(counter.complete_lap() for _ in range(10))


@pytest.mark.parametrize("speed,expected", [(3.3, "5:03 / 公里"), (2.9, "5:45 / 公里"), (0, "—"), (math.nan, "—")])
def test_pace(speed, expected):
    assert pace_text(speed) == expected
