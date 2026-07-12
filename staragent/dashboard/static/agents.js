const agentCatalog = Array.from(document.querySelectorAll(".agent-catalog-data [data-name]")).map((item) => ({
  name: item.dataset.name || "",
  label: item.dataset.label || item.dataset.name || "Agent CLI",
  command: item.dataset.command || item.dataset.name || "",
}));

async function copyAgentText(button, value) {
  const original = button.textContent;
  try {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        button.textContent = "Copied";
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
    button.textContent = "Copied";
  } catch (_error) {
    button.textContent = "Copy failed";
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
  const nodeNames = [...new Set(rows.map((row) => row.dataset.node || "").filter(Boolean))];
  const catalogByName = new Map(agentCatalog.map((tool) => [tool.name, tool]));
  const refreshButton = agentToolsBand.querySelector(".agent-tools-refresh");
  const status = agentToolsBand.querySelector(".agent-tools-status");

  const toolState = (value) => ({
    available: {label: "ready", style: "connected"},
    missing: {label: "missing", style: "disconnected"},
    error: {label: "error", style: "error"},
    unknown: {label: "unknown", style: "optional"},
  }[value] || {label: "unknown", style: "optional"});

  const checkedTime = (value) => {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime()) ? "not checked" : date.toLocaleString();
  };

  const fallbackTool = (name, message) => {
    const catalog = catalogByName.get(name) || {name, label: name, command: name};
    return {...catalog, status: "unknown", error: message};
  };

  const installDescription = (tool) => {
    if (tool.status === "missing") {
      return "Not installed in the Node service PATH.";
    }
    if (!tool.install_method || tool.install_method === "unknown") {
      return "Install source unknown.";
    }
    return `${tool.install_method} installation`;
  };

  const formatPercent = (value) => {
    const number = Math.max(0, Math.min(100, Number(value || 0)));
    return Number.isInteger(number) ? String(number) : number.toFixed(1);
  };

  const formatResetTime = (value) => {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime()) ? "Reset time unavailable" : `Resets ${date.toLocaleString()}`;
  };

  const usageStatusLabel = (value) => ({
    available: "live",
    manual: "manual",
    unavailable: "unavailable",
    error: "error",
    unknown: "unknown",
  }[value] || "unknown");

  const usageStatusStyle = (value) => ({
    available: "connected",
    manual: "optional",
    unavailable: "optional",
    error: "error",
    unknown: "optional",
  }[value] || "optional");

  const authState = (value) => ({
    authenticated: {label: "logged in", style: "connected"},
    configured: {label: "configured", style: "connected"},
    not_authenticated: {label: "signed out", style: "disconnected"},
    not_configured: {label: "not configured", style: "disconnected"},
    unavailable: {label: "unavailable", style: "optional"},
    error: {label: "error", style: "error"},
    unknown: {label: "unknown", style: "optional"},
  }[value] || {label: "unknown", style: "optional"});

  const renderAgentAuth = (container, auth) => {
    container.replaceChildren();
    const state = authState(auth?.status || "unknown");
    container.className = `agent-cli-auth is-${auth?.status || "unknown"}`;

    const head = document.createElement("div");
    head.className = "agent-auth-head";
    const title = document.createElement("strong");
    title.textContent = "Login";
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
      details.push(`${count} source${count === 1 ? "" : "s"}`);
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
      copy.textContent = "Copy login";
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
    label.textContent = window.label || "Limit";
    const value = document.createElement("strong");
    value.textContent = `${formatPercent(remaining)}% left`;
    copy.append(label, value);
    const meter = document.createElement("div");
    meter.className = "agent-usage-meter";
    meter.setAttribute("role", "progressbar");
    meter.setAttribute("aria-label", `${window.label || "Usage"} remaining`);
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
      notes.push("Unlimited credits");
    } else if (bucket.credits?.has_credits && bucket.credits.balance) {
      notes.push(`Credits ${bucket.credits.balance}`);
    }
    if (bucket.individual_limit) {
      notes.push(`${formatPercent(bucket.individual_limit.remaining_percent)}% spend limit left`);
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
    title.textContent = "Usage";
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
      details.push(`${Number(usage.reset_credits)} reset${Number(usage.reset_credits) === 1 ? "" : "s"} available`);
    }
    if (usage.checked_at) {
      details.push(`checked ${checkedTime(usage.checked_at)}`);
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
      copy.textContent = "Copy command";
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

  const runAgentUpdate = async (row, tool, button) => {
    const node = row.dataset.node || "";
    const label = tool.label || tool.name || "Agent CLI";
    if (!confirm(`Update ${label} on ${node}?\n\nStarAgent will run the allowlisted update command shown here.`)) {
      return;
    }
    row.classList.add("is-updating");
    button.disabled = true;
    button.textContent = "Updating…";
    setAgentUpdateResult(row, `Updating ${label} on ${node}…`);
    try {
      const response = await fetch(
        `/api/nodes/${encodeURIComponent(node)}/agent-tools/${encodeURIComponent(tool.name)}/update`,
        {method: "POST"},
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.ok) {
        throw new Error(body.detail || body.error || "Update failed.");
      }
      await loadAgentNode(node, true);
      updateCardSummaries();
      const before = body.before_version || "previous version";
      const after = body.after_version || "current version";
      const message = body.changed
        ? `${label} updated: ${before} → ${after}`
        : `${label} update completed; ${after} is already current.`;
      setAgentUpdateResult(row, message);
      status.textContent = message;
    } catch (error) {
      const message = error.message || "Update failed.";
      setAgentUpdateResult(row, message, true);
      status.textContent = `${label} update failed on ${node}: ${message}`;
    } finally {
      row.classList.remove("is-updating");
      button.disabled = false;
      button.textContent = "Update now";
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
      || (tool.status === "missing" ? `${tool.command || tool.name} not found` : "No version reported");
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
      copy.textContent = tool.update_action === "install" ? "Copy install" : "Copy update";
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
        update.textContent = "Update now";
        update.addEventListener("click", () => runAgentUpdate(row, tool, update));
        actions.appendChild(update);
      }
    }

    const parts = [checkedTime(payload.checked_at)];
    if (payload.stale) {
      parts.push("stale");
    }
    if (!payload.supported) {
      parts.push("Node update required");
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
    for (const row of rows.filter((item) => item.dataset.node === node)) {
      const agent = row.closest(".agent-cli-card")?.dataset.agent || "";
      renderAgentNode(
        row,
        tools.get(agent) || fallbackTool(agent, payload.error || "No result reported."),
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
      const parts = [`${counts.available} / ${cardRows.length} machines ready`];
      if (counts.missing) {
        parts.push(`${counts.missing} missing`);
      }
      if (counts.error) {
        parts.push(`${counts.error} error`);
      }
      if (counts.unknown) {
        parts.push(`${counts.unknown} unknown`);
      }
      card.querySelector(".agent-cli-summary").textContent = cardRows.length
        ? parts.join(" · ")
        : "No machines configured.";
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
    try {
      const query = refresh ? "?refresh=true" : "";
      const response = await fetch(`/api/nodes/${encodeURIComponent(node)}/agent-tools${query}`);
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || "Agent CLI check failed.");
      }
      renderNodePayload(node, body);
      return true;
    } catch (error) {
      const message = error.message || "Agent CLI check failed.";
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
    }
  };

  const loadAgentTools = async (refresh = false) => {
    refreshButton.disabled = true;
    if (!nodeNames.length) {
      updateCardSummaries();
      status.textContent = "No machines configured.";
      refreshButton.disabled = false;
      return;
    }
    status.textContent = `Checking ${nodeNames.length} machine${nodeNames.length === 1 ? "" : "s"}…`;
    const results = await Promise.all(nodeNames.map((node) => loadAgentNode(node, refresh)));
    updateCardSummaries();
    const failed = results.filter((ok) => !ok).length;
    status.textContent = failed
      ? `${nodeNames.length - failed} / ${nodeNames.length} machines checked; ${failed} request${failed === 1 ? "" : "s"} failed.`
      : `${nodeNames.length} machine${nodeNames.length === 1 ? "" : "s"} checked.`;
    refreshButton.disabled = false;
  };

  refreshButton.addEventListener("click", () => loadAgentTools(true));
  window.StarAgentAfterPaint(() => loadAgentTools(false));
}

const historyBand = document.querySelector(".agent-history-band");
if (historyBand) {
  const form = historyBand.querySelector(".agent-history-controls");
  const nodeSelect = form.querySelector('select[name="node"]');
  const agentSelect = form.querySelector('select[name="agent"]');
  const limitSelect = form.querySelector('select[name="limit"]');
  const scanButton = form.querySelector('button[type="submit"]');
  const status = historyBand.querySelector(".agent-history-status");
  const list = historyBand.querySelector(".agent-history-list");

  const formatTime = (value) => {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime()) ? "unknown time" : date.toLocaleString();
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

  const startHistorySession = async (entry, button) => {
    if (!entry.cwd || !entry.resume_command) {
      status.textContent = "This history entry has no working directory or resume command.";
      return;
    }
    const suffix = Date.now().toString(36).slice(-4);
    const shortId = String(entry.id || "history").slice(0, 8).replace(/[^A-Za-z0-9_.:-]/g, "");
    const name = `${entry.agent}-resume-${shortId}-${suffix}`.slice(0, 80);
    button.disabled = true;
    button.textContent = "Starting…";
    const response = await fetch("/api/workers", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        node: nodeSelect.value,
        name,
        cwd: entry.cwd,
        command: entry.resume_command,
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      status.textContent = body.detail || "Resume failed.";
      button.disabled = false;
      button.textContent = "Resume";
      return;
    }
    status.textContent = "Resumed conversation. Opening session…";
    setTimeout(() => {
      window.location.href = `/nodes/${encodeURIComponent(nodeSelect.value)}/sessions/${encodeURIComponent(name)}`;
    }, 350);
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
      label.textContent = entry.label || entry.agent || "Agent";
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
      title.textContent = entry.title || "Untitled conversation";

      const cwd = document.createElement("code");
      cwd.className = "agent-history-cwd";
      cwd.textContent = entry.cwd || "Working directory unavailable";
      cwd.title = entry.cwd || "";

      const metadata = document.createElement("div");
      metadata.className = "agent-history-metadata";
      appendMetadata(metadata, "Prompts", entry.prompt_count);
      appendMetadata(metadata, "Version", entry.cli_version);
      appendMetadata(metadata, "Branch", entry.git_branch);
      appendMetadata(metadata, "File", formatSize(entry.size_bytes));

      const actions = document.createElement("div");
      actions.className = "agent-history-actions";
      const resumeCode = document.createElement("code");
      resumeCode.textContent = entry.resume_command || "Resume command unavailable";
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-button inline-copy";
      copy.dataset.copy = entry.resume_command || "";
      copy.textContent = "Copy";
      copy.disabled = !entry.resume_command;
      const resume = document.createElement("button");
      resume.type = "button";
      resume.className = "agent-history-resume";
      resume.textContent = "Resume";
      resume.disabled = !entry.cwd || !entry.resume_command;
      resume.addEventListener("click", () => startHistorySession(entry, resume));
      actions.append(resumeCode, copy, resume);
      card.append(header, title, cwd, metadata, actions);
      list.appendChild(card);
    }
    if (!sessions.length) {
      const empty = document.createElement("div");
      empty.className = "agent-empty";
      empty.textContent = payload.error || "No matching conversation histories were found.";
      list.appendChild(empty);
    }
    const parts = [`${sessions.length} conversation${sessions.length === 1 ? "" : "s"}`];
    if (payload.truncated) {
      parts.push("limited result; narrow the CLI filter to see more");
    }
    if (payload.scanned_at) {
      parts.push(`scanned ${formatTime(payload.scanned_at)}`);
    }
    if (payload.error) {
      parts.push(payload.error);
    }
    status.textContent = parts.join(" · ");
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    scanButton.disabled = true;
    status.textContent = `Scanning ${nodeSelect.value}…`;
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
        throw new Error(body.detail || "History scan failed.");
      }
      renderHistory(body);
    } catch (error) {
      renderHistory({sessions: [], error: error.message || "History scan failed."});
    } finally {
      scanButton.disabled = false;
    }
  });
}
