"""
Centreon — MCP Server
Exposes Centreon monitoring data and actions as MCP tools for AI triage agents.
"""

import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Centreon Monitor")

CENTREON_HOST = os.environ.get("CENTREON_HOST", "")
CENTREON_API_TOKEN = os.environ.get("CENTREON_API_TOKEN", "")   # Long-lived API token (preferred)
CENTREON_USERNAME = os.environ.get("CENTREON_USERNAME", "")
CENTREON_PASSWORD = os.environ.get("CENTREON_PASSWORD", "")
CENTREON_VERIFY_SSL = os.environ.get("CENTREON_VERIFY_SSL", "true").lower() == "true"
CENTREON_READ_ONLY = os.environ.get("CENTREON_READ_ONLY", "false").lower() == "true"

_session_cache: dict[str, str] = {}


def _base_url() -> str:
    host = CENTREON_HOST.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"{host}/centreon/api/latest"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=CENTREON_VERIFY_SSL, timeout=15.0)


async def _get_token() -> str:
    """Return a valid auth token — static API token preferred, session login as fallback."""
    if CENTREON_API_TOKEN:
        return CENTREON_API_TOKEN

    if CENTREON_HOST in _session_cache:
        return _session_cache[CENTREON_HOST]

    if not CENTREON_USERNAME or not CENTREON_PASSWORD:
        raise RuntimeError(
            "No Centreon credentials. Set CENTREON_API_TOKEN or CENTREON_USERNAME + CENTREON_PASSWORD."
        )

    async with _client() as client:
        resp = await client.post(
            f"{_base_url()}/login",
            json={"security": {"credentials": {"login": CENTREON_USERNAME, "password": CENTREON_PASSWORD}}},
        )
        resp.raise_for_status()
        token = resp.json()["security"]["token"]

    _session_cache[CENTREON_HOST] = token
    return token


async def _headers() -> dict:
    token = await _get_token()
    return {"X-AUTH-TOKEN": token, "Content-Type": "application/json"}


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """Execute an API call, refreshing the session once on 401."""
    headers = await _headers()
    async with _client() as client:
        resp = await client.request(method, f"{_base_url()}{path}", headers=headers, **kwargs)
        if resp.status_code == 401 and not CENTREON_API_TOKEN:
            # Session expired — re-authenticate and retry once
            _session_cache.pop(CENTREON_HOST, None)
            headers = await _headers()
            resp = await client.request(method, f"{_base_url()}{path}", headers=headers, **kwargs)
        resp.raise_for_status()
        return resp


# Centreon status codes
_HOST_STATUS = {0: "UP", 1: "DOWN", 2: "UNREACHABLE", 4: "PENDING"}
_SVC_STATUS = {0: "OK", 1: "WARNING", 2: "CRITICAL", 3: "UNKNOWN", 4: "PENDING"}


@mcp.tool()
async def list_alerts(
    severity: Optional[str] = None,
    limit: int = 50,
) -> str:
    """
    List current alerts (non-OK hosts and services) from Centreon.
    severity filter: 'warning', 'critical', 'unknown' or None for all.
    Use this to understand what else is firing when triaging a specific incident.
    """
    results = []

    # Down hosts
    resp = await _request("GET", "/monitoring/hosts", params={"limit": limit, "page": 1})
    for h in resp.json().get("result", []):
        status_code = h.get("status", {}).get("code", 0)
        if status_code in (1, 2):  # DOWN, UNREACHABLE
            results.append(
                f"[HOST {_HOST_STATUS.get(status_code, '?')}] {h['name']} | "
                f"output: {h.get('output', 'N/A')} | "
                f"last check: {h.get('last_check', 'N/A')}"
            )

    # Non-OK services
    svc_search = '{"service.status":{"$in":[1,2,3]}}'
    if severity:
        code_map = {"warning": 1, "critical": 2, "unknown": 3}
        code = code_map.get(severity.lower())
        if code:
            svc_search = f'{{"service.status":{{"$eq":{code}}}}}'

    resp = await _request("GET", "/monitoring/services", params={"limit": limit, "page": 1, "search": svc_search})
    for s in resp.json().get("result", []):
        status_code = s.get("status", {}).get("code", 0)
        results.append(
            f"[SVC {_SVC_STATUS.get(status_code, '?')}] "
            f"{s.get('host', {}).get('name', '?')} / {s['name']} | "
            f"output: {s.get('output', 'N/A')}"
        )

    if not results:
        return "No active alerts in Centreon."
    return f"Active alerts ({len(results)}):\n" + "\n".join(f"- {r}" for r in results)


@mcp.tool()
async def get_host_status(host_name: str) -> str:
    """
    Get the current monitoring status of a specific host in Centreon.
    Returns host state, output, last check time, and all its services.
    Use this during triage to get a full picture of the affected host.
    """
    resp = await _request("GET", "/monitoring/hosts", params={"search": f'{{"host.name":"{host_name}"}}', "limit": 1})
    hosts = resp.json().get("result", [])
    if not hosts:
        return f"Host '{host_name}' not found in Centreon."
    host = hosts[0]

    svc_resp = await _request("GET", f"/monitoring/hosts/{host['id']}/services", params={"limit": 50})
    services = svc_resp.json().get("result", [])

    status_code = host.get("status", {}).get("code", 0)
    lines = [
        f"Host: {host['name']}",
        f"Status: {_HOST_STATUS.get(status_code, '?')}",
        f"Output: {host.get('output', 'N/A')}",
        f"Last check: {host.get('last_check', 'N/A')}",
        f"Acknowledged: {host.get('acknowledged', False)}",
        f"In downtime: {host.get('in_downtime', False)}",
        "",
        f"Services ({len(services)}):",
    ]
    for s in services:
        sc = s.get("status", {}).get("code", 0)
        lines.append(f"  [{_SVC_STATUS.get(sc, '?')}] {s['name']} | {s.get('output', '')}")

    return "\n".join(lines)


@mcp.tool()
async def list_scheduled_downtimes() -> str:
    """
    List all currently active scheduled maintenance downtimes in Centreon.
    CRITICAL: check this before escalating — alerts during planned maintenance
    should not page on-call engineers.
    """
    resp = await _request("GET", "/monitoring/downtimes", params={"limit": 100, "search": '{"downtime.is_cancelled":false}'})
    downtimes = resp.json().get("result", [])

    if not downtimes:
        return "No scheduled downtimes currently active in Centreon."

    lines = [f"Active downtimes ({len(downtimes)}):"]
    for d in downtimes:
        lines.append(
            f"  {d.get('host', {}).get('name', '?')} "
            f"[{d.get('service', {}).get('name', 'host-level')}] | "
            f"from: {d.get('start_time', '?')} to: {d.get('end_time', '?')} | "
            f"author: {d.get('author_name', '?')} | comment: {d.get('comment', '')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def acknowledge_host(
    host_name: str,
    comment: str,
    notify: bool = True,
) -> str:
    """
    Acknowledge a host problem in Centreon to suppress repeat notifications.
    Use when the incident is confirmed and being actively investigated.
    """
    if CENTREON_READ_ONLY:
        return "Read-only mode enabled — acknowledge_host is disabled (set CENTREON_READ_ONLY=false to allow writes)."

    resp = await _request("GET", "/monitoring/hosts", params={"search": f'{{"host.name":"{host_name}"}}', "limit": 1})
    hosts = resp.json().get("result", [])
    if not hosts:
        return f"Host '{host_name}' not found in Centreon."

    host_id = hosts[0]["id"]
    await _request("POST", f"/monitoring/hosts/{host_id}/acknowledgements",
                   json={"comment": comment, "notify": notify, "sticky": True, "persistent": True})

    return f"Host '{host_name}' acknowledged in Centreon: '{comment}'"


@mcp.tool()
async def schedule_downtime(
    host_name: str,
    start_time: str,
    end_time: str,
    comment: str,
    include_services: bool = True,
) -> str:
    """
    Schedule a maintenance downtime for a host in Centreon.
    start_time and end_time format: 'YYYY-MM-DDTHH:MM:SS' (ISO 8601).
    Use before planned remediation to prevent alert noise.
    """
    if CENTREON_READ_ONLY:
        return "Read-only mode enabled — schedule_downtime is disabled (set CENTREON_READ_ONLY=false to allow writes)."

    resp = await _request("GET", "/monitoring/hosts", params={"search": f'{{"host.name":"{host_name}"}}', "limit": 1})
    hosts = resp.json().get("result", [])
    if not hosts:
        return f"Host '{host_name}' not found in Centreon."

    host_id = hosts[0]["id"]
    await _request("POST", f"/monitoring/hosts/{host_id}/downtimes", json={
        "start_time": start_time,
        "end_time": end_time,
        "comment": comment,
        "is_fixed": True,
        "with_services": include_services,
    })

    return f"Downtime scheduled for '{host_name}' from {start_time} to {end_time}: '{comment}'"


@mcp.tool()
async def get_centreon_summary() -> str:
    """
    Get overall Centreon instance health summary: hosts and services counts by status.
    Use as a quick context check at the start of triage.
    """
    hosts_resp = await _request("GET", "/monitoring/hosts/count")
    host_counts = hosts_resp.json()

    svc_resp = await _request("GET", "/monitoring/services/count")
    svc_counts = svc_resp.json()

    return (
        f"Centreon summary — "
        f"Hosts: UP={host_counts.get('up', '?')} DOWN={host_counts.get('down', '?')} "
        f"UNREACHABLE={host_counts.get('unreachable', '?')} PENDING={host_counts.get('pending', '?')} | "
        f"Services: OK={svc_counts.get('ok', '?')} WARNING={svc_counts.get('warning', '?')} "
        f"CRITICAL={svc_counts.get('critical', '?')} UNKNOWN={svc_counts.get('unknown', '?')}"
    )


@mcp.tool()
async def get_host_timeline(
    host_name: str,
    hours: int = 6,
    limit: int = 30,
) -> str:
    """
    Get the event timeline for a host: state changes, acknowledgements, downtimes, comments.
    Use this to reconstruct the incident chronology — when did the host go DOWN,
    was it acknowledged, were there earlier flaps before the critical alert fired?
    This is the fastest way to answer "what happened on this host recently?"
    """
    resp = await _request("GET", "/monitoring/hosts", params={"search": f'{{"host.name":"{host_name}"}}', "limit": 1})
    hosts = resp.json().get("result", [])
    if not hosts:
        return f"Host '{host_name}' not found in Centreon."

    host_id = hosts[0]["id"]
    timeline_resp = await _request(
        "GET",
        f"/monitoring/hosts/{host_id}/timeline",
        params={"limit": limit, "sort_by": '{"date":"DESC"}'},
    )
    events = timeline_resp.json().get("result", [])

    if not events:
        return f"No timeline events found for '{host_name}'."

    _event_type = {
        "event": "State change",
        "acknowledgement": "Acknowledgement",
        "downtime": "Downtime",
        "comment": "Comment",
        "notification": "Notification",
    }

    lines = [f"Timeline for '{host_name}' ({len(events)} events):"]
    for e in events:
        etype = _event_type.get(e.get("type", ""), e.get("type", "?"))
        date = e.get("date", "?")
        content = e.get("content", "")
        status = e.get("status", {})
        status_name = status.get("name", "") if isinstance(status, dict) else ""
        detail = status_name or content or ""
        lines.append(f"  [{date}] {etype}: {detail[:150]}")

    return "\n".join(lines)


@mcp.tool()
async def request_check(
    host_name: str,
    service_name: Optional[str] = None,
) -> str:
    """
    Trigger an immediate check on a host or service without waiting for the next polling cycle.
    Use this after auto-remediation to confirm recovery in real time — rather than waiting
    up to 5 minutes for the next scheduled check.
    If service_name is omitted, checks the host itself.
    Disabled when CENTREON_READ_ONLY=true.
    """
    if CENTREON_READ_ONLY:
        return "Read-only mode enabled — request_check is disabled (set CENTREON_READ_ONLY=false to allow writes)."

    resp = await _request("GET", "/monitoring/hosts", params={"search": f'{{"host.name":"{host_name}"}}', "limit": 1})
    hosts = resp.json().get("result", [])
    if not hosts:
        return f"Host '{host_name}' not found in Centreon."

    host_id = hosts[0]["id"]

    if service_name:
        svc_resp = await _request("GET", f"/monitoring/hosts/{host_id}/services",
                                  params={"search": f'{{"service.display_name":"{service_name}"}}', "limit": 1})
        services = svc_resp.json().get("result", [])
        if not services:
            return f"Service '{service_name}' not found on host '{host_name}'."
        svc_id = services[0]["id"]
        await _request("POST", f"/monitoring/hosts/{host_id}/services/{svc_id}/checks",
                       json={"is_forced": True})
        return f"Immediate check triggered for '{host_name}/{service_name}'."
    else:
        await _request("POST", f"/monitoring/hosts/{host_id}/checks", json={"is_forced": True})
        return f"Immediate check triggered for host '{host_name}'."


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
