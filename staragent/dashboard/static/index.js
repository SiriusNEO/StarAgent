for (const button of document.querySelectorAll(".stop-session")) {
  button.addEventListener("click", async () => {
    const name = button.dataset.session;
    const node = button.dataset.node;
    if (!confirm(`Stop tmux session "${name}"?`)) {
      return;
    }
    button.disabled = true;
    button.textContent = "Stopping";
    const response = await fetch(`/api/nodes/${encodeURIComponent(node)}/sessions/${encodeURIComponent(name)}`, {method: "DELETE"});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      alert(body.detail || "Stop failed.");
      button.disabled = false;
      button.textContent = "Stop";
      return;
    }
    window.location.reload();
  });
}

const workerForm = document.querySelector(".worker-form");
if (workerForm) {
  const status = workerForm.querySelector(".worker-status");
  const nameInput = workerForm.querySelector('input[name="name"]');
  const cwdInput = workerForm.querySelector('input[name="cwd"]');
  const nodeSelect = workerForm.querySelector('select[name="node"]');
  const commandInput = workerForm.querySelector('input[name="command"]');
  const presetSelect = workerForm.querySelector('select[name="preset"]');
  const explorer = workerForm.querySelector(".explorer");
  const maintenance = workerForm.querySelector(".worker-cli-maintenance");
  const maintenanceTitle = maintenance.querySelector(".worker-cli-maintenance-title");
  const maintenanceStatus = maintenance.querySelector(".worker-cli-maintenance-status");
  const updateButton = maintenance.querySelector(".worker-cli-update");
  const createButton = workerForm.querySelector('button[type="submit"]');
  const historyControl = workerForm.querySelector(".worker-history");
  const historySelect = historyControl.querySelector(".worker-history-select");
  const historyScanButton = historyControl.querySelector(".worker-history-scan");
  const historyStatus = historyControl.querySelector(".worker-history-status");
  const historySelection = historyControl.querySelector(".worker-history-selection");
  const historyTitle = historySelection.querySelector(".worker-history-title");
  const historyMeta = historySelection.querySelector(".worker-history-meta");
  const historyCwd = historySelection.querySelector(".worker-history-cwd");
  const agentLabels = {codex: "Codex", claude: "Claude Code", opencode: "OpenCode"};
  const historyAgents = new Set(["codex", "claude"]);
  const initialParams = new URLSearchParams(window.location.search);
  let initialResumeId = "";
  let historyContext = "";
  let historyEntries = new Map();
  let historyRequestId = 0;
  let appliedHistoryId = "";
  let appliedHistoryCwd = "";
  let newConversationCwd = cwdInput.value;
  let suggestedSessionName = "";
  let workerExplorer = null;

  const selectedAgent = () => presetSelect.selectedOptions[0]?.dataset.agent || "";
  const selectedAgentLabel = () => agentLabels[selectedAgent()] || "Agent CLI";
  const selectedHistory = () => historyEntries.get(historySelect.value) || null;
  const formatHistoryTime = (value) => {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime()) ? "unknown time" : date.toLocaleString();
  };
  const preferredPresetForAgent = (agent) => (
    Array.from(presetSelect.options).find((option) => option.dataset.preset === agent)
    || Array.from(presetSelect.options).find((option) => option.dataset.agent === agent)
    || null
  );
  const applyPresetOption = (option) => {
    if (!option) {
      return false;
    }
    presetSelect.value = option.value;
    commandInput.value = option.value;
    return true;
  };
  const clearAppliedHistory = () => {
    if (appliedHistoryId && cwdInput.value === appliedHistoryCwd) {
      cwdInput.value = newConversationCwd;
      workerExplorer?.load(cwdInput.value);
    }
    if (suggestedSessionName && nameInput.value === suggestedSessionName) {
      nameInput.value = "";
    }
    appliedHistoryId = "";
    appliedHistoryCwd = "";
    suggestedSessionName = "";
    historySelection.hidden = true;
  };
  const resetHistory = (message) => {
    historyRequestId += 1;
    clearAppliedHistory();
    historyEntries = new Map();
    historySelect.replaceChildren(new Option("Start a new conversation", ""));
    historySelect.disabled = true;
    historyStatus.textContent = message;
  };
  const syncHistoryAvailability = () => {
    const agent = selectedAgent();
    const supported = historyAgents.has(agent);
    const nextContext = `${nodeSelect.value}\n${agent}`;
    if (nextContext !== historyContext) {
      historyContext = nextContext;
      resetHistory(supported
        ? `Scan ${agentLabels[agent]} history on ${nodeSelect.value} when needed.`
        : "Conversation resume is available for Codex and Claude Code presets.");
    }
    historyScanButton.disabled = !supported;
  };
  const applyHistorySelection = () => {
    const entry = selectedHistory();
    if (!entry) {
      clearAppliedHistory();
      return;
    }
    if (!appliedHistoryId) {
      newConversationCwd = cwdInput.value;
    }
    const shortId = String(entry.id || "history").slice(0, 8).replace(/[^A-Za-z0-9_.:-]/g, "");
    const nextSuggestion = `${entry.agent}-resume-${shortId}-${Date.now().toString(36).slice(-4)}`.slice(0, 80);
    if (!nameInput.value.trim() || nameInput.value === suggestedSessionName) {
      nameInput.value = nextSuggestion;
    }
    suggestedSessionName = nextSuggestion;
    appliedHistoryId = entry.id;
    appliedHistoryCwd = entry.cwd || "";
    if (entry.cwd) {
      cwdInput.value = entry.cwd;
      workerExplorer?.load(entry.cwd);
    }
    historyTitle.textContent = entry.title || `${agentLabels[entry.agent] || entry.agent} conversation`;
    const metadata = [
      agentLabels[entry.agent] || entry.agent,
      `updated ${formatHistoryTime(entry.updated_at)}`,
      Number(entry.prompt_count || 0) ? `${Number(entry.prompt_count)} prompts` : "",
      entry.git_branch ? `branch ${entry.git_branch}` : "",
    ].filter(Boolean);
    historyMeta.textContent = metadata.join(" · ");
    historyCwd.textContent = entry.cwd || "Working directory unavailable";
    historyCwd.title = entry.cwd || "";
    historySelection.hidden = false;
  };
  const scanHistory = async ({targetId = "", refresh = true} = {}) => {
    const agent = selectedAgent();
    if (!historyAgents.has(agent)) {
      syncHistoryAvailability();
      return;
    }
    const node = nodeSelect.value;
    const requestContext = `${node}\n${agent}`;
    const requestId = ++historyRequestId;
    historyScanButton.disabled = true;
    historySelect.disabled = true;
    historyStatus.textContent = `Scanning ${agentLabels[agent]} history on ${node}…`;
    const query = new URLSearchParams({agent, limit: "50", refresh: String(refresh)});
    try {
      const response = await fetch(`/api/nodes/${encodeURIComponent(node)}/agent-history?${query}`);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || "History scan failed.");
      }
      if (requestId !== historyRequestId || requestContext !== `${nodeSelect.value}\n${selectedAgent()}`) {
        return;
      }
      const sessions = (Array.isArray(body.sessions) ? body.sessions : [])
        .filter((entry) => entry?.agent === agent && entry.id);
      historyEntries = new Map(sessions.map((entry) => [entry.id, entry]));
      historySelect.replaceChildren(new Option("Start a new conversation", ""));
      for (const entry of sessions) {
        const title = entry.title || `${agentLabels[agent]} conversation ${String(entry.id).slice(0, 8)}`;
        const option = new Option(`${title} · ${formatHistoryTime(entry.updated_at)}`, entry.id);
        option.disabled = !entry.cwd;
        option.title = entry.cwd || "Working directory unavailable";
        historySelect.appendChild(option);
      }
      historySelect.disabled = false;
      if (targetId && historyEntries.has(targetId)) {
        historySelect.value = targetId;
      }
      applyHistorySelection();
      const parts = [`${sessions.length} conversation${sessions.length === 1 ? "" : "s"}`];
      if (body.truncated) {
        parts.push("showing the newest 50");
      }
      if (targetId && !historyEntries.has(targetId)) {
        parts.push("requested conversation was not found");
      }
      if (body.error) {
        parts.push(body.error);
      }
      historyStatus.textContent = parts.join(" · ");
    } catch (error) {
      if (requestId === historyRequestId) {
        resetHistory(error.message || "History scan failed.");
        historyScanButton.disabled = false;
      }
    } finally {
      if (requestId === historyRequestId) {
        historyScanButton.disabled = !historyAgents.has(selectedAgent());
      }
    }
  };
  const syncCliMaintenance = () => {
    const agent = selectedAgent();
    const supported = ["codex", "claude", "opencode"].includes(agent);
    maintenance.hidden = !supported;
    if (!supported) {
      return;
    }
    const label = selectedAgentLabel();
    maintenanceTitle.textContent = `Update ${label}`;
    maintenanceStatus.textContent = `Run the managed update on ${nodeSelect.value} before creating the Session.`;
    updateButton.textContent = `Update ${label}`;
  };

  const requestedNode = initialParams.get("node") || "";
  const requestedAgent = initialParams.get("agent") || "";
  const requestedResumeId = initialParams.get("resume") || "";
  const requestedNodeOption = Array.from(nodeSelect.options).find((option) => option.value === requestedNode);
  if (
    requestedNodeOption
    && historyAgents.has(requestedAgent)
    && requestedResumeId
    && applyPresetOption(preferredPresetForAgent(requestedAgent))
  ) {
    nodeSelect.value = requestedNodeOption.value;
    cwdInput.value = "";
    newConversationCwd = "";
    initialResumeId = requestedResumeId;
  }

  presetSelect.addEventListener("change", () => {
    if (presetSelect.value) {
      commandInput.value = presetSelect.value;
    }
    syncCliMaintenance();
    syncHistoryAvailability();
    commandInput.focus();
  });
  commandInput.addEventListener("input", () => {
    if (commandInput.value !== presetSelect.value) {
      presetSelect.value = "";
      syncCliMaintenance();
      syncHistoryAvailability();
    }
  });
  historySelect.addEventListener("change", applyHistorySelection);
  historyScanButton.addEventListener("click", () => scanHistory({
    targetId: historySelect.value,
    refresh: true,
  }));

  updateButton.addEventListener("click", async () => {
    const agent = selectedAgent();
    const label = selectedAgentLabel();
    const node = nodeSelect.value;
    if (!agent || !confirm(`Update ${label} on ${node} before starting the Session?`)) {
      return;
    }
    updateButton.disabled = true;
    createButton.disabled = true;
    maintenance.classList.add("is-updating");
    maintenance.classList.remove("is-error", "is-success");
    maintenanceStatus.textContent = `Updating ${label} on ${node}…`;
    try {
      const response = await fetch(
        `/api/nodes/${encodeURIComponent(node)}/agent-tools/${encodeURIComponent(agent)}/update`,
        {method: "POST"},
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.ok) {
        throw new Error(body.detail || body.error || "Update failed.");
      }
      const before = body.before_version || "previous version";
      const after = body.after_version || "current version";
      maintenanceStatus.textContent = body.changed
        ? `${label} updated: ${before} → ${after}. You can create the Session now.`
        : `${label} update completed; ${after} is already current.`;
      maintenance.classList.add("is-success");
    } catch (error) {
      maintenanceStatus.textContent = error.message || "Update failed.";
      maintenance.classList.add("is-error");
    } finally {
      maintenance.classList.remove("is-updating");
      updateButton.disabled = false;
      createButton.disabled = false;
      updateButton.textContent = `Update ${label}`;
    }
  });

  workerExplorer = window.StarAgentExplorer.mount(explorer, {
    node: () => nodeSelect.value,
    getPath: () => cwdInput.value,
    setPath: (path) => {
      cwdInput.value = path;
    },
    includeFiles: false,
    allowCreateDirectory: true,
    loadingText: "Loading folders...",
    emptyText: "No matching folders.",
  });

  cwdInput.addEventListener("change", () => workerExplorer.load(cwdInput.value));
  nodeSelect.addEventListener("change", () => {
    syncHistoryAvailability();
    cwdInput.value = "";
    newConversationCwd = "";
    workerExplorer.load("");
    syncCliMaintenance();
  });
  syncCliMaintenance();
  syncHistoryAvailability();
  window.StarAgentAfterPaint(() => {
    workerExplorer.load(cwdInput.value);
    if (initialResumeId) {
      scanHistory({targetId: initialResumeId, refresh: false});
    }
  });

  workerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(workerForm);
    const payload = {
      name: String(form.get("name") || "").trim(),
      node: String(form.get("node") || "local").trim(),
      cwd: String(form.get("cwd") || "").trim(),
      command: String(form.get("command") || "").trim()
    };
    const resume = selectedHistory();
    if (historySelect.value && !resume) {
      status.textContent = "Scan and choose the conversation again before creating the Session.";
      return;
    }
    if (resume) {
      payload.resume = {agent: resume.agent, id: resume.id};
    }
    if (!payload.name || !payload.cwd || !payload.command) {
      status.textContent = "Name, working directory, and command are required.";
      return;
    }
    status.textContent = resume ? "Resuming conversation…" : "Creating…";
    createButton.disabled = true;
    try {
      const response = await fetch("/api/workers", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        status.textContent = body.detail || "Start failed.";
        createButton.disabled = false;
        return;
      }
      status.textContent = resume ? "Conversation resumed." : "Created.";
      setTimeout(() => {
        window.location.href = `/nodes/${encodeURIComponent(payload.node)}/sessions/${encodeURIComponent(payload.name)}`;
      }, 350);
    } catch (error) {
      status.textContent = error.message || "Start failed.";
      createButton.disabled = false;
    }
  });
}

const adoptForm = document.querySelector(".adopt-form");
if (adoptForm) {
  let initialAdoptScanDone = false;
  const status = adoptForm.querySelector(".adopt-status");
  const sessionSelect = adoptForm.querySelector('select[name="name"]');
  const nodeSelect = adoptForm.querySelector('select[name="node"]');
  const list = adoptForm.querySelector(".adopt-list");
  const scanButton = adoptForm.querySelector(".adopt-scan");

  async function scanAdoptableSessions() {
    initialAdoptScanDone = true;
    status.textContent = "Scanning...";
    sessionSelect.innerHTML = '<option value="">Scanning...</option>';
    list.innerHTML = "";
    const response = await fetch(`/api/adoptable-sessions?node=${encodeURIComponent(nodeSelect.value)}`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      status.textContent = body.detail || "Scan failed.";
      sessionSelect.innerHTML = '<option value="">Scan failed</option>';
      return;
    }
    const data = await response.json();
    const sessions = data.sessions || [];
    sessionSelect.innerHTML = "";
    if (!sessions.length) {
      sessionSelect.innerHTML = '<option value="">No CLI tmux sessions</option>';
      list.innerHTML = '<div class="explorer-empty">No adoptable Codex, Claude, or OpenCode tmux sessions found.</div>';
      status.textContent = "No sessions found.";
      return;
    }
    for (const item of sessions) {
      const option = document.createElement("option");
      option.value = item.name;
      option.textContent = `${item.name} · ${item.cli} · ${item.cwd || "-"}`;
      sessionSelect.appendChild(option);

      const row = document.createElement("button");
      row.type = "button";
      row.className = "directory-row";
      row.innerHTML = `<span>${item.cli}</span><strong>${item.name}</strong><small>${item.cwd || ""}</small>`;
      row.addEventListener("click", () => {
        sessionSelect.value = item.name;
      });
      list.appendChild(row);
    }
    status.textContent = `${sessions.length} session(s) found.`;
  }

  scanButton.addEventListener("click", scanAdoptableSessions);
  nodeSelect.addEventListener("change", scanAdoptableSessions);
  adoptForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      node: nodeSelect.value,
      name: sessionSelect.value
    };
    if (!payload.name) {
      status.textContent = "Choose a tmux session first.";
      return;
    }
    status.textContent = "Adopting...";
    const response = await fetch("/api/adopt", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      status.textContent = body.detail || "Adopt failed.";
      return;
    }
    status.textContent = "Adopted.";
    setTimeout(() => window.location.reload(), 500);
  });
  window.StarAgentAfterPaint(() => {
    if (!initialAdoptScanDone) {
      scanAdoptableSessions();
    }
  });
}
