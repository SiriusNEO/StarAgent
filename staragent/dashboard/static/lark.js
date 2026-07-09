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

const secretPlaceholder = (present, emptyText) => present ? "Saved; leave blank to keep" : emptyText;

const renderWorkerOutput = (text) => {
  if (!output) return;
  const next = text || "No Lark worker output.";
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
  form.elements.app_secret.placeholder = secretPlaceholder(!!secrets.app_secret, "Required");
  form.elements.verification_token.placeholder = secretPlaceholder(!!secrets.verification_token, "Optional");
  form.elements.encrypt_key.placeholder = secretPlaceholder(!!secrets.encrypt_key, "Optional");
  form.elements.node_token.placeholder = secretPlaceholder(!!secrets.node_token, "Optional for remote nodes");
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
    value.textContent = `${item.value || (item.required ? "required" : "optional")}${source}`;
    info.append(label, value);
    const pill = document.createElement("span");
    setPill(pill, item.present, item.present ? "set" : (item.required ? "missing" : "optional"), !item.required);
    row.append(info, pill);
    configList.appendChild(row);
  }
  const missing = config.missing_required || [];
  configSummary.textContent = missing.length ? "missing values" : "ready";
  configValue.textContent = missing.length ? "missing" : "ready";
  configCount.textContent = `${missing.length} blocking`;
  missingMessage.textContent = missing.length
    ? `Missing: ${missing.join(", ")}`
    : (worker.running ? "Lark worker is running." : "Ready to start the Lark worker.");
  if (config.path) {
    configPath.textContent = config.path;
  }
};

const renderLark = (body, options = {}) => {
  const worker = body.worker || {};
  const sdk = body.sdk || {};
  const config = body.config || {};
  workerValue.textContent = worker.status || "unknown";
  statusLabel.textContent = worker.status || "unknown";
  setPill(workerPill, !!worker.running, worker.status || "stopped");
  if (workerSessionLink && worker.session_url) {
    workerSessionLink.href = worker.session_url;
  }
  runtimePath.textContent = sdk.venv_executable || "-";
  setPill(runtimePill, !!sdk.venv_ready, sdk.venv_ready ? "ready" : "missing");
  setPill(sdkPill, !!sdk.installed, sdk.installed ? "installed" : "missing");
  sdkValue.textContent = sdk.venv_ready ? "ready" : "missing";
  runtimeShort.textContent = sdk.installed ? "SDK installed" : "SDK missing";
  renderWorkerOutput(worker.recent_output);
  renderConfig(config, worker);
  if (options.syncForm) {
    syncForm(body);
  }
};

const fetchLark = async (options = {}) => {
  const response = await fetch("/api/lark/status");
  if (!response.ok) throw new Error("Status request failed.");
  const body = await response.json();
  renderLark(body, options);
  return body;
};

const larkAction = async (path, pending) => {
  larkStatus.textContent = pending;
  const response = await fetch(path, {method: "POST"});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    larkStatus.textContent = body.detail || "Action failed.";
    await fetchLark().catch(() => {});
    return;
  }
  larkStatus.textContent = body.status || "Done.";
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
  title.textContent = body.ok ? "Connection test passed" : "Connection test failed";
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
    name.textContent = item.name || "Check";
    const detail = document.createElement("span");
    detail.textContent = item.detail || "";
    text.append(name, detail);
    const pill = document.createElement("span");
    pill.className = `pill lark-test-pill is-${item.status || (item.ok ? "passed" : "failed")}`;
    pill.textContent = item.status || (item.ok ? "passed" : "failed");
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
  larkStatus.textContent = "Testing connection...";
  const response = await fetch("/api/lark/test", {method: "POST"});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    larkStatus.textContent = body.detail || "Connection test failed.";
    return;
  }
  renderConnectionTest(body);
  larkStatus.textContent = body.ok ? "Connection test passed." : "Connection test failed.";
};

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  larkSaveStatus.textContent = "Saving...";
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
    larkSaveStatus.textContent = body.detail || "Save failed.";
    return;
  }
  renderLark(body.lark || await fetchLark(), {syncForm: true});
  const running = !!(body.lark && body.lark.worker && body.lark.worker.running);
  larkSaveStatus.textContent = running ? "Saved. Restart worker to apply." : "Saved.";
});

document.querySelector(".lark-clear-config")?.addEventListener("click", async () => {
  if (!window.confirm("Clear saved Lark config from this dashboard?")) {
    return;
  }
  larkSaveStatus.textContent = "Clearing...";
  const response = await fetch("/api/lark/config", {method: "DELETE"});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    larkSaveStatus.textContent = body.detail || "Clear failed.";
    return;
  }
  renderLark(body.lark || await fetchLark(), {syncForm: true});
  larkSaveStatus.textContent = "Cleared.";
});

document.querySelector(".lark-refresh")?.addEventListener("click", () => {
  larkStatus.textContent = "Refreshing...";
  fetchLark({syncForm: true}).then(() => {
    larkStatus.textContent = "Refreshed.";
  }).catch((error) => {
    larkStatus.textContent = error.message;
  });
});
document.querySelector(".lark-test")?.addEventListener("click", () => {
  testConnection().catch((error) => {
    larkStatus.textContent = error.message;
  });
});
document.querySelector(".lark-start")?.addEventListener("click", () => larkAction("/api/lark/start", "Starting..."));
document.querySelector(".lark-stop")?.addEventListener("click", () => larkAction("/api/lark/stop", "Stopping..."));

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
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = "Copy"; }, 1200);
  });
}
