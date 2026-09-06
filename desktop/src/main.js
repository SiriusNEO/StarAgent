import {invoke} from "@tauri-apps/api/core";
import "./styles.css";

const DEFAULT_ENDPOINT = "http://127.0.0.1:8765";
const ENDPOINT_KEY = "staragent.desktop.hubEndpoint";
const LANGUAGE_KEY = "staragent.desktop.language";

const messages = {
  en: {
    desktopEdition: "Desktop",
    eyebrow: "Native workspace gateway",
    title: "Open your agent workspace.",
    subtitle: "Launch StarAgent on this machine, or connect securely to an existing Hub.",
    localMode: "Local mode",
    localTitle: "This machine",
    localDescription: "Start the single-Node Launcher and keep all sessions on this computer.",
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
    wsl: "WSL2",
  },
  zh: {
    desktopEdition: "桌面版",
    eyebrow: "原生工作区入口",
    title: "打开你的 Agent 工作区",
    subtitle: "在当前机器启动 StarAgent，或安全连接到已有 Hub。",
    localMode: "本机模式",
    localTitle: "当前机器",
    localDescription: "启动单 Node Launcher，所有 Session 都保留在这台机器上。",
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
    wsl: "WSL2",
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
  staragentDot: document.querySelector("#staragent-dot"),
  staragentValue: document.querySelector("#staragent-value"),
  tmuxDot: document.querySelector("#tmux-dot"),
  tmuxValue: document.querySelector("#tmux-value"),
  installHint: document.querySelector("#install-hint"),
  copyInstall: document.querySelector("#copy-install"),
  hubForm: document.querySelector("#hub-form"),
  hubEndpoint: document.querySelector("#hub-endpoint"),
  connectHub: document.querySelector("#connect-hub"),
  messageBar: document.querySelector("#message-bar"),
  messageText: document.querySelector("#message-text"),
};

let language = localStorage.getItem(LANGUAGE_KEY)
  || (navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en");
let environment = null;

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
  if (environment) renderEnvironment(environment);
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

function renderEnvironment(info) {
  environment = info;
  elements.runtimeLoading.hidden = true;
  elements.runtimeStatus.hidden = false;

  const strategyAvailable = info.strategy === "native" || info.wslAvailable;
  markAvailability(elements.strategyDot, strategyAvailable);
  elements.strategyValue.textContent = info.strategy === "wsl" ? t("wsl") : t("native");
  markAvailability(elements.staragentDot, info.staragentAvailable);
  elements.staragentValue.textContent = info.staragentVersion || (info.staragentAvailable ? t("available") : t("unavailable"));
  markAvailability(elements.tmuxDot, info.tmuxAvailable);
  elements.tmuxValue.textContent = info.tmuxVersion || (info.tmuxAvailable ? t("available") : t("unavailable"));

  const ready = strategyAvailable && info.staragentAvailable && info.tmuxAvailable;
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
