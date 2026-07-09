const dependencyPanel = document.querySelector(".dependency-panel");
if (dependencyPanel) {
  const status = dependencyPanel.querySelector(".dependency-status");
  const list = dependencyPanel.querySelector(".dependency-list");
  const rowFor = (item) => {
    const row = document.createElement("div");
    row.className = "dependency-row";
    const info = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.label;
    const detail = document.createElement("span");
    detail.textContent = item.installed ? (item.version || "installed") : item.install_command;
    const note = document.createElement("span");
    note.className = "dependency-note";
    note.textContent = `${item.required ? "Required" : "Optional"} · ${item.note || ""}`.trim();
    info.append(title, detail);
    if (note.textContent) {
      info.appendChild(note);
    }
    const pill = document.createElement("span");
    const state = item.installed ? "connected" : (item.required ? "disconnected" : "optional");
    pill.className = `pill node-status-${state}`;
    pill.textContent = item.installed ? "installed" : (item.required ? "missing" : "optional");
    row.append(info, pill);
    return row;
  };
  const renderDependencies = (items) => {
    list.innerHTML = "";
    for (const item of items) {
      list.appendChild(rowFor(item));
    }
  };
  const checkDependencies = async () => {
    const response = await fetch("/api/dependencies");
    const body = await response.json();
    const items = body.dependencies || [];
    renderDependencies(items);
    const missingRequired = items.filter((item) => item.required && !item.installed);
    const missingOptional = items.filter((item) => !item.required && !item.installed);
    if (!missingRequired.length) {
      status.textContent = missingOptional.length
        ? "Required dependencies are installed. Tailscale is optional for LAN-only setups."
        : "All dependencies are installed.";
      return;
    }
    status.textContent = `Installing ${missingRequired.map((item) => item.label).join(", ")}...`;
    const ensure = await fetch("/api/dependencies/ensure", {method: "POST"});
    const ensured = await ensure.json();
    const next = ensured.dependencies || [];
    renderDependencies(next);
    const failed = next.filter((item) => item.required && !item.installed);
    status.textContent = failed.length
      ? `Install failed for ${failed.map((item) => item.label).join(", ")}. Run the shown command manually.`
      : "Dependencies installed.";
  };
  window.StarAgentAfterPaint(() => {
    checkDependencies().catch((error) => {
      status.textContent = error.message || "Dependency check failed.";
    });
  });
}

const nodeForm = document.querySelector(".node-form");
if (nodeForm) {
  const status = nodeForm.querySelector(".node-status");
  nodeForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(nodeForm);
    const payload = {
      mode: String(form.get("mode") || "lan").trim(),
      name: String(form.get("name") || "").trim(),
      url: endpointFromHostPort(String(form.get("host") || ""), String(form.get("port") || "8081"))
    };
    if (!payload.name || !payload.url) {
      status.textContent = "Name and node are required.";
      return;
    }
    status.textContent = "Adding...";
    try {
      await addNode(payload, status);
    } catch (error) {
      status.textContent = error.message;
    }
  });
}

async function addNode(payload, statusEl) {
  const response = await fetch("/api/nodes", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Add node failed.");
  }
  if (statusEl) {
    statusEl.textContent = "Added.";
  }
  window.location.reload();
}

function endpointFromHostPort(host, port) {
  const target = String(host || "").trim().replace(/^https?:\/\//, "").replace(/\/+$/, "");
  const selectedPort = String(port || "").trim();
  if (!target || !selectedPort) {
    return target;
  }
  if (/[/?#]/.test(target)) {
    return target;
  }
  if (target.includes(":") && !target.startsWith("[") && target.split(":").length === 2) {
    return target;
  }
  return `${target}:${selectedPort}`;
}

const tailscaleDashboard = document.querySelector(".tailscale-dashboard");
if (tailscaleDashboard) {
  let tailscaleLoaded = false;
  const tailnet = tailscaleDashboard.querySelector(".tailscale-tailnet");
  const hubIp = tailscaleDashboard.querySelector(".tailscale-hub-ip");
  const peerCount = tailscaleDashboard.querySelector(".tailscale-peer-count");
  const refresh = tailscaleDashboard.querySelector(".tailscale-refresh");
  const peersEl = tailscaleDashboard.querySelector(".tailscale-peers");

  const loadTailscale = async () => {
    tailscaleLoaded = true;
    tailscaleDashboard.hidden = false;
    tailnet.textContent = "Checking Hub Tailscale...";
    hubIp.textContent = "-";
    peerCount.textContent = "-";
    peersEl.innerHTML = "";
    const response = await fetch("/api/tailscale/hub");
    const body = await response.json();
    if (!body.available) {
      tailnet.textContent = body.error || `State: ${body.backend_state || "unavailable"}`;
      peersEl.innerHTML = '<div class="explorer-empty">Hub Tailscale is not running.</div>';
      return;
    }
    const self = body.self || {};
    const peers = body.peers || [];
    tailnet.textContent = body.tailnet || body.magic_dns_suffix || "Running";
    hubIp.textContent = (self.addresses || [])[0] || self.preferred_node || "-";
    peerCount.textContent = `${peers.filter((peer) => peer.online).length} / ${peers.length} online`;
    if (!peers.length) {
      peersEl.innerHTML = '<div class="explorer-empty">No tailnet peers found.</div>';
      return;
    }
    for (const peer of peers) {
      peersEl.appendChild(tailscalePeerRow(peer));
    }
  };

  const tailscalePeerRow = (peer) => {
    const row = document.createElement("div");
    row.className = "tailscale-peer";
    const info = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = peer.name || peer.preferred_node || "unknown";
    const meta = document.createElement("span");
    meta.textContent = [
      peer.preferred_node,
      peer.os,
      peer.relay ? `relay ${peer.relay}` : ""
    ].filter(Boolean).join(" · ");
    info.append(name, meta);
    const state = document.createElement("span");
    state.className = `pill node-status-${peer.online ? "connected" : "disconnected"}`;
    state.textContent = peer.online ? "online" : "offline";
    const addFields = document.createElement("div");
    addFields.className = "tailscale-add-fields";
    const hostCode = document.createElement("code");
    hostCode.className = "inline-command";
    hostCode.textContent = peer.preferred_node || "";
    const portInput = document.createElement("input");
    portInput.className = "tailscale-port-input";
    portInput.type = "number";
    portInput.min = "1";
    portInput.max = "65535";
    portInput.value = "8081";
    portInput.inputMode = "numeric";
    portInput.ariaLabel = "Node port";
    addFields.append(hostCode, portInput);
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Add Tailscale";
    button.disabled = !peer.preferred_node;
    button.addEventListener("click", async () => {
      const endpointValue = endpointFromHostPort(peer.preferred_node, portInput.value);
      if (!endpointValue) {
        portInput.focus();
        return;
      }
      button.disabled = true;
      button.textContent = "Adding";
      try {
        await addNode(
          {mode: "lan", name: peer.name || peer.preferred_node, url: endpointValue.trim()},
          document.querySelector(".node-status")
        );
      } catch (error) {
        button.disabled = false;
        button.textContent = "Add Tailscale";
        document.querySelector(".node-status").textContent = error.message;
      }
    });
    row.append(info, state, addFields, button);
    return row;
  };

  refresh.addEventListener("click", loadTailscale);
  window.StarAgentAfterPaint(() => {
    if (tailscaleLoaded) {
      return;
    }
    loadTailscale().catch((error) => {
      tailscaleDashboard.hidden = false;
      tailnet.textContent = error.message || "Tailscale check failed.";
    });
  });
}

for (const button of document.querySelectorAll(".node-remove")) {
  button.addEventListener("click", async () => {
    const name = button.dataset.node;
    if (!confirm(`Remove node "${name}"?`)) {
      return;
    }
    button.disabled = true;
    const response = await fetch(`/api/nodes/${encodeURIComponent(name)}`, {method: "DELETE"});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      alert(body.detail || "Remove node failed.");
      button.disabled = false;
      return;
    }
    window.location.reload();
  });
}

for (const button of document.querySelectorAll(".copy-button")) {
  button.addEventListener("click", async () => {
    const text = button.dataset.copy || "";
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      button.textContent = "Copied";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 1200);
  });
}
