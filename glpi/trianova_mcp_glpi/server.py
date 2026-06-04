"""
GLPI — MCP Server
Exposes GLPI ITSM/CMDB data as MCP tools for AI triage agents.
"""

import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("GLPI ITSM")

GLPI_HOST = os.environ.get("GLPI_HOST", "")
GLPI_APP_TOKEN = os.environ.get("GLPI_APP_TOKEN", "")
GLPI_USER_TOKEN = os.environ.get("GLPI_USER_TOKEN", "")
GLPI_VERIFY_SSL = os.environ.get("GLPI_VERIFY_SSL", "true").lower() == "true"

_session_cache: dict[str, str] = {}


def _base_url() -> str:
    host = GLPI_HOST.rstrip("/")
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"{host}/apirest.php"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=GLPI_VERIFY_SSL, timeout=15.0)


async def _init_session() -> str:
    """Open a new GLPI API session and cache it."""
    async with _client() as client:
        resp = await client.get(
            f"{_base_url()}/initSession",
            headers={
                "App-Token": GLPI_APP_TOKEN,
                "Authorization": f"user_token {GLPI_USER_TOKEN}",
            },
        )
        resp.raise_for_status()
        session_token = resp.json()["session_token"]
    _session_cache[GLPI_HOST] = session_token
    return session_token


async def _get_session_token() -> str:
    """Return cached session token, initialising a new session if needed."""
    if GLPI_HOST not in _session_cache:
        return await _init_session()
    return _session_cache[GLPI_HOST]


async def _headers() -> dict:
    session_token = await _get_session_token()
    return {
        "App-Token": GLPI_APP_TOKEN,
        "Session-Token": session_token,
        "Content-Type": "application/json",
    }


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    """Execute an API call, refreshing the session once on 401."""
    headers = await _headers()
    async with _client() as client:
        resp = await client.request(method, f"{_base_url()}{path}", headers=headers, **kwargs)
        if resp.status_code == 401:
            # Session expired — re-authenticate and retry once
            _session_cache.pop(GLPI_HOST, None)
            headers = await _headers()
            resp = await client.request(method, f"{_base_url()}{path}", headers=headers, **kwargs)
        resp.raise_for_status()
        return resp


# GLPI ticket status codes
_TICKET_STATUS = {
    1: "New", 2: "Processing (assigned)", 3: "Processing (planned)",
    4: "Pending", 5: "Solved", 6: "Closed"
}

# GLPI ticket priorities
_TICKET_PRIORITY = {
    1: "Very Low", 2: "Low", 3: "Medium", 4: "High", 5: "Very High", 6: "Major"
}

# GLPI ticket types
_TICKET_TYPE = {1: "Incident", 2: "Request"}


@mcp.tool()
async def search_tickets(
    hostname: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 20,
) -> str:
    """
    Search GLPI tickets with optional filters by hostname or status.
    status values: 'new', 'assigned', 'planned', 'pending', 'solved', 'closed'
    Use this to check if a ticket already exists for this host before creating a new one,
    and to understand recurring issues on a specific host.
    """
    status_map = {
        "new": 1, "assigned": 2, "planned": 3,
        "pending": 4, "solved": 5, "closed": 6
    }

    criteria = []
    if hostname:
        criteria.append({"field": "14", "searchtype": "contains", "value": hostname})
    if status:
        code = status_map.get(status.lower())
        if code:
            criteria.append({"field": "12", "searchtype": "equals", "value": str(code)})

    params: dict = {
        "range": f"0-{limit - 1}",
        "forcedisplay[0]": 2,   # ID
        "forcedisplay[1]": 1,   # Title
        "forcedisplay[2]": 12,  # Status
        "forcedisplay[3]": 14,  # Associated items
        "forcedisplay[4]": 19,  # Last update
        "forcedisplay[5]": 18,  # Time to resolve
        "forcedisplay[6]": 10,  # Urgency
    }
    for i, c in enumerate(criteria):
        params[f"criteria[{i}][field]"] = c["field"]
        params[f"criteria[{i}][searchtype]"] = c["searchtype"]
        params[f"criteria[{i}][value]"] = c["value"]

    resp = await _request("GET", "/search/Ticket", params=params)
    data = resp.json()

    tickets = data.get("data", [])
    if not tickets:
        return "No tickets found matching your criteria in GLPI."

    lines = [f"Tickets found ({data.get('totalcount', len(tickets))} total, showing {len(tickets)}):"]
    for t in tickets:
        status_code = int(t.get("12", 0))
        lines.append(
            f"  #{t.get('2', '?')} [{_TICKET_STATUS.get(status_code, '?')}] "
            f"{t.get('1', 'N/A')} | "
            f"last update: {t.get('19', 'N/A')} | "
            f"items: {t.get('14', 'N/A')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def get_ticket(ticket_id: int) -> str:
    """
    Get full details of a specific GLPI ticket by ID.
    Returns title, description, status, priority, associated assets, and followups.
    Use this to get full context on an existing incident ticket.
    """
    resp = await _request("GET", f"/Ticket/{ticket_id}", params={"with_followups": True, "with_documents": False})
    ticket = resp.json()

    fu_resp = await _request("GET", f"/Ticket/{ticket_id}/ITILFollowup", params={"range": "0-10"})
    followups = fu_resp.json() if fu_resp.status_code == 200 else []

    status_code = ticket.get("status", 0)
    priority_code = ticket.get("priority", 0)
    type_code = ticket.get("type", 1)

    lines = [
        f"Ticket #{ticket_id}",
        f"Type: {_TICKET_TYPE.get(type_code, '?')}",
        f"Status: {_TICKET_STATUS.get(status_code, '?')}",
        f"Priority: {_TICKET_PRIORITY.get(priority_code, '?')}",
        f"Title: {ticket.get('name', 'N/A')}",
        f"Created: {ticket.get('date', 'N/A')}",
        f"Last update: {ticket.get('date_mod', 'N/A')}",
        f"Solve by: {ticket.get('time_to_resolve', 'N/A')}",
        f"Description: {ticket.get('content', 'N/A')[:500]}",
    ]
    if followups:
        lines.append(f"\nFollowups ({len(followups)}):")
        for f in followups[-5:]:
            lines.append(f"  [{f.get('date', '?')}] {f.get('content', '')[:200]}")

    return "\n".join(lines)


@mcp.tool()
async def create_ticket(
    title: str,
    description: str,
    priority: int = 3,
    ticket_type: int = 1,
    category_id: Optional[int] = None,
) -> str:
    """
    Create a new ticket in GLPI.
    priority: 1=Very Low, 2=Low, 3=Medium, 4=High, 5=Very High, 6=Major
    ticket_type: 1=Incident, 2=Request
    Use this when an incident requires formal tracking in the ITSM system.
    Returns the created ticket ID.
    """
    payload: dict = {
        "input": {
            "name": title,
            "content": description,
            "priority": priority,
            "type": ticket_type,
            "urgency": priority,
            "impact": priority,
        }
    }
    if category_id:
        payload["input"]["itilcategories_id"] = category_id

    resp = await _request("POST", "/Ticket", json=payload)
    created = resp.json()
    ticket_id = created.get("id", "?")
    return (
        f"Ticket #{ticket_id} created in GLPI | "
        f"Type: {_TICKET_TYPE.get(ticket_type, '?')} | "
        f"Priority: {_TICKET_PRIORITY.get(priority, '?')} | "
        f"Title: {title}"
    )


@mcp.tool()
async def update_ticket(
    ticket_id: int,
    status: Optional[int] = None,
    solution: Optional[str] = None,
    followup: Optional[str] = None,
) -> str:
    """
    Update a GLPI ticket: change its status, add a solution, or add a followup comment.
    status: 1=New, 2=Assigned, 3=Planned, 4=Pending, 5=Solved, 6=Closed
    Use this to keep GLPI in sync with auto-remediation outcomes.
    """
    if status is not None or solution is not None:
        payload: dict = {"input": {}}
        if status is not None:
            payload["input"]["status"] = status
        if solution is not None:
            payload["input"]["solution"] = solution
            payload["input"]["solutiontypes_id"] = 1
        await _request("PUT", f"/Ticket/{ticket_id}", json=payload)

    if followup:
        await _request("POST", "/ITILFollowup", json={
            "input": {"items_id": ticket_id, "itemtype": "Ticket", "content": followup}
        })

    parts = []
    if status is not None:
        parts.append(f"status → {_TICKET_STATUS.get(status, '?')}")
    if solution:
        parts.append("solution added")
    if followup:
        parts.append("followup added")

    return f"Ticket #{ticket_id} updated: {', '.join(parts)}"


@mcp.tool()
async def get_asset(hostname: str) -> str:
    """
    Look up a computer/server asset in the GLPI CMDB by hostname.
    Returns hardware specs, OS, location, owner, warranty status, and change history.
    Use this to get full asset context during triage — warranty status, hardware specs,
    recent changes on this machine.
    """
    resp = await _request("GET", "/search/Computer", params={
        "criteria[0][field]": "1",
        "criteria[0][searchtype]": "contains",
        "criteria[0][value]": hostname,
        "forcedisplay[0]": 2,   # ID
        "forcedisplay[1]": 1,   # Name
        "forcedisplay[2]": 31,  # OS
        "forcedisplay[3]": 45,  # RAM
        "forcedisplay[4]": 5,   # Serial
        "forcedisplay[5]": 23,  # Location
        "forcedisplay[6]": 19,  # Last update
        "range": "0-5",
    })
    data = resp.json()

    assets = data.get("data", [])
    if not assets:
        return f"No asset found for hostname '{hostname}' in GLPI CMDB."

    asset = assets[0]
    asset_id = asset.get("2", "?")

    log_resp = await _request("GET", f"/Computer/{asset_id}/Log", params={"range": "0-5", "order": "DESC", "sort": "1"})
    logs = log_resp.json() if log_resp.status_code == 200 else []

    lines = [
        f"Asset: {asset.get('1', 'N/A')} (ID: {asset_id})",
        f"OS: {asset.get('45', 'N/A')}",
        f"RAM: {asset.get('32', 'N/A')}",
        f"Serial: {asset.get('11', 'N/A')}",
        f"Location: {asset.get('29', 'N/A')}",
        f"Last update: {asset.get('19', 'N/A')}",
    ]
    if logs and isinstance(logs, list):
        lines.append(f"\nRecent changes ({len(logs)}):")
        for log in logs[:5]:
            lines.append(f"  [{log.get('date_mod', '?')}] {log.get('old_value', '')} → {log.get('new_value', '')}")

    return "\n".join(lines)


@mcp.tool()
async def search_recent_changes(hostname: str, days: int = 7) -> str:
    """
    Search for recent changes recorded in GLPI for a specific asset over the last N days.
    Use this to correlate an incident with recent hardware/software changes on the host.
    This is often the fastest way to identify root cause (e.g. a recent RAM upgrade or OS update).
    """
    resp = await _request("GET", "/search/Computer", params={
        "criteria[0][field]": "1",
        "criteria[0][searchtype]": "contains",
        "criteria[0][value]": hostname,
        "forcedisplay[0]": 2,
        "range": "0-1",
    })
    data = resp.json()

    assets = data.get("data", [])
    if not assets:
        return f"No asset found for '{hostname}' in GLPI."

    asset_id = assets[0].get("2", "")

    log_resp = await _request("GET", f"/Computer/{asset_id}/Log", params={"range": "0-50", "order": "DESC"})
    logs = log_resp.json()

    if not isinstance(logs, list) or not logs:
        return f"No change history found for '{hostname}' in the last {days} days."

    lines = [f"Recent changes for '{hostname}' (last {days} days):"]
    for log in logs[:20]:
        lines.append(
            f"  [{log.get('date_mod', '?')}] "
            f"Field: {log.get('itemtype_link', log.get('linked_action', '?'))} | "
            f"{log.get('old_value', 'N/A')} → {log.get('new_value', 'N/A')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def assign_ticket(
    ticket_id: int,
    user_id: Optional[int] = None,
    group_id: Optional[int] = None,
    followup: Optional[str] = None,
) -> str:
    """
    Assign a GLPI ticket to a specific user and/or group.
    Use this to route an incident to the right technician or team after triage.
    At least one of user_id or group_id must be provided.
    """
    if user_id is None and group_id is None:
        return "Error: provide at least one of user_id or group_id."

    actors = []
    if user_id is not None:
        actors.append({"type": 2, "items_id": user_id, "itemtype": "User"})   # type 2 = assigned
    if group_id is not None:
        actors.append({"type": 2, "items_id": group_id, "itemtype": "Group"})

    await _request("PUT", f"/Ticket/{ticket_id}", json={
        "input": {"_actors": {"assign": actors}, "status": 2}  # status 2 = Processing (assigned)
    })

    if followup:
        await _request("POST", "/ITILFollowup", json={
            "input": {"items_id": ticket_id, "itemtype": "Ticket", "content": followup}
        })

    parts = []
    if user_id:
        parts.append(f"user #{user_id}")
    if group_id:
        parts.append(f"group #{group_id}")
    return f"Ticket #{ticket_id} assigned to {', '.join(parts)} — status set to Assigned."


@mcp.tool()
async def search_assets_by_ip(ip_address: str) -> str:
    """
    Look up a computer/server asset in the GLPI CMDB by IP address.
    Alerts often carry an IP rather than a hostname — use this when get_asset(hostname) returns nothing.
    Returns hostname, OS, location, owner, and last update.
    """
    resp = await _request("GET", "/search/Computer", params={
        "criteria[0][field]": "126",       # field 126 = IP address on NetworkPort
        "criteria[0][searchtype]": "equals",
        "criteria[0][value]": ip_address,
        "forcedisplay[0]": 2,    # ID
        "forcedisplay[1]": 1,    # Name
        "forcedisplay[2]": 31,   # OS
        "forcedisplay[3]": 23,   # Location
        "forcedisplay[4]": 19,   # Last update
        "forcedisplay[5]": 70,   # Owner (user)
        "range": "0-5",
    })
    data = resp.json()

    assets = data.get("data", [])
    if not assets:
        return f"No asset found for IP '{ip_address}' in GLPI CMDB."

    lines = [f"Assets matching IP {ip_address}:"]
    for a in assets:
        lines.append(
            f"  #{a.get('2', '?')} {a.get('1', 'N/A')} | "
            f"OS: {a.get('31', 'N/A')} | "
            f"Location: {a.get('23', 'N/A')} | "
            f"Owner: {a.get('70', 'N/A')} | "
            f"Last update: {a.get('19', 'N/A')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def list_categories(search: Optional[str] = None) -> str:
    """
    List available GLPI ticket categories, optionally filtered by name.
    Use this before create_ticket to find the correct category_id for the incident type
    (e.g. 'network', 'server', 'security', 'application').
    """
    params: dict = {"range": "0-50", "forcedisplay[0]": 2, "forcedisplay[1]": 1}
    if search:
        params["criteria[0][field]"] = "1"
        params["criteria[0][searchtype]"] = "contains"
        params["criteria[0][value]"] = search

    resp = await _request("GET", "/search/ITILCategory", params=params)
    data = resp.json()

    categories = data.get("data", [])
    if not categories:
        return "No categories found in GLPI."

    lines = ["GLPI ticket categories (use category_id in create_ticket):"]
    for c in categories:
        lines.append(f"  ID {c.get('2', '?')} — {c.get('1', 'N/A')}")
    return "\n".join(lines)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
