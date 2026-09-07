(() => {
  const root = document.querySelector("[data-node-dependencies]");
  if (!root) {
    return;
  }

  const t = (key, values = {}) => window.StarAgentI18n?.t(key, values) || key;
  const node = root.dataset.node || "local";
  const nodeAvailable = root.dataset.nodeAvailable === "true";
  const summary = root.querySelector("[data-dependency-summary]");
  const list = root.querySelector("[data-dependency-list]");
  const refreshButton = root.querySelector(".node-dependencies-refresh");
  const dialog = root.querySelector(".dependency-install-dialog");
  let installing = false;

  const stateFor = (dependency) => {
    if (dependency.status === "available" || dependency.installed) {
      return {label: t("common.installed"), style: "connected"};
    }
    if (dependency.status === "error") {
      return {label: t("dependencies.attention"), style: "error"};
    }
    if (dependency.status === "missing") {
      return dependency.required
        ? {label: t("common.missing"), style: "disconnected"}
        : {label: t("common.optional"), style: "optional"};
    }
    return {label: t("common.unknown"), style: "optional"};
  };

  const dependencyNote = (dependency) => {
    const key = `dependencies.${dependency.name}.note`;
    const translated = t(key);
    return translated === key ? (dependency.note || "") : translated;
  };

  const iconFor = (name) => {
    const icon = document.createElement("span");
    icon.className = `node-dependency-icon is-${name}`;
    icon.setAttribute("aria-hidden", "true");
    if (name === "tailscale") {
      icon.innerHTML = '<svg viewBox="0 0 24 24"><circle cx="7" cy="7" r="2"></circle><circle cx="12" cy="7" r="2"></circle><circle cx="17" cy="7" r="2"></circle><circle cx="7" cy="12" r="2"></circle><circle cx="12" cy="12" r="2"></circle><circle cx="17" cy="12" r="2"></circle><circle cx="7" cy="17" r="2"></circle><circle cx="12" cy="17" r="2"></circle><circle cx="17" cy="17" r="2"></circle></svg>';
    } else if (name === "nodejs") {
      icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="m12 2.8 8 4.6v9.2l-8 4.6-8-4.6V7.4l8-4.6Z"></path><path d="M9 9.2v5.6l3 1.7 3-1.7V12"></path></svg>';
    } else {
      icon.innerHTML = '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="3"></rect><path d="m7 9 3 3-3 3M12 15h5"></path></svg>';
    }
    return icon;
  };

  const copyText = async (button, value) => {
    const original = button.textContent;
    try {
      if (navigator.clipboard?.writeText) {
        try {
          await navigator.clipboard.writeText(value);
          button.textContent = t("nodes.copied");
          window.setTimeout(() => {
            button.textContent = original;
          }, 1200);
          return;
        } catch (_error) {
          // Fall back for non-secure dashboard origins.
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
        throw new Error("copy failed");
      }
      button.textContent = t("nodes.copied");
    } catch (_error) {
      button.textContent = t("dependencies.copy_failed");
    }
    window.setTimeout(() => {
      button.textContent = original;
    }, 1200);
  };

  const sourceLabel = (option) => {
    if (option.source === "mirror") {
      return t("dependencies.china_mirror");
    }
    if (option.source === "official") {
      return t("dependencies.official_source");
    }
    return t("dependencies.system_source");
  };

  const methodLabel = (method) => method === "install_script"
    ? t("dependencies.install_script")
    : t("dependencies.package_manager");

  const confirmInstall = (dependency, option) => {
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(window.confirm(t("dependencies.confirm", {
        dependency: dependency.label,
        node,
        source: option.provider,
      })));
    }
    dialog.querySelector("#dependency-install-title").textContent = t(
      "dependencies.install_named",
      {dependency: dependency.label},
    );
    dialog.querySelector("[data-dependency-dialog-node]").textContent = node;
    dialog.querySelector("[data-dependency-dialog-source]").textContent = [
      option.provider,
      methodLabel(option.method),
    ].filter(Boolean).join(" · ");
    dialog.querySelector("[data-dependency-dialog-command]").textContent = option.command || "—";
    dialog.querySelector("[data-dependency-dialog-note]").textContent = option.elevated
      ? t("dependencies.admin_note")
      : t("dependencies.install_safety");
    const dialogIcon = dialog.querySelector(".dependency-dialog-icon");
    dialogIcon.className = `dependency-dialog-icon is-${dependency.name}`;
    dialogIcon.replaceChildren(iconFor(dependency.name));
    dialog.returnValue = "cancel";
    return new Promise((resolve) => {
      dialog.addEventListener(
        "close",
        () => resolve(dialog.returnValue === "confirm"),
        {once: true},
      );
      dialog.showModal();
      requestAnimationFrame(() => {
        dialog.querySelector(".agent-update-dialog-cancel")?.focus({preventScroll: true});
      });
    });
  };

  const renderResources = (dependency) => {
    const resources = document.createElement("div");
    resources.className = "node-dependency-resources";
    const label = document.createElement("span");
    label.textContent = t("dependencies.downloads");
    resources.appendChild(label);
    for (const resource of Array.isArray(dependency.resources) ? dependency.resources : []) {
      if (!resource.url) {
        continue;
      }
      const link = document.createElement("a");
      link.href = resource.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = resource.china
        ? t("dependencies.mirror_download", {provider: resource.label})
        : t("dependencies.official_download");
      if (resource.china) {
        link.classList.add("is-china");
      }
      resources.appendChild(link);
    }
    return resources;
  };

  const renderDependencyInstaller = (dependency, options, payload, card) => {
    const panel = document.createElement("section");
    panel.className = "launcher-install is-compact";
    const list = document.createElement("div");
    list.className = "launcher-source-list";
    const choices = [];

    for (const option of options) {
      const choice = document.createElement("button");
      choice.type = "button";
      choice.className = "launcher-source-choice";
      choice.setAttribute("aria-pressed", "false");
      choice.classList.toggle("is-unavailable", !option.available);
      const radio = document.createElement("span");
      radio.className = "launcher-source-radio";
      radio.setAttribute("aria-hidden", "true");
      const information = document.createElement("span");
      information.className = "launcher-source-copy";
      const source = document.createElement("span");
      source.className = "launcher-source-title";
      const provider = document.createElement("strong");
      provider.textContent = option.provider || sourceLabel(option);
      const badge = document.createElement("span");
      badge.className = option.source === "mirror"
        ? "agent-install-badge is-china"
        : "agent-install-badge";
      badge.textContent = sourceLabel(option);
      source.append(provider, badge);
      if (option.recommended) {
        const recommended = document.createElement("span");
        recommended.className = "agent-install-badge is-recommended";
        recommended.textContent = t("dependencies.recommended");
        source.appendChild(recommended);
      }
      if (option.elevated) {
        const elevated = document.createElement("span");
        elevated.className = "agent-install-badge node-dependency-admin-badge";
        elevated.textContent = t("dependencies.admin");
        source.appendChild(elevated);
      }
      const method = document.createElement("span");
      method.className = "launcher-source-method";
      method.textContent = option.available
        ? methodLabel(option.method)
        : t("dependencies.missing_tools", {
          tools: (option.missing_requirements || []).join(", "),
        });
      information.append(source, method);
      const arrow = document.createElement("span");
      arrow.className = "launcher-source-arrow";
      arrow.setAttribute("aria-hidden", "true");
      arrow.textContent = "›";
      choice.append(radio, information, arrow);
      list.appendChild(choice);
      choices.push({button: choice, option});
    }
    panel.appendChild(list);

    const selection = document.createElement("div");
    selection.className = "launcher-install-selection";
    const selectionCopy = document.createElement("div");
    selectionCopy.className = "launcher-install-selection-copy";
    const selectionLabel = document.createElement("span");
    selectionLabel.textContent = t("dependencies.selected_source");
    const selectionName = document.createElement("strong");
    const selectionHint = document.createElement("small");
    const sourceLink = document.createElement("a");
    sourceLink.target = "_blank";
    sourceLink.rel = "noopener noreferrer";
    sourceLink.textContent = t("dependencies.open_source");
    selectionCopy.append(selectionLabel, selectionName, selectionHint, sourceLink);

    const details = document.createElement("details");
    details.className = "launcher-command-details";
    const detailsSummary = document.createElement("summary");
    detailsSummary.textContent = t("dependencies.command_details");
    const command = document.createElement("code");
    details.append(detailsSummary, command);

    const controls = document.createElement("div");
    controls.className = "launcher-install-actions";
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "copy-button inline-copy launcher-install-copy";
    copy.textContent = t("dependencies.copy_command");
    const install = document.createElement("button");
    install.type = "button";
    install.className = "agent-cli-update-button agent-install-button node-dependency-install-button launcher-install-primary";
    install.textContent = t("dependencies.install_now");
    controls.append(copy, install);
    selection.append(selectionCopy, details, controls);
    panel.appendChild(selection);

    let selectedOption = null;
    const choose = (option) => {
      selectedOption = option;
      choices.forEach(({button, option: candidate}) => {
        const active = candidate.id === option.id;
        button.classList.toggle("is-selected", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
      });
      const enabled = Boolean(payload.installs_supported && !payload.stale && option.available);
      selectionName.textContent = [option.provider, methodLabel(option.method)]
        .filter(Boolean)
        .join(" · ");
      selectionHint.textContent = enabled
        ? t("dependencies.install_ready")
        : t("dependencies.copy_fallback");
      selectionHint.classList.toggle("is-warning", !enabled);
      sourceLink.hidden = !option.source_url;
      sourceLink.href = option.source_url || "#";
      details.hidden = !option.command;
      command.textContent = option.command || "";
      copy.hidden = !option.command;
      copy.onclick = () => copyText(copy, option.command || "");
      install.dataset.enabled = enabled ? "true" : "false";
      install.disabled = !enabled || installing;
      install.title = !option.available
        ? t("dependencies.missing_tools", {
          tools: (option.missing_requirements || []).join(", "),
        })
        : (!payload.installs_supported || payload.stale)
          ? t("dependencies.node_update_required")
          : "";
    };
    choices.forEach(({button, option}) => {
      button.addEventListener("click", () => choose(option));
    });
    install.addEventListener("click", () => {
      if (selectedOption) {
        installDependency(dependency, selectedOption, card);
      }
    });
    choose(
      options.find((option) => option.recommended && option.available)
        || options.find((option) => option.available)
        || options.find((option) => option.recommended)
        || options[0],
    );
    return panel;
  };

  const renderDependency = (dependency, payload) => {
    const card = document.createElement("article");
    const state = stateFor(dependency);
    card.className = `node-dependency-card is-${dependency.status || "unknown"}`;
    card.dataset.dependency = dependency.name || "";

    const head = document.createElement("header");
    const identity = document.createElement("div");
    identity.className = "node-dependency-identity";
    identity.appendChild(iconFor(dependency.name));
    const title = document.createElement("div");
    const eyebrow = document.createElement("span");
    eyebrow.textContent = dependency.required
      ? t("dependencies.required_by_staragent")
      : t("dependencies.optional_integration");
    const name = document.createElement("strong");
    name.textContent = dependency.label || dependency.name;
    title.append(eyebrow, name);
    identity.appendChild(title);
    const pill = document.createElement("span");
    pill.className = `pill node-status-${state.style}`;
    pill.textContent = state.label;
    head.append(identity, pill);

    const body = document.createElement("div");
    body.className = "node-dependency-body";
    const note = document.createElement("p");
    note.textContent = dependencyNote(dependency);
    body.appendChild(note);
    if (dependency.version) {
      const version = document.createElement("code");
      version.className = "node-dependency-version";
      version.textContent = dependency.version;
      version.title = dependency.executable || "";
      body.appendChild(version);
    }
    if (dependency.error) {
      const error = document.createElement("p");
      error.className = "node-dependency-error";
      error.textContent = dependency.error;
      body.appendChild(error);
    }

    const maintenance = document.createElement("div");
    maintenance.className = "node-dependency-maintenance";
    maintenance.appendChild(renderResources(dependency));
    const options = Array.isArray(dependency.install_options)
      ? dependency.install_options
      : [];
    if (!dependency.installed && options.length) {
      const heading = document.createElement("div");
      heading.className = "node-dependency-install-heading";
      const installTitle = document.createElement("strong");
      installTitle.textContent = t("dependencies.install_options");
      const hint = document.createElement("span");
      hint.textContent = t("dependencies.install_hint");
      heading.append(installTitle, hint);
      maintenance.appendChild(heading);
      maintenance.appendChild(renderDependencyInstaller(dependency, options, payload, card));
    } else if (!dependency.installed) {
      const manual = document.createElement("p");
      manual.className = "node-dependency-manual";
      manual.textContent = payload.supported
        ? t("dependencies.manual_install")
        : t("dependencies.node_update_required");
      maintenance.appendChild(manual);
    }
    const result = document.createElement("p");
    result.className = "node-dependency-result";
    result.hidden = true;
    maintenance.appendChild(result);
    card.append(head, body, maintenance);
    return card;
  };

  const render = (payload) => {
    const dependencies = Array.isArray(payload.dependencies) ? payload.dependencies : [];
    list.replaceChildren(...dependencies.map((item) => renderDependency(item, payload)));
    list.setAttribute("aria-busy", "false");
    if (payload.error && !payload.supported) {
      summary.textContent = payload.error;
      root.dataset.state = "error";
      return;
    }
    const missingRequired = dependencies.filter((item) => item.required && !item.installed);
    const ready = dependencies.filter((item) => item.installed).length;
    root.dataset.state = missingRequired.length ? "attention" : "ready";
    summary.textContent = missingRequired.length
      ? t("dependencies.required_missing", {
        names: missingRequired.map((item) => item.label).join(", "),
      })
      : t("dependencies.ready_summary", {ready, total: dependencies.length});
  };

  const load = async (refresh = false) => {
    refreshButton.disabled = true;
    list.setAttribute("aria-busy", "true");
    summary.textContent = t("dependencies.checking");
    try {
      const query = refresh ? "?refresh=true" : "";
      const response = await fetch(
        `/api/nodes/${encodeURIComponent(node)}/dependencies${query}`,
        {cache: "no-store"},
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || t("dependencies.check_failed"));
      }
      render(body);
      return body;
    } catch (error) {
      root.dataset.state = "error";
      summary.textContent = error?.message || t("dependencies.check_failed");
      list.setAttribute("aria-busy", "false");
      return null;
    } finally {
      refreshButton.disabled = installing;
    }
  };

  async function installDependency(dependency, option, card) {
    if (installing || !await confirmInstall(dependency, option)) {
      return;
    }
    installing = true;
    root.classList.add("is-installing");
    root.querySelectorAll(".node-dependency-install-button").forEach((button) => {
      button.disabled = true;
    });
    root.querySelectorAll(".launcher-source-choice, .launcher-install-copy").forEach((button) => {
      button.disabled = true;
    });
    refreshButton.disabled = true;
    const primary = card.querySelector(".node-dependency-install-button");
    if (primary) {
      primary.textContent = t("agents.installing");
    }
    const result = card.querySelector(".node-dependency-result");
    result.hidden = false;
    result.classList.remove("is-error");
    result.textContent = t("dependencies.installing", {
      dependency: dependency.label,
      node,
    });
    try {
      const response = await fetch(
        `/api/nodes/${encodeURIComponent(node)}/dependencies/${encodeURIComponent(dependency.name)}/install/${encodeURIComponent(option.id)}`,
        {method: "POST", cache: "no-store"},
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.ok) {
        throw new Error(body.detail || body.error || body.output || t("dependencies.install_failed"));
      }
      await load(true);
      summary.textContent = body.changed
        ? t("dependencies.installed", {dependency: dependency.label})
        : t("dependencies.already_installed", {dependency: dependency.label});
    } catch (error) {
      result.classList.add("is-error");
      result.textContent = error?.message || t("dependencies.install_failed");
      root.dataset.state = "error";
      summary.textContent = t("dependencies.install_failed_named", {
        dependency: dependency.label,
      });
    } finally {
      installing = false;
      root.classList.remove("is-installing");
      refreshButton.disabled = false;
      root.querySelectorAll(".node-dependency-install-button").forEach((button) => {
        button.disabled = button.dataset.enabled !== "true";
        button.textContent = t("dependencies.install_now");
      });
      root.querySelectorAll(".launcher-source-choice, .launcher-install-copy").forEach((button) => {
        button.disabled = false;
      });
    }
  }

  refreshButton.addEventListener("click", () => load(true));
  if (!nodeAvailable) {
    root.dataset.state = "error";
    summary.textContent = t("dependencies.node_unavailable", {node});
    list.setAttribute("aria-busy", "false");
    refreshButton.disabled = true;
    return;
  }
  window.StarAgentAfterPaint(() => load(false));
})();
