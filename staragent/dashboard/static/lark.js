const t = (key, values = {}) => window.StarAgentI18n?.t(key, values) || key;
const larkState = (value) => t(`lark.state.${value || "stopped"}`);

const larkStatus = document.querySelector(".lark-action-status");
const larkSaveStatus = document.querySelector(".lark-save-status");
const larkTestResult = document.querySelector(".lark-test-result");
const form = document.querySelector(".lark-config-form");
const workerValue = document.querySelector(".lark-worker-value");
const configValue = document.querySelector(".lark-config-value");
const configCount = document.querySelector(".lark-config-count");
const sdkValue = document.querySelector(".lark-sdk-value");
const runtimeShort = document.querySelector(".lark-runtime-short");
const statusLabel = document.querySelector(".lark-status-label");
const workerPill = document.querySelector(".lark-worker-pill");
const runtimePill = document.querySelector(".lark-runtime-pill");
const sdkPill = document.querySelector(".lark-sdk-pill");
const runtimePath = document.querySelector(".lark-runtime-path");
const configSummary = document.querySelector(".lark-config-summary");
const configList = document.querySelector(".lark-config-list");
const missingMessage = document.querySelector(".lark-missing-message");
const output = document.querySelector(".lark-output");
const configPath = document.querySelector(".lark-config-path");
const workerSessionLink = document.querySelector(".lark-session-link");

const setPill = (el, ok, text, optional = false) => {
  if (!el) return;
  el.className = `pill node-status-${ok ? "connected" : (optional ? "optional" : "disconnected")}`;
  el.textContent = text;
};

const secretPlaceholder = (present, emptyText) => present ? t("lark.saved_placeholder") : emptyText;

const renderWorkerOutput = (text) => {
  if (!output) return;
  const next = text || t("lark.no_output");
  const atBottom = output.scrollTop + output.clientHeight >= output.scrollHeight - 16;
  if (output.textContent !== next) {
    output.textContent = next;
    if (atBottom) {
      output.scrollTop = output.scrollHeight;
    }
  }
};

const syncForm = (body) => {
  if (!form || !body.form) return;
  form.elements.app_id.value = body.form.app_id || "";
  form.elements.allowed_chats.value = body.form.allowed_chats || "";
  form.elements.allowed_users.value = body.form.allowed_users || "";
  form.elements.dashboard_url.value = body.form.dashboard_url || "";
  form.elements.allow_all.checked = !!body.form.allow_all;
  const secrets = body.form.secrets || {};
  form.elements.app_secret.value = "";
  form.elements.verification_token.value = "";
  form.elements.encrypt_key.value = "";
  form.elements.node_token.value = "";
  form.elements.app_secret.placeholder = secretPlaceholder(!!secrets.app_secret, t("common.required"));
  form.elements.verification_token.placeholder = secretPlaceholder(!!secrets.verification_token, t("common.optional"));
  form.elements.encrypt_key.placeholder = secretPlaceholder(!!secrets.encrypt_key, t("common.optional"));
  form.elements.node_token.placeholder = secretPlaceholder(!!secrets.node_token, t("lark.optional_remote"));
};

const renderConfig = (config, worker = {}) => {
  configList.innerHTML = "";
  for (const item of config.items || []) {
    const row = document.createElement("div");
    row.className = "lark-config-row";
    row.dataset.configName = item.name;
    const info = document.createElement("div");
    const label = document.createElement("strong");
    label.textContent = item.label || item.name;
    const value = document.createElement("span");
    const source = item.source ? ` · ${item.source}` : "";
    value.textContent = `${item.value || (item.required ? t("lark.required") : t("lark.optional"))}${source}`;
    info.append(label, value);
    const pill = document.createElement("span");
    setPill(pill, item.present, item.present ? t("lark.set") : (item.required ? t("lark.missing") : t("lark.optional")), !item.required);
    row.append(info, pill);
    configList.appendChild(row);
  }
  const missing = config.missing_required || [];
  configSummary.textContent = missing.length ? t("lark.missing_values") : t("lark.ready");
  configValue.textContent = missing.length ? t("lark.missing") : t("lark.ready");
  configCount.textContent = t("lark.blocking", {count: missing.length});
  missingMessage.textContent = missing.length
    ? t("lark.missing_list", {names: missing.join(", ")})
    : (worker.running ? t("lark.worker_running") : t("lark.ready_to_start"));
  if (config.path) {
    configPath.textContent = config.path;
  }
};

const renderLark = (body, options = {}) => {
  const worker = body.worker || {};
  const sdk = body.sdk || {};
  const config = body.config || {};
  workerValue.textContent = larkState(worker.status);
  statusLabel.textContent = larkState(worker.status);
  setPill(workerPill, !!worker.running, larkState(worker.status));
  if (workerSessionLink && worker.session_url) {
    workerSessionLink.href = worker.session_url;
  }
  runtimePath.textContent = sdk.venv_executable || "-";
  setPill(runtimePill, !!sdk.venv_ready, sdk.venv_ready ? t("lark.ready") : t("lark.missing"));
  setPill(sdkPill, !!sdk.installed, sdk.installed ? t("common.installed") : t("lark.missing"));
  sdkValue.textContent = sdk.venv_ready ? t("lark.ready") : t("lark.missing");
  runtimeShort.textContent = sdk.installed ? t("lark.sdk_installed") : t("lark.sdk_missing");
  renderWorkerOutput(worker.recent_output);
  renderConfig(config, worker);
  if (options.syncForm) {
    syncForm(body);
  }
};

const fetchLark = async (options = {}) => {
  const response = await fetch("/api/lark/status");
  if (!response.ok) throw new Error(t("lark.status_failed"));
  const body = await response.json();
  renderLark(body, options);
  return body;
};

const larkAction = async (path, pending) => {
  larkStatus.textContent = pending;
  const response = await fetch(path, {method: "POST"});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    larkStatus.textContent = body.detail || t("lark.action_failed");
    await fetchLark().catch(() => {});
    return;
  }
  larkStatus.textContent = body.status ? larkState(body.status) : t("lark.done");
  renderLark(body.lark || await fetchLark());
};

const renderConnectionTest = (body) => {
  if (!larkTestResult) return;
  larkTestResult.hidden = false;
  larkTestResult.className = `lark-test-result is-${body.status || (body.ok ? "passed" : "failed")}`;
  larkTestResult.replaceChildren();

  const header = document.createElement("div");
  header.className = "lark-test-header";
  const title = document.createElement("strong");
  title.textContent = body.ok ? t("lark.test_passed") : t("lark.test_failed");
  const meta = document.createElement("span");
  meta.textContent = body.base_url ? body.base_url : (body.checked_at || "");
  header.append(title, meta);
  larkTestResult.appendChild(header);

  const steps = document.createElement("div");
  steps.className = "lark-test-steps";
  for (const item of body.steps || []) {
    const row = document.createElement("div");
    row.className = `lark-test-step is-${item.status || (item.ok ? "passed" : "failed")}`;
    const text = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = item.name || t("lark.check");
    const detail = document.createElement("span");
    detail.textContent = item.detail || "";
    text.append(name, detail);
    const pill = document.createElement("span");
    pill.className = `pill lark-test-pill is-${item.status || (item.ok ? "passed" : "failed")}`;
    pill.textContent = larkState(item.status || (item.ok ? "passed" : "failed"));
    row.append(text, pill);
    steps.appendChild(row);
  }
  larkTestResult.appendChild(steps);

  if (body.bot && Object.keys(body.bot).length) {
    const bot = document.createElement("p");
    bot.className = "form-hint lark-test-bot";
    const name = body.bot.name || body.bot.app_name || "Bot";
    const openId = body.bot.open_id ? ` · open_id ${body.bot.open_id}` : "";
    bot.textContent = `${name}${openId}`;
    larkTestResult.appendChild(bot);
  }
};

const testConnection = async () => {
  larkStatus.textContent = t("lark.testing");
  const response = await fetch("/api/lark/test", {method: "POST"});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    larkStatus.textContent = body.detail || t("lark.test_failed_done");
    return;
  }
  renderConnectionTest(body);
  larkStatus.textContent = body.ok ? t("lark.test_passed_done") : t("lark.test_failed_done");
};

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  larkSaveStatus.textContent = t("lark.saving");
  const payload = {
    app_id: form.elements.app_id.value,
    app_secret: form.elements.app_secret.value,
    allowed_chats: form.elements.allowed_chats.value,
    allowed_users: form.elements.allowed_users.value,
    allow_all: form.elements.allow_all.checked,
    verification_token: form.elements.verification_token.value,
    encrypt_key: form.elements.encrypt_key.value,
    dashboard_url: form.elements.dashboard_url.value,
    node_token: form.elements.node_token.value,
  };
  const response = await fetch("/api/lark/config", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    larkSaveStatus.textContent = body.detail || t("lark.save_failed");
    return;
  }
  renderLark(body.lark || await fetchLark(), {syncForm: true});
  const running = !!(body.lark && body.lark.worker && body.lark.worker.running);
  larkSaveStatus.textContent = running ? t("lark.saved_restart") : t("lark.saved");
});

document.querySelector(".lark-clear-config")?.addEventListener("click", async () => {
  if (!window.confirm(t("lark.clear_confirm"))) {
    return;
  }
  larkSaveStatus.textContent = t("lark.clearing");
  const response = await fetch("/api/lark/config", {method: "DELETE"});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    larkSaveStatus.textContent = body.detail || t("lark.clear_failed");
    return;
  }
  renderLark(body.lark || await fetchLark(), {syncForm: true});
  larkSaveStatus.textContent = t("lark.cleared");
});

document.querySelector(".lark-refresh")?.addEventListener("click", () => {
  larkStatus.textContent = t("lark.refreshing");
  fetchLark({syncForm: true}).then(() => {
    larkStatus.textContent = t("lark.refreshed");
  }).catch((error) => {
    larkStatus.textContent = error.message;
  });
});
document.querySelector(".lark-test")?.addEventListener("click", () => {
  testConnection().catch((error) => {
    larkStatus.textContent = error.message;
  });
});
document.querySelector(".lark-start")?.addEventListener("click", () => larkAction("/api/lark/start", t("lark.starting")));
document.querySelector(".lark-stop")?.addEventListener("click", () => larkAction("/api/lark/stop", t("lark.stopping")));

output.scrollTop = output.scrollHeight;
setInterval(() => {
  if (!document.hidden) {
    fetchLark().catch(() => {});
  }
}, 3000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    fetchLark().catch(() => {});
  }
});

for (const button of document.querySelectorAll(".copy-button")) {
  button.addEventListener("click", async () => {
    const text = button.dataset.copy || "";
    const originalLabel = button.textContent;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    button.textContent = t("nodes.copied");
    setTimeout(() => { button.textContent = originalLabel; }, 1200);
  });
}
