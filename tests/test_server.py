"""Offline tests against a real AvailabilityGrids.php response.

Fixture ground truth (verified against the poll page's AvailableAtSlot data):
  poll with dates 2026-09-10 / 2026-09-11, 09:00-18:00 Asia/Shanghai
  Alice free: Sep 11 09:00-10:00 UTC   (column-major slots 68-71)
  Bob   free: Sep 11 08:45-10:00 UTC   (column-major slots 67-71)
"""
from pathlib import Path

import pytest

import server

FIXTURE = Path(__file__).parent / "fixtures" / "grid_two_people.html"


@pytest.fixture(scope="module")
def results():
    return server.parse_grid(FIXTURE.read_text())


def test_parse_participants(results):
    assert results["participants"] == ["Alice", "Bob"]
    assert results["total_participants"] == 2


def test_parse_slot_count_and_order(results):
    slots = results["slots"]
    assert len(slots) == 72  # 2 days x 9 hours x 4
    assert slots == sorted(slots, key=lambda s: s["time"])
    assert slots[0]["time"] == "2026-09-10T01:00:00+00:00"
    assert slots[0]["date"] == "Sep 10"
    assert slots[-1]["time"] == "2026-09-11T09:45:00+00:00"
    assert slots[-1]["date"] == "Sep 11"


def test_parse_availability_matches_ground_truth(results):
    free = {s["time"]: s["free"] for s in results["slots"] if s["free"]}
    assert free == {
        "2026-09-11T08:45:00+00:00": ["Bob"],
        "2026-09-11T09:00:00+00:00": ["Alice", "Bob"],
        "2026-09-11T09:15:00+00:00": ["Alice", "Bob"],
        "2026-09-11T09:30:00+00:00": ["Alice", "Bob"],
        "2026-09-11T09:45:00+00:00": ["Alice", "Bob"],
    }


def test_parse_renders_in_requested_timezone():
    r = server.parse_grid(FIXTURE.read_text(), tz="Asia/Shanghai")
    assert r["slots"][0]["time"] == "2026-09-10T09:00:00+08:00"
    assert r["timezone"] == "Asia/Shanghai"


def test_best_windows_two_people_full_window(results):
    wins = server.best_windows(results, min_attendees=2, min_duration_minutes=30)
    assert wins == [{
        "date": "Sep 11",
        "start": "2026-09-11T09:00:00+00:00",
        "end": "2026-09-11T10:00:00+00:00",
        "duration_minutes": 60,
        "attendees": ["Alice", "Bob"],
    }]


def test_best_windows_one_person_prefers_more_attendees_then_longer(results):
    wins = server.best_windows(results, min_attendees=1, min_duration_minutes=15)
    assert wins[0]["attendees"] == ["Alice", "Bob"]
    assert wins[0]["duration_minutes"] == 60
    # Bob-only 08:45 slot is its own 15-min window
    assert {"start": "2026-09-11T08:45:00+00:00", "attendees": ["Bob"]} == {
        k: wins[1][k] for k in ("start", "attendees")}


def test_best_windows_respects_min_duration(results):
    assert server.best_windows(results, min_attendees=2, min_duration_minutes=90) == []


def test_best_windows_attendees_is_intersection():
    fake = {"slots": [
        {"time": "2026-01-01T09:00:00+00:00", "date": "Jan 01", "free": ["A", "B"]},
        {"time": "2026-01-01T09:15:00+00:00", "date": "Jan 01", "free": ["B", "C"]},
        {"time": "2026-01-01T09:30:00+00:00", "date": "Jan 01", "free": ["B", "C"]},
    ]}
    wins = server.best_windows(fake, min_attendees=2, min_duration_minutes=30)
    assert wins == [{
        "date": "Jan 01",
        "start": "2026-01-01T09:15:00+00:00",
        "end": "2026-01-01T09:45:00+00:00",
        "duration_minutes": 30,
        "attendees": ["B", "C"],
    }]


def test_best_windows_breaks_on_time_gap():
    fake = {"slots": [
        {"time": "2026-01-01T09:00:00+00:00", "date": "Jan 01", "free": ["A"]},
        {"time": "2026-01-01T10:00:00+00:00", "date": "Jan 01", "free": ["A"]},
    ]}
    assert server.best_windows(fake, min_attendees=1, min_duration_minutes=30) == []


def test_parse_poll_url():
    assert server._parse_poll_url("https://www.when2meet.com/?38390901-GqoiQ") == (
        "38390901", "GqoiQ")
    assert server._parse_poll_url("https://when2meet.com/?1-A") == ("1", "A")
    with pytest.raises(ValueError):
        server._parse_poll_url("https://when2meet.com/")


@pytest.mark.parametrize("dates,earliest,latest,tz", [
    ([], 9, 18, "UTC"),
    (["2026/09/10"], 9, 18, "UTC"),
    (["2026-02-30"], 9, 18, "UTC"),
    (["2026-09-10"], 18, 9, "UTC"),
    (["2026-09-10"], 9, 24, "UTC"),
    (["2026-09-10"], 9, 18, "Mars/Olympus"),
])
def test_validate_create_args_rejects(dates, earliest, latest, tz):
    with pytest.raises(Exception):
        server._validate_create_args(dates, earliest, latest, tz)


def test_parse_grid_without_grid_raises():
    with pytest.raises(RuntimeError):
        server.parse_grid("<html><body>nothing</body></html>")
