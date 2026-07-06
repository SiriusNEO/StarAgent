---
name: staragent-dev
description: Use this skill when developing or refactoring StarAgent code, fixing dashboard behavior, changing tmux/session logic, editing node APIs, or validating StarAgent changes.
---

# StarAgent Development

StarAgent is a tmux-first control plane for coding CLI sessions. Treat live tmux state as the behavioral source of truth. Config and persisted metadata should only enrich or route live tmux state, not replace it.

## Architecture

- **Hub**: web dashboard, coordinator, local node, and proxy for remote node APIs.
- **Node**: machine-local API that owns tmux sessions and workspace file access.
- **Agent session**: tmux session running a coding CLI.
- **System session**: tmux session running infrastructure such as `staragent-hub`, `staragent-node`, or `staragent-tailscaled`.
- **Terminal**: live tmux PTY view.
- **Chat**: transcript projection from tmux pane output using CLI-specific parsers.

Keep the `agent` vs `node` vocabulary clean: agents are coding CLIs, nodes are machines.

## Key Files

- `staragent/main.py`: Typer CLI commands.
- `staragent/hub.py`: node registry and remote request helpers.
- `staragent/node/app.py`: remote node API.
- `staragent/dashboard/app.py`: Hub dashboard routes and API surface.
- `staragent/runtime.py`: tmux session operations.
- `staragent/status.py`: session collection and live status.
- `staragent/transcript.py` and `staragent/session_parser.py`: Chat transcript parsing.
- `staragent/presets.py`: coding CLI command presets.
- `staragent/dashboard/templates/`: Jinja views.
- `staragent/dashboard/static/`: CSS and browser-side JS.

## Development Rules

- Keep tmux as the source of truth.
- Keep local and remote behavior aligned; remote sessions should go through the node API like normal network traffic.
- Preserve the distinction between `agent` and `system` sessions.
- Avoid compatibility aliases for renamed concepts unless explicitly requested. This project prefers clean refactors.
- Keep StarAgent runtime files project-local under `.staragent/` unless `STARAGENT_STATE_DIR` explicitly overrides it.
- Keep Tailscale-specific operational docs under `tailscale/`; core StarAgent code should only require a reachable endpoint.
- For UI changes, check desktop and mobile behavior for Sessions, Nodes, Chat, Terminal, File Explorer, and Changed Files.

## Remote Node Debugging

Remote Node bugs should be debugged from the Hub process perspective.

- The browser only talks to the Hub; Node reachability means the Hub can reach the configured Node URL.
- Use `staragent verify-node <endpoint>` as the first CLI-level check; it validates both `/api/health` and authenticated `/api/sessions`.
- Test `GET /api/health` first because it does not require auth.
- Test `GET /api/sessions` with `Authorization: Bearer $STARAGENT_AUTH_TOKEN` next because it exercises token handling and tmux collection.
- Use `curl --noproxy '*'` for LAN or Tailscale endpoints when proxy variables may be set.
- Prefer real Tailscale DNS names or `100.x` IPs over SSH aliases; aliases may only exist in one shell's SSH config.
- If Add Node fails but health works, inspect authenticated `/api/sessions`, timeout behavior, and node registry persistence.
- Remote collection can be slower than local tmux calls; keep UI and API timeouts explicit and long enough for realistic remote tmux scans.
- Do not move Tailscale lifecycle management into core Hub/Node logic. Keep StarAgent focused on reachable HTTP/WebSocket endpoints.

## Chat And Terminal

Terminal is the ground-truth tmux view. Chat is derived from captured tmux output.

When fixing Chat:

- Prefer CLI-specific parsing over generic string guessing.
- Preserve turn ordering across refreshes.
- Avoid duplicate user or agent messages.
- Keep Working state synchronized with the actual CLI state when possible.
- If Chat and Terminal disagree, debug from captured tmux output first.

## Validation

Run focused checks after code changes:

```bash
ruff check staragent tests
pytest
```

For service or UI/runtime changes, also verify the running Hub:

```bash
systemctl status staragent-dashboard.service --no-pager
curl -H "Authorization: Bearer $STARAGENT_AUTH_TOKEN" http://127.0.0.1:8080/api/nodes
```

If a broad formatting check fails only because unrelated existing files are unformatted, report that clearly and avoid formatting unrelated files.

## Deployment Notes

The usual systemd deployment runs:

```bash
staragent hub --host 0.0.0.0 --port 8080 --session staragent-hub
```

After code changes that affect the dashboard process, restart:

```bash
systemctl restart staragent-dashboard.service
```
