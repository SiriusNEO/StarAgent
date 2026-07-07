---
name: staragent-use
description: Use this skill when operating StarAgent as a user: starting the Hub or Nodes, creating sessions, attaching to tmux, managing nodes, or diagnosing runtime connectivity.
---

# StarAgent Use

StarAgent is a tmux-first dashboard for long-lived coding CLI sessions. Live tmux sessions are the source of truth. The web dashboard, Chat, Terminal, and file views are projections over real tmux state.

## Concepts

- **Hub**: the web dashboard and coordinator. It also acts as the local node.
- **Node**: a machine that owns tmux sessions and exposes them through the StarAgent node API.
- **Agent session**: a tmux session running Codex, Claude Code, Gemini, OpenCode, or shell.
- **System session**: infrastructure tmux session such as `staragent-hub`, `staragent-node`, or `staragent-tailscaled`.
- **Terminal**: live tmux PTY view and ground-truth display.
- **Chat**: mobile-friendly structured view derived from the tmux transcript.

Reserve `agent` for coding CLIs. Use `node` for infrastructure hosts.

## Runtime State

Project-local state should live in `.staragent/` by default.

Important environment variables:

- `STARAGENT_AUTH_TOKEN`: dashboard auth token for the Hub.
- `STARAGENT_NODE_TOKEN`: shared token used by remote nodes.
- `STARAGENT_STATE_DIR`: optional runtime state directory override.

## Start StarAgent

Install:

```bash
pip install -e '.[dev]'
```

Start the Hub:

```bash
staragent hub --host 0.0.0.0 --port 8080
```

`staragent hub` creates the `staragent-hub` tmux system session by default.

Start a remote Node:

```bash
export STARAGENT_NODE_TOKEN="<same token as the Hub>"
staragent node-ts
```

Use `staragent node` instead when LAN or another network layer already exposes the Node.
Add the node endpoint from the dashboard Nodes page after it is reachable from the Hub.

## Sessions

Create a local session:

```bash
staragent create my-session -C /path/to/workspace -p codex-yolo
```

Create a remote session:

```bash
staragent create my-session -C /path/to/workspace -p codex-yolo --node <node-name>
```

List local sessions:

```bash
staragent ps
```

Attach directly:

```bash
tmux attach -t <session-name>
```

Stop a session:

```bash
staragent kill <session-name>
```

## Dashboard Workflow

1. Use Nodes to add reachable machines.
2. Use Sessions to create or adopt tmux sessions.
3. Use Chat for normal agent interaction, especially on mobile.
4. Use Terminal to inspect the exact tmux UI or debug Chat parsing.
5. Use File Explorer and Changed Files to inspect the workspace owned by the session node.

When Chat and Terminal disagree, trust Terminal first.

## Networking

StarAgent only needs a reachable node endpoint. It does not manage VPN setup.

Supported layouts:

- Local only: Hub and sessions run on one machine.
- LAN: Hub reaches `http://<lan-ip>:8081`.
- Tailscale: Hub reaches `http://<100.x-ip>:8081` or a tailnet DNS name.

Tailscale setup and helper scripts live under `tailscale/`.

## Remote Hub/Node Checklist

Validate remote Node connectivity from the Hub machine, not from the browser device. The dashboard can only add a Node if the Hub process can reach that Node URL.

1. On the remote machine, start the Node with the Hub token:

```bash
export STARAGENT_NODE_TOKEN="<same token as the Hub>"
staragent node-ts
```

2. For LAN or another network layer that does not need `tailscale serve`, use `staragent node` instead.

3. From the Hub machine, verify network reachability:

```bash
staragent verify-node <node-host-or-100.x-ip>
```

This checks both `/api/health` and authenticated `/api/sessions`. Use `--mode remote` only when the endpoint should use normal proxy handling instead of LAN/Tailscale no-proxy handling.

4. If the command fails, verify the raw endpoints from the Hub machine:

```bash
curl --noproxy '*' http://<node-host-or-100.x-ip>:8081/api/health
curl --noproxy '*' \
  -H "Authorization: Bearer $STARAGENT_AUTH_TOKEN" \
  http://<node-host-or-100.x-ip>:8081/api/sessions
```

5. Add exactly that working endpoint in the dashboard Nodes page, for example `http://100.x.x.x:8081` or `http://node.tailnet-name.ts.net:8081`.

Common failure modes:

- Use the actual Tailscale node name or `100.x` IP. A local SSH alias may not resolve from the Hub.
- If shell proxies are configured, use `--noproxy '*'` when testing tailnet or LAN endpoints.
- Health can succeed while `/api/sessions` fails if the token is wrong or missing.
- A slow Node with many tmux sessions can make Add look like a timeout; check `/api/sessions` latency from the Hub.
- In userspace Tailscale, always pass the same `--socket` used to start `tailscaled`.

Useful Tailscale checks:

```bash
tailscale status
tailscale ping <node-name-or-100.x-ip>
tailscale ip -4
```

For userspace Tailscale:

```bash
sudo tailscale --socket=.staragent/tailscaled.sock status
sudo tailscale --socket=.staragent/tailscaled.sock ping <node-name-or-100.x-ip>
sudo tailscale --socket=.staragent/tailscaled.sock ip -4
```

## Runtime Checks

Check the dashboard service:

```bash
systemctl status staragent-dashboard.service --no-pager
```

Check nodes through the Hub API:

```bash
curl -H "Authorization: Bearer $STARAGENT_AUTH_TOKEN" http://127.0.0.1:8080/api/nodes
```
