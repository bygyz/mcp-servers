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
CENTREON_USERNAME = os.environ.get("CENTREON_USERNAME", "")
CENTREON_PASSWORD = os.environ.get("CENTREON_PASSWORD", "")
CENTREON_VERIFY_SSL = os.environ.get("CENTREON_VERIFY_SSL", "true").lower() == "true"

_token_cache: dict[str, str] = {}


def _base_url() -> str:
    host = CENTREON_HOST.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"{host}/centreon/api/latest"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=CENTREON_VERIFY_SSL, timeout=15.0)


async def _get_token() -> str:
    """Authenticate and return a session token (cached per process)."""
    cache_key = CENTREON_HOST
    if cache_key in _token_cache:
        return _token_cache[cache_key]

    async with _client() as client:
        resp = await client.post(
            f"{_base_url()}/login",
            json={"security": {"credentials": {"login": CENTREON_USERNAME, "password": CENTREON_PASSWORD}}},
        )
        resp.raise_for_status()
        token = resp.json()["security"]["token"]

    _token_cache[cache_key] = token
    return token


async def _auth_headers() -> dict:
    token = await _get_token()
    return {"X-AUTH-TOKEN": token, "Content-Type": "application/json"}


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
    headers = await _auth_headers()
    results = []

    async with _client() as client:
        # Down hosts
        params: dict = {"limit": limit, "page": 1}
        resp = await client.get(f"{_base_url()}/monitoring/hosts", headers=headers, params=params)
        resp.raise_for_status()
        for h in resp.json().get("result", []):
            status_code = h.get("status", {}).get("code", 0)
            if status_code in (1, 2):  # DOWN, UNREACHABLE
                results.append(
                    f"[HOST {_HOST_STATUS.get(status_code, '?')}] {h['name']} | "
                    f"output: {h.get('output', 'N/A')} | "
                    f"last check: {h.get('last_check', 'N/A')}"
                )

        # Non-OK services
        svc_params: dict = {"limit": limit, "page": 1, "search": '{"service.status":{"$in":[1,2,3]}}'}
        if severity:
            code_map = {"warning": 1, "critical": 2, "unknown": 3}
            code = code_map.get(severity.lower())
            if code:
                svc_params["search"] = f'{{"service.status":{{"$eq":{code}}}}}'

        resp = await client.get(f"{_base_url()}/monitoring/services", headers=headers, params=svc_params)
        resp.raise_for_status()
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
    headers = await _auth_headers()

    async with _client() as client:
        resp = await client.get(
            f"{_base_url()}/monitoring/hosts",
            headers=headers,
            params={"search": f'{{"host.name":"{host_name}"}}', "limit": 1},
        )
        resp.raise_for_status()
        hosts = resp.json().get("result", [])
        if not hosts:
            return f"Host '{host_name}' not found in Centreon."
        host = hosts[0]

        svc_resp = await client.get(
            f"{_base_url()}/monitoring/hosts/{host['id']}/services",
            headers=headers,
            params={"limit": 50},
        )
        svc_resp.raise_for_status()
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
    headers = await _auth_headers()

    async with _client() as client:
        resp = await client.get(
            f"{_base_url()}/monitoring/downtimes",
            headers=headers,
            params={"limit": 100, "search": '{"downtime.is_cancelled":false}'},
        )
        resp.raise_for_status()
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
    headers = await _auth_headers()

    async with _client() as client:
        resp = await client.get(
            f"{_base_url()}/monitoring/hosts",
            headers=headers,
            params={"search": f'{{"host.name":"{host_name}"}}', "limit": 1},
        )
        resp.raise_for_status()
        hosts = resp.json().get("result", [])
        if not hosts:
            return f"Host '{host_name}' not found in Centreon."

        host_id = hosts[0]["id"]
        ack_resp = await client.post(
            f"{_base_url()}/monitoring/hosts/{host_id}/acknowledgements",
            headers=headers,
            json={"comment": comment, "notify": notify, "sticky": True, "persistent": True},
        )
        ack_resp.raise_for_status()

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
    headers = await _auth_headers()

    async with _client() as client:
        resp = await client.get(
            f"{_base_url()}/monitoring/hosts",
            headers=headers,
            params={"search": f'{{"host.name":"{host_name}"}}', "limit": 1},
        )
        resp.raise_for_status()
        hosts = resp.json().get("result", [])
        if not hosts:
            return f"Host '{host_name}' not found in Centreon."

        host_id = hosts[0]["id"]
        dt_resp = await client.post(
            f"{_base_url()}/monitoring/hosts/{host_id}/downtimes",
            headers=headers,
            json={
                "start_time": start_time,
                "end_time": end_time,
                "comment": comment,
                "is_fixed": True,
                "with_services": include_services,
            },
        )
        dt_resp.raise_for_status()

    return (
        f"Downtime scheduled for '{host_name}' from {start_time} to {end_time}: '{comment}'"
    )


@mcp.tool()
async def get_centreon_summary() -> str:
    """
    Get overall Centreon instance health summary: hosts and services counts by status.
    Use as a quick context check at the start of triage.
    """
    headers = await _auth_headers()

    async with _client() as client:
        hosts_resp = await client.get(
            f"{_base_url()}/monitoring/hosts/count", headers=headers
        )
        hosts_resp.raise_for_status()
        host_counts = hosts_resp.json()

        svc_resp = await client.get(
            f"{_base_url()}/monitoring/services/count", headers=headers
        )
        svc_resp.raise_for_status()
        svc_counts = svc_resp.json()

    return (
        f"Centreon summary — "
        f"Hosts: UP={host_counts.get('up', '?')} DOWN={host_counts.get('down', '?')} "
        f"UNREACHABLE={host_counts.get('unreachable', '?')} PENDING={host_counts.get('pending', '?')} | "
        f"Services: OK={svc_counts.get('ok', '?')} WARNING={svc_counts.get('warning', '?')} "
        f"CRITICAL={svc_counts.get('critical', '?')} UNKNOWN={svc_counts.get('unknown', '?')}"
    )


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
