# When2Meet MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) server for [When2Meet](https://when2meet.com), enabling AI agents to create polls, read availability results, and find optimal meeting times programmatically.

## Features

- **Create polls** — spin up a When2Meet poll in one API call
- **Read results** — get structured per-15-min availability data
- **Find best slots** — automatically find contiguous windows where enough people are free

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
```

## Tools

| Tool | Description |
|------|-------------|
| `create_poll` | Create a new availability poll. Returns the shareable URL. |
| `get_poll_results` | Read who is free at each 15-min slot, sorted by time. Optional `timezone`. Read-only. |
| `find_best_slot` | Maximal contiguous windows where the same people are free for the whole window (attendees = intersection). Sorted by attendee count, then duration. Read-only. |

## How It Works

This server is a thin Python wrapper around When2Meet's existing HTTP endpoints. No backend changes required — it speaks the same protocol a browser would.

| Endpoint | Purpose |
|----------|---------|
| `POST /SaveNewEvent.php` | Create a new poll |
| `POST /AvailabilityGrids.php` | Fetch availability grid (one hex bitmask per participant, column-major slot order) |

## Tests

Offline tests run against a recorded `AvailabilityGrids.php` response whose
ground truth was verified on the live poll page:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## License

MIT
