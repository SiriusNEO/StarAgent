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

  const authRoot = document.querySelector("[data-codex-auth-terminal]");
  if (authRoot) {
    const authNode = authRoot.dataset.node || "";
    const authTerminalElement = authRoot.querySelector(".codex-auth-terminal");
    const authScreen = authTerminalElement.querySelector(".terminal-screen");
    const authStatus = authTerminalElement.querySelector(".terminal-connection-state");
    const authClose = authRoot.querySelector(".codex-auth-dialog-close");
    const authDone = authRoot.querySelector(".codex-auth-dialog-done");
    let authTerm = null;
    let authFitAddon = null;
    let authSocket = null;
    let authRefreshDispatched = false;

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
        detail: {node: authNode},
      }));
    };

    const initializeAuthTerminal = async () => {
      if (authTerm) {
        return;
      }
      await ensureAssets();
      authScreen.querySelector(".codex-auth-terminal-placeholder")?.remove();
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
      authRefreshDispatched = false;
      authStatus.textContent = t("agents.codex_login_starting");
      try {
        await initializeAuthTerminal();
      } catch (error) {
        authStatus.textContent = error?.message || t("agents.test_assets_failed");
        return;
      }
      authTerm.reset();
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${protocol}//${window.location.host}/ws/nodes/${encodeURIComponent(authNode)}`
        + "/agent-tools/codex/auth/login";
      authSocket = new WebSocket(url);
      authSocket.binaryType = "arraybuffer";
      authSocket.addEventListener("open", () => {
        authStatus.textContent = t("agents.codex_login_connected");
        fitAuth();
        authTerm.focus();
      });
      authSocket.addEventListener("message", (event) => {
        if (typeof event.data === "string") {
          authTerm.write(event.data);
        } else {
          authTerm.write(new Uint8Array(event.data));
        }
      });
      authSocket.addEventListener("error", () => {
        authStatus.textContent = t("agents.codex_login_error");
      });
      authSocket.addEventListener("close", (event) => {
        authSocket = null;
        authStatus.textContent = event.reason && event.reason !== "terminal exited"
          ? event.reason
          : t("agents.codex_login_closed");
        refreshAuthStatus();
      });
    };

    const closeAuthDialog = () => {
      if (authRoot.open) {
        authRoot.close();
      }
    };

    window.addEventListener("staragent:codex-login", (event) => {
      if ((event.detail?.node || "") !== authNode) {
        return;
      }
      if (!authRoot.open) {
        authRoot.showModal();
      }
      requestAnimationFrame(() => {
        fitAuth();
        connectAuthTerminal();
      });
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
        authSocket.close(1000, "login dialog closed");
      }
      refreshAuthStatus();
    });
    window.addEventListener("beforeunload", () => {
      if (authSocket && authSocket.readyState < WebSocket.CLOSING) {
        authSocket.close(1000, "page unload");
      }
    });
  }
})();
