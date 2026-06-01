from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any


def tailscale_hub_payload() -> dict[str, object]:
    if not shutil.which("tailscale"):
        return {
            "available": False,
            "installed": False,
            "running": False,
            "error": "tailscale command not found",
            "peers": [],
        }
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "installed": True,
            "running": False,
            "error": "tailscale status timed out",
            "peers": [],
        }
    except OSError as exc:
        return {
            "available": False,
            "installed": True,
            "running": False,
            "error": str(exc),
            "peers": [],
        }
    if result.returncode != 0:
        return {
            "available": False,
            "installed": True,
            "running": False,
            "error": result.stderr.strip() or result.stdout.strip() or "tailscale status failed",
            "peers": [],
        }
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {
            "available": False,
            "installed": True,
            "running": False,
            "error": f"invalid tailscale status JSON: {exc}",
            "peers": [],
        }

    backend_state = str(data.get("BackendState") or "")
    self_node = node_payload(data.get("Self") or {})
    peers = [
        node_payload(peer) for peer in (data.get("Peer") or {}).values() if isinstance(peer, dict)
    ]
    peers.sort(key=lambda peer: (not bool(peer["online"]), str(peer["name"]).lower()))
    return {
        "available": backend_state == "Running" and bool(self_node.get("addresses")),
        "installed": True,
        "running": backend_state == "Running",
        "backend_state": backend_state,
        "tailnet": (data.get("CurrentTailnet") or {}).get("Name") or "",
        "magic_dns_suffix": data.get("MagicDNSSuffix") or "",
        "self": self_node,
        "peers": peers,
        "error": "",
    }


def node_payload(value: dict[str, Any]) -> dict[str, object]:
    addresses = [str(item) for item in value.get("TailscaleIPs") or []]
    dns_name = str(value.get("DNSName") or "").rstrip(".")
    name = str(value.get("HostName") or dns_name or (addresses[0] if addresses else "unknown"))
    preferred = dns_name or (addresses[0] if addresses else name)
    return {
        "name": name,
        "dns_name": dns_name,
        "preferred_node": preferred,
        "addresses": addresses,
        "os": value.get("OS") or "",
        "online": bool(value.get("Online")),
        "active": bool(value.get("Active")),
        "relay": value.get("Relay") or "",
        "endpoint": f"http://{preferred}:8081" if preferred else "",
    }
