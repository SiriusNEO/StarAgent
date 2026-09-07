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
  const updateDialog = agentToolsBand.querySelector(
    ".agent-update-dialog:not(.agent-install-dialog)",
  );
  const installDialog = agentToolsBand.querySelector(".agent-install-dialog");

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

  const credentialDescription = (auth) => {
    const credentialType = auth?.credential_type || "unknown";
    if (credentialType === "environment") {
      const name = auth?.credential_name || t("agents.unknown");
      return auth?.status === "not_configured"
        ? t("agents.credential.environment_missing", {name})
        : t("agents.credential.environment", {name});
    }
    const key = ({
      api_key: "agents.credential.api_key",
      bearer_token: "agents.credential.bearer_token",
      chatgpt: "agents.credential.chatgpt",
      command: "agents.credential.command",
      external: "agents.credential.external",
      none: "agents.credential.none",
    })[credentialType];
    return key ? t(key) : "";
  };

  const renderAgentAuth = (container, auth, tool, row) => {
    container.replaceChildren();
    const state = authState(auth?.status || "unknown");
    container.className = `agent-cli-auth is-${auth?.status || "unknown"}`;

    const head = document.createElement("div");
    head.className = "agent-auth-head";
    const title = document.createElement("strong");
    title.textContent = t("agents.access");
    const statusPill = document.createElement("span");
    statusPill.className = `pill node-status-${state.style}`;
    statusPill.textContent = state.label;
    head.append(title, statusPill);
    container.appendChild(head);

    const details = [];
    if (auth?.provider) {
      details.push(t("agents.provider_name", {provider: auth.provider}));
    }
    const credential = credentialDescription(auth);
    if (credential) {
      details.push(credential);
    } else if (auth?.method) {
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

    if (auth?.action && tool?.name !== "codex") {
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

    if (tool?.name === "codex" && tool?.status === "available") {
      const controls = document.createElement("div");
      controls.className = "agent-auth-controls";

      const refresh = document.createElement("button");
      refresh.type = "button";
      refresh.className = "agent-auth-button is-secondary";
      refresh.textContent = t("agents.refresh_access");
      refresh.addEventListener("click", async () => {
        refresh.disabled = true;
        await loadAgentNode(row.dataset.node || "", true);
        updateCardSummaries();
      });
      controls.appendChild(refresh);

      if (auth?.status === "not_authenticated") {
        const login = document.createElement("button");
        login.type = "button";
        login.className = "agent-auth-button is-primary";
        login.textContent = t("agents.login_codex");
        login.addEventListener("click", () => {
          window.dispatchEvent(new CustomEvent("staragent:codex-login", {
            detail: {node: row.dataset.node || ""},
          }));
        });
        controls.appendChild(login);
      } else if (auth?.status === "authenticated") {
        const logout = document.createElement("button");
        logout.type = "button";
        logout.className = "agent-auth-button is-danger";
        logout.textContent = t("agents.logout_codex");
        let confirmationTimer = 0;
        logout.addEventListener("click", async () => {
          if (logout.dataset.confirming !== "true") {
            logout.dataset.confirming = "true";
            logout.textContent = t("agents.confirm_logout");
            window.clearTimeout(confirmationTimer);
            confirmationTimer = window.setTimeout(() => {
              logout.dataset.confirming = "false";
              logout.textContent = t("agents.logout_codex");
            }, 5000);
            return;
          }
          window.clearTimeout(confirmationTimer);
          controls.querySelectorAll("button").forEach((button) => {
            button.disabled = true;
          });
          logout.textContent = t("agents.logging_out");
          const node = row.dataset.node || "";
          try {
            const response = await fetch(
              `/api/nodes/${encodeURIComponent(node)}/agent-tools/codex/auth/logout`,
              {method: "POST", cache: "no-store"},
            );
            const body = await response.json().catch(() => ({}));
            if (!response.ok || !body.ok) {
              throw new Error(body.detail || t("agents.auth.error"));
            }
            await loadAgentNode(node, true);
            updateCardSummaries();
          } catch (error) {
            status.textContent = t("agents.logout_failed", {
              message: error?.message || t("agents.auth.error"),
            });
            controls.querySelectorAll("button").forEach((button) => {
              button.disabled = false;
            });
            logout.dataset.confirming = "false";
            logout.textContent = t("agents.logout_codex");
          }
        });
        controls.appendChild(logout);
      }
      container.appendChild(controls);
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
      message.textContent = usage.message_code === "provider_rate_limits_unavailable"
        ? t("agents.usage.provider_unavailable_message")
        : usage.message;
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

  const installMethodLabel = (method) => method === "native"
    ? t("agents.install_native")
    : t("agents.install_npm");

  const installSourceLabel = (option) => option.source === "official"
    ? t("agents.install_official")
    : t("agents.install_china_mirror");

  const confirmAgentInstall = (row, tool, option) => {
    const node = row.dataset.node || t("agents.this_node");
    const label = tool.label || tool.name || t("sessions.agent_cli");
    const source = `${option.provider} · ${installMethodLabel(option.method)}`;
    if (!installDialog || typeof installDialog.showModal !== "function") {
      return Promise.resolve(window.confirm(t("agents.install_confirm", {
        agent: label,
        node,
        source,
      })));
    }

    const card = row.closest(".agent-cli-card");
    const sourceIcon = sidebarItems
      .find((item) => item.dataset.agent === tool.name)
      ?.querySelector("img");
    const dialogIcon = installDialog.querySelector(".agent-update-dialog-icon img");
    const accent = card ? getComputedStyle(card).getPropertyValue("--agent-brand").trim() : "";
    installDialog.dataset.agent = tool.name || "";
    installDialog.style.setProperty("--agent-brand", accent || "var(--accent)");
    installDialog.querySelector("#agent-install-title").textContent = t("agents.install", {
      agent: label,
    });
    installDialog.querySelector(".agent-install-dialog-node").textContent = node;
    installDialog.querySelector(".agent-install-dialog-source").textContent = source;
    const commandPanel = installDialog.querySelector(".agent-update-dialog-command");
    commandPanel.hidden = !option.command;
    commandPanel.querySelector("code").textContent = option.command || "";
    if (sourceIcon && dialogIcon) {
      dialogIcon.src = sourceIcon.src;
    }

    installDialog.returnValue = "cancel";
    return new Promise((resolve) => {
      installDialog.addEventListener(
        "close",
        () => resolve(installDialog.returnValue === "confirm"),
        {once: true},
      );
      installDialog.showModal();
      requestAnimationFrame(() => {
        installDialog.querySelector(".agent-update-dialog-cancel")?.focus({preventScroll: true});
      });
    });
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

  installDialog?.addEventListener("click", (event) => {
    if (event.target === installDialog) {
      installDialog.close("cancel");
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

  const runAgentInstall = async (row, tool, option, button) => {
    const node = row.dataset.node || "";
    const label = tool.label || tool.name || t("sessions.agent_cli");
    if (!await confirmAgentInstall(row, tool, option)) {
      return;
    }
    row.classList.add("is-installing");
    refreshButton.disabled = true;
    row.querySelectorAll(".agent-install-button").forEach((installButton) => {
      installButton.disabled = true;
    });
    row.querySelectorAll(".launcher-source-choice, .launcher-install-copy").forEach((control) => {
      control.disabled = true;
    });
    button.textContent = t("agents.installing");
    setAgentUpdateResult(row, t("agents.installing_on", {
      agent: label,
      node,
      source: option.provider,
    }));
    try {
      const response = await fetch(
        `/api/nodes/${encodeURIComponent(node)}/agent-tools/${encodeURIComponent(tool.name)}/install/${encodeURIComponent(option.id)}`,
        {method: "POST"},
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.ok) {
        throw new Error(body.detail || body.error || body.output || t("agents.install_failed"));
      }
      await loadAgentNode(node, true);
      updateCardSummaries();
      const version = body.after_version || t("sessions.current_version");
      const message = body.changed
        ? t("agents.installed", {agent: label, version})
        : t("agents.install_already", {agent: label, version});
      setAgentUpdateResult(row, message);
      status.textContent = message;
    } catch (error) {
      const message = error.message || t("agents.install_failed");
      setAgentUpdateResult(row, message, true);
      status.textContent = t("agents.install_failed_on", {agent: label, node, message});
    } finally {
      row.classList.remove("is-installing");
      refreshButton.disabled = false;
      row.querySelectorAll(".agent-install-button").forEach((installButton) => {
        installButton.disabled = installButton.dataset.enabled !== "true";
        installButton.textContent = t("agents.install_now");
      });
      row.querySelectorAll(".launcher-source-choice, .launcher-install-copy").forEach((control) => {
        control.disabled = false;
      });
    }
  };

  const renderAgentInstallOptions = (row, tool, payload, actions) => {
    const options = Array.isArray(tool.install_options) ? tool.install_options : [];
    const panel = document.createElement("section");
    panel.className = "agent-install-panel launcher-install";

    const head = document.createElement("div");
    head.className = "agent-install-panel-head";
    const headCopy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = t("agents.install_choose");
    const hint = document.createElement("span");
    hint.textContent = t("agents.install_choose_hint");
    headCopy.append(title, hint);
    const mirrorNote = document.createElement("small");
    mirrorNote.textContent = t("agents.install_mirror_note");
    head.append(headCopy, mirrorNote);
    panel.appendChild(head);

    const steps = document.createElement("ol");
    steps.className = "launcher-install-steps";
    [
      t("agents.install_step_runtime"),
      t("agents.install_step_source"),
      t("agents.install_step_install"),
    ].forEach((label, index) => {
      const step = document.createElement("li");
      step.classList.toggle("is-complete", index === 0);
      step.classList.toggle("is-active", index === 1);
      const number = document.createElement("span");
      number.textContent = String(index + 1);
      const text = document.createElement("strong");
      text.textContent = label;
      step.append(number, text);
      steps.appendChild(step);
    });
    panel.appendChild(steps);

    const list = document.createElement("div");
    list.className = "launcher-source-list";
    const choices = [];
    for (const option of options) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "launcher-source-choice";
      item.setAttribute("aria-pressed", "false");
      item.classList.toggle("is-unavailable", !option.available);
      const radio = document.createElement("span");
      radio.className = "launcher-source-radio";
      radio.setAttribute("aria-hidden", "true");
      const information = document.createElement("span");
      information.className = "launcher-source-copy";
      const sourceLine = document.createElement("div");
      sourceLine.className = "launcher-source-title";
      const provider = document.createElement("strong");
      provider.textContent = option.provider || installSourceLabel(option);
      sourceLine.appendChild(provider);

      const sourceBadge = document.createElement("span");
      sourceBadge.className = option.china
        ? "agent-install-badge is-china"
        : "agent-install-badge";
      sourceBadge.textContent = installSourceLabel(option);
      sourceLine.appendChild(sourceBadge);
      if (option.recommended) {
        const recommended = document.createElement("span");
        recommended.className = "agent-install-badge is-recommended";
        recommended.textContent = t("agents.install_recommended");
        sourceLine.appendChild(recommended);
      }

      const method = document.createElement("span");
      method.className = "launcher-source-method";
      const missingRequirements = Array.isArray(option.missing_requirements)
        ? option.missing_requirements.join(", ")
        : "";
      if (!option.available) {
        method.textContent = t("agents.install_missing_requirement", {
          commands: missingRequirements,
        });
      } else {
        method.textContent = installMethodLabel(option.method);
      }
      information.append(sourceLine, method);
      const arrow = document.createElement("span");
      arrow.className = "launcher-source-arrow";
      arrow.setAttribute("aria-hidden", "true");
      arrow.textContent = "›";
      item.append(radio, information, arrow);
      list.appendChild(item);
      choices.push({button: item, option});
    }
    panel.appendChild(list);

    if (!options.length) {
      const empty = document.createElement("p");
      empty.className = "agent-install-empty";
      empty.textContent = t("agents.install_options_unavailable");
      panel.appendChild(empty);
    } else if (!payload.installs_supported || payload.stale) {
      const compatibility = document.createElement("p");
      compatibility.className = "agent-install-compatibility";
      compatibility.textContent = t("agents.install_node_update_required");
      panel.appendChild(compatibility);
    }

    if (options.length) {
      const selection = document.createElement("div");
      selection.className = "launcher-install-selection";
      const selectionCopy = document.createElement("div");
      selectionCopy.className = "launcher-install-selection-copy";
      const selectionLabel = document.createElement("span");
      selectionLabel.textContent = t("agents.install_selected_source");
      const selectionName = document.createElement("strong");
      const selectionHint = document.createElement("small");
      const sourceLink = document.createElement("a");
      sourceLink.target = "_blank";
      sourceLink.rel = "noopener noreferrer";
      sourceLink.textContent = t("agents.install_open_source");
      selectionCopy.append(selectionLabel, selectionName, selectionHint, sourceLink);

      const commandDetails = document.createElement("details");
      commandDetails.className = "launcher-command-details";
      const commandSummary = document.createElement("summary");
      commandSummary.textContent = t("agents.install_command_details");
      const command = document.createElement("code");
      commandDetails.append(commandSummary, command);

      const controls = document.createElement("div");
      controls.className = "launcher-install-actions";
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "copy-button inline-copy launcher-install-copy";
      copy.textContent = t("agents.copy_install");
      const install = document.createElement("button");
      install.type = "button";
      install.className = "agent-cli-update-button agent-install-button launcher-install-primary";
      install.textContent = t("agents.install_now");
      controls.append(copy, install);
      selection.append(selectionCopy, commandDetails, controls);
      panel.appendChild(selection);

      let selectedOption = null;
      const choose = (option) => {
        selectedOption = option;
        steps.children[1]?.classList.remove("is-active");
        steps.children[1]?.classList.add("is-complete");
        steps.children[2]?.classList.add("is-active");
        choices.forEach(({button: choice, option: candidate}) => {
          const active = candidate.id === option.id;
          choice.classList.toggle("is-selected", active);
          choice.setAttribute("aria-pressed", active ? "true" : "false");
        });
        const oneClickAvailable = Boolean(
          payload.installs_supported && !payload.stale && option.available,
        );
        selectionName.textContent = [
          option.provider,
          installMethodLabel(option.method),
        ].filter(Boolean).join(" · ");
        selectionHint.textContent = oneClickAvailable
          ? t("agents.install_ready")
          : t("agents.install_copy_fallback");
        selectionHint.classList.toggle("is-warning", !oneClickAvailable);
        sourceLink.hidden = !option.source_url;
        sourceLink.href = option.source_url || "#";
        commandDetails.hidden = !option.command;
        command.textContent = option.command || "";
        copy.hidden = !option.command;
        copy.dataset.copy = option.command || "";
        install.dataset.enabled = oneClickAvailable ? "true" : "false";
        install.disabled = !oneClickAvailable;
        const missingRequirements = Array.isArray(option.missing_requirements)
          ? option.missing_requirements.join(", ")
          : "";
        install.title = !option.available
          ? t("agents.install_missing_requirement", {commands: missingRequirements})
          : (!payload.installs_supported || payload.stale)
            ? t("agents.install_node_update_required")
            : "";
      };
      choices.forEach(({button: choice, option}) => {
        choice.addEventListener("click", () => choose(option));
      });
      install.addEventListener("click", () => {
        if (selectedOption) {
          runAgentInstall(row, tool, selectedOption, install);
        }
      });
      choose(
        options.find((option) => option.recommended && option.available)
          || options.find((option) => option.available)
          || options.find((option) => option.recommended)
          || options[0],
      );
    }
    actions.appendChild(panel);
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
    const isMissing = tool.status === "missing";
    row.classList.toggle("is-missing", isMissing);
    if (isMissing) {
      auth.replaceChildren();
      usage.replaceChildren();
      auth.hidden = true;
      usage.hidden = true;
    } else {
      auth.hidden = false;
      renderAgentAuth(auth, tool.auth || {}, tool, row);
      renderAgentUsage(usage, tool.usage || {});
    }

    actions.replaceChildren();
    const updateStatus = tool.update?.status || "unknown";
    if (isMissing) {
      renderAgentInstallOptions(row, tool, payload, actions);
    } else if (updateStatus === "up_to_date") {
      const current = document.createElement("span");
      current.className = "agent-cli-update-state is-current";
      current.textContent = tool.update.latest_version
        ? t("agents.up_to_date_version", {version: tool.update.latest_version})
        : t("agents.up_to_date");
      current.title = tool.update.checked_at ? checkedTime(tool.update.checked_at) : "";
      actions.appendChild(current);
    } else if (tool.update_command) {
      if (updateStatus === "update_available" && tool.update.latest_version) {
        const available = document.createElement("span");
        available.className = "agent-cli-update-state is-available";
        available.textContent = t("agents.update_available_version", {
          version: tool.update.latest_version,
        });
        actions.appendChild(available);
      }
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
    row.dataset.updateStatus = updateStatus;
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
  window.addEventListener("staragent:agent-auth-finished", async (event) => {
    const node = event.detail?.node || "";
    if (!nodeNames.includes(node)) {
      return;
    }
    await loadAgentNode(node, true);
    updateCardSummaries();
  });
  window.StarAgentAfterPaint(() => loadAgentTools(false));
}

const harnessConfiguration = document.querySelector("[data-harness-configuration]");
if (harnessConfiguration) {
  const node = harnessConfiguration.dataset.node || "";
  const agent = harnessConfiguration.dataset.agent || "";
  const status = harnessConfiguration.querySelector(".harness-config-status");
  const reloadButton = harnessConfiguration.querySelector(".harness-config-reload");
  const editor = harnessConfiguration.querySelector(".harness-config-editor");
  const configPath = harnessConfiguration.querySelector(".harness-config-path");
  const configFormat = harnessConfiguration.querySelector(".harness-config-format");
  const configDocs = harnessConfiguration.querySelector(".harness-config-docs");
  const configSource = harnessConfiguration.querySelector(".harness-config-source");
  const configExists = harnessConfiguration.querySelector(".harness-config-exists");
  const configModified = harnessConfiguration.querySelector(".harness-config-modified");
  const configState = harnessConfiguration.querySelector(".harness-config-file-state");
  const configSave = harnessConfiguration.querySelector(".harness-config-save");
  const inheritedEnvironment = harnessConfiguration.querySelector(".harness-env-inherited");
  const inheritedEnvironmentList = harnessConfiguration.querySelector(
    ".harness-env-inherited-list",
  );
  const environmentList = harnessConfiguration.querySelector(".harness-env-list");
  const environmentEmpty = harnessConfiguration.querySelector(".harness-env-empty");
  const environmentState = harnessConfiguration.querySelector(".harness-env-state");
  const environmentPath = harnessConfiguration.querySelector(".harness-env-path");
  const environmentAdd = harnessConfiguration.querySelector(".harness-env-add");
  const environmentSave = harnessConfiguration.querySelector(".harness-env-save");
  const suggestions = harnessConfiguration.querySelector(".harness-env-suggestions");

  let supported = false;
  let configEditable = false;
  let initialConfig = "";
  let initialEnvironment = "[]";
  let reloadArmed = false;
  let reloadTimer = 0;

  const responseDetail = (body, fallback) => {
    if (typeof body?.detail === "string") {
      return body.detail;
    }
    if (typeof body?.error === "string") {
      return body.error;
    }
    return fallback;
  };

  const requestConfiguration = async (path, options = {}) => {
    const response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers: options.body
        ? {"Content-Type": "application/json", ...(options.headers || {})}
        : options.headers,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(responseDetail(body, `${response.status} ${response.statusText}`));
    }
    return body;
  };

  const environmentRows = () => Array.from(environmentList.querySelectorAll(".harness-env-row"));

  const environmentData = ({validate = false} = {}) => {
    const variables = [];
    const seen = new Set();
    for (const row of environmentRows()) {
      const name = row.querySelector(".harness-env-name").value.trim();
      const value = row.querySelector(".harness-env-value").value;
      if (validate && !name) {
        throw new Error(t("agents.empty_variable_name"));
      }
      if (!name && !value) {
        continue;
      }
      if (validate && seen.has(name)) {
        throw new Error(t("agents.duplicate_variable", {name}));
      }
      seen.add(name);
      variables.push([name, value]);
    }
    return variables.sort(([left], [right]) => left.localeCompare(right));
  };

  const environmentCountText = (count) => (
    count === 1
      ? t("agents.environment_saved_one")
      : t("agents.environment_saved", {count})
  );

  const formatConfigurationTime = (value) => {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime())
      ? "—"
      : date.toLocaleString(window.StarAgentI18n?.language || []);
  };

  const configIsDirty = () => editor.value !== initialConfig;
  const environmentIsDirty = () => JSON.stringify(environmentData()) !== initialEnvironment;

  const updateDirtyState = () => {
    const configDirty = configIsDirty();
    const environmentDirty = environmentIsDirty();
    configSave.disabled = !supported || !configEditable || !configDirty;
    environmentSave.disabled = !supported || !environmentDirty;
    if (configDirty) {
      configState.textContent = t("agents.file_modified");
      configState.classList.add("is-dirty");
    } else {
      configState.classList.remove("is-dirty");
    }
    if (environmentDirty) {
      environmentState.textContent = t("agents.environment_modified");
      environmentState.classList.add("is-dirty");
    } else {
      environmentState.classList.remove("is-dirty");
    }
    environmentEmpty.hidden = environmentRows().length > 0;
  };

  const variableLooksSecret = (name) => (
    /(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSCODE|CREDENTIAL|AUTH)/i.test(name)
  );

  const addEnvironmentRow = (variable = {}, {focus = false} = {}) => {
    const row = document.createElement("div");
    row.className = "harness-env-row";

    const name = document.createElement("input");
    name.type = "text";
    name.className = "harness-env-name";
    name.value = variable.name || "";
    name.placeholder = t("agents.variable_name");
    name.autocomplete = "off";
    name.spellcheck = false;
    if (suggestions?.id) {
      name.setAttribute("list", suggestions.id);
    }

    const valueWrap = document.createElement("div");
    valueWrap.className = "harness-env-value-wrap";
    const value = document.createElement("input");
    const secret = Boolean(variable.secret || variableLooksSecret(name.value));
    value.type = secret ? "password" : "text";
    value.className = "harness-env-value";
    value.value = variable.value || "";
    value.placeholder = t("agents.variable_value");
    value.autocomplete = "new-password";
    value.spellcheck = false;

    const reveal = document.createElement("button");
    reveal.type = "button";
    reveal.className = "harness-env-reveal";
    reveal.setAttribute("aria-label", t("agents.reveal_value"));
    reveal.title = t("agents.reveal_value");
    reveal.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z"/><circle cx="12" cy="12" r="2.5"/></svg>';
    reveal.addEventListener("click", () => {
      const revealing = value.type === "password";
      value.type = revealing ? "text" : "password";
      reveal.classList.toggle("is-revealing", revealing);
      reveal.setAttribute(
        "aria-label",
        revealing ? t("agents.hide_value") : t("agents.reveal_value"),
      );
      reveal.title = revealing ? t("agents.hide_value") : t("agents.reveal_value");
      value.focus({preventScroll: true});
    });
    reveal.hidden = !secret;
    valueWrap.append(value, reveal);

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "harness-env-remove";
    remove.setAttribute("aria-label", t("agents.remove_variable"));
    remove.title = t("agents.remove_variable");
    remove.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 11v5M14 11v5"/></svg>';
    remove.addEventListener("click", () => {
      row.remove();
      updateDirtyState();
    });

    name.addEventListener("input", () => {
      const nowSecret = variableLooksSecret(name.value);
      reveal.hidden = !nowSecret;
      if (nowSecret && !reveal.classList.contains("is-revealing")) {
        value.type = "password";
      } else if (!nowSecret) {
        value.type = "text";
        reveal.classList.remove("is-revealing");
      }
      updateDirtyState();
    });
    value.addEventListener("input", updateDirtyState);
    row.append(name, valueWrap, remove);
    environmentList.appendChild(row);
    updateDirtyState();
    if (focus) {
      name.focus({preventScroll: true});
    }
  };

  const applyConfig = (config) => {
    configEditable = supported && Boolean(config?.editable);
    initialConfig = typeof config?.content === "string" ? config.content : "";
    editor.value = initialConfig;
    editor.disabled = !configEditable;
    configPath.textContent = config?.path || "—";
    configPath.title = config?.path || "";
    configFormat.textContent = String(config?.format || "").toUpperCase() || "—";
    configSource.textContent = config?.source === "default"
      ? t("agents.configuration_default_source")
      : (config?.source ? `$${config.source}` : "—");
    configExists.textContent = config?.exists
      ? t("agents.file_exists")
      : t("agents.file_missing");
    configExists.classList.toggle("is-present", Boolean(config?.exists));
    configModified.textContent = formatConfigurationTime(config?.modified_at);
    configDocs.href = config?.docs_url || "#";
    configDocs.hidden = !config?.docs_url;
    if (config?.error) {
      configState.textContent = config.error;
      configState.classList.add("is-error");
    } else if (config?.exists) {
      configState.textContent = t("agents.file_loaded", {size: Number(config.size || 0)});
      configState.classList.remove("is-error");
    } else {
      configState.textContent = t("agents.file_not_created");
      configState.classList.remove("is-error");
    }
    configState.classList.remove("is-dirty");
    configSave.disabled = true;
  };

  const applyEnvironment = (environment, {saved = false} = {}) => {
    environmentList.replaceChildren();
    const variables = Array.isArray(environment?.variables) ? environment.variables : [];
    const inherited = Array.isArray(environment?.inherited) ? environment.inherited : [];
    for (const variable of variables) {
      addEnvironmentRow(variable);
    }
    inheritedEnvironmentList.replaceChildren();
    for (const variable of inherited) {
      const item = document.createElement("span");
      item.className = "harness-env-inherited-item";
      item.classList.toggle("is-empty", !variable.configured);
      item.classList.toggle("is-overridden", Boolean(variable.overridden));
      const name = document.createElement("code");
      name.textContent = variable.name || "";
      const state = document.createElement("small");
      state.textContent = variable.overridden
        ? t("agents.environment_overridden")
        : (variable.configured ? t("agents.environment_set") : t("agents.environment_empty"));
      item.append(name, state);
      inheritedEnvironmentList.appendChild(item);
    }
    inheritedEnvironment.hidden = inherited.length === 0;
    environmentPath.textContent = environment?.path || "—";
    environmentPath.title = environment?.path || "";
    initialEnvironment = JSON.stringify(environmentData());
    environmentState.textContent = saved
      ? environmentCountText(variables.length)
      : t("agents.environment_summary", {
        managed: variables.length,
        inherited: inherited.length,
      });
    environmentState.classList.remove("is-dirty", "is-error");
    environmentAdd.disabled = !supported;
    environmentSave.disabled = true;
    environmentEmpty.hidden = variables.length > 0;
  };

  const loadConfiguration = async () => {
    reloadButton.disabled = true;
    status.textContent = t("agents.configuration_loading");
    try {
      const payload = await requestConfiguration(
        `/api/nodes/${encodeURIComponent(node)}/agent-tools/${encodeURIComponent(agent)}/configuration`,
      );
      supported = Boolean(payload.supported);
      applyConfig(payload.config || {});
      applyEnvironment(payload.environment || {});
      status.textContent = supported
        ? (payload.checked_at
          ? t("agents.configuration_loaded_at", {
            node,
            time: formatConfigurationTime(payload.checked_at),
          })
          : t("agents.configuration_loaded", {node}))
        : (payload.error || t("agents.configuration_unsupported"));
      harnessConfiguration.classList.toggle("is-unsupported", !supported);
    } catch (error) {
      supported = false;
      configEditable = false;
      editor.disabled = true;
      environmentAdd.disabled = true;
      configSave.disabled = true;
      environmentSave.disabled = true;
      status.textContent = t("agents.configuration_failed", {
        message: error?.message || t("common.unknown"),
      });
      harnessConfiguration.classList.add("is-unsupported");
    } finally {
      reloadButton.disabled = false;
    }
  };

  editor.addEventListener("input", updateDirtyState);
  environmentAdd.addEventListener("click", () => addEnvironmentRow({}, {focus: true}));

  configSave.addEventListener("click", async () => {
    configSave.disabled = true;
    configState.textContent = t("agents.saving");
    try {
      const payload = await requestConfiguration(
        `/api/nodes/${encodeURIComponent(node)}/agent-tools/${encodeURIComponent(agent)}/configuration/file`,
        {method: "PUT", body: JSON.stringify({content: editor.value})},
      );
      supported = Boolean(payload.supported);
      applyConfig(payload.config || {});
      configState.textContent = t("agents.file_saved", {
        size: Number(payload.config?.size || 0),
      });
      window.dispatchEvent(new CustomEvent("staragent:agent-auth-finished", {detail: {node}}));
    } catch (error) {
      configState.textContent = t("agents.save_failed", {
        message: error?.message || t("common.unknown"),
      });
      configState.classList.add("is-error");
      configSave.disabled = !configIsDirty();
    }
  });

  environmentSave.addEventListener("click", async () => {
    let variables;
    try {
      variables = Object.fromEntries(environmentData({validate: true}));
    } catch (error) {
      environmentState.textContent = error.message;
      environmentState.classList.add("is-error");
      return;
    }
    environmentSave.disabled = true;
    environmentState.textContent = t("agents.saving");
    try {
      const payload = await requestConfiguration(
        `/api/nodes/${encodeURIComponent(node)}/agent-tools/${encodeURIComponent(agent)}/configuration/environment`,
        {method: "PUT", body: JSON.stringify({variables})},
      );
      supported = Boolean(payload.supported);
      applyEnvironment(payload.environment || {}, {saved: true});
      if (!configIsDirty()) {
        applyConfig(payload.config || {});
      }
      window.dispatchEvent(new CustomEvent("staragent:agent-auth-finished", {detail: {node}}));
    } catch (error) {
      environmentState.textContent = t("agents.save_failed", {
        message: error?.message || t("common.unknown"),
      });
      environmentState.classList.add("is-error");
      environmentSave.disabled = !environmentIsDirty();
    }
  });

  reloadButton.addEventListener("click", () => {
    if ((configIsDirty() || environmentIsDirty()) && !reloadArmed) {
      reloadArmed = true;
      reloadButton.classList.add("is-warning");
      reloadButton.querySelector("span").textContent = t("agents.reload_discard");
      window.clearTimeout(reloadTimer);
      reloadTimer = window.setTimeout(() => {
        reloadArmed = false;
        reloadButton.classList.remove("is-warning");
        reloadButton.querySelector("span").textContent = t("common.reload");
      }, 5000);
      return;
    }
    reloadArmed = false;
    reloadButton.classList.remove("is-warning");
    reloadButton.querySelector("span").textContent = t("common.reload");
    loadConfiguration();
  });

  window.StarAgentAfterPaint(loadConfiguration);
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
