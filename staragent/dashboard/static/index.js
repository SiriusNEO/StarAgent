const t = (key, values = {}) => window.StarAgentI18n?.t(key, values) || key;

for (const button of document.querySelectorAll(".stop-session")) {
  button.addEventListener("click", async () => {
    const name = button.dataset.session;
    const node = button.dataset.node;
    if (!confirm(t("sessions.stop_confirm", {name}))) {
      return;
    }
    button.disabled = true;
    button.textContent = t("sessions.stopping");
    const response = await fetch(`/api/nodes/${encodeURIComponent(node)}/sessions/${encodeURIComponent(name)}`, {method: "DELETE"});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      alert(body.detail || t("sessions.stop_failed"));
      button.disabled = false;
      button.textContent = t("common.stop");
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
  const nodeSelect = workerForm.querySelector('[name="node"]');
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
  const selectedAgentLabel = () => agentLabels[selectedAgent()] || t("sessions.agent_cli");
  const selectedHistory = () => historyEntries.get(historySelect.value) || null;
  const formatHistoryTime = (value) => {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime())
      ? t("sessions.unknown_time")
      : date.toLocaleString(window.StarAgentI18n?.language || []);
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
    historySelect.replaceChildren(new Option(t("sessions.start_new"), ""));
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
        ? t("sessions.history_scan_on", {agent: agentLabels[agent], node: nodeSelect.value})
        : t("sessions.history_supported"));
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
    historyTitle.textContent = entry.title || t("sessions.conversation_title", {agent: agentLabels[entry.agent] || entry.agent});
    const metadata = [
      agentLabels[entry.agent] || entry.agent,
      t("sessions.history_updated", {time: formatHistoryTime(entry.updated_at)}),
      Number(entry.prompt_count || 0) ? t("sessions.history_prompts", {count: Number(entry.prompt_count)}) : "",
      entry.git_branch ? t("sessions.history_branch", {branch: entry.git_branch}) : "",
    ].filter(Boolean);
    historyMeta.textContent = metadata.join(" · ");
    historyCwd.textContent = entry.cwd || t("sessions.cwd_unavailable");
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
    historyStatus.textContent = t("sessions.history_scanning", {agent: agentLabels[agent], node});
    const query = new URLSearchParams({agent, limit: "50", refresh: String(refresh)});
    try {
      const response = await fetch(`/api/nodes/${encodeURIComponent(node)}/agent-history?${query}`);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || t("sessions.history_failed"));
      }
      if (requestId !== historyRequestId || requestContext !== `${nodeSelect.value}\n${selectedAgent()}`) {
        return;
      }
      const sessions = (Array.isArray(body.sessions) ? body.sessions : [])
        .filter((entry) => entry?.agent === agent && entry.id);
      historyEntries = new Map(sessions.map((entry) => [entry.id, entry]));
      historySelect.replaceChildren(new Option(t("sessions.start_new"), ""));
      for (const entry of sessions) {
        const title = entry.title || `${t("sessions.conversation_title", {agent: agentLabels[agent]})} ${String(entry.id).slice(0, 8)}`;
        const option = new Option(`${title} · ${formatHistoryTime(entry.updated_at)}`, entry.id);
        option.disabled = !entry.cwd;
        option.title = entry.cwd || t("sessions.cwd_unavailable");
        historySelect.appendChild(option);
      }
      historySelect.disabled = false;
      if (targetId && historyEntries.has(targetId)) {
        historySelect.value = targetId;
      }
      applyHistorySelection();
      const parts = [sessions.length === 1
        ? t("sessions.history_count_one")
        : t("sessions.history_count", {count: sessions.length})];
      if (body.truncated) {
        parts.push(t("sessions.history_truncated"));
      }
      if (targetId && !historyEntries.has(targetId)) {
        parts.push(t("sessions.history_not_found"));
      }
      if (body.error) {
        parts.push(body.error);
      }
      historyStatus.textContent = parts.join(" · ");
    } catch (error) {
      if (requestId === historyRequestId) {
        resetHistory(error.message || t("sessions.history_failed"));
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
    maintenanceTitle.textContent = t("sessions.update_named", {agent: label});
    maintenanceStatus.textContent = t("sessions.update_node_hint", {node: nodeSelect.value});
    updateButton.textContent = t("sessions.update_named", {agent: label});
  };

  const requestedNode = initialParams.get("node") || "";
  const requestedAgent = initialParams.get("agent") || "";
  const requestedResumeId = initialParams.get("resume") || "";
  if (
    (!requestedNode || requestedNode === nodeSelect.value)
    && historyAgents.has(requestedAgent)
    && requestedResumeId
    && applyPresetOption(preferredPresetForAgent(requestedAgent))
  ) {
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
    if (!agent || !confirm(t("sessions.update_confirm", {agent: label, node}))) {
      return;
    }
    updateButton.disabled = true;
    createButton.disabled = true;
    maintenance.classList.add("is-updating");
    maintenance.classList.remove("is-error", "is-success");
    maintenanceStatus.textContent = t("sessions.updating", {agent: label, node});
    try {
      const response = await fetch(
        `/api/nodes/${encodeURIComponent(node)}/agent-tools/${encodeURIComponent(agent)}/update`,
        {method: "POST"},
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.ok) {
        throw new Error(body.detail || body.error || t("sessions.update_failed"));
      }
      const before = body.before_version || t("sessions.previous_version");
      const after = body.after_version || t("sessions.current_version");
      maintenanceStatus.textContent = body.changed
        ? t("sessions.updated", {agent: label, before, after})
        : t("sessions.already_current", {agent: label, version: after});
      maintenance.classList.add("is-success");
    } catch (error) {
      maintenanceStatus.textContent = error.message || t("sessions.update_failed");
      maintenance.classList.add("is-error");
    } finally {
      maintenance.classList.remove("is-updating");
      updateButton.disabled = false;
      createButton.disabled = false;
      updateButton.textContent = t("sessions.update_named", {agent: label});
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
    loadingText: t("sessions.loading_folders"),
    emptyText: t("sessions.no_folders"),
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
      status.textContent = t("sessions.choose_history_again");
      return;
    }
    if (resume) {
      payload.resume = {agent: resume.agent, id: resume.id};
    }
    if (!payload.name || !payload.cwd || !payload.command) {
      status.textContent = t("sessions.required_fields");
      return;
    }
    status.textContent = resume ? t("sessions.resuming") : t("sessions.creating");
    createButton.disabled = true;
    try {
      const response = await fetch("/api/workers", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        status.textContent = body.detail || t("sessions.start_failed");
        createButton.disabled = false;
        return;
      }
      status.textContent = resume ? t("sessions.resumed") : t("sessions.created");
      setTimeout(() => {
        window.location.href = `/nodes/${encodeURIComponent(payload.node)}/sessions/${encodeURIComponent(payload.name)}`;
      }, 350);
    } catch (error) {
      status.textContent = error.message || t("sessions.start_failed");
      createButton.disabled = false;
    }
  });
}

const adoptForm = document.querySelector(".adopt-form");
if (adoptForm) {
  let initialAdoptScanDone = false;
  let adoptableSessions = new Map();
  let scanRequestId = 0;
  const status = adoptForm.querySelector(".adopt-status");
  const statusTitle = adoptForm.querySelector(".adopt-status-title");
  const sessionSelect = adoptForm.querySelector('select[name="name"]');
  const nodeSelect = adoptForm.querySelector('[name="node"]');
  const list = adoptForm.querySelector(".adopt-list");
  const scanButton = adoptForm.querySelector(".adopt-scan");
  const scanButtonLabel = scanButton.querySelector("span");
  const adoptButton = adoptForm.querySelector(".adopt-submit");
  const adoptButtonLabel = adoptButton.querySelector("span");
  const agentMetadata = {
    codex: {label: "Codex", icon: "/static/agent-icons/codex.svg"},
    claude: {label: "Claude Code", icon: "/static/agent-icons/claude.svg"},
    opencode: {label: "OpenCode", icon: "/static/agent-icons/opencode.svg"},
  };

  const setAdoptStatus = (title, message, state = "idle") => {
    statusTitle.textContent = title;
    status.textContent = message;
    adoptForm.dataset.state = state;
  };

  const renderAdoptEmpty = (title, message, state = "empty") => {
    const empty = document.createElement("div");
    empty.className = `adopt-empty-state is-${state}`;
    const icon = document.createElement("span");
    icon.className = "adopt-empty-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = state === "scanning" ? "…" : (state === "error" ? "!" : "0");
    const heading = document.createElement("strong");
    heading.textContent = title;
    const copy = document.createElement("span");
    copy.textContent = message;
    empty.append(icon, heading, copy);
    list.replaceChildren(empty);
  };

  const selectAdoptableSession = (name) => {
    const selected = adoptableSessions.get(name);
    sessionSelect.value = selected ? name : "";
    for (const card of list.querySelectorAll(".adopt-session-card")) {
      const isSelected = card.dataset.session === sessionSelect.value;
      card.classList.toggle("is-selected", isSelected);
      card.setAttribute("aria-pressed", String(isSelected));
      const choice = card.querySelector(".adopt-session-choice");
      if (choice) {
        choice.textContent = isSelected ? t("sessions.selected") : t("sessions.select");
      }
    }
    adoptButton.disabled = !selected;
    if (!selected) {
      setAdoptStatus(t("sessions.choose"), t("sessions.choose_detected"));
      return;
    }
    const agent = agentMetadata[selected.cli]?.label || selected.cli || t("sessions.agent_cli");
    setAdoptStatus(
      t("sessions.ready_adopt"),
      `${selected.name} · ${agent} · ${selected.cwd || t("sessions.cwd_unavailable")}`,
      "ready",
    );
  };

  const createAdoptableCard = (item) => {
    const agent = Object.hasOwn(agentMetadata, item.cli) ? item.cli : "unknown";
    const metadata = agentMetadata[agent] || {label: item.cli || t("sessions.agent_cli"), icon: ""};
    const card = document.createElement("button");
    card.type = "button";
    card.className = `adopt-session-card is-${agent}`;
    card.dataset.session = item.name;
    card.setAttribute("aria-pressed", "false");

    const icon = document.createElement("span");
    icon.className = "adopt-session-agent-icon";
    icon.setAttribute("aria-hidden", "true");
    if (metadata.icon) {
      const image = document.createElement("img");
      image.src = metadata.icon;
      image.alt = "";
      image.width = 28;
      image.height = 28;
      icon.appendChild(image);
    } else {
      icon.textContent = ">_";
    }

    const body = document.createElement("span");
    body.className = "adopt-session-copy";
    const eyebrow = document.createElement("span");
    eyebrow.className = "adopt-session-agent";
    eyebrow.textContent = metadata.label;
    const name = document.createElement("strong");
    name.textContent = item.name;
    const cwd = document.createElement("code");
    cwd.textContent = item.cwd || t("sessions.cwd_unavailable");
    cwd.title = item.cwd || "";
    const details = document.createElement("span");
    details.className = "adopt-session-meta";
    const live = document.createElement("span");
    live.className = "adopt-session-live";
    live.textContent = t("sessions.live");
    details.appendChild(live);
    const processId = Number(item.cli_pid || item.pane_pid || 0);
    if (processId > 0) {
      const pid = document.createElement("span");
      pid.textContent = `PID ${processId}`;
      details.appendChild(pid);
    }
    body.append(eyebrow, name, cwd, details);

    const choice = document.createElement("span");
    choice.className = "adopt-session-choice";
    choice.textContent = t("sessions.select");
    card.append(icon, body, choice);
    card.addEventListener("click", () => selectAdoptableSession(item.name));
    return card;
  };

  async function scanAdoptableSessions() {
    initialAdoptScanDone = true;
    const requestId = ++scanRequestId;
    adoptableSessions = new Map();
    sessionSelect.replaceChildren(new Option(t("sessions.scanning"), ""));
    scanButton.disabled = true;
    adoptButton.disabled = true;
    scanButtonLabel.textContent = t("sessions.scanning");
    setAdoptStatus(t("sessions.scanning_node"), t("sessions.scanning_hint"), "scanning");
    renderAdoptEmpty(
      t("sessions.inspecting"),
      t("sessions.inspecting_hint", {node: nodeSelect.value}),
      "scanning",
    );
    try {
      const response = await fetch(
        `/api/adoptable-sessions?node=${encodeURIComponent(nodeSelect.value)}`,
      );
      const body = await response.json().catch(() => ({}));
      if (requestId !== scanRequestId) {
        return;
      }
      if (!response.ok) {
        throw new Error(body.detail || t("sessions.history_failed"));
      }
      const sessions = Array.isArray(body.sessions) ? body.sessions : [];
      sessionSelect.replaceChildren(new Option(t("sessions.choose_tmux"), ""));
      if (!sessions.length) {
        renderAdoptEmpty(
          t("sessions.no_adoptable"),
          t("sessions.no_adoptable_hint"),
        );
        setAdoptStatus(t("sessions.nothing_adopt"), t("sessions.none_detected"));
        return;
      }
      const cards = [];
      for (const item of sessions) {
        if (!item?.name) {
          continue;
        }
        adoptableSessions.set(item.name, item);
        const option = document.createElement("option");
        option.value = item.name;
        option.textContent = `${item.name} · ${item.cli || t("sessions.agent_cli")}`;
        sessionSelect.appendChild(option);
        cards.push(createAdoptableCard(item));
      }
      list.replaceChildren(...cards);
      const firstSession = cards[0]?.dataset.session || "";
      if (firstSession) {
        selectAdoptableSession(firstSession);
      } else {
        renderAdoptEmpty(t("sessions.no_adoptable"), t("sessions.no_usable"));
        setAdoptStatus(t("sessions.nothing_adopt"), t("sessions.none_detected"));
      }
    } catch (error) {
      if (requestId !== scanRequestId) {
        return;
      }
      const message = error.message || t("sessions.history_failed");
      sessionSelect.replaceChildren(new Option(t("sessions.scan_failed"), ""));
      renderAdoptEmpty(t("sessions.could_not_scan"), message, "error");
      setAdoptStatus(t("sessions.scan_failed"), message, "error");
    } finally {
      if (requestId === scanRequestId) {
        scanButton.disabled = false;
        scanButtonLabel.textContent = t("sessions.scan_again");
      }
    }
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
      setAdoptStatus(t("sessions.choose"), t("sessions.choose_detected"), "error");
      return;
    }
    adoptButton.disabled = true;
    scanButton.disabled = true;
    adoptButtonLabel.textContent = t("sessions.adopting");
    for (const card of list.querySelectorAll(".adopt-session-card")) {
      card.disabled = true;
    }
    setAdoptStatus(t("sessions.adopting_title"), t("sessions.registering", {name: payload.name}), "scanning");
    try {
      const response = await fetch("/api/adopt", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || t("sessions.adopt_failed"));
      }
      adoptButtonLabel.textContent = t("sessions.adopted");
      setAdoptStatus(
        t("sessions.adopted_title"),
        t("sessions.adopted_message", {name: payload.name}),
        "success",
      );
      setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      setAdoptStatus(t("sessions.adopt_failed"), error.message || t("sessions.adopt_failed"), "error");
      adoptButton.disabled = false;
      scanButton.disabled = false;
      adoptButtonLabel.textContent = t("sessions.adopt");
      for (const card of list.querySelectorAll(".adopt-session-card")) {
        card.disabled = false;
      }
    }
  });
  window.StarAgentAfterPaint(() => {
    if (!initialAdoptScanDone) {
      scanAdoptableSessions();
    }
  });
}
