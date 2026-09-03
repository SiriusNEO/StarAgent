const t = (key, values = {}) => window.StarAgentI18n?.t(key, values) || key;

const agentCatalog = Array.from(document.querySelectorAll(".agent-catalog-data [data-name]")).map((item) => ({
  name: item.dataset.name || "",
  label: item.dataset.label || item.dataset.name || t("sessions.agent_cli"),
  command: item.dataset.command || item.dataset.name || "",
}));

async function copyAgentText(button, value) {
  const original = button.textContent;
  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = t("nodes.copied");
        setTimeout(() => {
          button.textContent = original;
        }, 1200);
        return;
      } catch (_error) {
        // Fall back for non-secure HTTP dashboard origins.
      }
    }
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    if (!copied) {
      throw new Error("Clipboard is unavailable");
    }
    button.textContent = t("nodes.copied");
  } catch (_error) {
    button.textContent = t("agents.copy_failed");
  }
  setTimeout(() => {
    button.textContent = original;
  }, 1200);
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-copy]");
  if (!button) {
    return;
  }
  copyAgentText(button, button.dataset.copy || "");
});

const agentToolsBand = document.querySelector(".agent-tools-band");
if (agentToolsBand) {
  const cards = Array.from(agentToolsBand.querySelectorAll(".agent-cli-card"));
  const rows = Array.from(agentToolsBand.querySelectorAll(".agent-cli-node-row"));
  const sidebarItems = Array.from(document.querySelectorAll(".agent-switcher-item[data-agent]"));
  const nodeNames = [...new Set(rows.map((row) => row.dataset.node || "").filter(Boolean))];
  const catalogByName = new Map(agentCatalog.map((tool) => [tool.name, tool]));
  const refreshButton = agentToolsBand.querySelector(".agent-tools-refresh");
  const status = agentToolsBand.querySelector(".agent-tools-status");
  const updateDialog = agentToolsBand.querySelector(".agent-update-dialog");

  const toolState = (value) => ({
    available: {label: t("agents.state.ready"), style: "connected"},
    missing: {label: t("agents.state.missing"), style: "disconnected"},
    error: {label: t("agents.state.error"), style: "error"},
    unknown: {label: t("agents.state.checking"), style: "optional"},
  }[value] || {label: t("agents.state.checking"), style: "optional"});

  const checkedTime = (value) => {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime())
      ? t("agents.not_checked")
      : date.toLocaleString(window.StarAgentI18n?.language || []);
  };

  const fallbackTool = (name, message) => {
    const catalog = catalogByName.get(name) || {name, label: name, command: name};
    return {...catalog, status: message ? "error" : "unknown", error: message};
  };

  const renderSidebarItem = (item, tool, payload) => {
    const statusName = tool.status || "unknown";
    const state = toolState(statusName);
    const stateElement = item.querySelector("[data-agent-state]");
    const stateLabel = stateElement?.querySelector("span");
    const stale = Boolean(payload.stale);
    item.dataset.status = statusName;
    item.classList.toggle("is-stale", stale);
    if (stateLabel) {
      stateLabel.textContent = stale && statusName === "available" ? t("agents.state.stale") : state.label;
    }
    const details = [state.label];
    if (tool.version) {
      details.push(tool.version);
    }
    if (stale) {
      details.push(t("agents.cached_result"));
    }
    item.title = details.join(" · ");
  };

  const installDescription = (tool) => {
    if (tool.status === "missing") {
      return t("agents.not_installed");
    }
    if (!tool.install_method || tool.install_method === "unknown") {
      return t("agents.install_unknown");
    }
    return t("agents.installation", {method: tool.install_method});
  };

  const formatPercent = (value) => {
    const number = Math.max(0, Math.min(100, Number(value || 0)));
    return Number.isInteger(number) ? String(number) : number.toFixed(1);
  };

  const formatResetTime = (value) => {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime())
      ? t("agents.reset_unavailable")
      : t("agents.resets", {time: date.toLocaleString(window.StarAgentI18n?.language || [])});
  };

  const usageStatusLabel = (value) => ({
    available: t("agents.usage.live"),
    manual: t("agents.usage.manual"),
    unavailable: t("agents.usage.unavailable"),
    error: t("agents.usage.error"),
    unknown: t("agents.usage.unknown"),
  }[value] || t("agents.usage.unknown"));

  const usageStatusStyle = (value) => ({
    available: "connected",
    manual: "optional",
    unavailable: "optional",
    error: "error",
    unknown: "optional",
  }[value] || "optional");

  const authState = (value) => ({
    authenticated: {label: t("agents.auth.authenticated"), style: "connected"},
    configured: {label: t("agents.auth.configured"), style: "connected"},
    not_authenticated: {label: t("agents.auth.not_authenticated"), style: "disconnected"},
    not_configured: {label: t("agents.auth.not_configured"), style: "disconnected"},
    unavailable: {label: t("agents.auth.unavailable"), style: "optional"},
    error: {label: t("agents.auth.error"), style: "error"},
    unknown: {label: t("agents.auth.unknown"), style: "optional"},
  }[value] || {label: t("agents.auth.unknown"), style: "optional"});

  const renderAgentAuth = (container, auth) => {
    container.replaceChildren();
    const state = authState(auth?.status || "unknown");
    container.className = `agent-cli-auth is-${auth?.status || "unknown"}`;

    const head = document.createElement("div");
    head.className = "agent-auth-head";
    const title = document.createElement("strong");
    title.textContent = t("agents.login");
    const statusPill = document.createElement("span");
    statusPill.className = `pill node-status-${state.style}`;
    statusPill.textContent = state.label;
    head.append(title, statusPill);
    container.appendChild(head);

    const details = [];
    if (auth?.method) {
      details.push(auth.method);
    }
    if (Number(auth?.provider_count || 0) > 0) {
      const count = Number(auth.provider_count);
      details.push(count === 1 ? t("agents.source_count_one") : t("agents.source_count", {count}));
    }
    if (auth?.detail) {
      details.push(auth.detail);
    }
    if (details.length) {
      const detail = document.createElement("div");
      detail.className = "agent-auth-detail";
      detail.textContent = details.join(" · ");
      container.appendChild(detail);
    }

    if (auth?.action) {
      const action = document.createElement("div");
      action.className = "agent-auth-action";
      const command = document.createElement("code");
      command.textContent = auth.action;
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-button inline-copy";
      copy.dataset.copy = auth.action;
      copy.textContent = t("agents.copy_login");
      action.append(command, copy);
      container.appendChild(action);
    }
  };

  const renderUsageWindow = (window) => {
    const remaining = Math.max(0, Math.min(100, Number(window.remaining_percent || 0)));
    const item = document.createElement("div");
    item.className = "agent-usage-window";
    const copy = document.createElement("div");
    copy.className = "agent-usage-window-copy";
    const label = document.createElement("span");
    label.textContent = window.label || t("agents.limit");
    const value = document.createElement("strong");
    value.textContent = t("agents.left", {percent: formatPercent(remaining)});
    copy.append(label, value);
    const meter = document.createElement("div");
    meter.className = "agent-usage-meter";
    meter.setAttribute("role", "progressbar");
    meter.setAttribute("aria-label", t("agents.usage_remaining", {label: window.label || t("agents.usage_title")}));
    meter.setAttribute("aria-valuemin", "0");
    meter.setAttribute("aria-valuemax", "100");
    meter.setAttribute("aria-valuenow", String(remaining));
    const fill = document.createElement("span");
    fill.style.width = `${remaining}%`;
    fill.className = remaining <= 20
      ? "is-low"
      : (remaining <= 50 ? "is-medium" : "");
    meter.appendChild(fill);
    const reset = document.createElement("small");
    reset.textContent = formatResetTime(window.resets_at);
    item.append(copy, meter, reset);
    return item;
  };

  const renderUsageBucket = (bucket) => {
    const section = document.createElement("section");
    section.className = "agent-usage-bucket";
    const head = document.createElement("div");
    head.className = "agent-usage-bucket-head";
    const label = document.createElement("strong");
    label.textContent = bucket.label || bucket.id || "Codex";
    const plan = document.createElement("span");
    plan.textContent = bucket.plan || "";
    plan.hidden = !bucket.plan;
    head.append(label, plan);
    section.appendChild(head);
    for (const window of Array.isArray(bucket.windows) ? bucket.windows : []) {
      section.appendChild(renderUsageWindow(window));
    }
    const notes = [];
    if (bucket.credits?.unlimited) {
      notes.push(t("agents.unlimited_credits"));
    } else if (bucket.credits?.has_credits && bucket.credits.balance) {
      notes.push(t("agents.credits", {balance: bucket.credits.balance}));
    }
    if (bucket.individual_limit) {
      notes.push(t("agents.spend_left", {percent: formatPercent(bucket.individual_limit.remaining_percent)}));
    }
    if (bucket.reached) {
      notes.push(bucket.reached.replaceAll("_", " "));
    }
    if (notes.length) {
      const note = document.createElement("small");
      note.className = bucket.reached ? "agent-usage-warning" : "";
      note.textContent = notes.join(" · ");
      section.appendChild(note);
    }
    return section;
  };

  const renderAgentUsage = (container, usage) => {
    container.replaceChildren();
    const state = usage?.status || "unknown";
    container.className = `agent-cli-usage is-${state}`;
    if (state === "unsupported") {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    const head = document.createElement("div");
    head.className = "agent-usage-head";
    const title = document.createElement("strong");
    title.textContent = t("agents.usage_title");
    const statusPill = document.createElement("span");
    statusPill.className = `pill node-status-${usageStatusStyle(state)}`;
    statusPill.textContent = usageStatusLabel(state);
    head.append(title, statusPill);
    container.appendChild(head);

    for (const bucket of Array.isArray(usage.buckets) ? usage.buckets : []) {
      container.appendChild(renderUsageBucket(bucket));
    }

    const details = [];
    if (Number(usage.reset_credits || 0) > 0) {
      const count = Number(usage.reset_credits);
      details.push(count === 1 ? t("agents.reset_available_one") : t("agents.resets_available", {count}));
    }
    if (usage.checked_at) {
      details.push(t("agents.checked", {time: checkedTime(usage.checked_at)}));
    }
    if (details.length) {
      const meta = document.createElement("div");
      meta.className = "agent-usage-meta";
      meta.textContent = details.join(" · ");
      container.appendChild(meta);
    }
    if (usage.message) {
      const message = document.createElement("div");
      message.className = "agent-usage-message";
      message.textContent = usage.message;
      container.appendChild(message);
    }
    if (usage.action) {
      const action = document.createElement("div");
      action.className = "agent-usage-action";
      const command = document.createElement("code");
      command.textContent = usage.action;
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-button inline-copy";
      copy.dataset.copy = usage.action;
      copy.textContent = t("agents.copy_command");
      action.append(command, copy);
      container.appendChild(action);
    }
  };

  const setAgentUpdateResult = (row, message, isError = false) => {
    const result = row.querySelector(".agent-cli-update-result");
    result.textContent = message || "";
    result.hidden = !message;
    result.classList.toggle("is-error", isError);
  };

  const confirmAgentUpdate = (row, tool) => {
    const node = row.dataset.node || t("agents.this_node");
    const label = tool.label || tool.name || t("sessions.agent_cli");
    if (!updateDialog || typeof updateDialog.showModal !== "function") {
      return Promise.resolve(window.confirm(t("agents.update_confirm", {agent: label, node})));
    }

    const card = row.closest(".agent-cli-card");
    const sourceIcon = sidebarItems
      .find((item) => item.dataset.agent === tool.name)
      ?.querySelector("img");
    const dialogIcon = updateDialog.querySelector(".agent-update-dialog-icon img");
    const accent = card ? getComputedStyle(card).getPropertyValue("--agent-brand").trim() : "";
    updateDialog.dataset.agent = tool.name || "";
    updateDialog.style.setProperty("--agent-brand", accent || "var(--accent)");
    updateDialog.querySelector("#agent-update-title").textContent = t("agents.update", {agent: label});
    updateDialog.querySelector(".agent-update-dialog-node").textContent = node;
    updateDialog.querySelector(".agent-update-dialog-version").textContent = tool.version
      || row.querySelector(".agent-cli-version")?.textContent
      || t("agents.unknown");
    updateDialog.querySelector(".agent-update-dialog-command code").textContent = tool.update_command
      || t("agents.no_update_command");
    if (sourceIcon && dialogIcon) {
      dialogIcon.src = sourceIcon.src;
    }

    updateDialog.returnValue = "cancel";
    return new Promise((resolve) => {
      updateDialog.addEventListener(
        "close",
        () => resolve(updateDialog.returnValue === "confirm"),
        {once: true},
      );
      updateDialog.showModal();
      requestAnimationFrame(() => {
        updateDialog.querySelector(".agent-update-dialog-cancel")?.focus({preventScroll: true});
      });
    });
  };

  updateDialog?.addEventListener("click", (event) => {
    if (event.target === updateDialog) {
      updateDialog.close("cancel");
    }
  });

  const runAgentUpdate = async (row, tool, button) => {
    const node = row.dataset.node || "";
    const label = tool.label || tool.name || t("sessions.agent_cli");
    if (!await confirmAgentUpdate(row, tool)) {
      return;
    }
    row.classList.add("is-updating");
    button.disabled = true;
    button.textContent = t("agents.updating");
    setAgentUpdateResult(row, t("agents.updating_on", {agent: label, node}));
    try {
      const response = await fetch(
        `/api/nodes/${encodeURIComponent(node)}/agent-tools/${encodeURIComponent(tool.name)}/update`,
        {method: "POST"},
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.ok) {
        throw new Error(body.detail || body.error || t("agents.update_failed"));
      }
      await loadAgentNode(node, true);
      updateCardSummaries();
      const before = body.before_version || t("sessions.previous_version");
      const after = body.after_version || t("sessions.current_version");
      const message = body.changed
        ? t("agents.updated", {agent: label, before, after})
        : t("agents.already_current", {agent: label, version: after});
      setAgentUpdateResult(row, message);
      status.textContent = message;
    } catch (error) {
      const message = error.message || t("agents.update_failed");
      setAgentUpdateResult(row, message, true);
      status.textContent = t("agents.update_failed_on", {agent: label, node, message});
    } finally {
      row.classList.remove("is-updating");
      button.disabled = false;
      button.textContent = t("agents.update_now");
    }
  };

  const renderAgentNode = (row, tool, payload) => {
    const state = toolState(tool.status);
    const version = row.querySelector(".agent-cli-version");
    const pill = row.querySelector(".agent-cli-status");
    const install = row.querySelector(".agent-cli-install");
    const auth = row.querySelector(".agent-cli-auth");
    const usage = row.querySelector(".agent-cli-usage");
    const actions = row.querySelector(".agent-cli-actions");
    const meta = row.querySelector(".agent-cli-meta");
    const error = row.querySelector(".agent-cli-error");

    version.textContent = tool.version
      || (tool.status === "missing"
        ? t("agents.not_found", {command: tool.command || tool.name})
        : t("agents.no_version"));
    version.title = tool.executable || "";
    pill.className = `pill node-status-${state.style} agent-cli-status`;
    pill.textContent = state.label;
    install.textContent = installDescription(tool);
    install.title = tool.update_note || "";
    renderAgentAuth(auth, tool.auth || {});
    renderAgentUsage(usage, tool.usage || {});

    actions.replaceChildren();
    if (tool.update_command) {
      const command = document.createElement("code");
      command.textContent = tool.update_command;
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-button inline-copy";
      copy.dataset.copy = tool.update_command;
      copy.textContent = tool.update_action === "install" ? t("agents.copy_install") : t("agents.copy_update");
      actions.append(command, copy);
      if (
        tool.status === "available"
        && tool.update_action === "update"
        && payload.updates_supported
        && !payload.stale
      ) {
        const update = document.createElement("button");
        update.type = "button";
        update.className = "agent-cli-update-button";
        update.textContent = t("agents.update_now");
        update.addEventListener("click", () => runAgentUpdate(row, tool, update));
        actions.appendChild(update);
      }
    }

    const parts = [checkedTime(payload.checked_at)];
    if (payload.stale) {
      parts.push(t("agents.state.stale"));
    }
    if (!payload.supported) {
      parts.push(t("agents.node_update_required"));
    }
    meta.textContent = parts.join(" · ");
    const errorMessage = tool.status === "error" ? tool.error : (payload.error || "");
    error.textContent = errorMessage;
    error.hidden = !errorMessage;
    row.dataset.toolStatus = tool.status || "unknown";
    row.dataset.authStatus = tool.auth?.status || "unknown";
    row.dataset.stale = payload.stale ? "true" : "false";
    row.classList.toggle("is-stale", Boolean(payload.stale));
  };

  const renderNodePayload = (node, payload) => {
    const tools = new Map(
      (Array.isArray(payload.tools) ? payload.tools : [])
        .filter((tool) => tool && tool.name)
        .map((tool) => [tool.name, tool]),
    );
    for (const item of sidebarItems) {
      const agent = item.dataset.agent || "";
      renderSidebarItem(
        item,
        tools.get(agent) || fallbackTool(agent, payload.error || t("agents.no_result")),
        payload,
      );
    }
    for (const row of rows.filter((item) => item.dataset.node === node)) {
      const agent = row.closest(".agent-cli-card")?.dataset.agent || "";
      renderAgentNode(
        row,
        tools.get(agent) || fallbackTool(agent, payload.error || t("agents.no_result")),
        payload,
      );
    }
  };

  const updateCardSummaries = () => {
    for (const card of cards) {
      const cardRows = Array.from(card.querySelectorAll(".agent-cli-node-row"));
      const counts = {available: 0, missing: 0, error: 0, unknown: 0};
      for (const row of cardRows) {
        const value = row.dataset.toolStatus || "unknown";
        counts[value] = (counts[value] || 0) + 1;
      }
      let cardStatus = "unknown";
      if (counts.error) {
        cardStatus = "error";
      } else if (counts.missing) {
        cardStatus = "missing";
      } else if (counts.available === cardRows.length && cardRows.length) {
        cardStatus = "available";
      }
      card.dataset.status = cardStatus;

      const summary = card.querySelector(".agent-cli-summary");
      if (!cardRows.length) {
        summary.textContent = t("agents.no_node");
      } else if (cardRows.length === 1) {
        const node = cardRows[0].dataset.node || t("agents.this_node");
        summary.textContent = ({
          available: t("agents.ready_on", {node}),
          missing: t("agents.install_on", {node}),
          error: t("agents.attention_on", {node}),
          unknown: t("agents.checking_availability", {node}),
        })[cardStatus];
      } else {
        const parts = [t("agents.nodes_ready", {ready: counts.available, total: cardRows.length})];
        if (counts.missing) {
          parts.push(t("agents.missing_count", {count: counts.missing}));
        }
        if (counts.error) {
          parts.push(t("agents.error_count", {count: counts.error}));
        }
        if (counts.unknown) {
          parts.push(t("agents.checking_count", {count: counts.unknown}));
        }
        summary.textContent = parts.join(" · ");
      }
      card.classList.toggle(
        "is-stale",
        cardRows.some((row) => row.dataset.stale === "true"),
      );
    }
  };

  const loadAgentNode = async (node, refresh = false) => {
    const nodeRows = rows.filter((row) => row.dataset.node === node);
    for (const row of nodeRows) {
      row.classList.add("is-loading");
    }
    for (const item of sidebarItems) {
      item.classList.add("is-loading");
      const stateLabel = item.querySelector("[data-agent-state] span");
      if (stateLabel) {
        stateLabel.textContent = t("agents.state.checking");
      }
    }
    try {
      const query = refresh ? "?refresh=true" : "";
      const response = await fetch(`/api/nodes/${encodeURIComponent(node)}/agent-tools${query}`);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || t("agents.check_failed"));
      }
      renderNodePayload(node, body);
      return true;
    } catch (error) {
      const message = error.message || t("agents.check_failed");
      renderNodePayload(node, {
        supported: false,
        stale: true,
        error: message,
        tools: [],
      });
      return false;
    } finally {
      for (const row of nodeRows) {
        row.classList.remove("is-loading");
      }
      for (const item of sidebarItems) {
        item.classList.remove("is-loading");
      }
    }
  };

  const loadAgentTools = async (refresh = false) => {
    refreshButton.disabled = true;
    if (!nodeNames.length) {
      updateCardSummaries();
      status.textContent = t("agents.no_node");
      refreshButton.disabled = false;
      return;
    }
    status.textContent = nodeNames.length === 1
      ? t("agents.checking_node", {node: nodeNames[0]})
      : t("agents.checking_nodes", {count: nodeNames.length});
    const results = await Promise.all(nodeNames.map((node) => loadAgentNode(node, refresh)));
    updateCardSummaries();
    const failed = results.filter((ok) => !ok).length;
    status.textContent = failed
      ? (failed === 1
        ? t("agents.check_result_one_failed", {ready: nodeNames.length - failed, total: nodeNames.length})
        : t("agents.check_result", {ready: nodeNames.length - failed, total: nodeNames.length, failed}))
      : nodeNames.length === 1
        ? t("agents.node_current", {node: nodeNames[0]})
        : t("agents.nodes_checked", {count: nodeNames.length});
    refreshButton.disabled = false;
  };

  refreshButton.addEventListener("click", () => loadAgentTools(true));
  window.StarAgentAfterPaint(() => loadAgentTools(false));
}

const historyBand = document.querySelector(".agent-history-band");
if (historyBand) {
  const form = historyBand.querySelector(".agent-history-controls");
  const nodeSelect = form.querySelector('[name="node"]');
  const agentSelect = form.querySelector('[name="agent"]');
  const limitSelect = form.querySelector('select[name="limit"]');
  const scanButton = form.querySelector('button[type="submit"]');
  const status = historyBand.querySelector(".agent-history-status");
  const list = historyBand.querySelector(".agent-history-list");

  const formatTime = (value) => {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime())
      ? t("sessions.unknown_time")
      : date.toLocaleString(window.StarAgentI18n?.language || []);
  };

  const formatSize = (value) => {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes <= 0) {
      return "0 B";
    }
    if (bytes < 1024) {
      return `${bytes} B`;
    }
    if (bytes < 1024 * 1024) {
      return `${(bytes / 1024).toFixed(1)} KB`;
    }
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const appendMetadata = (container, label, value) => {
    if (!value) {
      return;
    }
    const item = document.createElement("span");
    item.textContent = `${label}: ${value}`;
    container.appendChild(item);
  };

  const openHistoryInCreateSession = (entry) => {
    if (!entry.cwd || !entry.id || !entry.agent) {
      status.textContent = t("agents.history_missing_metadata");
      return;
    }
    const query = new URLSearchParams({
      node: nodeSelect.value,
      agent: entry.agent,
      resume: entry.id,
    });
    window.location.href = `/nodes/${encodeURIComponent(nodeSelect.value)}/sessions?${query}#create-session`;
  };

  const renderHistory = (payload) => {
    list.replaceChildren();
    const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
    for (const entry of sessions) {
      const card = document.createElement("article");
      card.className = "agent-history-card";

      const header = document.createElement("header");
      const identity = document.createElement("div");
      const label = document.createElement("span");
      label.className = "pill mode-agent";
      label.textContent = entry.label || entry.agent || t("detail.agent");
      const updated = document.createElement("span");
      updated.className = "agent-history-updated";
      updated.textContent = formatTime(entry.updated_at);
      identity.append(label, updated);
      const id = document.createElement("code");
      id.textContent = String(entry.id || "").slice(0, 13);
      id.title = entry.id || "";
      header.append(identity, id);

      const title = document.createElement("strong");
      title.className = "agent-history-preview";
      title.textContent = entry.title || t("agents.untitled");

      const cwd = document.createElement("code");
      cwd.className = "agent-history-cwd";
      cwd.textContent = entry.cwd || t("sessions.cwd_unavailable");
      cwd.title = entry.cwd || "";

      const metadata = document.createElement("div");
      metadata.className = "agent-history-metadata";
      appendMetadata(metadata, t("agents.prompts"), entry.prompt_count);
      appendMetadata(metadata, t("agents.version"), entry.cli_version);
      appendMetadata(metadata, t("agents.branch"), entry.git_branch);
      appendMetadata(metadata, t("agents.file"), formatSize(entry.size_bytes));

      const actions = document.createElement("div");
      actions.className = "agent-history-actions";
      const resumeCode = document.createElement("code");
      resumeCode.textContent = entry.resume_command || t("agents.resume_unavailable");
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-button inline-copy";
      copy.dataset.copy = entry.resume_command || "";
      copy.textContent = t("common.copy");
      copy.disabled = !entry.resume_command;
      const resume = document.createElement("button");
      resume.type = "button";
      resume.className = "agent-history-resume";
      resume.textContent = t("agents.use_in_create");
      resume.disabled = !entry.cwd || !entry.id || !entry.agent;
      resume.addEventListener("click", () => openHistoryInCreateSession(entry));
      actions.append(resumeCode, copy, resume);
      card.append(header, title, cwd, metadata, actions);
      list.appendChild(card);
    }
    if (!sessions.length) {
      const empty = document.createElement("div");
      empty.className = "agent-empty";
      empty.textContent = payload.error || t("agents.no_history");
      list.appendChild(empty);
    }
    const parts = [sessions.length === 1
      ? t("agents.history_count_one")
      : t("agents.history_count", {count: sessions.length})];
    if (payload.truncated) {
      parts.push(t("agents.history_limited"));
    }
    if (payload.scanned_at) {
      parts.push(t("agents.scanned", {time: formatTime(payload.scanned_at)}));
    }
    if (payload.error) {
      parts.push(payload.error);
    }
    status.textContent = parts.join(" · ");
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    scanButton.disabled = true;
    status.textContent = t("agents.scanning_node", {node: nodeSelect.value});
    list.replaceChildren();
    const query = new URLSearchParams({
      agent: agentSelect.value,
      limit: limitSelect.value,
      refresh: "true",
    });
    try {
      const response = await fetch(`/api/nodes/${encodeURIComponent(nodeSelect.value)}/agent-history?${query}`);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || t("sessions.history_failed"));
      }
      renderHistory(body);
    } catch (error) {
      renderHistory({sessions: [], error: error.message || t("sessions.history_failed")});
    } finally {
      scanButton.disabled = false;
    }
  });
}
