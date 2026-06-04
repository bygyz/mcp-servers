# trianova-mcp-prtg

MCP server for **PRTG Network Monitor**. Exposes PRTG monitoring data as tools that AI agents can call during incident triage.

Built by [Trianova](https://trianova.io) — AI-powered IT ops for SMBs and MSPs.

## Tools

| Tool | Description |
|------|-------------|
| `get_prtg_summary` | Overall health: total sensors, down, warning, paused counts |
| `list_alerts` | Active alerts — down and warning sensors across the instance |
| `get_device_status` | All sensors for a specific host with current status and last value |
| `get_sensor_details` | Full details for a specific sensor — type, thresholds, last error, uptime |
| `get_channels` | Channel values and configured limits — see exactly which threshold was breached |
| `get_sensor_history` | Historical data for a sensor over the last N hours |
| `get_messages` | System log messages for a device — config changes, errors, probe events |
| `list_scheduled_downtimes` | Active maintenance windows — check before escalating |
| `acknowledge_alert` | Acknowledge an alert to suppress repeat notifications |
| `pause_sensor` | Pause a sensor for a given duration during remediation |

## Installation

```bash
pip install trianova-mcp-prtg
```

## Configuration

Set the following environment variables:

```bash
# Required
PRTG_HOST=https://prtg.yourdomain.com

# Auth — API token (preferred)
PRTG_API_TOKEN=your-api-token

# Auth — username/passhash (alternative)
PRTG_USERNAME=admin
PRTG_PASSHASH=your-passhash

# Optional
PRTG_VERIFY_SSL=true      # Set to false for self-signed certs
PRTG_READ_ONLY=false      # Set to true to block acknowledge_alert and pause_sensor
MCP_TRANSPORT=stdio       # stdio (default) or sse
```

To generate a PRTG passhash: PRTG web UI → Account Settings → My Account → passhash.

## Usage

### With any MCP client

Add to your MCP client config:

```json
{
  "mcpServers": {
    "prtg": {
      "command": "trianova-mcp-prtg",
      "env": {
        "PRTG_HOST": "https://prtg.yourdomain.com",
        "PRTG_API_TOKEN": "your-api-token"
      }
    }
  }
}
```

### With Trianova (SSE mode)

```bash
MCP_TRANSPORT=sse PRTG_HOST=https://prtg.yourdomain.com PRTG_API_TOKEN=xxx trianova-mcp-prtg
```

Then register the SSE endpoint in Trianova: **Settings → MCP Servers → Add → URL: http://localhost:8080/sse**

### Standalone

```bash
PRTG_HOST=https://prtg.yourdomain.com PRTG_API_TOKEN=xxx trianova-mcp-prtg
```

## Example triage flow

When Trianova receives an alert for `switch-core-01`, the triage agent can:

1. `get_prtg_summary()` — check overall health (is this an isolated issue or widespread?)
2. `list_scheduled_downtimes()` — is there a maintenance window active? (don't page on-call)
3. `get_device_status("switch-core-01")` — get all sensor statuses for the device
4. `get_sensor_history(sensor_id=1042, hours=4)` — was bandwidth climbing before the alert?

Result: the triage agent classifies the incident with full context instead of just the raw alert message.

## License

MIT — use it, fork it, contribute back.
