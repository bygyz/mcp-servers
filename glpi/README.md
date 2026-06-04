# trianova-mcp-glpi

MCP server for **GLPI** ITSM and CMDB. Exposes GLPI ticketing and asset data as tools that AI agents can call during incident triage and auto-remediation.

Built by [Trianova](https://trianova.io) — AI-powered IT ops for SMBs and MSPs.

## Tools

**Tickets**

| Tool | Description |
|------|-------------|
| `search_tickets` | Search tickets by hostname or status — detect duplicates before creating |
| `get_ticket` | Full ticket details including followups |
| `create_ticket` | Create a new incident or request ticket |
| `update_ticket` | Update status, add solution, or add a followup comment |
| `assign_ticket` | Assign a ticket to a user and/or group — route to the right technician |

**Problems (ITIL)**

| Tool | Description |
|------|-------------|
| `list_problems` | List problems — check if a recurring incident is a known problem |
| `get_problem` | Full problem details — root cause, workarounds, followups |
| `create_problem` | Create a problem when multiple incidents share the same root cause |
| `update_problem` | Advance status or add followup notes |

**Changes (ITIL)**

| Tool | Description |
|------|-------------|
| `list_changes` | List changes — check for planned work before remediating |
| `get_change` | Full change details — approval status, planned dates, steps |
| `create_change` | Create a change request to formally track a planned remediation |
| `update_change` | Advance status (e.g. Applied after auto-remediation) or add followup |

**Assets & CMDB**

| Tool | Description |
|------|-------------|
| `get_asset` | Look up a server/computer in the CMDB — hardware, OS, warranty, location |
| `search_assets_by_ip` | Look up a CMDB asset by IP address — for alerts that carry an IP not a hostname |
| `search_recent_changes` | Recent changes recorded on a host — correlate incidents with changes |
| `list_categories` | List ticket categories — find the right category_id before creating a ticket |

## Installation

```bash
pip install trianova-mcp-glpi
```

## Configuration

```bash
GLPI_HOST=https://glpi.yourdomain.com
GLPI_APP_TOKEN=your-app-token        # Generated in GLPI: Setup → General → API

# Auth — user token (preferred)
GLPI_USER_TOKEN=your-user-token      # Generated in GLPI: user profile → API token

# Auth — username/password (fallback if no user token)
GLPI_USERNAME=your-username
GLPI_PASSWORD=your-password

GLPI_VERIFY_SSL=true                 # Set to false for self-signed certs
MCP_TRANSPORT=stdio                  # stdio (default) or sse
```

To enable the GLPI API: **Setup → General → API → Enable REST API → Add API client**.

## Usage

### With any MCP client

```json
{
  "mcpServers": {
    "glpi": {
      "command": "trianova-mcp-glpi",
      "env": {
        "GLPI_HOST": "https://glpi.yourdomain.com",
        "GLPI_APP_TOKEN": "your-app-token",
        "GLPI_USER_TOKEN": "your-user-token"
      }
    }
  }
}
```

### With Trianova (SSE mode)

```bash
MCP_TRANSPORT=sse GLPI_HOST=https://glpi.yourdomain.com \
  GLPI_APP_TOKEN=xxx GLPI_USER_TOKEN=yyy trianova-mcp-glpi
```

Then register in Trianova: **Settings → MCP Servers → Add → URL: http://localhost:8080/sse**

## Example triage flows

### Incident deduplication
Alert: `db-prod-02 — disk 94%`

1. `search_tickets(hostname="db-prod-02", status="new")` → finds ticket #4821 opened 2 days ago for the same issue
2. The agent detects a recurring problem, not an isolated spike → escalates P2 instead of auto-resolving P3
3. `update_ticket(4821, followup="New alert: disk at 94%. Auto-remediation running.")` → keeps GLPI in sync

### Root cause via CMDB
Alert: `app-srv-03 — memory at 98%`

1. `get_asset("app-srv-03")` → 8GB RAM, last updated 3 months ago
2. `search_recent_changes("app-srv-03", days=7)` → finds a software deployment 6 hours ago
3. The agent correlates the memory spike with the recent deployment → `next_step` includes rollback recommendation

### Post-remediation sync
After Trianova auto-resolves an incident via SSH:

1. `create_ticket(title="[Auto-resolved] nginx restart on web-01", priority=3)` → creates audit trail
2. `update_ticket(id, status=5, solution="Trianova restarted nginx via SSH. Service restored in 8s.")` → marks solved

## License

MIT — use it, fork it, contribute back.
