# When2Meet MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for [When2Meet](https://when2meet.com), enabling AI agents to create polls, read availability results, and find optimal meeting times programmatically.

## Features

- **Create polls** — specific dates or days-of-the-week, in one API call
- **Read results** — get structured per-15-min availability data
- **Find best slots** — contiguous windows where the same people are free, with required/excluded people, date filters and a result limit
- **Vote on behalf** — submit availability for a named participant programmatically

## Quick Start

### Install

```bash
git clone https://github.com/LOGIC-10/when2meet-mcp.git
cd when2meet-mcp
pip install -r requirements.txt   # httpx + mcp[cli]; works with MCP SDK 1.x and 2.x
```

### Run as MCP server

```bash
python server.py
```

Configure in your MCP client (Claude Desktop, Cursor, etc.):

```json
{
  "mcpServers": {
    "when2meet": {
      "command": "python",
      "args": ["/path/to/server.py"]
    }
  }
}
```

### CLI usage (without MCP client)

```bash
# Create a poll (specific dates)
python server.py create --name "Weekly sync" --dates 2026-09-10 2026-09-11 2026-09-12

# Create a days-of-the-week poll
python server.py create --name "Office hours" --dates Monday Wednesday Friday --type days_of_week

# Read results (times rendered in --tz, default UTC)
python server.py results --url "https://when2meet.com/?12345-ABCDE" --tz Asia/Shanghai

# Find best slots (optionally: --require NAME.. --exclude NAME.. --dates DATE.. --limit N)
python server.py best --url "https://when2meet.com/?12345-ABCDE" --min-people 2 --min-minutes 30 --require Alice --limit 3

# Vote on behalf of a person (times must be 15-min slots of the poll; any ISO form)
python server.py vote --url "https://when2meet.com/?12345-ABCDE" --name Alice \
    --times 2026-09-10T09:00+08:00 2026-09-10T09:15+08:00 --tz Asia/Shanghai
```

## Tools

| Tool | Description |
|------|-------------|
| `create_poll` | Create a poll. `poll_type` is `specific_dates` (dates `YYYY-MM-DD`) or `days_of_week` (weekday names or 0–6, 0 = Sunday). `latest_hour` 0 or 24 means midnight. Returns the shareable URL. |
| `get_poll_results` | Read event name, poll type, the poll's own timezone, its dates and hour range, participants (with ids) and who is free at each 15-min slot, sorted by time; `date` is an ISO date (weekday name for days-of-the-week polls). Optional `timezone`. Read-only. |
| `find_best_slot` | Maximal contiguous windows where the same people are free for the whole window (attendees = intersection). Optional `required`, `exclude`, `dates`, `limit`. Sorted by attendee count, then duration. Read-only. |
| `vote` | Submit availability on behalf of a named participant (created on first use; `password` is that participant's own). Times are ISO 8601 in any form; each must be a slot of the poll or the call is rejected. Fully replaces prior votes; an empty list clears them. |

## How It Works

This server is a thin Python wrapper around When2Meet's existing HTTP endpoints. No backend changes required — it speaks the same protocol a browser would.

| Endpoint | Purpose |
|----------|---------|
| `POST /SaveNewEvent.php` | Create a new poll |
| `GET /?<id>-<code>` | Poll page: `TimeOfSlot[]`, `PeopleNames[]`/`PeopleIDs[]`, `AvailableAtSlot[i].push(id)` — availability keyed by participant id |
| `POST /ProcessLogin.php` | Sign in / create a participant (returns the numeric id, or "Wrong password.") |
| `POST /SaveTimes.php` | Save availability: the site stores the full `availability` bit string (one char per `TimeOfSlot`), replacing prior state |

`AvailabilityGrids.php` is deliberately **not** used for reading: its `hexAvailability` comments are emitted in participant-creation order and only for people who have saved availability, while its `PeopleNames` are alphabetised, so the two cannot be paired reliably once names and creation order differ.

Every request is retried twice on transport errors and 5xx responses (0.5 s, then 1.5 s back-off); 4xx and site-level errors are reported immediately.

## Tests

Offline tests run against six recorded poll pages (two people; six people
whose alphabetical order differs from creation order; a creation-order probe;
a poll spanning the US DST change with 280 slots; two days-of-the-week polls)
plus an `httpx.MockTransport` for the create/vote/retry paths. Expected values
are what was submitted to the site, not what the parser read back:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The tool wrappers work with both MCP SDK 1.x and 2.x and surface
validation/HTTP errors as tool errors.

## License

MIT
