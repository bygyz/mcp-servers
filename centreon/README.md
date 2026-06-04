# trianova-mcp-centreon

MCP server for **Centreon**. Exposes Centreon monitoring data and actions as tools that AI agents can call during incident triage.

Built by [Trianova](https://trianova.io) — AI-powered IT ops for SMBs and MSPs.

## Tools

| Tool | Description |
|------|-------------|
| `get_centreon_summary` | Overall health: hosts and services counts by status |
| `list_alerts` | Active alerts — non-OK hosts and services, filterable by severity |
| `get_host_status` | Full status of a specific host with all its services |
| `list_scheduled_downtimes` | Active maintenance windows — check before escalating |
| `acknowledge_host` | Acknowledge a host problem to suppress repeat notifications |
| `schedule_downtime` | Schedule a maintenance window before planned remediation |

## Installation

```bash
pip install trianova-mcp-centreon
```

## Configuration

```bash
CENTREON_HOST=https://centreon.yourdomain.com
CENTREON_USERNAME=trianova-api
CENTREON_PASSWORD=your-password
CENTREON_VERIFY_SSL=true   # Set to false for self-signed certs
MCP_TRANSPORT=stdio        # stdio (default) or sse
```

Create a dedicated read-only API user in Centreon: **Configuration → Users → Add** with role "API Access".
For `acknowledge_host` and `schedule_downtime`, the user needs write access.

## Usage

### With Claude Desktop

```json
{
  "mcpServers": {
    "centreon": {
      "command": "trianova-mcp-centreon",
      "env": {
        "CENTREON_HOST": "https://centreon.yourdomain.com",
        "CENTREON_USERNAME": "trianova-api",
        "CENTREON_PASSWORD": "your-password"
      }
    }
  }
}
```

### With Trianova (SSE mode)

```bash
MCP_TRANSPORT=sse CENTREON_HOST=https://centreon.yourdomain.com \
  CENTREON_USERNAME=trianova-api CENTREON_PASSWORD=xxx trianova-mcp-centreon
```

Then register in Trianova: **Settings → MCP Servers → Add → URL: http://localhost:8080/sse**

## Example triage flow

Alert received: `web-prod-01 — HTTP service CRITICAL`

1. `list_scheduled_downtimes()` — is there a planned maintenance? (avoid false escalation)
2. `get_host_status("web-prod-01")` — is the host itself up? Which other services are affected?
3. `list_alerts(severity="critical")` — is this isolated or part of a wider outage?

If the host is up but the HTTP service is down → Claude recommends `restart_service nginx` (P3).
If the host itself is down → Claude escalates P1 with `requires_human: true`.

## License

MIT — use it, fork it, contribute back.
