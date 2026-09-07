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


# ─── poll metadata ─────────────────────────────────────────────────

def test_metadata_specific_dates(two):
    assert two["poll_timezone"] == "Asia/Shanghai"
    assert two["dates"] == ["2026-09-10", "2026-09-11"]
    assert (two["earliest_time"], two["latest_time"]) == ("09:00", "18:00")


def test_metadata_dst_and_days_of_week():
    dst = load("page_dst_new_york")
    assert dst["poll_timezone"] == "America/New_York"
    assert (dst["earliest_time"], dst["latest_time"]) == ("00:00", "23:00")
    dow = load("page_days_of_week")
    assert dow["poll_timezone"] is None
    assert dow["dates"] == ["Monday", "Wednesday", "Friday"]
    assert (dow["earliest_time"], dow["latest_time"]) == ("09:00", "12:00")


# ─── window constraints ─────────────────────────────────────────────

def test_best_windows_required(six):
    wins = server.best_windows(six, 2, 30, required=["王小明"])
    assert [w["attendees"] for w in wins] == [
        ["Alice", "Bob", "王小明"], ["Alice", "王小明"]]


def test_best_windows_exclude_changes_result(six):
    wins = server.best_windows(six, 2, 30, exclude=["Bob"])
    assert all("Bob" not in w["attendees"] for w in wins)
    # same attendee count -> longer window first
    assert (wins[0]["attendees"], wins[0]["duration_minutes"]) == (["Alice", "王小明"], 90)
    assert (wins[1]["attendees"], wins[1]["duration_minutes"]) == (["Alice", "Zoë O'Brien"], 30)


def test_best_windows_dates_and_limit(six):
    wins = server.best_windows(six, 2, 30, dates=["2026-09-15"], limit=1)
    assert len(wins) == 1
    assert wins[0]["date"] == "2026-09-15"
    assert wins[0]["attendees"] == ["Alice", "Bob", "王小明"]
    assert server.best_windows(six, 2, 30, dates=["2026-12-25"]) == []


def test_best_windows_constraint_validation(six):
    with pytest.raises(ValueError, match="Unknown participant"):
        server.best_windows(six, 2, 30, required=["Nobody"])
    with pytest.raises(ValueError, match="both required and excluded"):
        server.best_windows(six, 2, 30, required=["Bob"], exclude=["Bob"])
    with pytest.raises(ValueError, match="limit"):
        server.best_windows(six, 2, 30, limit=0)


# ─── days-of-the-week creation ──────────────────────────────────────

@pytest.mark.parametrize("dates,expected", [
    (["Monday", "Wednesday", "Friday"], "1|3|5"),
    (["sun", "SAT"], "0|6"),
    (["2", "tue"], "2"),
])
def test_encode_days_of_week(dates, expected):
    assert server._encode_possible_dates(dates, "days_of_week") == expected


def test_encode_days_of_week_rejects_bad_input():
    with pytest.raises(ValueError, match="Not a weekday"):
        server._encode_possible_dates(["2026-09-10"], "days_of_week")
    with pytest.raises(ValueError, match="poll_type"):
        server._encode_possible_dates(["Monday"], "weekly")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        server._encode_possible_dates(["Monday"], "specific_dates")


# ─── HTTP layer with a mock transport (no network) ──────────────────

import asyncio
import httpx


@pytest.fixture
def transport(monkeypatch):
    """Install an httpx.MockTransport; returns the list of captured requests."""
    calls = []
    state = {"handler": None}

    def handler(request):
        calls.append(request)
        return state["handler"](request)

    monkeypatch.setattr(server, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(server, "RETRY_DELAYS", (0, 0))
    state["calls"] = calls
    return state


def test_create_poll_posts_expected_form(transport):
    def h(req):
        assert req.url.path == "/SaveNewEvent.php"
        return httpx.Response(200, text="<script>window.location='./?123-AbC'</script>")
    transport["handler"] = h
    url = asyncio.run(server.create_poll("Sync", ["Monday", "Friday"], 10, 24,
                                         "UTC", "days_of_week"))
    assert url == "https://www.when2meet.com/?123-AbC"
    form = dict(x.split("=") for x in transport["calls"][0].content.decode().split("&"))
    assert form == {"NewEventName": "Sync", "DateTypes": "DaysOfTheWeek",
                    "PossibleDates": "1%7C5", "NoEarlierThan": "10",
                    "NoLaterThan": "0", "TimeZone": "UTC"}


def test_get_poll_results_retries_then_succeeds(transport):
    page = (FX / "page_two_people.html").read_text()
    attempts = iter([httpx.Response(502), httpx.Response(503), httpx.Response(200, text=page)])
    transport["handler"] = lambda req: next(attempts)
    r = asyncio.run(server.get_poll_results("https://when2meet.com/?38390901-GqoiQ"))
    assert r["participants"] == ["Alice", "Bob"]
    assert len(transport["calls"]) == 3


def test_get_poll_results_gives_up_after_retries(transport):
    transport["handler"] = lambda req: httpx.Response(500)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(server.get_poll_results("https://when2meet.com/?38390901-GqoiQ"))
    assert len(transport["calls"]) == 3


def test_get_poll_results_retries_transport_errors(transport):
    page = (FX / "page_two_people.html").read_text()
    n = {"i": 0}
    def h(req):
        n["i"] += 1
        if n["i"] == 1:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, text=page)
    transport["handler"] = h
    assert asyncio.run(server.get_poll_results("https://when2meet.com/?38390901-GqoiQ"))["participants"] == ["Alice", "Bob"]


def test_get_poll_results_no_retry_on_4xx(transport):
    transport["handler"] = lambda req: httpx.Response(404)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(server.get_poll_results("https://when2meet.com/?38390901-GqoiQ"))
    assert len(transport["calls"]) == 1


def test_vote_on_behalf_full_flow(transport):
    page = (FX / "page_two_people.html").read_text()
    def h(req):
        if req.method == "GET":
            return httpx.Response(200, text=page)
        if req.url.path == "/ProcessLogin.php":
            return httpx.Response(200, text="4242")
        assert req.url.path == "/SaveTimes.php"
        return httpx.Response(200, text="")
    transport["handler"] = h
    r = asyncio.run(server.vote_on_behalf(
        "https://when2meet.com/?38390901-GqoiQ", "Cara",
        ["2026-09-10T09:00+08:00", "2026-09-10T09:15+08:00"], tz="Asia/Shanghai"))
    assert r == {"name": "Cara", "person_id": "4242", "slots_marked": 2,
                 "times": ["2026-09-10T09:00:00+08:00", "2026-09-10T09:15:00+08:00"]}
    save = transport["calls"][-1]
    form = dict(x.split("=") for x in save.content.decode().split("&"))
    assert form["person"] == "4242" and form["event"] == "38390901"
    assert form["availability"] == "11" + "0" * 70
    assert form["slots"] == "1789002000%2C1789002900"


def test_vote_on_behalf_wrong_password_is_value_error(transport):
    page = (FX / "page_two_people.html").read_text()
    transport["handler"] = lambda req: (httpx.Response(200, text=page) if req.method == "GET"
                                        else httpx.Response(200, text="Wrong password."))
    with pytest.raises(ValueError, match="Wrong password"):
        asyncio.run(server.vote_on_behalf("https://when2meet.com/?38390901-GqoiQ",
                                          "Alice", [], password="x"))
    assert not any(c.url.path == "/SaveTimes.php" for c in transport["calls"])


# ─── days-of-the-week poll with votes (created through create_poll) ──

def test_days_of_week_voted_poll_end_to_end():
    """Created with poll_type="days_of_week", dates Monday/wed/FRI, 14-16
    Asia/Shanghai. Ann voted all 8 Wednesday slots, Ben the first 4."""
    r = load("page_days_of_week_voted")
    assert r["poll_type"] == "days_of_week"
    assert r["event_name"] == "weekly dow e2e"
    assert r["dates"] == ["Monday", "Wednesday", "Friday"]
    assert (r["earliest_time"], r["latest_time"]) == ("14:00", "16:00")
    assert len(r["slots"]) == 24
    assert r["participants"] == ["Ann", "Ben"]
    wins = server.best_windows(r, 2, 30)
    assert [(w["date"], w["start"][11:16], w["end"][11:16], w["duration_minutes"], w["attendees"])
            for w in wins] == [("Wednesday", "14:00", "15:00", 60, ["Ann", "Ben"])]
    ann = server.best_windows(r, 1, 30, required=["Ann"], dates=["Wednesday"])
    assert [(w["duration_minutes"], w["attendees"]) for w in ann] == [
        (60, ["Ann", "Ben"]), (120, ["Ann"])]
    assert server.best_windows(r, 1, 30, dates=["Monday"]) == []
