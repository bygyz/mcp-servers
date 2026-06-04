"""
PRTG Network Monitor — MCP Server
Exposes PRTG monitoring data and actions as MCP tools for AI triage agents.
"""

import os
from typing import Optional
from datetime import datetime, timedelta

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("PRTG Network Monitor")

PRTG_HOST = os.environ.get("PRTG_HOST", "")
PRTG_API_TOKEN = os.environ.get("PRTG_API_TOKEN", "")
PRTG_USERNAME = os.environ.get("PRTG_USERNAME", "")
PRTG_PASSHASH = os.environ.get("PRTG_PASSHASH", "")
PRTG_VERIFY_SSL = os.environ.get("PRTG_VERIFY_SSL", "true").lower() == "true"
PRTG_READ_ONLY = os.environ.get("PRTG_READ_ONLY", "false").lower() == "true"


def _base_url() -> str:
    host = PRTG_HOST.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"{host}/api"


def _auth_params() -> dict:
    if PRTG_API_TOKEN:
        return {"apitoken": PRTG_API_TOKEN}
    return {"username": PRTG_USERNAME, "passhash": PRTG_PASSHASH}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=PRTG_VERIFY_SSL, timeout=15.0)


async def _table(content: str, columns: str, count: int = 200, **kwargs) -> dict:
    """Shared helper for PRTG table.json API calls."""
    params: dict = {**_auth_params(), "content": content, "columns": columns,
                    "count": count, "output": "json"}
    for key, value in kwargs.items():
        if value is not None:
            params[key] = value
    async with _client() as client:
        resp = await client.get(f"{_base_url()}/table.json", params=params)
        resp.raise_for_status()
        return resp.json()


# PRTG sensor status codes
_STATUS = {
    1: "Unknown", 2: "Scanning", 3: "Up", 4: "Warning",
    5: "Down", 6: "No Probe", 7: "Paused by User", 8: "Paused by Dependencies",
    9: "Paused by Schedule", 10: "Unusual", 11: "Not Licensed",
    12: "Paused Until", 13: "Down (Acknowledged)", 14: "Down (Partial)"
}


@mcp.tool()
async def list_alerts(include_acknowledged: bool = False) -> str:
    """
    List active alerts and down/warning sensors in PRTG.
    Returns sensor name, device, status, message, and last check time.
    Use this to understand the current alert landscape before triaging an incident.
    """
    filter_status = "4,5,10,13,14" if include_acknowledged else "4,5,10,14"
    data = await _table(
        "sensors", "objid,name,device,host,status,message,lastvalue,lastcheck",
        filter_status=filter_status,
    )
    sensors = data.get("sensors", [])
    if not sensors:
        return "No active alerts in PRTG."

    lines = [f"Active alerts ({len(sensors)} total):"]
    for s in sensors:
        status = _STATUS.get(s.get("status_raw", 0), s.get("status", "?"))
        lines.append(
            f"- [{status}] {s['device']} / {s['name']} | "
            f"msg: {s.get('message', 'N/A')} | last check: {s.get('lastcheck', 'N/A')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_device_status(host: str) -> str:
    """
    Get the monitoring status of a specific device or host in PRTG.
    Returns all sensors for that device with their current status and last value.
    Use this to get a full picture of a host's health during incident triage.
    """
    data = await _table(
        "sensors", "objid,name,status,message,lastvalue,lastcheck,uptime",
        count=100, filter_device=host,
    )
    sensors = data.get("sensors", [])
    if not sensors:
        return f"No sensors found for device '{host}' in PRTG."

    lines = [f"Sensors for '{host}' ({len(sensors)} total):"]
    for s in sensors:
        status = _STATUS.get(s.get("status_raw", 0), s.get("status", "?"))
        lines.append(
            f"  [{status}] {s['name']} | value: {s.get('lastvalue', 'N/A')} | "
            f"msg: {s.get('message', '')} | uptime: {s.get('uptime', 'N/A')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_sensor_history(
    sensor_id: int,
    hours: int = 2,
) -> str:
    """
    Get historical data for a PRTG sensor over the last N hours.
    Use this to understand trends — e.g. was CPU gradually climbing before the alert?
    """
    end = datetime.now()
    start = end - timedelta(hours=hours)

    async with _client() as client:
        params = {
            **_auth_params(),
            "id": sensor_id,
            "sdate": start.strftime("%Y-%m-%d-%H-%M-%S"),
            "edate": end.strftime("%Y-%m-%d-%H-%M-%S"),
            "avg": 0,
            "output": "json",
        }
        resp = await client.get(f"{_base_url()}/historicdata.json", params=params)
        resp.raise_for_status()
        data = resp.json()

    records = data.get("histdata", [])
    if not records:
        return f"No historical data for sensor {sensor_id} in the last {hours}h."

    lines = [f"History for sensor {sensor_id} (last {hours}h, {len(records)} points):"]
    for r in records[-20:]:  # Last 20 data points
        lines.append(f"  {r.get('datetime', '?')} → {r.get('value', 'N/A')}")
    return "\n".join(lines)


@mcp.tool()
async def list_scheduled_downtimes() -> str:
    """
    List all scheduled maintenance windows (downtimes) currently active or planned in PRTG.
    CRITICAL: check this before escalating an incident — alerts during planned maintenance
    should not page on-call engineers.
    """
    data = await _table(
        "sensors", "objid,name,device,pauseuntil,message",
        count=100, filter_status="12",
    )
    sensors = data.get("sensors", [])
    if not sensors:
        return "No scheduled downtimes currently active in PRTG."

    lines = [f"Active scheduled downtimes ({len(sensors)}):"]
    for s in sensors:
        lines.append(
            f"  {s['device']} / {s['name']} | until: {s.get('pauseuntil', 'unknown')} | "
            f"reason: {s.get('message', 'N/A')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def acknowledge_alert(
    object_id: int,
    message: str,
) -> str:
    """
    Acknowledge an active alert in PRTG to suppress further notifications.
    Use only when the incident is understood and being handled.
    object_id is the PRTG sensor or device object ID.
    """
    if PRTG_READ_ONLY:
        return "Read-only mode enabled — acknowledge_alert is disabled (set PRTG_READ_ONLY=false to allow writes)."

    async with _client() as client:
        resp = await client.get(f"{_base_url()}/acknowledgealarm.htm",
                                params={**_auth_params(), "id": object_id, "ackmsg": message})
        resp.raise_for_status()

    return f"Alert acknowledged for object {object_id}: '{message}'"


@mcp.tool()
async def pause_sensor(
    object_id: int,
    duration_minutes: int,
    message: str,
) -> str:
    """
    Pause a PRTG sensor for a given duration (in minutes).
    Use during planned remediation to prevent alert noise.
    """
    if PRTG_READ_ONLY:
        return "Read-only mode enabled — pause_sensor is disabled (set PRTG_READ_ONLY=false to allow writes)."

    async with _client() as client:
        resp = await client.get(f"{_base_url()}/pauseobjectfor.htm",
                                params={**_auth_params(), "id": object_id,
                                        "action": 1, "duration": duration_minutes, "pausemsg": message})
        resp.raise_for_status()

    return f"Sensor {object_id} paused for {duration_minutes} minutes: '{message}'"


@mcp.tool()
async def get_prtg_summary() -> str:
    """
    Get overall PRTG instance health summary: total devices, sensors up/down/warning.
    Use as a quick context check at the start of triage.
    """
    async with _client() as client:
        resp = await client.get(f"{_base_url()}/getstatus.json", params={**_auth_params(), "output": "json"})
        resp.raise_for_status()
        data = resp.json()

    s = data.get("prtg-status", data)
    return (
        f"PRTG summary — "
        f"Sensors: {s.get('Sensors', '?')} total | "
        f"Down: {s.get('SensorsDown', '?')} | "
        f"Warning: {s.get('SensorsWarning', '?')} | "
        f"Unusual: {s.get('SensorsUnusual', '?')} | "
        f"Paused: {s.get('SensorsPaused', '?')} | "
        f"Up: {s.get('SensorsUp', '?')}"
    )


@mcp.tool()
async def get_sensor_details(sensor_id: int) -> str:
    """
    Get full details for a specific PRTG sensor by its object ID.
    Returns sensor type, status, current value, thresholds, last error, uptime, and tags.
    Use this after get_device_status identifies a failing sensor — get the full context
    including error messages and configured limits before escalating.
    """
    async with _client() as client:
        resp = await client.get(f"{_base_url()}/getsensordetails.json",
                                params={**_auth_params(), "id": sensor_id, "output": "json"})
        resp.raise_for_status()
        data = resp.json()

    sensor = data.get("sensordata", data)
    lines = [
        f"Sensor #{sensor_id}: {sensor.get('name', 'N/A')}",
        f"Type: {sensor.get('sensortype', 'N/A')}",
        f"Status: {sensor.get('statustext', sensor.get('status', 'N/A'))}",
        f"Last value: {sensor.get('lastvalue', 'N/A')}",
        f"Last check: {sensor.get('lastcheck', 'N/A')}",
        f"Last up: {sensor.get('lastup', 'N/A')}",
        f"Last error: {sensor.get('lasterror', 'N/A')}",
        f"Uptime: {sensor.get('uptime', 'N/A')}",
        f"Device: {sensor.get('devicename', 'N/A')}",
        f"Group: {sensor.get('groupname', 'N/A')}",
        f"Tags: {sensor.get('tags', 'N/A')}",
        f"Message: {sensor.get('message', 'N/A')}",
    ]
    return "\n".join(lines)


@mcp.tool()
async def get_channels(sensor_id: int) -> str:
    """
    Get all monitoring channels for a PRTG sensor with their current values and configured limits.
    Channels are the individual metrics a sensor tracks (e.g. CPU %, memory MB, disk %).
    Use this to understand exactly which threshold was breached and by how much —
    essential for assessing severity before escalating or auto-remediating.
    """
    data = await _table(
        "channels",
        "objid,name,lastvalue,minimum,maximum,limitmaxerror,limitmaxwarning,limitmode",
        count=50, id=sensor_id, noraw=1,
    )
    channels = data.get("channels", [])
    if not channels:
        return f"No channels found for sensor {sensor_id}."

    lines = [f"Channels for sensor #{sensor_id} ({len(channels)}):"]
    for ch in channels:
        limit_info = ""
        if ch.get("limitmaxerror"):
            limit_info = f" | error threshold: {ch['limitmaxerror']}"
        elif ch.get("limitmaxwarning"):
            limit_info = f" | warning threshold: {ch['limitmaxwarning']}"
        lines.append(
            f"  {ch.get('name', 'N/A')}: {ch.get('lastvalue', 'N/A')}"
            f"{limit_info}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_messages(
    host: Optional[str] = None,
    date_range: Optional[str] = None,
    limit: int = 50,
) -> str:
    """
    Get PRTG system log messages, optionally filtered by device name and date range.
    date_range values: 'today', 'yesterday', '7days', '30days'
    Use this to find recent events on a device — configuration changes, sensor errors,
    probe reconnects — that could explain an incident without waiting for a full audit.
    """
    kwargs: dict = {"sortby": "-datetime"}
    if host:
        kwargs["filter_name"] = f"@sub({host})"
    if date_range:
        kwargs["filter_drel"] = date_range

    data = await _table(
        "messages",
        "objid,datetime,parent,type,name,status,message",
        count=limit, **kwargs,
    )
    messages = data.get("messages", [])
    if not messages:
        scope = f" for '{host}'" if host else ""
        return f"No messages found{scope} in PRTG."

    lines = [f"Messages ({len(messages)}):"]
    for m in messages:
        lines.append(
            f"  [{m.get('datetime', '?')}] [{m.get('type', '?')}] "
            f"{m.get('name', 'N/A')} — {m.get('message', '')[:150]}"
        )
    return "\n".join(lines)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
