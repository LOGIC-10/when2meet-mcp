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
import re, asyncio, json, argparse
from datetime import datetime, timezone
import httpx
from bs4 import BeautifulSoup

W2M_BASE = "https://www.when2meet.com"


# ─── Core functions ─────────────────────────────────────────────────

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
    async with httpx.AsyncClient(follow_redirects=False) as client:
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
        m = re.search(r"\?(\d+-[A-Za-z0-9]+)", resp.text)
        if not m:
            raise RuntimeError(f"Failed to create poll: {resp.text[:300]}")
        return f"{W2M_BASE}/?{m.group(1)}"


async def get_poll_results(poll_url: str) -> dict:
    """Read the current availability grid of an existing poll.

    Use this after participants have submitted their availability to see
    who is free at each 15-minute time slot. Read-only — does not modify
    the poll.

    Args:
        poll_url: The When2Meet poll URL returned by create_poll().

    Returns:
        A dict with participants list and per-slot availability.
    """
    eid, code = poll_url.split("?")[1].split("-")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{W2M_BASE}/AvailabilityGrids.php",
            data={"id": eid, "code": code, "participantTimeZone": "UTC"},
            headers={"Referer": poll_url},
        )

    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    # Date column headers
    date_cols = {}
    for div in soup.select(
        "div[style*='text-align:center;font-size:10px;width:44px']"
    ):
        parts = div.get_text(separator=" ", strip=True).split()
        if len(parts) >= 2:
            date_cols[len(date_cols)] = " ".join(parts[:2])

    # Time slots from GroupGrid (matches hexAvailability bit order)
    slots = []
    gg = soup.select_one("#GroupGridSlots") or soup.select(".GroupGrid")
    slot_els = gg.select("[data-time]") if gg else soup.select("[data-time]")
    for el in slot_els:
        dt = datetime.fromtimestamp(int(el["data-time"]), tz=timezone.utc)
        slots.append({
            "time": dt.isoformat(),
            "date": date_cols.get(int(el["data-col"]), "?"),
        })

    # Participants + hex bitmask
    names = re.findall(r"PeopleNames\[\d+\]\s*=\s*'([^']+)'", html)
    hm = re.search(r"hexAvailability:\s*([0-9a-fA-F]+)", html)
    bits = bin(int(hm.group(1), 16))[2:].zfill(len(hm.group(1)) * 4) if hm else ""

    result = []
    for i, s in enumerate(slots):
        free = [names[p] for p in range(len(names))
                if p * len(slots) + i < len(bits)
                and bits[p * len(slots) + i] == "1"]
        result.append({"time": s["time"], "date": s["date"], "free": free})

    return {
        "participants": names,
        "total_participants": len(names),
        "slots": result,
    }


async def find_best_slot(
    poll_url: str,
    min_attendees: int = 2,
    min_duration_minutes: int = 30,
) -> list[dict]:
    """Find the best contiguous meeting time slots from a poll.

    Use this after participants have voted to automatically identify time
    windows where enough people are available. Returns sorted recommendations
    — slots with the most available attendees first. Read-only.

    Args:
        poll_url: The When2Meet poll URL.
        min_attendees: Minimum people required. Default 2.
        min_duration_minutes: Minimum meeting length. Default 30 (multiple of 15).

    Returns:
        Sorted list of recommended time windows.
    """
    results = await get_poll_results(poll_url)
    n = min_duration_minutes // 15
    best, by_date = [], {}

    for s in results["slots"]:
        by_date.setdefault(s["date"], []).append(s)

    for date, day in by_date.items():
        day.sort(key=lambda x: x["time"])
        rs = rc = 0
        for i, s in enumerate(day):
            if len(s["free"]) >= min_attendees:
                rc += 1
            else:
                if rc >= n:
                    best.append({
                        "date": date,
                        "start": day[rs]["time"],
                        "end": day[rs + n - 1]["time"],
                        "attendees": day[rs]["free"],
                    })
                rs = i + 1
                rc = 0
        if rc >= n:
            best.append({
                "date": date,
                "start": day[rs]["time"],
                "end": day[rs + n - 1]["time"],
                "attendees": day[rs]["free"],
            })

    best.sort(key=lambda x: -len(x["attendees"]))
    return best


# ─── Entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    # Try MCP server mode first, fall back to CLI
    import sys

    if len(sys.argv) > 1:
        # CLI mode
        p = argparse.ArgumentParser(description="When2Meet CLI")
        sub = p.add_subparsers(dest="command")

        pc = sub.add_parser("create")
        pc.add_argument("--name", required=True)
        pc.add_argument("--dates", required=True, nargs="+")
        pc.add_argument("--earliest", type=int, default=9)
        pc.add_argument("--latest", type=int, default=18)
        pc.add_argument("--tz", default="Asia/Shanghai")

        pr = sub.add_parser("results")
        pr.add_argument("--url", required=True)

        pb = sub.add_parser("best")
        pb.add_argument("--url", required=True)
        pb.add_argument("--min-people", type=int, default=2)
        pb.add_argument("--min-minutes", type=int, default=30)

        args = p.parse_args()
        if args.command == "create":
            print(asyncio.run(create_poll(
                args.name, args.dates, args.earliest, args.latest, args.tz)))
        elif args.command == "results":
            print(json.dumps(asyncio.run(get_poll_results(args.url)),
                  indent=2, ensure_ascii=False))
        elif args.command == "best":
            print(json.dumps(asyncio.run(
                find_best_slot(args.url, args.min_people, args.min_minutes)),
                  indent=2, ensure_ascii=False))
    else:
        # MCP server mode
        try:
            from mcp.server.fastmcp import FastMCP

            mcp = FastMCP("when2meet")

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
            async def get_poll_results_tool(poll_url: str) -> dict:
                """Read who is free at each 15-min slot. Read-only."""
                return await get_poll_results(poll_url)

            @mcp.tool(title="Find Best Meeting Slot",
                      annotations={"readOnlyHint": True})
            async def find_best_slot_tool(poll_url: str,
                    min_attendees: int = 2,
                    min_duration_minutes: int = 30) -> list[dict]:
                """Find best contiguous slots. Sorted by attendees. Read-only."""
                return await find_best_slot(poll_url, min_attendees,
                                            min_duration_minutes)

            mcp.run()
        except ImportError:
            print("MCP SDK not installed. Install with: pip install 'mcp[cli]'")
            print("Or use CLI mode: python server.py --help")
            sys.exit(1)
