# trianova-mcp-zabbix

MCP server for **Zabbix** (5.x / 6.x / 7.x). Exposes Zabbix monitoring data and actions as tools that AI triage agents can call during incident response.

Built by [Trianova](https://trianova.io) — AI-powered IT ops for SMBs and MSPs.

## Tools

| Tool | Description |
|------|-------------|
| `list_active_problems` | Active problems filtered by severity and/or hostname |
| `get_host_status` | Host availability, active triggers, and interface status |
| `get_item_history` | Historical values for a metric — see how it evolved before the alert |
| `get_recent_events` | Event timeline for a host — when did things start going wrong? |
| `acknowledge_problem` | Acknowledge (and optionally close) a problem event |
| `list_maintenance_windows` | Active and upcoming maintenance windows — check before escalating |
| `create_maintenance_window` | Create a maintenance window before running remediation |
| `get_host_inventory` | CMDB-style inventory data — OS, hardware, location, contact |

## Installation

```bash
pip install trianova-mcp-zabbix
```

## Configuration

```bash
ZABBIX_HOST=https://zabbix.yourdomain.com

# Auth — API token (Zabbix 5.4+, preferred)
ZABBIX_API_TOKEN=your-api-token      # Zabbix UI: User → API tokens

# Auth — username/password (all versions)
ZABBIX_USER=your-username
ZABBIX_PASSWORD=your-password

ZABBIX_VERIFY_SSL=true               # Set to false for self-signed certs
ZABBIX_READ_ONLY=false               # Set to true to block all write operations
MCP_TRANSPORT=stdio                  # stdio (default) or sse
```

To create a Zabbix API token: **User menu → API tokens → Create API token**.

## Usage

### With any MCP client

```json
{
  "mcpServers": {
    "zabbix": {
      "command": "trianova-mcp-zabbix",
      "env": {
        "ZABBIX_HOST": "https://zabbix.yourdomain.com",
        "ZABBIX_API_TOKEN": "your-api-token"
      }
    }
  }
}
```

### With Trianova (SSE mode)

```bash
MCP_TRANSPORT=sse ZABBIX_HOST=https://zabbix.yourdomain.com \
  ZABBIX_API_TOKEN=xxx trianova-mcp-zabbix
```

Then register in Trianova: **Settings → MCP Servers → Add → URL: http://localhost:8080/sse**

## Example triage flows

### Alert context enrichment
Alert: `web-prod-01 — CPU utilisation > 90%`

1. `list_maintenance_windows()` — is this host in scheduled maintenance? (avoid false escalation)
2. `get_host_status("web-prod-01")` — host reachable? other active triggers?
3. `get_item_history("web-prod-01", "system.cpu.util", hours=3)` — steady climb or sudden spike?
4. `get_recent_events("web-prod-01", hours=6)` — were there earlier warnings?

Result: the agent classifies the incident with full context — not just the raw alert.

### Pre-remediation workflow
Before Trianova runs SSH auto-remediation on `db-prod-02`:

1. `create_maintenance_window("db-prod-02", duration_minutes=10)` — suppress alerts during fix
2. Trianova executes `purge_disk` via SSH
3. `acknowledge_problem(event_id, message="Auto-resolved: disk purged via SSH", close=True)` — close the problem

### MSP multi-client monitoring
```python
# For each client host with a disk alert:
list_active_problems(min_severity=3, hostname="client-fileserver-01")
get_host_inventory("client-fileserver-01")  # location, hardware — know which client is affected
```

## License

MIT — use it, fork it, contribute back.
