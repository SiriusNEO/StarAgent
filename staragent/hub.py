from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from staragent.agent_history import (
    HISTORY_AGENTS,
    agent_history_payload,
    history_payload_with_node,
    unavailable_agent_history_payload,
)
from staragent.agent_tools import (
    AGENT_TOOL_UPDATE_TIMEOUT_SECONDS,
    AgentToolUpdateBusyError,
    agent_tools_payload,
    normalize_agent_tools_payload,
    normalize_agent_update_result,
    payload_with_node,
    unknown_agent_tools_payload,
    update_agent_tool,
)
from staragent.auth import node_auth_token
from staragent.event_log import (
    append_node_event,
    ingest_node_events,
    node_ingest_cursor,
)
from staragent.models import SessionConfig, SessionStatus, SessionView
from staragent.paths import state_dir
from staragent.runtime import is_staragent_system_session
from staragent.session_seen import completion_seen, mark_completion_seen
from staragent.state import atomic_write_json, locked_file
from staragent.status import (
    collect_session_navigation_views,
    collect_session_view,
    collect_session_views,
)

NODE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
NODE_MODES = {"lan", "remote"}
NODES_PATH = state_dir() / "nodes.json"
DEFAULT_AGENT_PORT = 8081
NODE_HEARTBEAT_INTERVAL_SECONDS = 15.0
NODE_HEARTBEAT_GRACE_SECONDS = 60.0
NODE_AUTH_FAILURE_STATUS_CODES = {401, 403}
NODE_REQUEST_TIMEOUT_SECONDS = 5.0
NODE_STATUS_REQUEST_TIMEOUT_SECONDS = 8.0
NODE_HEALTH_REQUEST_TIMEOUT_SECONDS = 2.0
NODE_AGENT_TOOL_REQUEST_TIMEOUT_SECONDS = 5.0
NODE_AGENT_UPDATE_REQUEST_TIMEOUT_SECONDS = AGENT_TOOL_UPDATE_TIMEOUT_SECONDS + 15.0
NODE_AGENT_TOOL_HUB_CACHE_SECONDS = 90.0
NODE_AGENT_HISTORY_REQUEST_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class NodeEntry:
    name: str
    url: str | None = None
    mode: str = "lan"

    @property
    def is_local(self) -> bool:
        return self.mode == "local" or self.url in {None, "", "local"}


@dataclass(frozen=True)
class HubSession:
    node_id: str
    view: SessionView

    @property
    def key(self) -> str:
        return f"{self.node_id}/{self.view.name}"

    @property
    def status(self) -> str:
        report = self.view.status_report
        if (
            report
            and report.status == "review"
            and not report.question
            and completion_seen(self.node_id, self.view.name, report.status_revision)
        ):
            return "idle"
        return self.view.status

    @property
    def needs_attention(self) -> bool:
        return self.status == "review"

    def __getattr__(self, name: str):
        return getattr(self.view, name)


def mark_hub_session_seen(session: HubSession) -> bool:
    report = session.view.status_report
    if report and report.status_revision and not report.question:
        mark_completion_seen(session.node_id, session.view.name, report.status_revision)
        return True
    return False


@dataclass(frozen=True)
class NodeView:
    entry: NodeEntry
    status: str
    sessions: tuple[HubSession, ...] = ()
    error: str = ""

    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def mode(self) -> str:
        return "local" if self.entry.is_local else self.entry.mode

    @property
    def endpoint(self) -> str:
        if self.entry.is_local:
            return "local"
        return self.entry.url or ""

    @property
    def session_count(self) -> int:
        return len(self.sessions)

    @property
    def is_removable(self) -> bool:
        return self.name != "local"


@dataclass
class NodeHeartbeat:
    endpoint: str
    sessions: tuple[HubSession, ...]
    last_success: float
    last_health_success: float = 0.0
    failures: int = 0
    last_error: str = ""


@dataclass
class NodeAgentToolsCache:
    endpoint: str
    payload: dict[str, object]
    cached_at: float


NODE_HEARTBEATS: dict[str, NodeHeartbeat] = {}
NODE_REPORTED_STATES: dict[str, tuple[str, str]] = {}
NODE_LOG_SYNC_ERRORS: dict[str, str] = {}
NODE_AGENT_TOOLS: dict[str, NodeAgentToolsCache] = {}
NODE_HEARTBEATS_LOCK = threading.Lock()


def load_nodes() -> list[NodeEntry]:
    nodes = persisted_nodes()
    env_nodes = env_node_entries()
    merged = {node.name: node for node in nodes}
    for node in env_nodes:
        merged[node.name] = node
    if "local" not in merged:
        merged = {"local": NodeEntry(name="local", url="local", mode="local"), **merged}
    return list(merged.values())


def persisted_nodes() -> list[NodeEntry]:
    with locked_file(NODES_PATH):
        return _persisted_nodes_unlocked()


def _persisted_nodes_unlocked() -> list[NodeEntry]:
    if not NODES_PATH.exists():
        return [NodeEntry(name="local", url="local", mode="local")]
    try:
        with NODES_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return [NodeEntry(name="local", url="local", mode="local")]
    nodes = data.get("nodes", data)
    entries = []
    if isinstance(nodes, dict):
        iterator = (
            (
                name,
                data.get("url") if isinstance(data, dict) else data,
                data.get("mode") if isinstance(data, dict) else "",
            )
            for name, data in nodes.items()
        )
    else:
        iterator = ((item.get("name"), item.get("url"), item.get("mode")) for item in nodes)
    for name, url, mode in iterator:
        if name:
            url = str(url or "local")
            entries.append(normalized_node_entry(str(name), url, str(mode or "")))
    return entries or [NodeEntry(name="local", url="local", mode="local")]


def _save_nodes_unlocked(nodes: list[NodeEntry]) -> None:
    payload = {
        "nodes": [
            {"name": node.name, "url": node.url or "local", "mode": node.mode} for node in nodes
        ]
    }
    atomic_write_json(NODES_PATH, payload)


def add_node(name: str, url: str, mode: str = "lan") -> NodeEntry:
    name = name.strip()
    if not NODE_NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Node name may only contain letters, numbers, dot, underscore, colon, or dash"
        )
    if name == "local":
        raise ValueError("local node is built in")
    mode = normalize_node_mode(mode)
    url = normalize_node_url(url)
    entry = NodeEntry(name=name, url=url, mode=mode)
    with locked_file(NODES_PATH):
        nodes = [node for node in _persisted_nodes_unlocked() if node.name != name]
        nodes.append(entry)
        _save_nodes_unlocked(sorted(nodes, key=lambda node: node.name))
    return entry


def normalized_node_entry(name: str, url: str, mode: str = "") -> NodeEntry:
    if url in {None, "", "local"} or name == "local":
        return NodeEntry(name="local", url="local", mode="local")
    return NodeEntry(name=name, url=normalize_node_url(str(url)), mode=normalize_node_mode(mode))


def normalize_node_mode(value: str) -> str:
    mode = (value or "lan").strip().lower()
    if mode not in NODE_MODES:
        raise ValueError("Node mode must be lan or remote")
    return mode


def normalize_node_url(value: str) -> str:
    target = value.strip().rstrip("/")
    if not target:
        raise ValueError("Node or URL is required")
    if "://" in target:
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Use a node/IP or an http(s) node URL")
        return target
    if "/" in target or "?" in target or "#" in target:
        raise ValueError("Use only the node name or IP address")
    parsed = urllib.parse.urlparse(f"http://{target}")
    if not parsed.hostname:
        raise ValueError("Use a node name or IP address")
    if parsed.port:
        return f"http://{target}"
    return f"http://{target}:{DEFAULT_AGENT_PORT}"


def remove_node(name: str) -> None:
    if name == "local":
        raise ValueError("local node cannot be removed")
    with locked_file(NODES_PATH):
        nodes = [node for node in _persisted_nodes_unlocked() if node.name != name]
        _save_nodes_unlocked(sorted(nodes, key=lambda node: node.name))
    with NODE_HEARTBEATS_LOCK:
        NODE_AGENT_TOOLS.pop(name, None)


def env_node_entries() -> list[NodeEntry]:
    raw = os.environ.get("STARAGENT_NODES", "").strip()
    if not raw:
        return []
    if raw.startswith("{"):
        data = json.loads(raw)
        return [normalized_node_entry(str(name), str(url)) for name, url in data.items()]
    nodes = []
    for item in raw.split(","):
        if not item.strip():
            continue
        name, _, url = item.partition("=")
        nodes.append(normalized_node_entry(name.strip(), url.strip() or "local"))
    return nodes


def node_by_name(name: str) -> NodeEntry:
    for node in load_nodes():
        if node.name == name:
            return node
    raise KeyError(name)


def collect_hub_sessions() -> list[HubSession]:
    sessions = []
    for node in collect_node_views():
        sessions.extend(node.sessions)
    return sorted(sessions, key=lambda item: (item.node_id, item.name))


def collect_node_views(prefer_cached: bool = False) -> list[NodeView]:
    entries = load_nodes()
    if len(entries) <= 1:
        return [collect_node_view(node, prefer_cached=prefer_cached) for node in entries]
    with ThreadPoolExecutor(max_workers=min(len(entries), 8)) as executor:
        nodes = list(
            executor.map(
                lambda node: collect_node_view(node, prefer_cached=prefer_cached),
                entries,
            )
        )
    return sorted(nodes, key=lambda item: item.name)


def collect_session_navigation_nodes() -> list[NodeView]:
    """Build the session switcher without remote I/O or expensive local pane parsing."""
    nodes = [collect_node_navigation_view(entry) for entry in load_nodes()]
    return sorted(nodes, key=lambda item: (not item.entry.is_local, item.name))


def collect_node_navigation_view(entry: NodeEntry) -> NodeView:
    """Collect one Node for navigation without contacting any remote Node."""
    if entry.is_local:
        sessions = tuple(
            HubSession(node_id=entry.name, view=view) for view in collect_session_navigation_views()
        )
        return NodeView(entry=entry, status="connected", sessions=sessions)
    return cached_remote_node_view(entry) or NodeView(
        entry=entry,
        status="disconnected",
        error="Waiting for the first Node heartbeat.",
    )


def refresh_remote_node_heartbeats() -> None:
    for node in load_nodes():
        if not node.is_local:
            collect_node_view(node)


def collect_node_view(node: NodeEntry, prefer_cached: bool = False) -> NodeView:
    sessions: list[HubSession] = []
    if node.is_local:
        sessions.extend(
            HubSession(node_id=node.name, view=view) for view in collect_session_views()
        )
        return NodeView(entry=node, status="connected", sessions=tuple(sessions))
    if prefer_cached:
        cached = cached_remote_node_view(node)
        if cached:
            return cached
    try:
        sessions.extend(remote_sessions(node))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        view = remote_node_failure_view(node, exc)
        report_node_connection_state(node, view.status, view.error)
        return view
    session_tuple = tuple(sessions)
    remember_node_heartbeat(node, session_tuple)
    report_node_connection_state(node, "connected")
    return NodeView(entry=node, status="connected", sessions=session_tuple)


def cached_remote_node_view(node: NodeEntry) -> NodeView | None:
    with NODE_HEARTBEATS_LOCK:
        heartbeat = NODE_HEARTBEATS.get(node.name)
        if heartbeat is None or heartbeat.endpoint != node_endpoint(node):
            return None
        age = max(0.0, time.monotonic() - heartbeat.last_success)
        if heartbeat.last_success <= 0 or age > NODE_HEARTBEAT_GRACE_SECONDS:
            return NodeView(entry=node, status="disconnected", error=heartbeat.last_error)
        if heartbeat.failures:
            detail = heartbeat.last_error or f"stale: last heartbeat {int(age)}s ago"
            return NodeView(
                entry=node,
                status="stale",
                sessions=heartbeat.sessions,
                error=detail,
            )
        return NodeView(entry=node, status="connected", sessions=heartbeat.sessions)


def collect_node_session(
    node: NodeEntry,
    name: str,
    *,
    prefer_cached: bool = False,
) -> HubSession | None:
    if node.is_local:
        view = collect_session_view(name)
        return HubSession(node_id=node.name, view=view) if view else None
    if prefer_cached:
        cached = cached_remote_node_view(node)
        if cached and cached.status == "connected":
            session = next((item for item in cached.sessions if item.name == name), None)
            if session:
                return session
    return next(
        (session for session in collect_node_view(node).sessions if session.name == name), None
    )


def remote_sessions(node: NodeEntry) -> list[HubSession]:
    payload = request_json(node, "GET", "/api/sessions", timeout=remote_node_status_timeout())
    capabilities = payload.get("capabilities")
    reported_agent_tools = payload.get("agent_tools")
    if isinstance(reported_agent_tools, dict):
        remember_node_agent_tools(node, reported_agent_tools)
    elif not (isinstance(capabilities, dict) and capabilities.get("agent_tools")):
        remember_node_agent_tools(
            node,
            unknown_agent_tools_payload(
                "Node update required before agent CLI detection is available."
            ),
        )
    if isinstance(capabilities, dict) and capabilities.get("logs"):
        try:
            sync_remote_node_logs(node)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
            report_node_log_sync_failure(node, str(exc))
        else:
            report_node_log_sync_recovered(node)
    return session_payloads_to_views(node, payload)


def sync_remote_node_logs(node: NodeEntry, *, max_pages: int = 4) -> int:
    cursor = node_ingest_cursor(node.name)
    ingested = 0
    for _ in range(max(1, max_pages)):
        query = urllib.parse.urlencode({"after": cursor, "limit": 250})
        payload = request_json(
            node,
            "GET",
            f"/api/logs?{query}",
            timeout=remote_node_status_timeout(),
        )
        events = payload.get("events")
        if not isinstance(events, list):
            raise ValueError("Node returned an invalid log payload")
        ingested += ingest_node_events(node.name, events)
        next_cursor = str(payload.get("next_cursor") or "")
        if next_cursor:
            cursor = next_cursor
        elif events:
            last = events[-1]
            cursor = str(last.get("id") or "") if isinstance(last, dict) else cursor
        if not payload.get("has_more") or not events:
            break
    return ingested


def remote_node_failure_view(node: NodeEntry, exc: Exception) -> NodeView:
    error = str(exc)
    if is_remote_auth_failure(exc):
        forget_node_heartbeat(node)
        remember_node_failure(node, error)
        return NodeView(entry=node, status="disconnected", error=error)

    if remote_node_health_ok(node):
        heartbeat = remember_node_health(node, error)
        detail = f"stale: health ok; sessions unavailable: {error}"
        if heartbeat:
            return NodeView(entry=node, status="stale", sessions=heartbeat.sessions, error=detail)
        return NodeView(entry=node, status="stale", error=detail)

    heartbeat = remember_node_failure(node, error)
    if heartbeat and node_heartbeat_is_fresh(heartbeat):
        age = max(0, int(time.monotonic() - heartbeat.last_success))
        detail = f"stale: last heartbeat {age}s ago; {error}"
        return NodeView(entry=node, status="stale", sessions=heartbeat.sessions, error=detail)
    return NodeView(entry=node, status="disconnected", error=error)


def remember_node_heartbeat(node: NodeEntry, sessions: tuple[HubSession, ...]) -> None:
    now = time.monotonic()
    with NODE_HEARTBEATS_LOCK:
        NODE_HEARTBEATS[node.name] = NodeHeartbeat(
            endpoint=node_endpoint(node),
            sessions=sessions,
            last_success=now,
            last_health_success=now,
        )


def remember_node_health(node: NodeEntry, error: str) -> NodeHeartbeat | None:
    with NODE_HEARTBEATS_LOCK:
        heartbeat = NODE_HEARTBEATS.get(node.name)
        if heartbeat is None or heartbeat.endpoint != node_endpoint(node):
            NODE_HEARTBEATS[node.name] = NodeHeartbeat(
                endpoint=node_endpoint(node),
                sessions=(),
                last_success=0.0,
                last_health_success=time.monotonic(),
                failures=1,
                last_error=error,
            )
            return None
        heartbeat.failures += 1
        heartbeat.last_error = error
        heartbeat.last_health_success = time.monotonic()
        return heartbeat


def remember_node_failure(node: NodeEntry, error: str) -> NodeHeartbeat | None:
    with NODE_HEARTBEATS_LOCK:
        heartbeat = NODE_HEARTBEATS.get(node.name)
        if heartbeat is None or heartbeat.endpoint != node_endpoint(node):
            heartbeat = NodeHeartbeat(
                endpoint=node_endpoint(node),
                sessions=(),
                last_success=0.0,
                failures=1,
                last_error=error,
            )
            NODE_HEARTBEATS[node.name] = heartbeat
            return heartbeat
        heartbeat.failures += 1
        heartbeat.last_error = error
        return heartbeat


def forget_node_heartbeat(node: NodeEntry) -> None:
    with NODE_HEARTBEATS_LOCK:
        NODE_HEARTBEATS.pop(node.name, None)


def clear_node_heartbeat_cache() -> None:
    with NODE_HEARTBEATS_LOCK:
        NODE_HEARTBEATS.clear()
        NODE_REPORTED_STATES.clear()
        NODE_LOG_SYNC_ERRORS.clear()
        NODE_AGENT_TOOLS.clear()


def node_agent_tools_payload(node: NodeEntry, *, refresh: bool = False) -> dict[str, object]:
    if node.is_local:
        return payload_with_node(
            agent_tools_payload(force=refresh),
            node.name,
        )
    cached = cached_node_agent_tools(node)
    if cached and not refresh and not cached.get("stale"):
        return cached
    path = f"/api/agent-tools?{urllib.parse.urlencode({'refresh': str(refresh).lower()})}"
    try:
        payload = request_json(
            node,
            "GET",
            path,
            timeout=NODE_AGENT_TOOL_REQUEST_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 405}:
            return payload_with_node(
                unknown_agent_tools_payload(
                    "Node update required before agent CLI detection is available."
                ),
                node.name,
            )
        return stale_or_unknown_node_agent_tools(node, cached, str(exc))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return stale_or_unknown_node_agent_tools(node, cached, str(exc))
    remember_node_agent_tools(node, payload)
    return payload_with_node(payload, node.name)


def node_agent_tool_update_payload(node: NodeEntry, agent: str) -> dict[str, object]:
    if node.is_local:
        result = update_agent_tool(agent)
    else:
        path = f"/api/agent-tools/{urllib.parse.quote(agent, safe='')}/update"
        try:
            payload = request_json(
                node,
                "POST",
                path,
                timeout=NODE_AGENT_UPDATE_REQUEST_TIMEOUT_SECONDS,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in {404, 405}:
                error = "Node update required before browser-managed CLI updates are available."
            elif exc.code == 409:
                raise AgentToolUpdateBusyError(
                    f"{agent} is already being updated on {node.name}."
                ) from exc
            else:
                error = f"Node rejected the update request: HTTP {exc.code}."
            payload = {"ok": False, "agent": agent, "error": error}
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            payload = {
                "ok": False,
                "agent": agent,
                "error": f"Node update request failed: {exc}",
            }
        result = normalize_agent_update_result(agent, payload)
    result["node"] = node.name
    if result.get("ok"):
        with NODE_HEARTBEATS_LOCK:
            NODE_AGENT_TOOLS.pop(node.name, None)
    return result


def node_agent_history_payload(
    node: NodeEntry,
    *,
    agent: str = "",
    limit: int = 50,
    refresh: bool = False,
) -> dict[str, object]:
    normalized_agent = str(agent or "").strip().lower()
    if normalized_agent and normalized_agent not in HISTORY_AGENTS:
        raise ValueError(f"History scanning is not supported for: {normalized_agent}")
    bounded_limit = max(1, min(int(limit), 100))
    if node.is_local:
        return history_payload_with_node(
            agent_history_payload(
                agent=normalized_agent,
                limit=bounded_limit,
                force=refresh,
            ),
            node.name,
        )
    path = "/api/agent-history?" + urllib.parse.urlencode(
        {
            "agent": normalized_agent,
            "limit": bounded_limit,
            "refresh": str(refresh).lower(),
        }
    )
    try:
        payload = request_json(
            node,
            "GET",
            path,
            timeout=NODE_AGENT_HISTORY_REQUEST_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as exc:
        error = (
            "Node update required before conversation history scanning is available."
            if exc.code in {404, 405}
            else f"History scan failed: {exc}"
        )
        return history_payload_with_node(unavailable_agent_history_payload(error), node.name)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return history_payload_with_node(
            unavailable_agent_history_payload(f"Node is unavailable: {exc}"),
            node.name,
        )
    return history_payload_with_node(payload, node.name)


def remember_node_agent_tools(node: NodeEntry, payload: object) -> None:
    normalized = normalize_agent_tools_payload(payload)
    with NODE_HEARTBEATS_LOCK:
        NODE_AGENT_TOOLS[node.name] = NodeAgentToolsCache(
            endpoint=node_endpoint(node),
            payload=normalized,
            cached_at=time.monotonic(),
        )


def cached_node_agent_tools(node: NodeEntry) -> dict[str, object] | None:
    with NODE_HEARTBEATS_LOCK:
        cached = NODE_AGENT_TOOLS.get(node.name)
        if cached is None or cached.endpoint != node_endpoint(node):
            return None
        age = max(0.0, time.monotonic() - cached.cached_at)
        heartbeat = NODE_HEARTBEATS.get(node.name)
        connection_stale = bool(
            heartbeat
            and (
                heartbeat.failures
                or heartbeat.last_success <= 0
                or not node_heartbeat_is_fresh(heartbeat)
            )
        )
        payload = dict(cached.payload)
    return payload_with_node(
        payload,
        node.name,
        stale=connection_stale or age > NODE_AGENT_TOOL_HUB_CACHE_SECONDS,
    )


def stale_or_unknown_node_agent_tools(
    node: NodeEntry,
    cached: dict[str, object] | None,
    error: str,
) -> dict[str, object]:
    if cached:
        return payload_with_node(cached, node.name, stale=True, error=error)
    return payload_with_node(
        unknown_agent_tools_payload(f"Node is unavailable: {error}", stale=True),
        node.name,
        stale=True,
    )


def report_node_connection_state(node: NodeEntry, status: str, error: str = "") -> None:
    endpoint = node_endpoint(node)
    with NODE_HEARTBEATS_LOCK:
        previous = NODE_REPORTED_STATES.get(node.name)
        current = (endpoint, status)
        if previous == current:
            return
        NODE_REPORTED_STATES[node.name] = current
    previous_status = previous[1] if previous and previous[0] == endpoint else ""
    if status == "connected":
        event = (
            "node.recovered" if previous_status in {"stale", "disconnected"} else "node.connected"
        )
        message = (
            "Node connection recovered."
            if event == "node.recovered"
            else "Node connected to the Hub."
        )
        level = "info"
    elif status == "stale":
        event = "node.stale"
        message = "Node health is reachable, but session status is stale."
        level = "warning"
    else:
        event = "node.disconnected"
        message = "Node disconnected from the Hub."
        level = "error"
    details: dict[str, object] = {"endpoint": endpoint}
    if previous_status:
        details["previous_status"] = previous_status
    if error:
        details["error"] = error
    append_node_event(
        node.name,
        level,
        event,
        message,
        source="hub.heartbeat",
        details=details,
    )


def report_node_log_sync_failure(node: NodeEntry, error: str) -> None:
    with NODE_HEARTBEATS_LOCK:
        if NODE_LOG_SYNC_ERRORS.get(node.name) == error:
            return
        NODE_LOG_SYNC_ERRORS[node.name] = error
    append_node_event(
        node.name,
        "warning",
        "logs.sync_failed",
        "Hub could not sync this Node's log outbox.",
        source="hub.logs",
        details={"error": error},
    )


def report_node_log_sync_recovered(node: NodeEntry) -> None:
    with NODE_HEARTBEATS_LOCK:
        previous_error = NODE_LOG_SYNC_ERRORS.pop(node.name, "")
    if not previous_error:
        return
    append_node_event(
        node.name,
        "info",
        "logs.sync_recovered",
        "Node log synchronization recovered.",
        source="hub.logs",
    )


def node_heartbeat_is_fresh(heartbeat: NodeHeartbeat) -> bool:
    return time.monotonic() - heartbeat.last_success <= NODE_HEARTBEAT_GRACE_SECONDS


def node_endpoint(node: NodeEntry) -> str:
    return "local" if node.is_local else node.url or ""


def is_remote_auth_failure(exc: Exception) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code in NODE_AUTH_FAILURE_STATUS_CODES


def remote_node_health_ok(node: NodeEntry) -> bool:
    try:
        request_json(node, "GET", "/api/health", timeout=remote_node_health_timeout())
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False
    return True


def session_payloads_to_views(node: NodeEntry, payload: dict) -> list[HubSession]:
    sessions = []
    for item in payload.get("sessions", []):
        status = SessionStatus.from_dict(item)
        if is_staragent_system_session(status.name):
            status = SessionStatus.from_dict(
                {**item, "session_type": "system", "agent": status.name}
            )
        config = SessionConfig(
            name=status.name,
            node=node.name,
            agent=status.agent,
            repo=status.repo,
            branch=status.branch,
            task=status.task,
        )
        view = SessionView(
            config=config,
            status_report=status,
        )
        sessions.append(HubSession(node_id=node.name, view=view))
    return sessions


def request_json(
    node: NodeEntry,
    method: str,
    path: str,
    body: dict | None = None,
    timeout: float = NODE_REQUEST_TIMEOUT_SECONDS,
) -> dict:
    if node.is_local:
        raise ValueError("local node does not use remote node requests")
    data = None
    headers = remote_node_headers()
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        urllib.parse.urljoin(node.url.rstrip("/") + "/", path.lstrip("/")),
        data=data,
        headers=headers,
        method=method,
    )
    with open_node_request(node, request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def request_raw(
    node: NodeEntry,
    method: str,
    path: str,
    timeout: float = NODE_REQUEST_TIMEOUT_SECONDS,
) -> tuple[bytes, str]:
    if node.is_local:
        raise ValueError("local node does not use remote node requests")
    request = urllib.request.Request(
        urllib.parse.urljoin(node.url.rstrip("/") + "/", path.lstrip("/")),
        headers=remote_node_headers(),
        method=method,
    )
    with open_node_request(node, request, timeout=timeout) as response:
        media_type = response.headers.get_content_type() or "application/octet-stream"
        return response.read(), media_type


def open_node_request(
    node: NodeEntry,
    request: urllib.request.Request,
    timeout: float = NODE_REQUEST_TIMEOUT_SECONDS,
):
    if node.mode == "lan":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def remote_node_status_timeout() -> float:
    raw = os.environ.get("STARAGENT_NODE_STATUS_TIMEOUT", "").strip()
    if not raw:
        return NODE_STATUS_REQUEST_TIMEOUT_SECONDS
    try:
        return max(0.1, float(raw))
    except ValueError:
        return NODE_STATUS_REQUEST_TIMEOUT_SECONDS


def remote_node_health_timeout() -> float:
    raw = os.environ.get("STARAGENT_NODE_HEALTH_TIMEOUT", "").strip()
    if not raw:
        return NODE_HEALTH_REQUEST_TIMEOUT_SECONDS
    try:
        return max(0.1, float(raw))
    except ValueError:
        return NODE_HEALTH_REQUEST_TIMEOUT_SECONDS


def websocket_url(node: NodeEntry, path: str) -> str:
    if node.is_local:
        raise ValueError("local node does not use remote websocket URL")
    parsed = urllib.parse.urlparse(node.url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base = parsed._replace(scheme=scheme).geturl().rstrip("/")
    url = urllib.parse.urljoin(base + "/", path.lstrip("/"))
    token = remote_node_token()
    if not token:
        return url
    separator = "&" if urllib.parse.urlparse(url).query else "?"
    return f"{url}{separator}token={urllib.parse.quote(token)}"


def remote_node_headers() -> dict[str, str]:
    token = remote_node_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def remote_node_token() -> str:
    return node_auth_token()
