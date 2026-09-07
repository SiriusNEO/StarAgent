(() => {
  const root = document.querySelector("[data-harness-test-terminal]");
  if (!root) {
    return;
  }

  const t = (key, values = {}) => window.StarAgentI18n?.t(key, values) || key;
  const node = root.dataset.node || "";
  const agent = root.dataset.agent || "";
  const terminalElement = root.querySelector(".agent-test-terminal");
  const screen = terminalElement.querySelector(".terminal-screen");
  const status = terminalElement.querySelector(".terminal-connection-state");
  const startButton = root.querySelector(".agent-test-terminal-start");
  const startLabel = startButton.querySelector("span");
  const inputLock = root.querySelector(".agent-test-input-lock");
  const inputLockLabel = inputLock.querySelector(".terminal-lock-label");

  let assetsPromise = null;
  let term = null;
  let fitAddon = null;
  let socket = null;
  let connected = false;
  let inputUnlocked = false;
  let restartAfterClose = false;
  let closedByPage = false;

  const loadScript = (url) => new Promise((resolve, reject) => {
    const existing = Array.from(document.scripts).find(
      (script) => script.dataset.staragentSrc === url,
    );
    if (existing) {
      if (existing.dataset.loaded === "true") {
        resolve();
      } else {
        existing.addEventListener("load", resolve, {once: true});
        existing.addEventListener("error", reject, {once: true});
      }
      return;
    }
    const script = document.createElement("script");
    script.src = url;
    script.async = true;
    script.dataset.staragentSrc = url;
    script.addEventListener("load", () => {
      script.dataset.loaded = "true";
      resolve();
    }, {once: true});
    script.addEventListener("error", reject, {once: true});
    document.head.appendChild(script);
  });

  const ensureAssets = () => {
    if (window.Terminal && window.FitAddon && window.WebLinksAddon) {
      return Promise.resolve();
    }
    if (!assetsPromise) {
      assetsPromise = loadScript(root.dataset.xtermJs)
        .then(() => Promise.all([
          loadScript(root.dataset.xtermFitJs),
          loadScript(root.dataset.xtermWebLinksJs),
        ]));
    }
    return assetsPromise;
  };

  const cssVariable = (name, fallback) => (
    getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
  );

  const terminalTheme = () => ({
    background: cssVariable("--terminal-bg", "#1e1e1e"),
    foreground: cssVariable("--terminal-fg", "#d4d4d4"),
    cursor: cssVariable("--terminal-cursor", "#aeafad"),
    selectionBackground: cssVariable("--terminal-selection", "#264f78"),
    black: "#000000",
    red: "#f14c4c",
    green: "#23d18b",
    yellow: "#f5f543",
    blue: "#3b8eea",
    magenta: "#d670d6",
    cyan: "#29b8db",
    white: "#e5e5e5",
    brightBlack: "#666666",
    brightRed: "#f14c4c",
    brightGreen: "#23d18b",
    brightYellow: "#f5f543",
    brightBlue: "#3b8eea",
    brightMagenta: "#d670d6",
    brightCyan: "#29b8db",
    brightWhite: "#ffffff",
  });

  const openTerminalLink = (_event, uri) => {
    let url;
    try {
      url = new URL(uri);
    } catch (_error) {
      return;
    }
    if (url.protocol === "http:" || url.protocol === "https:") {
      window.open(url.href, "_blank", "noopener,noreferrer");
    }
  };

  const setInputUnlocked = (unlocked, {focus = false} = {}) => {
    inputUnlocked = Boolean(unlocked && connected);
    if (term) {
      term.options.disableStdin = !inputUnlocked;
    }
    terminalElement.classList.toggle("is-input-locked", !inputUnlocked);
    terminalElement.classList.toggle("is-input-unlocked", inputUnlocked);
    inputLock.classList.toggle("is-locked", !inputUnlocked);
    inputLock.classList.toggle("is-unlocked", inputUnlocked);
    inputLock.setAttribute("aria-pressed", String(inputUnlocked));
    inputLock.setAttribute(
      "aria-label",
      inputUnlocked ? t("detail.lock_terminal") : t("detail.unlock_terminal"),
    );
    inputLock.title = inputUnlocked
      ? t("detail.terminal_unlocked_title")
      : t("detail.terminal_locked_title");
    inputLockLabel.textContent = inputUnlocked ? t("detail.unlocked") : t("detail.locked");
    if (term?.textarea) {
      term.textarea.readOnly = !inputUnlocked;
      term.textarea.setAttribute("aria-readonly", String(!inputUnlocked));
    }
    if (inputUnlocked && focus) {
      term.focus();
    } else if (!inputUnlocked) {
      term?.blur();
    }
  };

  const send = (payload) => {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    socket.send(JSON.stringify(payload));
    return true;
  };

  const fit = () => {
    if (!term || !fitAddon) {
      return;
    }
    try {
      fitAddon.fit();
      send({type: "resize", cols: term.cols, rows: term.rows});
    } catch (_error) {
      // A responsive layout can briefly report zero dimensions during navigation.
    }
  };

  const initializeTerminal = async () => {
    if (term) {
      return;
    }
    await ensureAssets();
    screen.querySelector(".agent-test-terminal-placeholder")?.remove();
    term = new Terminal({
      allowProposedApi: false,
      convertEol: true,
      cursorBlink: true,
      disableStdin: true,
      fontFamily: '"Cascadia Mono", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
      fontSize: 13,
      lineHeight: 1.18,
      fastScrollModifier: "alt",
      fastScrollSensitivity: 5,
      scrollSensitivity: 1.2,
      scrollback: 10000,
      theme: terminalTheme(),
    });
    fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(new WebLinksAddon.WebLinksAddon(openTerminalLink));
    term.open(screen);
    term.attachCustomKeyEventHandler(() => inputUnlocked);
    term.onData((data) => {
      if (!inputUnlocked || !send({type: "input", data})) {
        return;
      }
    });
    // xterm creates its hidden textarea during open(), so apply the initial
    // lock again after that element exists. This also keeps mobile keyboards
    // closed until the user explicitly unlocks the terminal.
    setInputUnlocked(false);
    window.addEventListener("staragent:themechange", () => {
      term.options.theme = terminalTheme();
    });
    document.addEventListener("pointerdown", (event) => {
      if (!terminalElement.contains(event.target)) {
        term.blur();
      }
    });
    const observer = new ResizeObserver(() => fit());
    observer.observe(terminalElement);
    requestAnimationFrame(fit);
  };

  const setStartState = (state) => {
    const starting = state === "starting";
    startButton.disabled = starting;
    startLabel.textContent = starting
      ? t("agents.test_starting")
      : state === "connected"
        ? t("agents.test_restart")
        : t("agents.test_start");
  };

  const connectTerminal = async () => {
    setStartState("starting");
    status.textContent = t("agents.test_starting");
    inputLock.disabled = true;
    setInputUnlocked(false);
    try {
      await initializeTerminal();
    } catch (error) {
      status.textContent = error?.message || t("agents.test_assets_failed");
      setStartState("idle");
      return;
    }
    term.reset();
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}/ws/nodes/${encodeURIComponent(node)}`
      + `/agent-tools/${encodeURIComponent(agent)}/terminal`;
    socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";
    socket.addEventListener("open", () => {
      connected = true;
      status.textContent = t("agents.test_ready");
      inputLock.disabled = false;
      setStartState("connected");
      fit();
    });
    socket.addEventListener("message", (event) => {
      if (typeof event.data === "string") {
        term.write(event.data);
      } else {
        term.write(new Uint8Array(event.data));
      }
    });
    socket.addEventListener("error", () => {
      status.textContent = t("agents.test_connection_error");
    });
    socket.addEventListener("close", (event) => {
      const restart = restartAfterClose;
      restartAfterClose = false;
      connected = false;
      socket = null;
      setInputUnlocked(false);
      inputLock.disabled = true;
      setStartState("idle");
      if (!closedByPage) {
        status.textContent = event.reason || t("agents.test_closed");
      }
      if (restart && !closedByPage) {
        setTimeout(connectTerminal, 180);
      }
    });
  };

  startButton.addEventListener("click", () => {
    if (socket && socket.readyState < WebSocket.CLOSING) {
      restartAfterClose = true;
      setStartState("starting");
      status.textContent = t("agents.test_restarting");
      socket.close(1000, "test terminal restart");
      return;
    }
    connectTerminal();
  });

  inputLock.addEventListener("click", () => {
    setInputUnlocked(!inputUnlocked, {focus: !inputUnlocked});
  });

  window.addEventListener("beforeunload", () => {
    closedByPage = true;
    if (socket && socket.readyState < WebSocket.CLOSING) {
      socket.close(1000, "page unload");
    }
  });

  const authRoot = document.querySelector("[data-harness-auth-terminal]");
  if (authRoot) {
    const authNode = authRoot.dataset.node || "";
    const authAgent = authRoot.dataset.agent || "";
    const authTerminalElement = authRoot.querySelector(".harness-auth-terminal");
    const authScreen = authTerminalElement.querySelector(".terminal-screen");
    const authStatus = authTerminalElement.querySelector(".terminal-connection-state");
    const authTerminalPanel = authRoot.querySelector("[data-harness-auth-terminal-panel]");
    const authApiKeyForm = authRoot.querySelector("[data-harness-api-key-form]");
    const authApiKeyInput = authApiKeyForm.querySelector("input[name='api_key']");
    const authApiKeyStatus = authApiKeyForm.querySelector("[data-harness-api-key-status]");
    const authMethodButtons = Array.from(authRoot.querySelectorAll("[data-auth-method]"));
    const authMethodDetails = Array.from(authRoot.querySelectorAll("[data-auth-method-detail]"));
    const authDeviceForbidden = authRoot.querySelector("[data-auth-device-forbidden]");
    const authClose = authRoot.querySelector(".harness-auth-dialog-close");
    const authDone = authRoot.querySelector(".harness-auth-dialog-done");
    const authStart = authRoot.querySelector(".harness-auth-dialog-start");
    const authStartLabel = authStart.querySelector("span");
    let authTerm = null;
    let authFitAddon = null;
    let authSocket = null;
    let authRefreshDispatched = false;
    let authMethod = authRoot.dataset.defaultMethod || "";
    let authAction = "login";
    let authTransport = "terminal";
    let authOutputTail = "";
    let authActionError = "";
    let authRequestBusy = false;
    let authDecoder = new TextDecoder();

    const sendAuth = (payload) => {
      if (!authSocket || authSocket.readyState !== WebSocket.OPEN) {
        return false;
      }
      authSocket.send(JSON.stringify(payload));
      return true;
    };

    const fitAuth = () => {
      if (!authTerm || !authFitAddon) {
        return;
      }
      try {
        authFitAddon.fit();
        sendAuth({type: "resize", cols: authTerm.cols, rows: authTerm.rows});
      } catch (_error) {
        // Dialog opening and closing can briefly report zero dimensions.
      }
    };

    const refreshAuthStatus = () => {
      if (authRefreshDispatched) {
        return;
      }
      authRefreshDispatched = true;
      window.dispatchEvent(new CustomEvent("staragent:agent-auth-finished", {
        detail: {node: authNode, agent: authAgent},
      }));
    };

    const setAuthControlsDisabled = (disabled) => {
      authMethodButtons.forEach((button) => {
        button.disabled = disabled;
      });
      authStart.disabled = disabled;
      if (authApiKeyInput) {
        authApiKeyInput.disabled = disabled;
      }
    };

    const setAuthMethod = (method, {focus = false} = {}) => {
      const selectedButton = authMethodButtons.find(
        (button) => button.dataset.authMethod === method,
      );
      if (!selectedButton || authRequestBusy) {
        return;
      }
      if (authSocket && authSocket.readyState < WebSocket.CLOSING) {
        authSocket.close(1000, "login method changed");
      }
      authMethod = method;
      authAction = selectedButton.dataset.authAction || "login";
      authTransport = selectedButton.dataset.authTransport || "terminal";
      authActionError = "";
      authOutputTail = "";
      authDeviceForbidden.hidden = true;
      authMethodButtons.forEach((button) => {
        const selected = button.dataset.authMethod === method;
        button.classList.toggle("is-active", selected);
        button.setAttribute("aria-pressed", String(selected));
      });
      authMethodDetails.forEach((detail) => {
        detail.hidden = detail.dataset.authMethodDetail !== method;
      });
      const usesTerminal = authTransport === "terminal";
      const usesApiKey = authTransport === "api_key";
      authTerminalPanel.hidden = !usesTerminal;
      authApiKeyForm.hidden = !usesApiKey;
      authApiKeyStatus.textContent = "";
      authApiKeyStatus.classList.remove("is-error", "is-success");
      authStartLabel.textContent = selectedButton.dataset.startLabel || t("agents.continue");
      if (usesApiKey) {
        if (focus) {
          requestAnimationFrame(() => authApiKeyInput.focus());
        }
      } else if (usesTerminal) {
        authStatus.textContent = t("agents.auth_not_started");
        requestAnimationFrame(fitAuth);
      }
    };

    const inspectAuthOutput = (text) => {
      if (authAgent !== "codex" || authMethod !== "device") {
        return;
      }
      authOutputTail = `${authOutputTail}${text}`.slice(-2400);
      if (/device code request failed[\s\S]*403|403 forbidden/i.test(authOutputTail)) {
        authActionError = t("agents.codex_device_forbidden");
        authDeviceForbidden.hidden = false;
        authStatus.textContent = authActionError;
      }
    };

    const initializeAuthTerminal = async () => {
      if (authTerm) {
        return;
      }
      await ensureAssets();
      authScreen.querySelector(".harness-auth-terminal-placeholder")?.remove();
      authTerm = new Terminal({
        allowProposedApi: false,
        convertEol: true,
        cursorBlink: true,
        disableStdin: false,
        fontFamily: '"Cascadia Mono", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
        fontSize: 13,
        lineHeight: 1.2,
        scrollback: 3000,
        theme: terminalTheme(),
      });
      authFitAddon = new FitAddon.FitAddon();
      authTerm.loadAddon(authFitAddon);
      authTerm.loadAddon(new WebLinksAddon.WebLinksAddon(openTerminalLink));
      authTerm.open(authScreen);
      authTerm.onData((data) => sendAuth({type: "input", data}));
      window.addEventListener("staragent:themechange", () => {
        authTerm.options.theme = terminalTheme();
      });
      const observer = new ResizeObserver(fitAuth);
      observer.observe(authTerminalElement);
    };

    const connectAuthTerminal = async () => {
      if (authTransport !== "terminal" || authRequestBusy) {
        return;
      }
      authRequestBusy = true;
      setAuthControlsDisabled(true);
      authRefreshDispatched = false;
      authActionError = "";
      authOutputTail = "";
      authDecoder = new TextDecoder();
      authDeviceForbidden.hidden = true;
      authStatus.textContent = t("agents.auth_starting");
      try {
        await initializeAuthTerminal();
      } catch (error) {
        authStatus.textContent = error?.message || t("agents.test_assets_failed");
        authRequestBusy = false;
        setAuthControlsDisabled(false);
        return;
      }
      authTerm.reset();
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${protocol}//${window.location.host}/ws/nodes/${encodeURIComponent(authNode)}`
        + `/agent-tools/${encodeURIComponent(authAgent)}/auth/${encodeURIComponent(authAction)}`
        + `/${encodeURIComponent(authMethod)}`;
      const socket = new WebSocket(url);
      authSocket = socket;
      socket.binaryType = "arraybuffer";
      socket.addEventListener("open", () => {
        authStatus.textContent = t("agents.auth_running");
        fitAuth();
        authTerm.focus();
      });
      socket.addEventListener("message", (event) => {
        if (typeof event.data === "string") {
          authTerm.write(event.data);
          inspectAuthOutput(event.data);
        } else {
          const bytes = new Uint8Array(event.data);
          authTerm.write(bytes);
          inspectAuthOutput(authDecoder.decode(bytes, {stream: true}));
        }
      });
      socket.addEventListener("error", () => {
        authStatus.textContent = t("agents.auth_terminal_error");
      });
      socket.addEventListener("close", (event) => {
        if (authSocket === socket) {
          authSocket = null;
        }
        authRequestBusy = false;
        setAuthControlsDisabled(false);
        authStatus.textContent = authActionError || (
          event.reason && event.reason !== "terminal exited"
            ? event.reason
            : t("agents.auth_finished")
        );
        refreshAuthStatus();
      });
    };

    const submitApiKey = async () => {
      const apiKey = authApiKeyInput.value.trim();
      if (!apiKey) {
        authApiKeyStatus.textContent = t("agents.codex_api_key_required");
        authApiKeyStatus.classList.add("is-error");
        authApiKeyInput.focus();
        return;
      }
      authRequestBusy = true;
      setAuthControlsDisabled(true);
      authRefreshDispatched = false;
      authApiKeyStatus.textContent = t("agents.codex_api_key_submitting", {node: authNode});
      authApiKeyStatus.classList.remove("is-error", "is-success");
      try {
        const requestBody = JSON.stringify({api_key: apiKey});
        authApiKeyInput.value = "";
        const response = await fetch(
          `/api/nodes/${encodeURIComponent(authNode)}/agent-tools/${encodeURIComponent(authAgent)}`
            + "/auth/login/api-key",
          {
            method: "POST",
            cache: "no-store",
            headers: {"Content-Type": "application/json"},
            body: requestBody,
          },
        );
        const body = await response.json().catch(() => ({}));
        if (!response.ok || !body.ok) {
          throw new Error(body.detail || t("agents.auth.error"));
        }
        authApiKeyStatus.textContent = t("agents.codex_api_key_success");
        authApiKeyStatus.classList.add("is-success");
        refreshAuthStatus();
      } catch (error) {
        authApiKeyStatus.textContent = t("agents.codex_api_key_failed", {
          message: error?.message || t("agents.auth.error"),
        });
        authApiKeyStatus.classList.add("is-error");
      } finally {
        authApiKeyInput.value = "";
        authRequestBusy = false;
        setAuthControlsDisabled(false);
      }
    };

    const closeAuthDialog = () => {
      if (authRoot.open) {
        authRoot.close();
      }
    };

    const openEnvironmentSettings = () => {
      closeAuthDialog();
      const configuration = document.querySelector("[data-harness-configuration]");
      if (!configuration) {
        return;
      }
      configuration.scrollIntoView({behavior: "smooth", block: "start"});
      configuration.querySelector(".harness-env-pane")?.classList.add("is-auth-target");
      window.setTimeout(() => {
        configuration.querySelector(".harness-env-pane")?.classList.remove("is-auth-target");
      }, 1600);
    };

    window.addEventListener("staragent:harness-auth", (event) => {
      if (
        (event.detail?.node || "") !== authNode
        || (event.detail?.agent || "") !== authAgent
      ) {
        return;
      }
      if (!authRoot.open) {
        authRoot.showModal();
      }
      authRefreshDispatched = false;
      setAuthMethod(authRoot.dataset.defaultMethod || authMethodButtons[0]?.dataset.authMethod || "");
      requestAnimationFrame(() => {
        fitAuth();
      });
    });
    authMethodButtons.forEach((button) => {
      button.addEventListener("click", () => {
        setAuthMethod(button.dataset.authMethod || "", {focus: true});
      });
    });
    authApiKeyForm.addEventListener("submit", (event) => {
      event.preventDefault();
      submitApiKey();
    });
    authStart.addEventListener("click", () => {
      if (authTransport === "api_key") {
        authApiKeyForm.requestSubmit();
      } else if (authTransport === "environment") {
        openEnvironmentSettings();
      } else {
        connectAuthTerminal();
      }
    });
    authClose.addEventListener("click", closeAuthDialog);
    authDone.addEventListener("click", closeAuthDialog);
    authRoot.addEventListener("click", (event) => {
      if (event.target === authRoot) {
        closeAuthDialog();
      }
    });
    authRoot.addEventListener("close", () => {
      if (authSocket && authSocket.readyState < WebSocket.CLOSING) {
        authSocket.close(1000, "authentication dialog closed");
      }
      authApiKeyInput.value = "";
      refreshAuthStatus();
    });
    window.addEventListener("beforeunload", () => {
      if (authSocket && authSocket.readyState < WebSocket.CLOSING) {
        authSocket.close(1000, "page unload");
      }
    });
    setAuthMethod(authMethod || authMethodButtons[0]?.dataset.authMethod || "");
  }
})();
