(() => {
  const t = (key, values = {}) => window.StarAgentI18n?.t(key, values) || key;

  const languageOptions = Array.from(document.querySelectorAll("[data-language]"));
  const languageStatus = document.querySelector(".settings-language-status");
  const setLanguageStatus = (message, isError = false) => {
    if (!languageStatus) {
      return;
    }
    languageStatus.textContent = message;
    languageStatus.classList.toggle("is-error", isError);
  };

  for (const option of languageOptions) {
    option.addEventListener("click", async () => {
      const language = option.dataset.language;
      if (!language || language === window.StarAgentI18n?.language) {
        return;
      }
      for (const candidate of languageOptions) {
        candidate.disabled = true;
      }
      setLanguageStatus(t("settings.language.saving"));
      try {
        const response = await fetch("/api/settings/language", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({language}),
        });
        if (!response.ok) {
          throw new Error("language preference request failed");
        }
        setLanguageStatus(t("settings.language.saved"));
        window.location.reload();
      } catch {
        for (const candidate of languageOptions) {
          candidate.disabled = false;
        }
        setLanguageStatus(t("settings.language.failed"), true);
      }
    });
  }

  const updateRoot = document.querySelector("[data-staragent-update]");
  if (!updateRoot) {
    return;
  }

  const checkPath = updateRoot.dataset.updateCheckUrl || "/api/settings/update/check";
  const applyPath = updateRoot.dataset.updateApplyUrl || "/api/settings/update/apply";
  const restartPath = updateRoot.dataset.updateRestartUrl || "/api/settings/update";
  const updateSupported = updateRoot.dataset.updateSupported !== "false";
  const nodeAvailable = updateRoot.dataset.updateAvailable !== "false";
  const layout = updateRoot.querySelector(".settings-update-layout");
  const stateLabel = updateRoot.querySelector("[data-update-state-label]");
  const message = updateRoot.querySelector("[data-update-message]");
  const channel = updateRoot.querySelector("[data-update-channel]");
  const current = updateRoot.querySelector("[data-update-current]");
  const latest = updateRoot.querySelector("[data-update-latest]");
  const tree = updateRoot.querySelector("[data-update-tree]");
  const checkButton = updateRoot.querySelector("[data-update-check]");
  const installButton = updateRoot.querySelector("[data-update-install]");
  const updateDialog = updateRoot.querySelector(".settings-update-dialog");
  let update = updateSupported ? null : {
    status: nodeAvailable ? "legacy" : "unavailable",
    reason: nodeAvailable ? "node_update_unsupported" : "node_unavailable",
    current_version: updateRoot.dataset.updateCurrentVersion || "",
    current_short_commit: updateRoot.dataset.updateCurrentCommit || "",
    branch: updateRoot.dataset.updateBranch || "",
    channel: updateRoot.dataset.updateChannel || "",
    can_update: false,
  };
  let busy = false;

  const reasonKeys = {
    ahead: "settings.update.reason.ahead",
    branch_changed: "settings.update.reason.branch_changed",
    detached_head: "settings.update.reason.detached_head",
    dirty_worktree: "settings.update.reason.dirty_worktree",
    diverged: "settings.update.reason.diverged",
    git_failed: "settings.update.reason.git_failed",
    git_missing: "settings.update.reason.git_missing",
    git_timeout: "settings.update.reason.git_timeout",
    invalid_git_response: "settings.update.reason.git_failed",
    invalid_remote_commit: "settings.update.reason.git_failed",
    missing_remote: "settings.update.reason.missing_remote",
    not_git_checkout: "settings.update.reason.not_git_checkout",
    node_unavailable: "settings.update.reason.node_unavailable",
    node_update_failed: "settings.update.reason.node_update_failed",
    node_update_unsupported: "settings.update.reason.node_update_unsupported",
    unofficial_remote: "settings.update.reason.unofficial_remote",
    unchecked: "settings.update.reason.unchecked",
    unsupported_branch: "settings.update.reason.unsupported_branch",
    update_blocked: "settings.update.reason.blocked",
    update_busy: "settings.update.reason.busy",
    update_failed: "settings.update.failed",
    update_verification_failed: "settings.update.reason.git_failed",
  };

  const reasonMessage = (reason, fallback = "") => {
    const key = reasonKeys[reason];
    return key ? t(key) : (fallback || t("settings.update.failed"));
  };

  const revisionLabel = (payload, prefix) => {
    const commit = payload?.[`${prefix}_short_commit`] || "";
    if (prefix === "current" && payload?.current_version) {
      return commit ? `v${payload.current_version} · ${commit}` : `v${payload.current_version}`;
    }
    return commit || "—";
  };

  const updateMessage = (payload) => {
    const branch = payload.branch || "—";
    if (payload.status === "legacy") {
      return reasonMessage("node_update_unsupported");
    }
    if (payload.status === "up_to_date") {
      return t("settings.update.current_detail", {branch});
    }
    if (payload.status === "update_available") {
      if (payload.blocked_reason) {
        return reasonMessage(payload.blocked_reason);
      }
      return t("settings.update.available_detail", {count: payload.behind || 1, branch});
    }
    if (payload.status === "ahead" || payload.status === "diverged") {
      return reasonMessage(payload.status);
    }
    if (payload.status === "unchecked") {
      return reasonMessage("unchecked");
    }
    return reasonMessage(payload.reason || payload.blocked_reason, payload.detail || "");
  };

  const stateText = (payload) => {
    if (payload.status === "legacy") {
      return t("settings.update.legacy");
    }
    if (payload.status === "up_to_date") {
      return t("settings.update.current");
    }
    if (payload.status === "update_available") {
      return payload.can_update
        ? t("settings.update.available")
        : t("settings.update.blocked");
    }
    if (payload.status === "ahead") {
      return t("settings.update.local_ahead");
    }
    if (payload.status === "checking") {
      return t("settings.update.checking");
    }
    return t("settings.update.unavailable");
  };

  const renderUpdate = (payload) => {
    update = payload;
    const state = payload.status || "unavailable";
    layout.dataset.updateState = state;
    layout.setAttribute("aria-busy", String(state === "checking" || state === "updating"));
    stateLabel.textContent = stateText(payload);
    message.textContent = updateMessage(payload);

    const channelName = payload.channel
      ? t(`settings.update.channel.${payload.channel}`)
      : t("settings.update.channel.unknown");
    channel.textContent = payload.branch ? `${channelName} · ${payload.branch}` : channelName;
    current.textContent = revisionLabel(payload, "current");
    current.title = payload.current_subject || payload.current_commit || "";
    latest.textContent = revisionLabel(payload, "latest");
    latest.title = payload.latest_subject || payload.latest_commit || "";
    const treeKnown = Boolean(payload.current_commit);
    tree.textContent = treeKnown
      ? (payload.dirty ? t("settings.update.tree.modified") : t("settings.update.tree.clean"))
      : "—";
    tree.classList.toggle("is-modified", treeKnown && Boolean(payload.dirty));

    const showInstall = state === "update_available";
    installButton.hidden = !showInstall;
    installButton.disabled = busy || !payload.can_update;
    checkButton.hidden = !updateSupported;
    checkButton.disabled = busy;
  };

  const setBusy = (phase) => {
    busy = Boolean(phase);
    if (phase) {
      layout.dataset.updateState = phase;
      layout.setAttribute("aria-busy", "true");
      stateLabel.textContent = phase === "updating"
        ? t("settings.update.updating")
        : t("settings.update.checking");
      message.textContent = phase === "updating"
        ? t("settings.update.updating_detail")
        : t("settings.update.checking_detail");
      checkButton.querySelector("span").textContent = t("settings.update.checking");
      checkButton.disabled = true;
      installButton.disabled = true;
      return;
    }
    checkButton.querySelector("span").textContent = t("settings.update.check_again");
    layout.setAttribute("aria-busy", "false");
    if (update) {
      renderUpdate(update);
    }
  };

  const requestUpdate = async (path, method = "POST") => {
    const response = await fetch(path, {method});
    const body = await response.json().catch(() => ({}));
    if (!response.ok || !body.ok) {
      const error = new Error(body.detail || t("settings.update.failed"));
      error.code = body.error || "";
      throw error;
    }
    return body;
  };

  const checkForUpdate = async () => {
    if (busy || !updateSupported) {
      return;
    }
    setBusy("checking");
    try {
      update = await requestUpdate(checkPath);
    } catch (error) {
      update = {
        ...(update || {}),
        status: "unavailable",
        reason: error.code || "git_failed",
        detail: error.message,
        can_update: false,
      };
    } finally {
      setBusy("");
    }
  };

  const confirmUpdate = () => {
    if (!updateDialog || typeof updateDialog.showModal !== "function") {
      return Promise.resolve(window.confirm(t("settings.update.confirm_title")));
    }
    updateDialog.querySelector("[data-update-dialog-current]").textContent = revisionLabel(update, "current");
    updateDialog.querySelector("[data-update-dialog-latest]").textContent = revisionLabel(update, "latest");
    updateDialog.returnValue = "cancel";
    return new Promise((resolve) => {
      updateDialog.addEventListener(
        "close",
        () => resolve(updateDialog.returnValue === "confirm"),
        {once: true},
      );
      updateDialog.showModal();
      requestAnimationFrame(() => {
        updateDialog.querySelector(".settings-update-dialog-cancel")?.focus({preventScroll: true});
      });
    });
  };

  const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
  const waitForServiceRestart = async () => {
    await delay(2500);
    for (let attempt = 0; attempt < 40; attempt += 1) {
      try {
        const separator = restartPath.includes("?") ? "&" : "?";
        const response = await fetch(`${restartPath}${separator}restart=${Date.now()}`, {
          cache: "no-store",
        });
        if (response.ok) {
          window.location.reload();
          return;
        }
      } catch {
        // The expected brief connection failure means the supervisor is restarting the Hub.
      }
      await delay(1000);
    }
    busy = false;
    layout.dataset.updateState = "unavailable";
    layout.setAttribute("aria-busy", "false");
    stateLabel.textContent = t("settings.update.restart_pending");
    message.textContent = t("settings.update.restart_slow");
  };

  const installUpdate = async () => {
    if (busy || !update?.can_update || !await confirmUpdate()) {
      return;
    }
    setBusy("updating");
    try {
      const result = await requestUpdate(applyPath);
      update = result.after || update;
      if (!result.updated) {
        setBusy("");
        return;
      }
      renderUpdate(update);
      layout.dataset.updateState = "restarting";
      layout.setAttribute("aria-busy", "true");
      stateLabel.textContent = result.restart_scheduled
        ? t("settings.update.restarting")
        : t("settings.update.restart_required");
      message.textContent = result.restart_scheduled
        ? t("settings.update.updated_restarting")
        : t("settings.update.updated_manual_restart");
      checkButton.disabled = true;
      installButton.hidden = true;
      if (result.restart_scheduled) {
        await waitForServiceRestart();
      }
    } catch (error) {
      update = {
        ...(update || {}),
        status: update?.status || "unavailable",
        blocked_reason: error.code || update?.blocked_reason || "",
        detail: error.message,
        can_update: false,
      };
      setBusy("");
    }
  };

  updateDialog?.addEventListener("click", (event) => {
    if (event.target === updateDialog) {
      updateDialog.close("cancel");
    }
  });
  checkButton.addEventListener("click", checkForUpdate);
  installButton.addEventListener("click", installUpdate);
  if (updateSupported) {
    window.StarAgentAfterPaint
      ? window.StarAgentAfterPaint(checkForUpdate, 600)
      : setTimeout(checkForUpdate, 0);
  } else {
    renderUpdate(update);
  }
})();
