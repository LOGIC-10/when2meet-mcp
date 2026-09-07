"""
When2Meet MCP Server
A Model Context Protocol server for When2Meet — let AI agents create polls,
read results, and find best meeting times programmatically.

Usage:
  # As MCP server (stdio)
  python server.py

  # As CLI
  python server.py create --name "Weekly sync" --dates 2026-09-10 2026-09-11
  python server.py results --url "https://when2meet.com/?123-ABC"
  python server.py best --url "https://when2meet.com/?123-ABC" --min-people 2
  python server.py vote --url "https://when2meet.com/?123-ABC" --name Alice \
      --times 2026-09-10T09:00+08:00 2026-09-10T09:15+08:00 --tz Asia/Shanghai
"""
import re
import sys
import html as html_lib
import json
import asyncio
import argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

W2M_BASE = "https://www.when2meet.com"
SLOT_MINUTES = 15
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
POLL_ID_RE = re.compile(r"\?(\d+)-([A-Za-z0-9]+)")
HTTP_TIMEOUT = 30.0
RETRY_DELAYS = (0.5, 1.5)          # back-off before attempt 2 and 3
TRANSPORT = None                   # tests inject an httpx.MockTransport here
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday"]
# When2Meet numbers weekdays like JavaScript getDay(): 0 = Sunday .. 6 = Saturday
SITE_WEEKDAY_NUMBER = {"sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3,
                       "thursday": 4, "friday": 5, "saturday": 6}
POLL_TYPES = ("specific_dates", "days_of_week")

# Markers on the poll page (the same data the site's own JS renders from).
TIME_OF_SLOT_RE = re.compile(r"TimeOfSlot\[(\d+)\]\s*=\s*(\d+)")
AVAILABLE_AT_SLOT_RE = re.compile(r"AvailableAtSlot\[(\d+)\]\.push\((\d+)\)")
PEOPLE_NAME_RE = re.compile(r"PeopleNames\[(\d+)\]\s*=\s*'((?:[^'\\]|\\.)*)'")
PEOPLE_ID_RE = re.compile(r"PeopleIDs\[(\d+)\]\s*=\s*(\d+)")
TITLE_RE = re.compile(r"<title>(.*?)\s*-\s*When2meet</title>", re.S)
# Days-of-the-week polls label slots "Monday 09:00:00 AM" (no calendar date).
DOW_LABEL_RE = re.compile(r'ShowSlot\(\d+,\s*"(Monday|Tuesday|Wednesday|Thursday|'
                          r'Friday|Saturday|Sunday) ')
POLL_TZ_RE = re.compile(r'select\.value\s*!=\s*"([A-Za-z_]+/[A-Za-z_/+\-0-9]+|UTC)"')


# ─── Validation helpers ─────────────────────────────────────────────

def _parse_poll_url(poll_url: str) -> tuple[str, str]:
    """Extract (event_id, code) from a When2Meet URL like https://when2meet.com/?123-ABC."""
    m = POLL_ID_RE.search(poll_url)
    if not m:
        raise ValueError(
            f"Not a When2Meet poll URL (expected '...?<id>-<code>'): {poll_url}")
    return m.group(1), m.group(2)


def _weekday_number(value: str) -> int:
    """Accept 'Monday', 'mon', or the site's own 0-6 numbering."""
    v = value.strip().lower()
    if v.isdigit() and 0 <= int(v) <= 6:
        return int(v)
    for name, num in SITE_WEEKDAY_NUMBER.items():
        if v == name or (len(v) >= 3 and name.startswith(v)):
            return num
    raise ValueError(f"Not a weekday: {value!r} (use Monday..Sunday or 0-6, "
                     "0 = Sunday)")


def _encode_possible_dates(dates: list[str], poll_type: str) -> str:
    """Return the PossibleDates form value for SaveNewEvent.php."""
    if poll_type not in POLL_TYPES:
        raise ValueError(f"poll_type must be one of {POLL_TYPES}")
    if not dates:
        raise ValueError("dates must contain at least one entry")
    if poll_type == "days_of_week":
        nums = sorted({_weekday_number(d) for d in dates})
        return "|".join(str(n) for n in nums)
    bad = [d for d in dates if not DATE_RE.match(d)]
    if bad:
        raise ValueError(f"dates must be YYYY-MM-DD, got: {bad}")
    for d in dates:
        datetime.strptime(d, "%Y-%m-%d")  # raises on impossible dates
    return "|".join(dates)


def _validate_create_args(dates: list[str], earliest_hour: int,
                          latest_hour: int, tz: str,
                          poll_type: str = "specific_dates") -> str:
    """Validate everything create_poll sends; returns the PossibleDates value."""
    possible = _encode_possible_dates(dates, poll_type)
    if not (0 <= earliest_hour <= 23):
        raise ValueError("earliest_hour must be in 0..23")
    if not (0 <= latest_hour <= 24):
        raise ValueError("latest_hour must be in 0..24 (0 or 24 = midnight)")
    if earliest_hour >= _end_hour(latest_hour):
        raise ValueError("earliest_hour must be before latest_hour")
    _check_tz(tz)
    return possible


def _check_tz(tz: str) -> None:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone: {tz!r}") from exc


def _end_hour(latest_hour: int) -> int:
    """When2Meet encodes an end of midnight as 0; treat 0 and 24 alike."""
    return 24 if latest_hour in (0, 24) else latest_hour


# ─── HTTP plumbing ───────────────────────────────────────────────────

def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True,
                             transport=TRANSPORT)


async def _request(client: httpx.AsyncClient, method: str, url: str,
                   **kwargs) -> httpx.Response:
    """Send a request, retrying transport errors and 5xx responses."""
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
        else:
            if resp.status_code < 500 or attempt == attempts - 1:
                resp.raise_for_status()
                return resp
        await asyncio.sleep(RETRY_DELAYS[attempt])
    raise RuntimeError("unreachable")


# ─── Pure parsing (no network; unit-tested) ─────────────────────────

def _decode_name(raw: str) -> str:
    """Undo the site's addslashes() + htmlspecialchars() on participant names."""
    return re.sub(r"\\(.)", r"\1", html_lib.unescape(raw))


def parse_event_page(html: str, tz: str = "UTC") -> dict:
    """Parse a When2Meet poll page into structured availability.

    The page embeds exactly what the site's own JavaScript renders from:
    ``TimeOfSlot[i]`` (epoch per 15-minute slot, chronological),
    ``PeopleNames[i]`` / ``PeopleIDs[i]`` (paired by index) and
    ``AvailableAtSlot[i].push(person_id)``. Because availability is keyed
    by person *id*, there is no ordering ambiguity — unlike the
    ``hexAvailability`` comments in AvailabilityGrids.php, which are
    emitted in creation order while names are alphabetised.

    Days-of-the-week polls carry placeholder 1978 dates; they are detected
    and reported with ``poll_type = "days_of_week"`` and weekday labels.
    """
    slot_times = [int(t) for _, t in sorted(
        ((int(i), t) for i, t in TIME_OF_SLOT_RE.findall(html)))]
    if not slot_times:
        raise RuntimeError("Poll not found (no time slots on the page): "
                           "check the id and code in the URL")
    zone = ZoneInfo(tz)
    is_dow = bool(DOW_LABEL_RE.search(html))

    names = {int(i): _decode_name(n) for i, n in PEOPLE_NAME_RE.findall(html)}
    ids = {int(i): pid for i, pid in PEOPLE_ID_RE.findall(html)}
    id_to_name = {ids[i]: names[i] for i in sorted(names) if i in ids}
    participants = [names[i] for i in sorted(names)]

    free_ids: dict[int, list[str]] = {}
    for i, pid in AVAILABLE_AT_SLOT_RE.findall(html):
        free_ids.setdefault(int(i), []).append(pid)

    slots = []
    for i, ts in enumerate(slot_times):
        if is_dow:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)   # placeholder date
            date_label = WEEKDAYS[dt.weekday()]
        else:
            dt = datetime.fromtimestamp(ts, tz=zone)
            date_label = dt.date().isoformat()
        free = sorted(id_to_name.get(pid, pid) for pid in free_ids.get(i, []))
        slots.append({"time": dt.isoformat(), "date": date_label, "free": free})
    slots.sort(key=lambda s: s["time"])

    m = TITLE_RE.search(html)
    tzm = POLL_TZ_RE.search(html)
    poll_tz = None if is_dow else (tzm.group(1) if tzm else None)
    # Hour range as configured on the poll, in the poll's own timezone.
    own_zone = timezone.utc if is_dow else ZoneInfo(poll_tz) if poll_tz else zone
    first = datetime.fromtimestamp(slot_times[0], tz=own_zone)
    last_end = datetime.fromtimestamp(slot_times[-1], tz=own_zone) + timedelta(
        minutes=SLOT_MINUTES)
    seen: list[str] = []
    for s in slots:
        if s["date"] not in seen:
            seen.append(s["date"])
    return {
        "event_name": html_lib.unescape(m.group(1).strip()) if m else "",
        "poll_type": "days_of_week" if is_dow else "specific_dates",
        "poll_timezone": poll_tz,
        "dates": seen,
        "earliest_time": first.strftime("%H:%M"),
        "latest_time": "24:00" if last_end.strftime("%H:%M") == "00:00"
        else last_end.strftime("%H:%M"),
        "participants": participants,
        "participant_ids": {id_to_name[pid]: pid for pid in id_to_name},
        "total_participants": len(participants),
        "timezone": "UTC (placeholder dates)" if is_dow else tz,
        "slot_minutes": SLOT_MINUTES,
        "slots": slots,
    }


def build_availability(slot_times: list[int], free_times: list[str],
                       tz: str = "UTC") -> tuple[str, list[int]]:
    """Turn ISO timestamps into the site's availability bit string.

    Returns ``(bits, matched_epochs)`` where ``bits`` has one character per
    slot in ``slot_times`` order. Accepts any ``datetime.fromisoformat``
    form (offsets, ``Z``, no seconds); naive values are interpreted in
    ``tz``. Raises ValueError listing any time that is not a poll slot.
    """
    zone = ZoneInfo(tz)
    wanted: set[int] = set()
    unmatched: list[str] = []
    for raw in free_times:
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Not an ISO 8601 timestamp: {raw!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zone)
        epoch = int(dt.timestamp())
        if epoch in slot_times:
            wanted.add(epoch)
        else:
            unmatched.append(raw)
    if unmatched:
        raise ValueError("These times are not 15-minute slots of this poll "
                         f"(use get_poll_results to list them): {unmatched}")
    bits = "".join("1" if t in wanted else "0" for t in slot_times)
    return bits, sorted(wanted)


def best_windows(results: dict, min_attendees: int = 2,
                 min_duration_minutes: int = 30,
                 required: list[str] | None = None,
                 exclude: list[str] | None = None,
                 dates: list[str] | None = None,
                 limit: int | None = None) -> list[dict]:
    """Find maximal contiguous windows where at least ``min_attendees`` people
    are free for the *whole* window (attendee set is the intersection across
    every slot in the window). Pure function over ``parse_event_page`` output.

    ``required``: every window must include all of these people.
    ``exclude``: these people are ignored entirely.
    ``dates``: only consider these ``date`` labels (ISO dates or weekdays).
    ``limit``: return at most this many windows (after sorting).
    """
    if min_attendees < 1:
        raise ValueError("min_attendees must be >= 1")
    if min_duration_minutes < SLOT_MINUTES:
        raise ValueError(f"min_duration_minutes must be >= {SLOT_MINUTES}")
    if limit is not None and limit < 1:
        raise ValueError("limit must be >= 1")
    known = set(results.get("participants") or
                {n for s in results["slots"] for n in s["free"]})
    required_set = set(required or [])
    exclude_set = set(exclude or [])
    unknown = (required_set | exclude_set) - known
    if unknown and known:
        raise ValueError(f"Unknown participant(s): {sorted(unknown)}; "
                         f"poll has {sorted(known)}")
    if required_set & exclude_set:
        raise ValueError("A person cannot be both required and excluded")
    need = max(1, min_duration_minutes // SLOT_MINUTES)
    step = timedelta(minutes=SLOT_MINUTES)
    date_filter = set(dates) if dates else None

    by_date: dict[str, list[dict]] = {}
    for s in results["slots"]:
        if date_filter is not None and s["date"] not in date_filter:
            continue
        free = [n for n in s["free"] if n not in exclude_set]
        by_date.setdefault(s["date"], []).append({**s, "free": free})

    found = []
    for date, day in by_date.items():
        day = sorted(day, key=lambda x: x["time"])
        for i, j, common in _candidate_windows(day, min_attendees, step):
            length = j - i + 1
            if length < need or not required_set <= common:
                continue
            end = datetime.fromisoformat(day[j]["time"]) + step
            found.append({
                "date": date,
                "start": day[i]["time"],
                "end": end.isoformat(),
                "duration_minutes": length * SLOT_MINUTES,
                "attendees": sorted(common),
            })

    found.sort(key=lambda w: (-len(w["attendees"]), -w["duration_minutes"], w["start"]))
    return found[:limit] if limit else found


def _candidate_windows(day: list[dict], min_attendees: int,
                       step: timedelta) -> list[tuple[int, int, frozenset]]:
    """Every maximal (start, end, attendees) window of a single day.

    From each start slot, extend rightwards while the running intersection
    still has ``min_attendees`` people; emit a window each time the
    intersection is about to shrink (and at the end). Windows fully covered
    by another window with a superset of attendees are dropped.
    """
    cands: list[tuple[int, int, frozenset]] = []
    for i in range(len(day)):
        common = frozenset(day[i]["free"])
        if len(common) < min_attendees:
            continue
        j = i
        while True:
            nxt = (frozenset(day[j + 1]["free"]) if j + 1 < len(day)
                   and _is_adjacent(day[j], day[j + 1], step) else None)
            can_extend = nxt is not None and len(common & nxt) >= min_attendees
            if not can_extend or (common & nxt) != common:
                cands.append((i, j, common))
            if not can_extend:
                break
            j += 1
            common &= nxt
    return [c for c in cands if not any(
        o != c and o[0] <= c[0] and o[1] >= c[1] and o[2] >= c[2]
        for o in cands)]


def _is_adjacent(a: dict, b: dict, step: timedelta) -> bool:
    return (datetime.fromisoformat(b["time"])
            - datetime.fromisoformat(a["time"])) == step


# ─── Core functions (network) ───────────────────────────────────────

async def create_poll(
    event_name: str,
    dates: list[str],
    earliest_hour: int = 9,
    latest_hour: int = 18,
    tz: str = "Asia/Shanghai",
    poll_type: str = "specific_dates",
) -> str:
    """Create a new When2Meet availability poll.

    Use this when coordinating a meeting time with multiple people.
    The poll asks participants to mark which 15-minute slots they are
    free on the given candidate dates. After creating the poll, share
    the returned URL with participants, then call get_poll_results or
    find_best_slot to read back availability.

    Args:
        event_name: Title shown on the poll page, e.g. "Weekly sync".
        dates: Candidate dates in YYYY-MM-DD format, or, for a
            days-of-the-week poll, weekday names ("Monday") or the site's
            0-6 numbers (0 = Sunday).
        earliest_hour: Earliest selectable hour (0-23). Default 9.
        latest_hour: End hour (1-24; 0 or 24 = midnight). Default 18.
        tz: IANA timezone string. Default "Asia/Shanghai".
        poll_type: "specific_dates" (default) or "days_of_week".

    Returns:
        The shareable When2Meet poll URL.
    """
    if not event_name.strip():
        raise ValueError("event_name must not be empty")
    possible = _validate_create_args(dates, earliest_hour, latest_hour, tz,
                                     poll_type)
    async with _make_client() as client:
        resp = await _request(
            client, "POST", f"{W2M_BASE}/SaveNewEvent.php",
            data={
                "NewEventName": event_name,
                "DateTypes": ("DaysOfTheWeek" if poll_type == "days_of_week"
                              else "SpecificDates"),
                "PossibleDates": possible,
                "NoEarlierThan": str(earliest_hour),
                "NoLaterThan": str(_end_hour(latest_hour) % 24),
                "TimeZone": tz,
            },
            headers={"Referer": f"{W2M_BASE}/"},
        )
        m = POLL_ID_RE.search(resp.text)
        if not m:
            raise RuntimeError(f"Failed to create poll: {resp.text[:300]}")
        return f"{W2M_BASE}/?{m.group(1)}-{m.group(2)}"


async def _fetch_poll_page(client: httpx.AsyncClient, poll_url: str) -> str:
    eid, code = _parse_poll_url(poll_url)
    resp = await _request(client, "GET", f"{W2M_BASE}/?{eid}-{code}")
    return resp.text


async def get_poll_results(poll_url: str, tz: str = "UTC") -> dict:
    """Read the current availability of an existing poll.

    Use this after participants have submitted their availability to see
    who is free at each 15-minute time slot. Read-only — does not modify
    the poll.

    Args:
        poll_url: The When2Meet poll URL returned by create_poll().
        tz: IANA timezone used to render slot times. Default "UTC".

    Returns:
        A dict with event_name, poll_type, participants, participant_ids
        and per-slot availability (slots sorted by time; ``date`` is an
        ISO date, or a weekday name for days-of-the-week polls).
    """
    _check_tz(tz)
    async with _make_client() as client:
        html = await _fetch_poll_page(client, poll_url)
    return parse_event_page(html, tz)


async def find_best_slot(
    poll_url: str,
    min_attendees: int = 2,
    min_duration_minutes: int = 30,
    tz: str = "UTC",
    required: list[str] | None = None,
    exclude: list[str] | None = None,
    dates: list[str] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Find the best contiguous meeting time windows from a poll.

    Use this after participants have voted to automatically identify time
    windows where enough people are available for the whole window.
    Returns maximal windows, sorted by attendee count then duration. Read-only.

    Args:
        poll_url: The When2Meet poll URL.
        min_attendees: Minimum people required for the whole window. Default 2.
        min_duration_minutes: Minimum meeting length. Default 30 (multiple of 15).
        tz: IANA timezone used to render times. Default "UTC".
        required: Names that must be free in every returned window.
        exclude: Names to ignore entirely.
        dates: Only consider these dates (ISO, or weekday names for
            days-of-the-week polls).
        limit: Return at most this many windows.

    Returns:
        Sorted list of recommended time windows.
    """
    results = await get_poll_results(poll_url, tz)
    return best_windows(results, min_attendees, min_duration_minutes,
                        required, exclude, dates, limit)


async def vote_on_behalf(
    poll_url: str,
    name: str,
    free_times: list[str],
    password: str = "",
    tz: str = "UTC",
) -> dict:
    """Submit availability on behalf of a participant.

    Signs in as *name* (creating the participant on first use) and marks
    the given 15-minute slots as available. The site stores the full bit
    string, so existing availability is **fully replaced** — pass every
    slot the person is free for; pass an empty list to clear it.

    Args:
        poll_url: The When2Meet poll URL.
        name: Participant display name.
        free_times: ISO 8601 timestamps of slots the person is free (any
            ``fromisoformat`` form; naive values are read in ``tz``). Use
            get_poll_results to list valid slot times.
        password: The participant's own password, if one was set when the
            name was first used. Empty string otherwise.
        tz: IANA timezone for naive ``free_times``. Default "UTC".

    Returns:
        Dict with name, person_id, slots_marked and the marked times.
    """
    eid, code = _parse_poll_url(poll_url)
    if not name.strip():
        raise ValueError("name must not be empty")
    _check_tz(tz)
    referer = f"{W2M_BASE}/?{eid}-{code}"

    async with _make_client() as client:
        page = await _fetch_poll_page(client, poll_url)
        slot_times = [int(t) for _, t in sorted(
            ((int(i), t) for i, t in TIME_OF_SLOT_RE.findall(page)))]
        if not slot_times:
            raise RuntimeError("Poll not found (no time slots on the page): "
                               "check the id and code in the URL")
        bits, marked = build_availability(slot_times, free_times, tz)

        login = await _request(
            client, "POST", f"{W2M_BASE}/ProcessLogin.php",
            data={"id": eid, "name": name, "password": password},
            headers={"Referer": referer})
        person_id = login.text.strip()
        if not person_id.isdigit():
            raise ValueError(f"Sign-in as {name!r} failed: "
                             f"{person_id or 'empty response'}")

        await _request(
            client, "POST", f"{W2M_BASE}/SaveTimes.php",
            data={
                "person": person_id,
                "event": eid,
                "slots": ",".join(str(t) for t in marked),
                "availability": bits,
                "password": password,
                "ChangeToAvailable": "true",
            },
            headers={"Referer": referer})

    zone = ZoneInfo(tz)
    return {
        "name": name,
        "person_id": person_id,
        "slots_marked": len(marked),
        "times": [datetime.fromtimestamp(t, tz=zone).isoformat() for t in marked],
    }


# ─── MCP server ─────────────────────────────────────────────────────

def _load_mcp_sdk():
    """Return (server class, ToolError) for whichever MCP SDK major is installed."""
    try:  # SDK 2.x
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
        return MCPServer, ToolError
    except ImportError:
        pass
    from mcp.server.fastmcp import FastMCP  # SDK 1.x
    from mcp.server.fastmcp.exceptions import ToolError
    return FastMCP, ToolError


TOOL_ERRORS = (ValueError, RuntimeError, httpx.HTTPError)


def run_mcp_server() -> None:
    try:
        server_cls, tool_error = _load_mcp_sdk()
    except ImportError as exc:
        print(f"MCP SDK import failed: {exc}", file=sys.stderr)
        print("Install with: pip install 'mcp[cli]'  "
              "(or use CLI mode: python server.py --help)", file=sys.stderr)
        sys.exit(1)

    mcp = server_cls("when2meet")

    @mcp.tool(title="Create Availability Poll",
              annotations={"readOnlyHint": False})
    async def create_poll_tool(event_name: str, dates: list[str],
                               earliest_hour: int = 9, latest_hour: int = 18,
                               timezone: str = "Asia/Shanghai",
                               poll_type: str = "specific_dates") -> str:
        """Create a poll. dates are YYYY-MM-DD, or weekday names when
        poll_type="days_of_week". Share the URL, then read results."""
        try:
            return await create_poll(event_name, dates, earliest_hour,
                                     latest_hour, timezone, poll_type)
        except TOOL_ERRORS as exc:
            raise tool_error(str(exc)) from exc

    @mcp.tool(title="Read Poll Results",
              annotations={"readOnlyHint": True})
    async def get_poll_results_tool(poll_url: str, timezone: str = "UTC") -> dict:
        """Read who is free at each 15-min slot, plus poll name/type. Read-only."""
        try:
            return await get_poll_results(poll_url, timezone)
        except TOOL_ERRORS as exc:
            raise tool_error(str(exc)) from exc

    @mcp.tool(title="Find Best Meeting Slot",
              annotations={"readOnlyHint": True})
    async def find_best_slot_tool(poll_url: str,
                                  min_attendees: int = 2,
                                  min_duration_minutes: int = 30,
                                  timezone: str = "UTC",
                                  required: list[str] | None = None,
                                  exclude: list[str] | None = None,
                                  dates: list[str] | None = None,
                                  limit: int | None = None) -> list[dict]:
        """Find best contiguous windows where the same people are free
        throughout. required/exclude/dates/limit narrow the search. Read-only."""
        try:
            return await find_best_slot(poll_url, min_attendees,
                                        min_duration_minutes, timezone,
                                        required, exclude, dates, limit)
        except TOOL_ERRORS as exc:
            raise tool_error(str(exc)) from exc

    @mcp.tool(title="Vote Availability",
              annotations={"readOnlyHint": False})
    async def vote_tool(poll_url: str, name: str,
                        free_times: list[str],
                        password: str = "",
                        timezone: str = "UTC") -> dict:
        """Mark slots as available for *name*. free_times is a list of
        ISO timestamps from get_poll_results. Fully replaces prior votes."""
        try:
            return await vote_on_behalf(poll_url, name, free_times,
                                        password, timezone)
        except TOOL_ERRORS as exc:
            raise tool_error(str(exc)) from exc

    mcp.run()


# ─── CLI ────────────────────────────────────────────────────────────

def run_cli(argv: list[str]) -> None:
    p = argparse.ArgumentParser(description="When2Meet CLI")
    sub = p.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("create", help="create a poll, print its URL")
    pc.add_argument("--name", required=True)
    pc.add_argument("--dates", required=True, nargs="+", metavar="YYYY-MM-DD")
    pc.add_argument("--earliest", type=int, default=9)
    pc.add_argument("--latest", type=int, default=18)
    pc.add_argument("--tz", default="Asia/Shanghai")
    pc.add_argument("--type", dest="poll_type", default="specific_dates",
                    choices=POLL_TYPES,
                    help="days_of_week: --dates are weekday names or 0-6")

    pr = sub.add_parser("results", help="print per-slot availability as JSON")
    pr.add_argument("--url", required=True)
    pr.add_argument("--tz", default="UTC")

    pb = sub.add_parser("best", help="print recommended windows as JSON")
    pb.add_argument("--url", required=True)
    pb.add_argument("--min-people", type=int, default=2)
    pb.add_argument("--min-minutes", type=int, default=30)
    pb.add_argument("--tz", default="UTC")
    pb.add_argument("--require", nargs="+", default=None, metavar="NAME")
    pb.add_argument("--exclude", nargs="+", default=None, metavar="NAME")
    pb.add_argument("--dates", nargs="+", default=None, metavar="DATE")
    pb.add_argument("--limit", type=int, default=None)

    pv = sub.add_parser("vote", help="submit availability for a person")
    pv.add_argument("--url", required=True)
    pv.add_argument("--name", required=True)
    pv.add_argument("--times", required=True, nargs="+",
                    metavar="ISO-TIMESTAMP",
                    help="free slot ISO timestamps (from 'results')")
    pv.add_argument("--password", default="")
    pv.add_argument("--tz", default="UTC")

    args = p.parse_args(argv)
    try:
        if args.command == "create":
            print(asyncio.run(create_poll(
                args.name, args.dates, args.earliest, args.latest, args.tz,
                args.poll_type)))
        elif args.command == "results":
            print(json.dumps(asyncio.run(get_poll_results(args.url, args.tz)),
                             indent=2, ensure_ascii=False))
        elif args.command == "best":
            print(json.dumps(asyncio.run(find_best_slot(
                args.url, args.min_people, args.min_minutes, args.tz,
                args.require, args.exclude, args.dates, args.limit)),
                indent=2, ensure_ascii=False))
        elif args.command == "vote":
            print(json.dumps(asyncio.run(vote_on_behalf(
                args.url, args.name, args.times, args.password, args.tz)),
                indent=2, ensure_ascii=False))
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli(sys.argv[1:])
    else:
        run_mcp_server()
