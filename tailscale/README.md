# Tailscale Networking

StarAgent does not manage Tailscale. Set up the network first, then add the reachable StarAgent node endpoint in the Hub dashboard.

The Hub only needs this to work:

```bash
curl http://<remote-100.x-ip>:8081/api/health
```

## Scripts

From the project root:

```bash
tailscale/scripts/one-click-remote-node.sh
```

Useful scripts:

```bash
tailscale/scripts/status.sh
tailscale/scripts/check-network.sh 100.x.x.x
tailscale/scripts/join-systemd.sh
tailscale/scripts/join-userspace.sh
tailscale/scripts/expose-agent.sh
```

## Normal Linux or VM

Run Tailscale normally:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable --now tailscaled
sudo tailscale up --ssh
```

Start the StarAgent node locally and expose it to the tailnet:

```bash
export STARAGENT_NODE_TOKEN="<same token as the Hub>"
staragent node-ts
tailscale ip -4
```

Add the printed `100.x` address in the Hub dashboard as a Remote node.

## No systemd or Container Shell

Run `tailscaled` in userspace networking mode:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
mkdir -p .staragent/tailscale-state
tmux new -ds staragent-tailscaled "sudo tailscaled --tun=userspace-networking --socket=.staragent/tailscaled.sock --statedir=.staragent/tailscale-state"
sudo tailscale --socket=.staragent/tailscaled.sock up --ssh
```

Start the StarAgent node locally and expose it to the tailnet:

```bash
export STARAGENT_NODE_TOKEN="<same token as the Hub>"
staragent node-ts --tailscale-socket .staragent/tailscaled.sock
sudo tailscale --socket=.staragent/tailscaled.sock ip -4
```

Add the printed `100.x` address in the Hub dashboard as a Remote node.
