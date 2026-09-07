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
"""
import re
import sys
import json
import asyncio
import argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

W2M_BASE = "https://www.when2meet.com"
SLOT_MINUTES = 15
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
POLL_ID_RE = re.compile(r"\?(\d+)-([A-Za-z0-9]+)")
HTTP_TIMEOUT = 30.0


# ─── Validation helpers ─────────────────────────────────────────────

def _parse_poll_url(poll_url: str) -> tuple[str, str]:
    """Extract (event_id, code) from a When2Meet URL like https://when2meet.com/?123-ABC."""
    m = POLL_ID_RE.search(poll_url)
    if not m:
        raise ValueError(
            f"Not a When2Meet poll URL (expected '...?<id>-<code>'): {poll_url}")
    return m.group(1), m.group(2)


def _validate_create_args(dates: list[str], earliest_hour: int,
                          latest_hour: int, tz: str) -> None:
    if not dates:
        raise ValueError("dates must contain at least one YYYY-MM-DD date")
    bad = [d for d in dates if not DATE_RE.match(d)]
    if bad:
        raise ValueError(f"dates must be YYYY-MM-DD, got: {bad}")
    for d in dates:
        datetime.strptime(d, "%Y-%m-%d")  # raises on impossible dates
    if not (0 <= earliest_hour <= 23 and 0 <= latest_hour <= 23):
        raise ValueError("earliest_hour and latest_hour must be in 0..23")
    if earliest_hour >= latest_hour:
        raise ValueError("earliest_hour must be before latest_hour")
    ZoneInfo(tz)  # raises ZoneInfoNotFoundError on unknown tz


# ─── Pure parsing (no network; unit-tested) ─────────────────────────

def parse_grid(html: str, tz: str = "UTC") -> dict:
    """Parse the HTML returned by AvailabilityGrids.php into structured data.

    The response carries one ``// hexAvailability: <hex>`` comment per
    participant (same order as ``PeopleNames``). Each hex value encodes that
    person's availability as a bit string in *column-major* slot order
    (slot index = col * rows + row, i.e. the site's ``TimeOfSlot`` order),
    with leading zeros stripped. The grid DOM, by contrast, is emitted in
    row-major order, so the two must be mapped explicitly.
    """
    soup = BeautifulSoup(html, "html.parser")
    zone = ZoneInfo(tz)

    # Date column headers, in column order.
    date_cols: list[str] = []
    for div in soup.select(
            "div[style*='text-align:center;font-size:10px;width:44px']"):
        parts = div.get_text(separator=" ", strip=True).split()
        if len(parts) >= 2:
            date_cols.append(" ".join(parts[:2]))

    # Group-grid slot elements (one per 15-min cell).
    gg = soup.select_one("#GroupGridSlots")
    slot_els = gg.select("[data-time]") if gg else [
        el for el in soup.select("[data-time]")
        if str(el.get("id", "")).startswith("GroupTime")]
    if not slot_els:
        raise RuntimeError("No availability grid found in response "
                           "(wrong poll id/code, or site markup changed)")
    rows = max(int(el["data-row"]) for el in slot_els) + 1
    total_slots = len(slot_els)

    names = re.findall(r"PeopleNames\[\d+\]\s*=\s*'([^']*)'", html)
    hexes = re.findall(r"hexAvailability:\s*([0-9a-fA-F]+)", html)
    masks = [bin(int(h, 16))[2:].zfill(total_slots) for h in hexes]
    if any(len(m) > total_slots for m in masks):
        raise RuntimeError("hexAvailability longer than slot count; "
                           "site encoding may have changed")

    slots = []
    for el in slot_els:
        col, row = int(el["data-col"]), int(el["data-row"])
        k = col * rows + row
        free = [names[p] for p in range(min(len(names), len(masks)))
                if masks[p][k] == "1"]
        dt = datetime.fromtimestamp(int(el["data-time"]), tz=zone)
        slots.append({
            "time": dt.isoformat(),
            "date": date_cols[col] if col < len(date_cols) else dt.strftime("%b %d"),
            "free": free,
        })
    slots.sort(key=lambda s: s["time"])

    return {
        "participants": names,
        "total_participants": len(names),
        "timezone": tz,
        "slot_minutes": SLOT_MINUTES,
        "slots": slots,
    }


def best_windows(results: dict, min_attendees: int = 2,
                 min_duration_minutes: int = 30) -> list[dict]:
    """Find maximal contiguous windows where at least ``min_attendees`` people
    are free for the *whole* window (attendee set is the intersection across
    every slot in the window). Pure function over ``parse_grid`` output.
    """
    if min_attendees < 1:
        raise ValueError("min_attendees must be >= 1")
    if min_duration_minutes < SLOT_MINUTES:
        raise ValueError(f"min_duration_minutes must be >= {SLOT_MINUTES}")
    need = max(1, min_duration_minutes // SLOT_MINUTES)
    step = timedelta(minutes=SLOT_MINUTES)

    by_date: dict[str, list[dict]] = {}
    for s in results["slots"]:
        by_date.setdefault(s["date"], []).append(s)

    found = []
    for date, day in by_date.items():
        day = sorted(day, key=lambda x: x["time"])
        for i, j, common in _candidate_windows(day, min_attendees, step):
            length = j - i + 1
            if length < need:
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
    return found


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
) -> str:
    """Create a new When2Meet availability poll.

    Use this when coordinating a meeting time with multiple people.
    The poll asks participants to mark which 15-minute slots they are
    free on the given candidate dates. After creating the poll, share
    the returned URL with participants, then call get_poll_results or
    find_best_slot to read back availability.

    Args:
        event_name: Title shown on the poll page, e.g. "Weekly sync".
        dates: Candidate dates to poll, in YYYY-MM-DD format.
        earliest_hour: Earliest selectable hour (0-23). Default 9.
        latest_hour: Latest selectable hour (0-23). Default 18.
        tz: IANA timezone string. Default "Asia/Shanghai".

    Returns:
        The shareable When2Meet poll URL.
    """
    if not event_name.strip():
        raise ValueError("event_name must not be empty")
    _validate_create_args(dates, earliest_hour, latest_hour, tz)
    async with httpx.AsyncClient(follow_redirects=False,
                                 timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{W2M_BASE}/SaveNewEvent.php",
            data={
                "NewEventName": event_name,
                "DateTypes": "SpecificDates",
                "PossibleDates": "|".join(dates),
                "NoEarlierThan": str(earliest_hour),
                "NoLaterThan": str(latest_hour),
                "TimeZone": tz,
            },
            headers={"Referer": f"{W2M_BASE}/"},
        )
        resp.raise_for_status()
        m = POLL_ID_RE.search(resp.text)
        if not m:
            raise RuntimeError(f"Failed to create poll: {resp.text[:300]}")
        return f"{W2M_BASE}/?{m.group(1)}-{m.group(2)}"


async def get_poll_results(poll_url: str, tz: str = "UTC") -> dict:
    """Read the current availability grid of an existing poll.

    Use this after participants have submitted their availability to see
    who is free at each 15-minute time slot. Read-only — does not modify
    the poll.

    Args:
        poll_url: The When2Meet poll URL returned by create_poll().
        tz: IANA timezone used to render slot times. Default "UTC".

    Returns:
        A dict with participants list and per-slot availability, slots
        sorted by time.
    """
    eid, code = _parse_poll_url(poll_url)
    ZoneInfo(tz)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{W2M_BASE}/AvailabilityGrids.php",
            data={"id": eid, "code": code, "participantTimeZone": tz},
            headers={"Referer": poll_url},
        )
        resp.raise_for_status()
    return parse_grid(resp.text, tz)


async def find_best_slot(
    poll_url: str,
    min_attendees: int = 2,
    min_duration_minutes: int = 30,
    tz: str = "UTC",
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

    Returns:
        Sorted list of recommended time windows.
    """
    results = await get_poll_results(poll_url, tz)
    return best_windows(results, min_attendees, min_duration_minutes)


# ─── MCP server ─────────────────────────────────────────────────────

def _load_mcp_server_class():
    """Return the server class for whichever MCP SDK major version is installed."""
    try:  # SDK 2.x
        from mcp.server.mcpserver import MCPServer
        return MCPServer
    except ImportError:
        pass
    from mcp.server.fastmcp import FastMCP  # SDK 1.x
    return FastMCP


def run_mcp_server() -> None:
    try:
        server_cls = _load_mcp_server_class()
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
                               timezone: str = "Asia/Shanghai") -> str:
        """Create a poll. Share the URL, then read results."""
        return await create_poll(event_name, dates, earliest_hour,
                                 latest_hour, timezone)

    @mcp.tool(title="Read Poll Results",
              annotations={"readOnlyHint": True})
    async def get_poll_results_tool(poll_url: str, timezone: str = "UTC") -> dict:
        """Read who is free at each 15-min slot. Read-only."""
        return await get_poll_results(poll_url, timezone)

    @mcp.tool(title="Find Best Meeting Slot",
              annotations={"readOnlyHint": True})
    async def find_best_slot_tool(poll_url: str,
                                  min_attendees: int = 2,
                                  min_duration_minutes: int = 30,
                                  timezone: str = "UTC") -> list[dict]:
        """Find best contiguous windows. Sorted by attendees. Read-only."""
        return await find_best_slot(poll_url, min_attendees,
                                    min_duration_minutes, timezone)

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

    pr = sub.add_parser("results", help="print per-slot availability as JSON")
    pr.add_argument("--url", required=True)
    pr.add_argument("--tz", default="UTC")

    pb = sub.add_parser("best", help="print recommended windows as JSON")
    pb.add_argument("--url", required=True)
    pb.add_argument("--min-people", type=int, default=2)
    pb.add_argument("--min-minutes", type=int, default=30)
    pb.add_argument("--tz", default="UTC")

    args = p.parse_args(argv)
    try:
        if args.command == "create":
            print(asyncio.run(create_poll(
                args.name, args.dates, args.earliest, args.latest, args.tz)))
        elif args.command == "results":
            print(json.dumps(asyncio.run(get_poll_results(args.url, args.tz)),
                             indent=2, ensure_ascii=False))
        elif args.command == "best":
            print(json.dumps(asyncio.run(find_best_slot(
                args.url, args.min_people, args.min_minutes, args.tz)),
                indent=2, ensure_ascii=False))
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli(sys.argv[1:])
    else:
        run_mcp_server()
