# When2Meet MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for [When2Meet](https://when2meet.com), enabling AI agents to create polls, read availability results, and find optimal meeting times programmatically.

## Features

- **Create polls** — spin up a When2Meet poll in one API call
- **Read results** — get structured per-15-min availability data
- **Find best slots** — automatically find contiguous windows where enough people are free
- **Vote on behalf** — submit availability for a named participant programmatically

## Quick Start

### Install

```bash
git clone https://github.com/LOGIC-10/when2meet-mcp.git
cd when2meet-mcp
pip install -r requirements.txt   # works with MCP SDK 1.x and 2.x
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
# Create a poll
python server.py create --name "Weekly sync" --dates 2026-09-10 2026-09-11 2026-09-12

# Read results (times rendered in --tz, default UTC)
python server.py results --url "https://when2meet.com/?12345-ABCDE" --tz Asia/Shanghai

# Find best slots
python server.py best --url "https://when2meet.com/?12345-ABCDE" --min-people 2 --min-minutes 30

# Vote availability for a person (pass ISO timestamps from 'results')
python server.py vote --url "https://when2meet.com/?12345-ABCDE" --name Alice   --times 2026-09-10T09:00:00+08:00 2026-09-10T09:15:00+08:00 --tz Asia/Shanghai
```

## Tools

| Tool | Description |
|------|-------------|
| `create_poll` | Create a new availability poll. `latest_hour` 0 or 24 means midnight. Returns the shareable URL. |
| `get_poll_results` | Read who is free at each 15-min slot, sorted by time. Optional `timezone`. Read-only. |
| `find_best_slot` | Maximal contiguous windows where the same people are free for the whole window (attendees = intersection). Sorted by attendee count, then duration. Read-only. |
| `vote` | Submit availability on behalf of a named participant. Pass ISO timestamps from `get_poll_results`. Fully replaces prior votes. |

## How It Works

This server is a thin Python wrapper around When2Meet's existing HTTP endpoints. No backend changes required — it speaks the same protocol a browser would.

| Endpoint | Purpose |
|----------|---------|
| `POST /SaveNewEvent.php` | Create a new poll |
| `POST /AvailabilityGrids.php` | Fetch availability grid (one hex bitmask per participant, column-major slot order) |
| `POST /ProcessLogin.php` | Register / identify a participant |
| `POST /SaveTimes.php` | Submit availability (toggle slots by TimeOfSlot index) |

## Tests

Offline tests run against a recorded `AvailabilityGrids.php` response whose
ground truth was verified on the live poll page:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The fixtures cover a 2-person/2-day poll and a 4-person/3-day poll with
non-ASCII and apostrophe names; the tool wrappers work with both MCP SDK 1.x
and 2.x and surface validation/HTTP errors as tool errors.

## License

MIT
