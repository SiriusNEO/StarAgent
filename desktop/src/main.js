import {Channel, invoke} from "@tauri-apps/api/core";
import "./styles.css";

const DEFAULT_ENDPOINT = "http://127.0.0.1:8765";
const ENDPOINT_KEY = "staragent.desktop.hubEndpoint";
const LANGUAGE_KEY = "staragent.desktop.language";

const messages = {
  en: {
    desktopEdition: "Desktop",
    eyebrow: "Native agent workspace",
    title: "Open your agent workspace.",
    subtitle: "Launch StarAgent on this machine, or connect securely to an existing Hub.",
    localMode: "Local mode",
    localTitle: "This machine",
    localDescription: "Start the single-Node Launcher locally. Windows uses the bundled ConPTY runtime—no WSL required.",
    runtime: "Runtime",
    checking: "Checking local runtime…",
    strategy: "Strategy",
    runtimeMissing: "Local prerequisites are missing.",
    copyCommand: "Copy command",
    copied: "Copied",
    openLocal: "Open Local Launcher",
    startingLocal: "Starting Launcher…",
    hubMode: "Hub mode",
    hubTitle: "Existing Hub",
    hubDescription: "Use a Hub running on Tailscale, your LAN, or an HTTPS endpoint.",
    hubAddress: "Hub address",
    hubNote: "HTTP is appropriate only on a trusted private network.",
    connectHub: "Connect to Hub",
    connecting: "Opening Hub…",
    securityTitle: "Separated by design.",
    securityBody: "Hub pages run without desktop command permissions. Close the workspace window to choose another connection.",
    available: "Available",
    unavailable: "Missing",
    alreadyRunning: "Launcher is already running. Opening it now…",
    ready: "Launcher is ready. Opening the workspace…",
    invalidEndpoint: "Enter a valid HTTP or HTTPS Hub address.",
    runtimeCheckFailed: "Could not inspect the local runtime.",
    unknownError: "Something went wrong. Try again or open StarAgent from a terminal for details.",
    native: "Native",
    bundled: "Bundled native",
    sessionBackend: "Session backend",
    checkUpdates: "Check for desktop updates",
    checkingUpdates: "Checking for updates…",
    updateKicker: "Signed desktop update",
    updateAvailable: "A new version is ready",
    versionTransition: "StarAgent {current} → {next}",
    updateRestartWarning: "The app will restart. Save work in local Sessions before updating.",
    later: "Later",
    updateAndRestart: "Update & restart",
    preparingUpdate: "Preparing secure download…",
    downloadingUpdate: "Downloading update…",
    installingUpdate: "Verifying package and starting installer…",
    upToDate: "StarAgent Desktop is up to date.",
    updateCheckFailed: "Could not check for desktop updates.",
    updateInstallFailed: "The desktop update could not be installed.",
  },
  zh: {
    desktopEdition: "桌面版",
    eyebrow: "原生 Agent 工作区",
    title: "打开你的 Agent 工作区",
    subtitle: "在当前机器启动 StarAgent，或安全连接到已有 Hub。",
    localMode: "本机模式",
    localTitle: "当前机器",
    localDescription: "在本机启动单 Node Launcher；Windows 使用内置 ConPTY，无需 WSL。",
    runtime: "运行环境",
    checking: "正在检测本机运行环境…",
    strategy: "启动方式",
    runtimeMissing: "本机还缺少运行依赖。",
    copyCommand: "复制命令",
    copied: "已复制",
    openLocal: "打开本机 Launcher",
    startingLocal: "正在启动 Launcher…",
    hubMode: "Hub 模式",
    hubTitle: "已有 Hub",
    hubDescription: "连接 Tailscale、局域网或 HTTPS 上运行的 Hub。",
    hubAddress: "Hub 地址",
    hubNote: "HTTP 只应在可信的私有网络中使用。",
    connectHub: "连接到 Hub",
    connecting: "正在打开 Hub…",
    securityTitle: "权限天然隔离。",
    securityBody: "Hub 页面没有桌面命令权限；关闭工作区窗口即可切换连接。",
    available: "可用",
    unavailable: "缺失",
    alreadyRunning: "Launcher 已在运行，正在打开…",
    ready: "Launcher 已就绪，正在打开工作区…",
    invalidEndpoint: "请输入有效的 HTTP 或 HTTPS Hub 地址。",
    runtimeCheckFailed: "无法检测本机运行环境。",
    unknownError: "发生了错误。请重试，或从 Terminal 启动 StarAgent 查看详情。",
    native: "本机",
    bundled: "内置原生运行时",
    sessionBackend: "Session 后端",
    checkUpdates: "检查桌面版更新",
    checkingUpdates: "正在检查更新…",
    updateKicker: "已签名的桌面版更新",
    updateAvailable: "新版本已经准备好",
    versionTransition: "StarAgent {current} → {next}",
    updateRestartWarning: "更新会重启应用。请先保存本地 Session 中正在进行的工作。",
    later: "稍后",
    updateAndRestart: "更新并重启",
    preparingUpdate: "正在准备安全下载…",
    downloadingUpdate: "正在下载更新…",
    installingUpdate: "正在验签并启动安装程序…",
    upToDate: "StarAgent 桌面版已是最新版本。",
    updateCheckFailed: "无法检查桌面版更新。",
    updateInstallFailed: "桌面版更新安装失败。",
  },
};

const elements = {
  languageButton: document.querySelector("#language-button"),
  runtimeLoading: document.querySelector("#runtime-loading"),
  runtimeStatus: document.querySelector("#runtime-status"),
  runtimeHelp: document.querySelector("#runtime-help"),
  refreshRuntime: document.querySelector("#refresh-runtime"),
  launchLocal: document.querySelector("#launch-local"),
  strategyDot: document.querySelector("#strategy-dot"),
  strategyValue: document.querySelector("#strategy-value"),
  runtimeDot: document.querySelector("#staragent-runtime-dot"),
  runtimeValue: document.querySelector("#staragent-runtime-value"),
  backendDot: document.querySelector("#session-backend-dot"),
  backendValue: document.querySelector("#session-backend-value"),
  installHint: document.querySelector("#install-hint"),
  copyInstall: document.querySelector("#copy-install"),
  hubForm: document.querySelector("#hub-form"),
  hubEndpoint: document.querySelector("#hub-endpoint"),
  connectHub: document.querySelector("#connect-hub"),
  messageBar: document.querySelector("#message-bar"),
  messageText: document.querySelector("#message-text"),
  updateButton: document.querySelector("#desktop-update-button"),
  desktopVersion: document.querySelector("#desktop-version"),
  updatePanel: document.querySelector("#desktop-update-panel"),
  updateVersion: document.querySelector("#desktop-update-version"),
  updateNotes: document.querySelector("#desktop-update-notes"),
  updateLater: document.querySelector("#desktop-update-later"),
  updateLaterButton: document.querySelector("#desktop-update-later-button"),
  updateInstall: document.querySelector("#desktop-update-install"),
  updateProgress: document.querySelector("#desktop-update-progress"),
  updateProgressBar: document.querySelector("#desktop-update-progress-bar"),
  updateProgressText: document.querySelector("#desktop-update-progress-text"),
};

let language = localStorage.getItem(LANGUAGE_KEY)
  || (navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en");
let environment = null;
let desktopUpdate = null;
let updateCheckInFlight = false;
let pendingUpdateReady = false;
let installingUpdate = false;

function t(key) {
  return messages[language][key] || messages.en[key] || key;
}

function applyLanguage() {
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  elements.languageButton.textContent = language === "zh" ? "EN" : "中";
  elements.languageButton.setAttribute(
    "aria-label",
    language === "zh" ? "Switch to English" : "切换到中文",
  );
  elements.updateButton.setAttribute("aria-label", t("checkUpdates"));
  elements.updateButton.title = t("checkUpdates");
  elements.updateLater.setAttribute("aria-label", t("later"));
  elements.updateLater.title = t("later");
  if (environment) renderEnvironment(environment);
  if (desktopUpdate && !installingUpdate) {
    renderDesktopUpdate(desktopUpdate, {reveal: false});
  }
}

function setMessage(message, type = "info") {
  elements.messageText.textContent = message;
  elements.messageBar.dataset.type = type;
  elements.messageBar.hidden = !message;
}

function setButtonBusy(button, busy, labelKey) {
  if (!button.dataset.defaultHtml) button.dataset.defaultHtml = button.innerHTML;
  button.disabled = busy;
  button.classList.toggle("is-busy", busy);
  if (busy) {
    button.innerHTML = `<span class="button-spinner" aria-hidden="true"></span><span>${t(labelKey)}</span>`;
  } else {
    button.innerHTML = button.dataset.defaultHtml;
  }
}

function markAvailability(dot, available) {
  dot.dataset.available = available ? "true" : "false";
}

function interpolate(message, values) {
  return Object.entries(values).reduce(
    (result, [key, value]) => result.replace(`{${key}}`, value),
    message,
  );
}

function renderDesktopUpdate(info, {reveal = true} = {}) {
  desktopUpdate = info;
  elements.desktopVersion.textContent = `v${info.currentVersion}`;
  elements.updateButton.classList.toggle("has-update", info.available);
  if (!info.available) {
    elements.updatePanel.hidden = true;
    return;
  }

  elements.updateVersion.textContent = interpolate(t("versionTransition"), {
    current: info.currentVersion,
    next: info.version,
  });
  elements.updateNotes.textContent = info.notes || "";
  elements.updateNotes.hidden = !info.notes;
  elements.updateProgress.hidden = true;
  elements.updateProgress.classList.remove("is-indeterminate", "is-error");
  elements.updateProgressBar.style.width = "0%";
  elements.updateProgressText.textContent = "";
  if (reveal) elements.updatePanel.hidden = false;
}

function setUpdateControlsDisabled(disabled) {
  elements.updateButton.disabled = disabled;
  elements.updateLater.disabled = disabled;
  elements.updateLaterButton.disabled = disabled;
  elements.updateInstall.disabled = disabled;
}

async function refreshDesktopUpdate({silent = false} = {}) {
  if (updateCheckInFlight || installingUpdate) return;
  updateCheckInFlight = true;
  pendingUpdateReady = false;
  elements.updateButton.classList.add("is-checking");
  if (!silent) setMessage(t("checkingUpdates"));
  try {
    const info = await invoke("check_desktop_update");
    pendingUpdateReady = info.available;
    renderDesktopUpdate(info);
    if (!info.available && !silent) setMessage(t("upToDate"), "success");
  } catch (error) {
    if (!silent) setMessage(`${t("updateCheckFailed")} ${String(error)}`, "error");
  } finally {
    updateCheckInFlight = false;
    elements.updateButton.classList.remove("is-checking");
  }
}

function dismissDesktopUpdate() {
  if (installingUpdate) return;
  elements.updatePanel.hidden = true;
}

function formatBytes(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function updateDownloadProgress(downloaded, total) {
  elements.updateProgress.hidden = false;
  if (total > 0) {
    const percent = Math.min(100, Math.round((downloaded / total) * 100));
    elements.updateProgress.classList.remove("is-indeterminate");
    elements.updateProgressBar.style.width = `${percent}%`;
    elements.updateProgressText.textContent = `${t("downloadingUpdate")} ${percent}%`;
    return;
  }
  elements.updateProgress.classList.add("is-indeterminate");
  elements.updateProgressText.textContent = `${t("downloadingUpdate")} ${formatBytes(downloaded)}`;
}

async function installDesktopUpdate() {
  if (installingUpdate) return;
  if (!pendingUpdateReady) {
    await refreshDesktopUpdate();
    return;
  }

  installingUpdate = true;
  pendingUpdateReady = false;
  setUpdateControlsDisabled(true);
  elements.updatePanel.hidden = false;
  elements.updateProgress.hidden = false;
  elements.updateProgress.classList.add("is-indeterminate");
  elements.updateProgress.classList.remove("is-error");
  elements.updateProgressText.textContent = t("preparingUpdate");
  let downloaded = 0;
  let total = null;
  const onEvent = new Channel((event) => {
    if (event.event === "started") {
      elements.updateProgressText.textContent = t("preparingUpdate");
    } else if (event.event === "progress") {
      downloaded += event.chunkLength;
      total = event.contentLength || total;
      updateDownloadProgress(downloaded, total);
    } else if (event.event === "downloaded") {
      elements.updateProgress.classList.remove("is-indeterminate");
      elements.updateProgressBar.style.width = "100%";
      elements.updateProgressText.textContent = t("installingUpdate");
    }
  });

  try {
    await invoke("install_desktop_update", {onEvent});
  } catch (error) {
    installingUpdate = false;
    setUpdateControlsDisabled(false);
    elements.updateProgress.classList.remove("is-indeterminate");
    elements.updateProgress.classList.add("is-error");
    elements.updateProgressText.textContent = t("updateInstallFailed");
    setMessage(`${t("updateInstallFailed")} ${String(error)}`, "error");
    await refreshDesktopUpdate({silent: true});
  }
}

function renderEnvironment(info) {
  environment = info;
  elements.desktopVersion.textContent = `v${info.desktopVersion}`;
  elements.runtimeLoading.hidden = true;
  elements.runtimeStatus.hidden = false;

  const strategyAvailable = info.strategy === "native" || info.strategy === "bundled";
  markAvailability(elements.strategyDot, strategyAvailable);
  elements.strategyValue.textContent = info.strategy === "bundled" ? t("bundled") : t("native");
  markAvailability(elements.runtimeDot, info.runtimeAvailable);
  elements.runtimeValue.textContent = info.runtimeVersion || (info.runtimeAvailable ? t("available") : t("unavailable"));
  markAvailability(elements.backendDot, info.sessionBackendAvailable);
  elements.backendValue.textContent = info.sessionBackend || (info.sessionBackendAvailable ? t("available") : t("unavailable"));

  const ready = strategyAvailable && info.runtimeAvailable && info.sessionBackendAvailable;
  elements.launchLocal.disabled = !ready;
  elements.runtimeHelp.hidden = ready;
  elements.installHint.textContent = info.installHint || "";
  elements.copyInstall.hidden = !info.installHint;
  if (info.runtimeRunning) elements.launchLocal.classList.add("runtime-running");
  else elements.launchLocal.classList.remove("runtime-running");
}

async function refreshEnvironment() {
  elements.runtimeLoading.hidden = false;
  elements.runtimeStatus.hidden = true;
  elements.runtimeHelp.hidden = true;
  elements.launchLocal.disabled = true;
  elements.refreshRuntime.classList.add("is-spinning");
  setMessage("");
  try {
    renderEnvironment(await invoke("environment_info"));
  } catch (error) {
    elements.runtimeLoading.hidden = true;
    setMessage(`${t("runtimeCheckFailed")} ${String(error)}`, "error");
  } finally {
    elements.refreshRuntime.classList.remove("is-spinning");
  }
}

async function openDashboard(endpoint) {
  return invoke("open_dashboard", {endpoint});
}

elements.launchLocal.addEventListener("click", async () => {
  setMessage("");
  setButtonBusy(elements.launchLocal, true, "startingLocal");
  try {
    const result = await invoke("start_local_runtime", {port: 8765});
    setMessage(t(result.reused ? "alreadyRunning" : "ready"), "success");
    await openDashboard(result.endpoint || DEFAULT_ENDPOINT);
  } catch (error) {
    setMessage(String(error) || t("unknownError"), "error");
  } finally {
    setButtonBusy(elements.launchLocal, false, "startingLocal");
    if (environment) renderEnvironment(environment);
  }
});

elements.hubForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const endpoint = elements.hubEndpoint.value.trim();
  if (!endpoint) {
    setMessage(t("invalidEndpoint"), "error");
    elements.hubEndpoint.focus();
    return;
  }
  setMessage("");
  setButtonBusy(elements.connectHub, true, "connecting");
  try {
    const normalized = await openDashboard(endpoint);
    localStorage.setItem(ENDPOINT_KEY, normalized);
    elements.hubEndpoint.value = normalized;
  } catch (error) {
    setMessage(String(error) || t("invalidEndpoint"), "error");
  } finally {
    setButtonBusy(elements.connectHub, false, "connecting");
  }
});

elements.refreshRuntime.addEventListener("click", refreshEnvironment);
elements.updateButton.addEventListener("click", () => {
  if (desktopUpdate?.available && pendingUpdateReady) {
    elements.updatePanel.hidden = false;
    return;
  }
  refreshDesktopUpdate();
});
elements.updateLater.addEventListener("click", dismissDesktopUpdate);
elements.updateLaterButton.addEventListener("click", dismissDesktopUpdate);
elements.updateInstall.addEventListener("click", installDesktopUpdate);
elements.languageButton.addEventListener("click", () => {
  language = language === "zh" ? "en" : "zh";
  localStorage.setItem(LANGUAGE_KEY, language);
  applyLanguage();
});
elements.copyInstall.addEventListener("click", async () => {
  const value = elements.installHint.textContent;
  if (!value) return;
  await navigator.clipboard.writeText(value);
  const original = elements.copyInstall.textContent;
  elements.copyInstall.textContent = t("copied");
  window.setTimeout(() => { elements.copyInstall.textContent = original; }, 1200);
});

elements.hubEndpoint.value = localStorage.getItem(ENDPOINT_KEY) || "";
applyLanguage();
refreshEnvironment();
refreshDesktopUpdate({silent: true});
