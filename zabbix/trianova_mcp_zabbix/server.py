"""
Zabbix — MCP Server
Exposes Zabbix monitoring data and actions as MCP tools for AI triage agents.
Supports Zabbix 5.x / 6.x / 7.x via JSON-RPC API.
"""

import os
import time
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Zabbix")

ZABBIX_HOST = os.environ.get("ZABBIX_HOST", "")
ZABBIX_API_TOKEN = os.environ.get("ZABBIX_API_TOKEN", "")  # Zabbix 5.4+ API token
ZABBIX_USER = os.environ.get("ZABBIX_USER", "")
ZABBIX_PASSWORD = os.environ.get("ZABBIX_PASSWORD", "")
ZABBIX_VERIFY_SSL = os.environ.get("ZABBIX_VERIFY_SSL", "true").lower() == "true"
ZABBIX_READ_ONLY = os.environ.get("ZABBIX_READ_ONLY", "false").lower() == "true"

_auth_cache: dict[str, tuple[str, float]] = {}  # host → (token, expires_at)
_SESSION_TTL = 3600  # re-authenticate after 1h


def _api_url() -> str:
    host = ZABBIX_HOST.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"{host}/api_jsonrpc.php"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=ZABBIX_VERIFY_SSL, timeout=15.0)


async def _get_auth_token() -> str:
    """Return a valid Zabbix auth token, re-authenticating when expired."""
    if ZABBIX_API_TOKEN:
        return ZABBIX_API_TOKEN

    now = time.time()
    cached = _auth_cache.get(ZABBIX_HOST)
    if cached and cached[1] > now:
        return cached[0]

    if not ZABBIX_USER or not ZABBIX_PASSWORD:
        raise RuntimeError(
            "No Zabbix credentials. Set ZABBIX_API_TOKEN or ZABBIX_USER + ZABBIX_PASSWORD."
        )

    async with _client() as client:
        resp = await client.post(
            _api_url(),
            json={
                "jsonrpc": "2.0",
                "method": "user.login",
                "params": {"username": ZABBIX_USER, "password": ZABBIX_PASSWORD},
                "id": 1,
            },
        )
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"Zabbix auth failed: {result['error']['data']}")
        token = result["result"]

    _auth_cache[ZABBIX_HOST] = (token, now + _SESSION_TTL)
    return token


async def _call(method: str, params: dict) -> object:
    """Execute a Zabbix API call, retrying once on auth errors."""
    token = await _get_auth_token()
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}

    # Zabbix 5.4+ API tokens go in Bearer header; session tokens go in payload
    headers = {}
    if ZABBIX_API_TOKEN:
        headers["Authorization"] = f"Bearer {ZABBIX_API_TOKEN}"
        # Do not include auth in payload for API token auth
    else:
        payload["auth"] = token

    async with _client() as client:
        resp = await client.post(_api_url(), json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            error_data = data["error"].get("data", "")
            # Session expired — invalidate cache and retry once
            if "Not authorised" in str(error_data) or "re-login" in str(error_data):
                _auth_cache.pop(ZABBIX_HOST, None)
                token = await _get_auth_token()
                payload["auth"] = token
                resp = await client.post(_api_url(), json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise RuntimeError(f"Zabbix API error: {data['error']}")
            else:
                raise RuntimeError(f"Zabbix API error: {data['error']}")

    return data["result"]


# Zabbix severity levels
_SEVERITY = {
    0: "Not classified",
    1: "Information",
    2: "Warning",
    3: "Average",
    4: "High",
    5: "Disaster",
}

# Zabbix problem status
_PROBLEM_STATUS = {0: "Problem", 1: "Resolved"}

# Zabbix trigger states
_TRIGGER_STATE = {0: "Normal", 1: "Unknown"}
_TRIGGER_VALUE = {0: "OK", 1: "PROBLEM"}


@mcp.tool()
async def list_active_problems(
    min_severity: int = 2,
    hostname: Optional[str] = None,
    limit: int = 30,
) -> str:
    """
    List active problems (triggered alerts) in Zabbix.
    min_severity: 0=Not classified, 1=Info, 2=Warning, 3=Average, 4=High, 5=Disaster
    Use this to get the current alert landscape before triaging an incident,
    or to check if a host has other active problems beyond the one reported.
    """
    params: dict = {
        "output": ["eventid", "name", "severity", "clock", "acknowledged", "hosts"],
        "selectHosts": ["host", "name"],
        "severities": list(range(min_severity, 6)),
        "suppressed": False,
        "recent": False,
        "sortfield": ["severity", "clock"],
        "sortorder": "DESC",
        "limit": limit,
    }
    if hostname:
        # Filter by hostname — need host ID first
        hosts = await _call("host.get", {
            "output": ["hostid"],
            "filter": {"host": [hostname]},
        })
        if not hosts:
            return f"Host '{hostname}' not found in Zabbix."
        params["hostids"] = [h["hostid"] for h in hosts]

    problems = await _call("problem.get", params)

    if not problems:
        return f"No active problems (severity ≥ {_SEVERITY.get(min_severity, min_severity)}) in Zabbix."

    lines = [f"Active problems ({len(problems)}):"]
    for p in problems:
        sev = int(p.get("severity", 0))
        host_name = p.get("hosts", [{}])[0].get("name", "?") if p.get("hosts") else "?"
        ack = "✓ ack" if p.get("acknowledged") == "1" else "unacked"
        ts = p.get("clock", "")
        lines.append(
            f"  [{_SEVERITY.get(sev, sev)}] {host_name} — {p.get('name', 'N/A')} "
            f"| {ack} | {ts}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_host_status(hostname: str) -> str:
    """
    Get the full status of a Zabbix host: availability, active triggers, and interfaces.
    Use this as the first call when triaging an incident — understand whether the host
    itself is reachable, which services are failing, and since when.
    """
    hosts = await _call("host.get", {
        "output": ["hostid", "host", "name", "available", "error", "description"],
        "selectInterfaces": ["ip", "dns", "type", "available"],
        "selectTriggers": ["triggerid", "description", "priority", "value", "lastchange", "error"],
        "filter": {"host": [hostname]},
        "triggerFilter": {"value": "1"},  # Only PROBLEM triggers
    })

    if not hosts:
        return f"Host '{hostname}' not found in Zabbix."

    host = hosts[0]
    availability = {"0": "Unknown", "1": "Available", "2": "Unavailable"}.get(
        str(host.get("available", 0)), "?"
    )

    lines = [
        f"Host: {host.get('name', hostname)} ({host.get('host', hostname)})",
        f"Availability: {availability}",
    ]
    if host.get("error"):
        lines.append(f"Error: {host['error']}")

    interfaces = host.get("interfaces", [])
    if interfaces:
        iface_types = {"1": "Agent", "2": "SNMP", "3": "IPMI", "4": "JMX"}
        for iface in interfaces:
            iface_avail = {"0": "Unknown", "1": "Available", "2": "Unavailable"}.get(
                str(iface.get("available", 0)), "?"
            )
            lines.append(
                f"  Interface [{iface_types.get(str(iface.get('type')), '?')}] "
                f"{iface.get('ip') or iface.get('dns', 'N/A')} — {iface_avail}"
            )

    triggers = host.get("triggers", [])
    if triggers:
        lines.append(f"\nActive triggers ({len(triggers)}):")
        for t in sorted(triggers, key=lambda x: int(x.get("priority", 0)), reverse=True):
            prio = int(t.get("priority", 0))
            lines.append(
                f"  [{_SEVERITY.get(prio, prio)}] {t.get('description', 'N/A')} "
                f"| since: {t.get('lastchange', 'N/A')}"
            )
    else:
        lines.append("\nNo active triggers.")

    return "\n".join(lines)


@mcp.tool()
async def get_item_history(
    hostname: str,
    item_key: str,
    hours: int = 3,
    limit: int = 20,
) -> str:
    """
    Get historical values for a specific Zabbix item (metric) on a host.
    item_key examples: 'system.cpu.util', 'vm.memory.size[available]',
                       'vfs.fs.size[/,pused]', 'net.if.in[eth0]'
    Use this to see how a metric evolved before the alert fired —
    was CPU climbing steadily or did it spike suddenly?
    """
    hosts = await _call("host.get", {
        "output": ["hostid"],
        "filter": {"host": [hostname]},
    })
    if not hosts:
        return f"Host '{hostname}' not found in Zabbix."

    host_id = hosts[0]["hostid"]

    items = await _call("item.get", {
        "output": ["itemid", "name", "key_", "units", "value_type"],
        "hostids": [host_id],
        "search": {"key_": item_key},
        "searchWildcardsEnabled": True,
    })
    if not items:
        return f"No item matching key '{item_key}' found on host '{hostname}'."

    item = items[0]
    item_id = item["itemid"]
    value_type = int(item.get("value_type", 0))

    time_from = int(time.time()) - hours * 3600
    history_type = 0 if value_type in (0, 3) else 1  # 0=float, 1=string

    history = await _call("history.get", {
        "output": "extend",
        "itemids": [item_id],
        "history": history_type,
        "time_from": time_from,
        "sortfield": "clock",
        "sortorder": "DESC",
        "limit": limit,
    })

    units = item.get("units", "")
    lines = [
        f"Item: {item.get('name')} [{item.get('key_')}] on {hostname}",
        f"Last {hours}h — {len(history)} values:",
    ]
    for h in history:
        ts = h.get("clock", "?")
        val = h.get("value", "?")
        lines.append(f"  {ts} → {val} {units}")

    return "\n".join(lines)


@mcp.tool()
async def get_recent_events(hostname: str, hours: int = 6, limit: int = 20) -> str:
    """
    Get recent Zabbix events (state changes) for a host over the last N hours.
    Use this to understand the incident timeline — when did things start going wrong,
    were there earlier warnings before the critical alert fired?
    """
    hosts = await _call("host.get", {
        "output": ["hostid"],
        "filter": {"host": [hostname]},
    })
    if not hosts:
        return f"Host '{hostname}' not found in Zabbix."

    host_id = hosts[0]["hostid"]
    time_from = int(time.time()) - hours * 3600

    events = await _call("event.get", {
        "output": ["eventid", "source", "object", "value", "clock", "name", "severity", "acknowledged"],
        "hostids": [host_id],
        "time_from": time_from,
        "source": 0,   # trigger events only
        "object": 0,   # triggers
        "sortfield": "clock",
        "sortorder": "DESC",
        "limit": limit,
    })

    if not events:
        return f"No events found for '{hostname}' in the last {hours}h."

    lines = [f"Events for '{hostname}' (last {hours}h, {len(events)} found):"]
    for e in events:
        sev = int(e.get("severity", 0))
        state = _TRIGGER_VALUE.get(int(e.get("value", 0)), "?")
        ack = "✓" if e.get("acknowledged") == "1" else " "
        lines.append(
            f"  [{ack}] {e.get('clock', '?')} [{_SEVERITY.get(sev, '?')}] "
            f"{state} — {e.get('name', 'N/A')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def acknowledge_problem(
    event_id: str,
    message: str,
    close: bool = False,
) -> str:
    """
    Acknowledge a Zabbix problem event, optionally closing it.
    event_id: from list_active_problems or get_recent_events
    close: set True to close the problem (marks it as resolved manually)
    Use this after auto-remediation to acknowledge the alert and suppress
    further notifications.
    Disabled when ZABBIX_READ_ONLY=true.
    """
    if ZABBIX_READ_ONLY:
        return "Read-only mode enabled — acknowledge_problem is disabled (set ZABBIX_READ_ONLY=false to allow writes)."
    action = 6 if close else 2  # 2=acknowledge, 6=acknowledge+close (Zabbix 3.4+)
    await _call("event.acknowledge", {
        "eventids": [event_id],
        "action": action,
        "message": message,
    })
    status = "acknowledged and closed" if close else "acknowledged"
    return f"Event #{event_id} {status}: {message}"


@mcp.tool()
async def list_maintenance_windows() -> str:
    """
    List active and upcoming Zabbix maintenance windows.
    Always check this before escalating an incident — alerts during a maintenance
    window are expected and should not page on-call.
    """
    now = int(time.time())

    maintenances = await _call("maintenance.get", {
        "output": ["maintenanceid", "name", "description", "active_since", "active_till"],
        "selectHosts": ["host", "name"],
        "selectGroups": ["name"],
        "filter": {"active_till": None},  # include active ones
    })

    active = [m for m in maintenances if int(m.get("active_since", 0)) <= now <= int(m.get("active_till", 0))]
    upcoming = [m for m in maintenances if int(m.get("active_since", 0)) > now]

    lines = []
    if active:
        lines.append(f"Active maintenance windows ({len(active)}):")
        for m in active:
            hosts = [h.get("name", h.get("host")) for h in m.get("hosts", [])]
            groups = [g.get("name") for g in m.get("groups", [])]
            scope = ", ".join(hosts or groups or ["all"])
            lines.append(
                f"  [{m.get('maintenanceid')}] {m.get('name')} | "
                f"until: {m.get('active_till')} | scope: {scope}"
            )
    else:
        lines.append("No active maintenance windows.")

    if upcoming:
        lines.append(f"\nUpcoming maintenance windows ({len(upcoming)}):")
        for m in upcoming[:5]:
            lines.append(
                f"  {m.get('name')} | from: {m.get('active_since')} to: {m.get('active_till')}"
            )

    return "\n".join(lines)


@mcp.tool()
async def create_maintenance_window(
    hostname: str,
    duration_minutes: int,
    name: Optional[str] = None,
    description: str = "",
) -> str:
    """
    Create a Zabbix maintenance window for a host to suppress alerts during remediation.
    Call this before executing SSH auto-remediation on a host so that Zabbix does not
    fire additional alerts while the fix is running.
    Returns the maintenance window ID.
    """
    hosts = await _call("host.get", {
        "output": ["hostid", "name"],
        "filter": {"host": [hostname]},
    })
    if not hosts:
        return f"Host '{hostname}' not found in Zabbix."

    host_id = hosts[0]["hostid"]
    host_display = hosts[0].get("name", hostname)
    now = int(time.time())
    till = now + duration_minutes * 60
    window_name = name or f"Trianova remediation — {host_display}"

    if ZABBIX_READ_ONLY:
        return "Read-only mode enabled — create_maintenance_window is disabled (set ZABBIX_READ_ONLY=false to allow writes)."

    result = await _call("maintenance.create", {
        "name": window_name,
        "description": description or f"Auto-created by triage agent for remediation on {host_display}",
        "active_since": now,
        "active_till": till,
        "hostids": [host_id],
        "timeperiods": [{"period": duration_minutes * 60, "timeperiod_type": 0}],
    })

    maint_id = result.get("maintenanceids", ["?"])[0]
    return (
        f"Maintenance window #{maint_id} created for '{host_display}' | "
        f"Duration: {duration_minutes} min | Name: {window_name}"
    )


@mcp.tool()
async def get_host_inventory(hostname: str) -> str:
    """
    Get CMDB-style inventory data for a Zabbix host (hardware, OS, location, contacts).
    Use this to enrich incident context — OS version, hardware model, rack location,
    responsible team — without needing a separate CMDB if Zabbix inventory is populated.
    """
    hosts = await _call("host.get", {
        "output": ["hostid", "host", "name", "description"],
        "selectInventory": "extend",
        "filter": {"host": [hostname]},
    })
    if not hosts:
        return f"Host '{hostname}' not found in Zabbix."

    host = hosts[0]
    inventory = host.get("inventory") or {}

    fields = [
        ("OS", inventory.get("os") or inventory.get("os_full")),
        ("Hardware", inventory.get("hardware") or inventory.get("hardware_full")),
        ("Model", inventory.get("model")),
        ("Serial", inventory.get("serialno_a")),
        ("Location", inventory.get("location")),
        ("Site", inventory.get("site_city")),
        ("Rack", inventory.get("chassis")),
        ("Contact", inventory.get("contact")),
        ("Notes", inventory.get("notes")),
    ]

    lines = [f"Inventory: {host.get('name', hostname)} ({host.get('host', hostname)})"]
    if host.get("description"):
        lines.append(f"Description: {host['description']}")

    populated = [(k, v) for k, v in fields if v]
    if populated:
        for k, v in populated:
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  No inventory data populated for this host.")

    return "\n".join(lines)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
