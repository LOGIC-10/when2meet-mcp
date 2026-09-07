"""Offline tests against recorded When2Meet poll pages.

Every fixture is a real page captured on 2026-09-07. The expected
availability below is what was *submitted* to the site (via
ProcessLogin.php + SaveTimes.php), not what the parser read back, so the
tests are not tautological.

  page_two_people          2026-09-10/11, 09-18 Asia/Shanghai, Alice + Bob
  page_six_people_mixed_order
                           2026-09-14..16, 09-18 Asia/Shanghai; six names
                           whose alphabetical order differs from creation
                           order, one sign-in-only participant (Carol), one
                           whose votes were later cleared (Dave)
  page_order_probe         2026-09-18, 09-11 UTC; created Zed, Amy, Mia, Bea
  page_dst_new_york        2026-10-31..11-02, 00-23 America/New_York (DST
                           ends on Nov 1): 280 slots, participant Pat
  page_days_of_week        "days of the week" poll, Mon/Wed/Fri 09-12
"""
from pathlib import Path

import pytest

import server

FX = Path(__file__).parent / "fixtures"


def load(name, tz="UTC"):
    return server.parse_event_page((FX / f"{name}.html").read_text(), tz)


def busy(results):
    return {s["time"]: s["free"] for s in results["slots"] if s["free"]}


# ─── two people ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def two():
    return load("page_two_people")


def test_two_people_metadata(two):
    assert two["event_name"] == "MCP test 0907"
    assert two["poll_type"] == "specific_dates"
    assert two["participants"] == ["Alice", "Bob"]
    assert set(two["participant_ids"]) == {"Alice", "Bob"}
    assert two["total_participants"] == 2


def test_two_people_slots_sorted_iso_dates(two):
    slots = two["slots"]
    assert len(slots) == 72                      # 2 days x 9 h x 4
    assert slots == sorted(slots, key=lambda s: s["time"])
    assert slots[0] == {"time": "2026-09-10T01:00:00+00:00",
                        "date": "2026-09-10", "free": []}
    assert slots[-1]["time"] == "2026-09-11T09:45:00+00:00"
    assert slots[-1]["date"] == "2026-09-11"


def test_two_people_availability(two):
    assert busy(two) == {
        "2026-09-11T08:45:00+00:00": ["Bob"],
        "2026-09-11T09:00:00+00:00": ["Alice", "Bob"],
        "2026-09-11T09:15:00+00:00": ["Alice", "Bob"],
        "2026-09-11T09:30:00+00:00": ["Alice", "Bob"],
        "2026-09-11T09:45:00+00:00": ["Alice", "Bob"],
    }


def test_timezone_rendering():
    r = load("page_two_people", tz="Asia/Shanghai")
    assert r["slots"][0]["time"] == "2026-09-10T09:00:00+08:00"
    assert r["slots"][0]["date"] == "2026-09-10"
    assert r["timezone"] == "Asia/Shanghai"


def test_best_windows_two_people(two):
    assert server.best_windows(two, 2, 30) == [{
        "date": "2026-09-11",
        "start": "2026-09-11T09:00:00+00:00",
        "end": "2026-09-11T10:00:00+00:00",
        "duration_minutes": 60,
        "attendees": ["Alice", "Bob"],
    }]
    assert server.best_windows(two, 2, 90) == []


# ─── six people, names not in creation order ────────────────────────

@pytest.fixture(scope="module")
def six():
    return load("page_six_people_mixed_order", tz="Asia/Shanghai")


def test_six_people_names_decoded_and_ids_paired(six):
    assert six["participants"] == ["Alice", "Bob", "Carol", "Dave",
                                   "Zoë O'Brien", "王小明"]
    assert six["participant_ids"]["Zoë O'Brien"] == "152005217"
    assert six["participant_ids"]["王小明"] == "152005219"


def test_six_people_availability_matches_submissions(six):
    """Submitted: Alice Sep14 09-12 + Sep15 14-16; Bob Sep14 10-13 + Sep15
    14-15 + Sep16 09-18; Zoë Sep14 11-11:30 + Sep16 09-18; 王小明 Sep15
    14:30-16:00; Carol never voted; Dave voted then cleared."""
    b = busy(six)
    assert sum(len(v) for v in b.values()) == 116
    assert b["2026-09-14T11:00:00+08:00"] == ["Alice", "Bob", "Zoë O'Brien"]
    assert b["2026-09-15T14:30:00+08:00"] == ["Alice", "Bob", "王小明"]
    assert b["2026-09-16T17:45:00+08:00"] == ["Bob", "Zoë O'Brien"]
    everyone = {n for v in b.values() for n in v}
    assert "Carol" not in everyone and "Dave" not in everyone


def test_six_people_best_windows(six):
    wins = server.best_windows(six, 2, 30)
    assert [(w["date"], w["start"][11:16], w["end"][11:16],
             w["duration_minutes"], w["attendees"]) for w in wins] == [
        ("2026-09-14", "11:00", "11:30", 30, ["Alice", "Bob", "Zoë O'Brien"]),
        ("2026-09-15", "14:30", "15:00", 30, ["Alice", "Bob", "王小明"]),
        ("2026-09-16", "09:00", "18:00", 540, ["Bob", "Zoë O'Brien"]),
        ("2026-09-14", "10:00", "12:00", 120, ["Alice", "Bob"]),
        ("2026-09-15", "14:30", "16:00", 90, ["Alice", "王小明"]),
        ("2026-09-15", "14:00", "15:00", 60, ["Alice", "Bob"]),
    ]
    assert server.best_windows(six, 4, 15) == []


# ─── creation order differs from alphabetical order ─────────────────

def test_order_probe_attributes_by_id_not_position():
    """Created Zed, Amy, Mia, Bea. Final state: Zed slots 0-1, Bea slot 7,
    Amy cleared, Mia signed in only."""
    r = load("page_order_probe")
    assert r["participants"] == ["Amy", "Bea", "Mia", "Zed"]
    slots = r["slots"]
    assert len(slots) == 8
    assert [s["free"] for s in slots] == [
        ["Zed"], ["Zed"], [], [], [], [], [], ["Bea"]]


# ─── DST day, 280 slots ─────────────────────────────────────────────

def test_dst_poll_slot_count_and_pat():
    """Pat: first slot, last slot, and the 12 slots from 05:30Z to 08:15Z on
    Nov 1 (the local 01:30-04:15 span that contains the repeated hour)."""
    r = load("page_dst_new_york", tz="America/New_York")
    assert len(r["slots"]) == 280
    pat = [s["time"] for s in r["slots"] if s["free"] == ["Pat"]]
    assert len(pat) == 14
    assert pat[0] == "2026-10-31T00:00:00-04:00"
    assert pat[-1] == "2026-11-02T22:45:00-05:00"
    assert "2026-11-01T01:30:00-04:00" in pat      # before fall-back
    assert "2026-11-01T01:30:00-05:00" in pat      # repeated hour, after
    dates = {s["date"] for s in r["slots"]}
    assert dates == {"2026-10-31", "2026-11-01", "2026-11-02"}


# ─── days-of-the-week poll ──────────────────────────────────────────

def test_days_of_week_poll():
    r = load("page_days_of_week", tz="Asia/Shanghai")
    assert r["poll_type"] == "days_of_week"
    assert r["event_name"] == "dow test"
    assert len(r["slots"]) == 36
    assert {s["date"] for s in r["slots"]} == {"Monday", "Wednesday", "Friday"}
    assert r["slots"][0]["time"].endswith("T09:00:00+00:00")   # not tz-shifted
    assert r["slots"][0]["date"] == "Monday"


# ─── window search on synthetic input ───────────────────────────────

def test_best_windows_attendees_is_intersection():
    fake = {"slots": [
        {"time": "2026-01-01T09:00:00+00:00", "date": "2026-01-01", "free": ["A", "B"]},
        {"time": "2026-01-01T09:15:00+00:00", "date": "2026-01-01", "free": ["B", "C"]},
        {"time": "2026-01-01T09:30:00+00:00", "date": "2026-01-01", "free": ["B", "C"]},
    ]}
    assert server.best_windows(fake, 2, 30) == [{
        "date": "2026-01-01",
        "start": "2026-01-01T09:15:00+00:00",
        "end": "2026-01-01T09:45:00+00:00",
        "duration_minutes": 30,
        "attendees": ["B", "C"],
    }]


def test_best_windows_breaks_on_time_gap():
    fake = {"slots": [
        {"time": "2026-01-01T09:00:00+00:00", "date": "2026-01-01", "free": ["A"]},
        {"time": "2026-01-01T10:00:00+00:00", "date": "2026-01-01", "free": ["A"]},
    ]}
    assert server.best_windows(fake, 1, 30) == []


def test_best_windows_rejects_bad_params(two):
    with pytest.raises(ValueError):
        server.best_windows(two, 0, 30)
    with pytest.raises(ValueError):
        server.best_windows(two, 1, 5)


# ─── build_availability (vote core) ─────────────────────────────────

SLOTS = [1789002000, 1789002900, 1789003800, 1789004700]   # 01:00-01:45Z Sep 10


def test_build_availability_accepts_iso_variants():
    bits, marked = server.build_availability(SLOTS, [
        "2026-09-10T01:00:00+00:00",     # full offset form
        "2026-09-10T01:15Z",             # Z, no seconds
        "2026-09-10T09:45:00+08:00",     # other zone
    ])
    assert bits == "1101"
    assert marked == [1789002000, 1789002900, 1789004700]


def test_build_availability_naive_uses_tz():
    bits, _ = server.build_availability(SLOTS, ["2026-09-10 09:30"], tz="Asia/Shanghai")
    assert bits == "0010"


def test_build_availability_empty_clears():
    assert server.build_availability(SLOTS, []) == ("0000", [])


def test_build_availability_rejects_non_slot_and_garbage():
    with pytest.raises(ValueError, match="not 15-minute slots"):
        server.build_availability(SLOTS, ["2026-09-10T01:05:00+00:00"])
    with pytest.raises(ValueError, match="ISO 8601"):
        server.build_availability(SLOTS, ["tomorrow 9am"])


# ─── validation helpers ─────────────────────────────────────────────

def test_parse_poll_url():
    assert server._parse_poll_url("https://www.when2meet.com/?38390901-GqoiQ") == (
        "38390901", "GqoiQ")
    assert server._parse_poll_url("https://when2meet.com/?1-A") == ("1", "A")
    with pytest.raises(ValueError):
        server._parse_poll_url("https://when2meet.com/")


def test_decode_name():
    assert server._decode_name("Zoë O\\&#039;Brien") == "Zoë O'Brien"
    assert server._decode_name("A &amp; B") == "A & B"
    assert server._decode_name("plain") == "plain"


@pytest.mark.parametrize("dates,earliest,latest,tz", [
    ([], 9, 18, "UTC"),
    (["2026/09/10"], 9, 18, "UTC"),
    (["2026-02-30"], 9, 18, "UTC"),
    (["2026-09-10"], 18, 9, "UTC"),
    (["2026-09-10"], 9, 25, "UTC"),
    (["2026-09-10"], 9, 18, "Mars/Olympus"),
])
def test_validate_create_args_rejects(dates, earliest, latest, tz):
    with pytest.raises(Exception):
        server._validate_create_args(dates, earliest, latest, tz)


@pytest.mark.parametrize("latest,ok", [(0, True), (24, True), (23, True), (25, False), (9, False)])
def test_validate_latest_hour_midnight(latest, ok):
    if ok:
        server._validate_create_args(["2026-09-20"], 9, latest, "UTC")
    else:
        with pytest.raises(ValueError):
            server._validate_create_args(["2026-09-20"], 9, latest, "UTC")


def test_bad_timezone_is_value_error():
    with pytest.raises(ValueError, match="Unknown IANA timezone"):
        server._check_tz("Mars/Olympus")


def test_parse_event_page_without_slots_raises():
    with pytest.raises(RuntimeError, match="Poll not found"):
        server.parse_event_page("<html><title> - When2meet</title></html>")


def test_vote_rejects_empty_name_and_bad_url():
    import asyncio
    with pytest.raises(ValueError, match="name"):
        asyncio.run(server.vote_on_behalf("https://when2meet.com/?1-ABC", " ", []))
    with pytest.raises(ValueError, match="poll URL"):
        asyncio.run(server.vote_on_behalf("https://when2meet.com/", "Alice", []))
