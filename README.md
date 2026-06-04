# Trianova MCP Servers

Open-source MCP (Model Context Protocol) servers for IT monitoring and ITSM tools.
Built by [Trianova](https://trianova.io) to connect AI triage agents to the tools IT teams already use.

## Available servers

| Package | Tool | Category | PyPI |
|---------|------|----------|------|
| [`trianova-mcp-prtg`](./prtg/) | PRTG Network Monitor | Monitoring | `pip install trianova-mcp-prtg` |
| [`trianova-mcp-centreon`](./centreon/) | Centreon | Monitoring | `pip install trianova-mcp-centreon` |
| [`trianova-mcp-glpi`](./glpi/) | GLPI ITSM/CMDB | Ticketing + Assets | `pip install trianova-mcp-glpi` |

## Why these servers exist

Most IT monitoring tools (PRTG, Centreon) and ITSM platforms (GLPI) don't have MCP servers yet.
Without them, AI agents triage incidents with only the raw alert — no historical context, no CMDB data,
no awareness of scheduled maintenance windows.

These servers give AI agents the same context a senior engineer has when they open their laptop at 2am.

## Quick start with Claude Desktop

```json
{
  "mcpServers": {
    "prtg": {
      "command": "trianova-mcp-prtg",
      "env": { "PRTG_HOST": "https://prtg.corp.local", "PRTG_API_TOKEN": "xxx" }
    },
    "centreon": {
      "command": "trianova-mcp-centreon",
      "env": { "CENTREON_HOST": "https://centreon.corp.local", "CENTREON_USERNAME": "api", "CENTREON_PASSWORD": "xxx" }
    },
    "glpi": {
      "command": "trianova-mcp-glpi",
      "env": { "GLPI_HOST": "https://glpi.corp.local", "GLPI_APP_TOKEN": "xxx", "GLPI_USER_TOKEN": "yyy" }
    }
  }
}
```

## Use with Trianova

These servers are natively supported by [Trianova](https://trianova.io).
Register them in **Settings → MCP Servers** and Claude will automatically
use them during incident triage.

## Contributing

PRs welcome — bug fixes, new tools, new servers.
Open an issue before submitting large changes.

## License

MIT
