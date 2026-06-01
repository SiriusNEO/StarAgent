from __future__ import annotations

import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from staragent.models import SessionConfig, SessionStatus, SessionView
from staragent.paths import state_dir
from staragent.runtime import is_staragent_system_session
from staragent.status import collect_session_views

NODE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
NODE_MODES = {"lan", "remote"}
NODES_PATH = state_dir() / "nodes.json"
DEFAULT_AGENT_PORT = 8081


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

    def __getattr__(self, name: str):
        return getattr(self.view, name)


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


def save_nodes(nodes: list[NodeEntry]) -> None:
    NODES_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "nodes": [
            {"name": node.name, "url": node.url or "local", "mode": node.mode} for node in nodes
        ]
    }
    NODES_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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
    nodes = [node for node in persisted_nodes() if node.name != name]
    entry = NodeEntry(name=name, url=url, mode=mode)
    nodes.append(entry)
    save_nodes(sorted(nodes, key=lambda node: node.name))
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
    nodes = [node for node in persisted_nodes() if node.name != name]
    save_nodes(sorted(nodes, key=lambda node: node.name))


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


def collect_node_views() -> list[NodeView]:
    nodes = []
    for node in load_nodes():
        nodes.append(collect_node_view(node))
    return sorted(nodes, key=lambda item: item.name)


def collect_node_view(node: NodeEntry) -> NodeView:
    sessions: list[HubSession] = []
    if node.is_local:
        sessions.extend(
            HubSession(node_id=node.name, view=view) for view in collect_session_views()
        )
        return NodeView(entry=node, status="connected", sessions=tuple(sessions))
    try:
        sessions.extend(remote_sessions(node))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return NodeView(entry=node, status="disconnected", error=str(exc))
    return NodeView(entry=node, status="connected", sessions=tuple(sessions))


def remote_sessions(node: NodeEntry) -> list[HubSession]:
    payload = request_json(node, "GET", "/api/sessions")
    return session_payloads_to_views(node, payload)


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


def request_json(node: NodeEntry, method: str, path: str, body: dict | None = None) -> dict:
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
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


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
    return (
        os.environ.get("STARAGENT_NODE_TOKEN", "").strip()
        or os.environ.get("STARAGENT_AUTH_TOKEN", "").strip()
    )


def valid_remote_node_token(value: str) -> bool:
    token = remote_node_token()
    return bool(token) and secrets.compare_digest(value or "", token)
