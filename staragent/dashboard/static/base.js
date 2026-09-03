(() => {
  const settings = window.StarAgentThemeSettings;
  if (!settings) {
    return;
  }

  const controlRoot = document.querySelector("[data-theme-settings]");
  const t = (key, values = {}) => window.StarAgentI18n?.t(key, values) || key;
  const controls = (selector) => controlRoot ? controlRoot.querySelectorAll(selector) : [];
  const {storageKey, legacyKey, backgroundModeKey, backgroundImageKey, surfaceModeKey, legacyMap} = settings;
  const themes = new Set(settings.themes);
  const backgroundModes = new Set(settings.backgroundModes);
  const surfaceModes = new Set(settings.surfaceModes);
  let backgrounds = [];
  let selectedBackgroundId = "";

  const selectedBackground = () => backgrounds.find((item) => item.id === selectedBackgroundId) || null;
  const applyTheme = (theme, persist = false) => {
    if (!themes.has(theme)) {
      return;
    }
    const root = document.documentElement;
    root.dataset.theme = theme;
    for (const option of controls(".theme-option")) {
      const active = option.dataset.theme === theme;
      option.classList.toggle("is-active", active);
      option.setAttribute("aria-checked", String(active));
    }
    if (persist) {
      localStorage.setItem(storageKey, theme);
      localStorage.removeItem(legacyKey);
    }
    window.dispatchEvent(new CustomEvent("staragent:themechange", {detail: {theme}}));
  };
  const setThemeStatus = (message) => {
    const status = controlRoot?.querySelector(".theme-upload-status");
    if (status) {
      status.textContent = message || "";
    }
  };
  const backgroundImageUrl = (background) => {
    if (!background || !background.url) {
      return "";
    }
    const separator = background.url.includes("?") ? "&" : "?";
    return `${background.url}${separator}v=${Number(background.mtime || 0)}`;
  };
  const backgroundThumbUrl = (background) => {
    const url = background && (background.thumb_url || background.url);
    if (!url) {
      return "";
    }
    const mtime = Number(background.thumb_mtime || background.mtime || 0);
    const separator = url.includes("?") ? "&" : "?";
    return `${url}${separator}v=${mtime}`;
  };
  const preloadBackground = (background) => new Promise((resolve) => {
    const url = backgroundImageUrl(background);
    if (!url) {
      resolve();
      return;
    }
    const image = new Image();
    image.onload = resolve;
    image.onerror = resolve;
    image.src = url;
  });
  const applyBackgroundMode = (mode, persist = false) => {
    if (!backgroundModes.has(mode)) {
      return;
    }
    if (mode === "image" && !selectedBackground() && persist) {
      setThemeStatus(t("theme.image_required"));
      return;
    }
    document.documentElement.dataset.bgMode = mode;
    for (const button of controls("[data-bg-mode]")) {
      const active = button.dataset.bgMode === mode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    }
    if (persist) {
      localStorage.setItem(backgroundModeKey, mode);
      localStorage.removeItem(backgroundImageKey);
    }
  };
  const renderBackgroundLibrary = () => {
    const library = controlRoot?.querySelector(".theme-background-library");
    const empty = controlRoot?.querySelector(".settings-gallery-empty");
    if (empty) {
      empty.hidden = backgrounds.length > 0;
    }
    if (!library) {
      return;
    }
    library.replaceChildren();
    if (!backgrounds.length) {
      library.hidden = true;
      return;
    }
    library.hidden = false;
    for (const background of backgrounds) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "theme-background-thumb";
      item.dataset.bgId = background.id;
      item.style.backgroundImage = `url("${backgroundThumbUrl(background)}")`;
      const active = background.id === selectedBackgroundId;
      item.setAttribute("aria-label", `${t("settings.gallery.select")}: ${background.id}`);
      item.setAttribute("aria-pressed", String(active));
      item.classList.toggle("is-active", active);
      if (active) {
        item.title = t("settings.gallery.active");
      }

      const deleteButton = document.createElement("span");
      deleteButton.className = "theme-background-delete";
      deleteButton.dataset.bgDelete = background.id;
      deleteButton.setAttribute("role", "button");
      deleteButton.setAttribute("aria-label", `${t("settings.gallery.delete")}: ${background.id}`);
      deleteButton.setAttribute("tabindex", "0");
      deleteButton.textContent = "×";
      item.append(deleteButton);
      library.append(item);
    }
  };
  const applyThemeConfig = (body = {}) => {
    backgrounds = Array.isArray(body.backgrounds) ? body.backgrounds.filter((item) => item && item.id && item.url) : [];
    selectedBackgroundId = body.selected_background_id || "";
    if (!selectedBackground() && body.background_url) {
      selectedBackgroundId = "legacy";
      backgrounds.unshift({
        id: "legacy",
        url: body.background_url,
        mtime: body.background_mtime || 0,
      });
    }
    if (!selectedBackground() && backgrounds.length) {
      selectedBackgroundId = backgrounds[0].id;
    }
    const background = selectedBackground();
    const root = document.documentElement;
    root.style.setProperty("--custom-bg-image", background ? `url("${backgroundImageUrl(background)}")` : "none");
    renderBackgroundLibrary();
    const clearButton = controlRoot?.querySelector(".theme-clear-background");
    if (clearButton) {
      clearButton.disabled = !background;
    }
    if (!background && root.dataset.bgMode === "image") {
      applyBackgroundMode("gradient", true);
    }
  };
  const applySurfaceMode = (mode, persist = false) => {
    if (!surfaceModes.has(mode)) {
      return;
    }
    document.documentElement.dataset.surfaceMode = mode;
    for (const button of controls("[data-surface-mode]")) {
      const active = button.dataset.surfaceMode === mode;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    }
    if (persist) {
      const surfaceKey = mode === "clear-glass" ? "clear" : mode;
      setThemeStatus(t("theme.surface_changed", {surface: t(`settings.surface.${surfaceKey}`)}));
      localStorage.setItem(surfaceModeKey, mode);
    }
  };
  let themeConfigPromise = null;
  const loadThemeConfig = async () => {
    try {
      const response = await fetch("/api/theme");
      if (!response.ok) {
        return;
      }
      const body = await response.json();
      applyThemeConfig(body);
      const savedMode = localStorage.getItem(backgroundModeKey) || document.documentElement.dataset.bgMode || "solid";
      const legacyImageMode = localStorage.getItem(backgroundImageKey) === "on" ? "image" : savedMode;
      applyBackgroundMode(legacyImageMode);
    } catch {
      setThemeStatus(t("theme.unavailable"));
    }
  };
  const ensureThemeConfig = () => {
    if (!themeConfigPromise) {
      themeConfigPromise = loadThemeConfig();
    }
    return themeConfigPromise;
  };

  const saved = localStorage.getItem(storageKey) || legacyMap[localStorage.getItem(legacyKey)];
  if (themes.has(saved)) {
    applyTheme(saved);
  } else {
    applyTheme(document.documentElement.dataset.theme || "tsinghua");
  }
  const savedMode = localStorage.getItem(backgroundModeKey) || document.documentElement.dataset.bgMode || "solid";
  applyBackgroundMode(savedMode === "image" || localStorage.getItem(backgroundImageKey) === "on" ? "image" : savedMode);
  applySurfaceMode(localStorage.getItem(surfaceModeKey) || document.documentElement.dataset.surfaceMode || "solid");
  if (document.documentElement.dataset.bgMode === "image") {
    ensureThemeConfig();
  } else {
    window.StarAgentAfterPaint(ensureThemeConfig);
  }

  if (!controlRoot) {
    window.StarAgentTheme = {applyTheme, applyBackgroundMode, applySurfaceMode, ensureThemeConfig};
    return;
  }

  controlRoot.addEventListener("click", async (event) => {
    const option = event.target.closest(".theme-option");
    if (option) {
      applyTheme(option.dataset.theme, true);
      return;
    }
    const modeButton = event.target.closest("[data-bg-mode]");
    if (modeButton) {
      applyBackgroundMode(modeButton.dataset.bgMode, true);
      return;
    }
    const surfaceButton = event.target.closest("[data-surface-mode]");
    if (surfaceButton) {
      applySurfaceMode(surfaceButton.dataset.surfaceMode, true);
      return;
    }
    const deleteBackgroundButton = event.target.closest("[data-bg-delete]");
    if (deleteBackgroundButton) {
      const backgroundId = deleteBackgroundButton.dataset.bgDelete;
      setThemeStatus(t("theme.deleting"));
      try {
        const response = await fetch(`/api/theme/backgrounds/${encodeURIComponent(backgroundId)}`, {method: "DELETE"});
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body.detail || "delete failed");
        }
        applyThemeConfig(body);
        if (!selectedBackground()) {
          applyBackgroundMode("gradient", true);
        }
        setThemeStatus(t("theme.deleted"));
      } catch {
        setThemeStatus(t("theme.delete_failed"));
      }
      return;
    }
    const backgroundButton = event.target.closest("[data-bg-id]");
    if (backgroundButton) {
      const backgroundId = backgroundButton.dataset.bgId;
      setThemeStatus(t("theme.selecting"));
      try {
        const response = await fetch(`/api/theme/backgrounds/${encodeURIComponent(backgroundId)}/select`, {method: "POST"});
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body.detail || "select failed");
        }
        const nextBackground = Array.isArray(body.backgrounds) ? body.backgrounds.find((item) => item && item.id === body.selected_background_id) : null;
        await preloadBackground(nextBackground);
        applyThemeConfig(body);
        applyBackgroundMode("image", true);
        setThemeStatus(t("theme.selected"));
      } catch {
        setThemeStatus(t("theme.select_failed"));
      }
      return;
    }
    const clearButton = event.target.closest(".theme-clear-background");
    if (!clearButton) {
      return;
    }
    clearButton.disabled = true;
    setThemeStatus(t("theme.clearing"));
    try {
      const response = await fetch("/api/theme/background", {method: "DELETE"});
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || "clear failed");
      }
      applyThemeConfig(body);
      applyBackgroundMode("gradient", true);
      setThemeStatus(t("theme.cleared"));
    } catch {
      clearButton.disabled = false;
      setThemeStatus(t("theme.clear_failed"));
    }
  });
  controlRoot.addEventListener("keydown", (event) => {
    const deleteBackgroundButton = event.target.closest("[data-bg-delete]");
    if (!deleteBackgroundButton || !["Enter", " "].includes(event.key)) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    deleteBackgroundButton.click();
  });
  const uploadInput = controlRoot.querySelector(".theme-upload-button input");
  if (uploadInput) {
    uploadInput.addEventListener("change", async () => {
      const file = uploadInput.files && uploadInput.files[0];
      if (!file) {
        return;
      }
      if (file.size > 8 * 1024 * 1024) {
        setThemeStatus(t("theme.image_too_large"));
        uploadInput.value = "";
        return;
      }
      setThemeStatus(t("theme.uploading"));
      try {
        const response = await fetch("/api/theme/background", {
          method: "POST",
          headers: {"Content-Type": file.type || "application/octet-stream"},
          body: await file.arrayBuffer(),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body.detail || "upload failed");
        }
        const nextBackground = Array.isArray(body.backgrounds) ? body.backgrounds.find((item) => item && item.id === body.selected_background_id) : null;
        await preloadBackground(nextBackground);
        applyThemeConfig(body);
        applyBackgroundMode("image", true);
        setThemeStatus(t("theme.uploaded"));
      } catch {
        setThemeStatus(t("theme.upload_failed"));
      } finally {
        uploadInput.value = "";
      }
    });
  }
  window.StarAgentTheme = {applyTheme, applyBackgroundMode, applySurfaceMode, ensureThemeConfig};
})();

(() => {
  const sidebar = document.querySelector(".node-workspace-sidebar");
  const toggle = document.querySelector(".node-workspace-toggle");
  const closeButton = document.querySelector(".node-workspace-close");
  const overlay = document.querySelector(".node-workspace-overlay");
  const resizeHandle = document.querySelector(".node-workspace-resize-handle");
  if (!sidebar || !toggle || !closeButton || !overlay) {
    return;
  }

  const mobileQuery = window.matchMedia("(max-width: 820px)");
  const sidebarWidthKey = "staragent.nodeSidebarWidth";
  const defaultSidebarWidth = 248;
  const minSidebarWidth = 220;
  const maxSidebarWidth = 320;
  const clampSidebarWidth = (value) => Math.max(minSidebarWidth, Math.min(maxSidebarWidth, Math.round(value)));
  let sidebarWidth = clampSidebarWidth(sidebar.getBoundingClientRect().width || defaultSidebarWidth);
  let hasCustomSidebarWidth = false;
  const applySidebarWidth = (value, {persist = false} = {}) => {
    sidebarWidth = clampSidebarWidth(value);
    document.documentElement.style.setProperty("--node-workspace-width", `${sidebarWidth}px`);
    if (resizeHandle) {
      resizeHandle.setAttribute("aria-valuenow", String(sidebarWidth));
    }
    if (persist) {
      hasCustomSidebarWidth = true;
      try {
        localStorage.setItem(sidebarWidthKey, String(sidebarWidth));
      } catch {
        // Keep resizing functional when storage is unavailable.
      }
    }
  };
  try {
    const savedWidth = Number(localStorage.getItem(sidebarWidthKey));
    if (Number.isFinite(savedWidth) && savedWidth > 0) {
      hasCustomSidebarWidth = true;
      applySidebarWidth(savedWidth);
    }
  } catch {
    // Use the CSS default when storage is unavailable.
  }
  if (resizeHandle) {
    resizeHandle.setAttribute("aria-valuenow", String(sidebarWidth));
  }

  const resetSidebarWidth = () => {
    hasCustomSidebarWidth = false;
    document.documentElement.style.removeProperty("--node-workspace-width");
    try {
      localStorage.removeItem(sidebarWidthKey);
    } catch {
      // The responsive CSS width still resets without storage access.
    }
    requestAnimationFrame(() => {
      sidebarWidth = clampSidebarWidth(sidebar.getBoundingClientRect().width || defaultSidebarWidth);
      if (resizeHandle) {
        resizeHandle.setAttribute("aria-valuenow", String(sidebarWidth));
      }
    });
  };

  if ("ResizeObserver" in window) {
    const sidebarResizeObserver = new ResizeObserver(() => {
      if (hasCustomSidebarWidth || document.body.classList.contains("is-node-workspace-resizing")) {
        return;
      }
      sidebarWidth = clampSidebarWidth(sidebar.getBoundingClientRect().width || defaultSidebarWidth);
      if (resizeHandle) {
        resizeHandle.setAttribute("aria-valuenow", String(sidebarWidth));
      }
    });
    sidebarResizeObserver.observe(sidebar);
  }

  if (resizeHandle) {
    let resizeStartX = 0;
    let resizeStartWidth = sidebarWidth;
    let activePointerId = null;
    const finishResize = () => {
      if (activePointerId === null) {
        return;
      }
      activePointerId = null;
      document.body.classList.remove("is-node-workspace-resizing");
      applySidebarWidth(sidebarWidth, {persist: true});
    };
    resizeHandle.addEventListener("pointerdown", (event) => {
      if (mobileQuery.matches || event.button !== 0) {
        return;
      }
      event.preventDefault();
      activePointerId = event.pointerId;
      resizeStartX = event.clientX;
      resizeStartWidth = sidebar.getBoundingClientRect().width;
      resizeHandle.setPointerCapture(event.pointerId);
      document.body.classList.add("is-node-workspace-resizing");
    });
    resizeHandle.addEventListener("pointermove", (event) => {
      if (event.pointerId !== activePointerId) {
        return;
      }
      applySidebarWidth(resizeStartWidth + event.clientX - resizeStartX);
    });
    resizeHandle.addEventListener("pointerup", finishResize);
    resizeHandle.addEventListener("pointercancel", finishResize);
    resizeHandle.addEventListener("lostpointercapture", finishResize);
    resizeHandle.addEventListener("dblclick", resetSidebarWidth);
    resizeHandle.addEventListener("keydown", (event) => {
      let nextWidth = sidebarWidth;
      if (event.key === "ArrowLeft") {
        nextWidth -= 12;
      } else if (event.key === "ArrowRight") {
        nextWidth += 12;
      } else if (event.key === "Home") {
        nextWidth = minSidebarWidth;
      } else if (event.key === "End") {
        nextWidth = maxSidebarWidth;
      } else {
        return;
      }
      event.preventDefault();
      applySidebarWidth(nextWidth, {persist: true});
    });
  }

  const setOpen = (open, {restoreFocus = false} = {}) => {
    const nextOpen = mobileQuery.matches && open;
    document.body.classList.toggle("is-node-workspace-open", nextOpen);
    toggle.setAttribute("aria-expanded", String(nextOpen));
    sidebar.setAttribute("aria-hidden", String(mobileQuery.matches && !nextOpen));
    if (nextOpen) {
      closeButton.focus({preventScroll: true});
    } else if (restoreFocus) {
      toggle.focus({preventScroll: true});
    }
  };

  toggle.addEventListener("click", () => {
    setOpen(!document.body.classList.contains("is-node-workspace-open"));
  });
  closeButton.addEventListener("click", () => setOpen(false, {restoreFocus: true}));
  overlay.addEventListener("click", () => setOpen(false, {restoreFocus: true}));
  sidebar.addEventListener("click", (event) => {
    if (event.target.closest("a") && mobileQuery.matches) {
      setOpen(false);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("is-node-workspace-open")) {
      setOpen(false, {restoreFocus: true});
    }
  });
  mobileQuery.addEventListener("change", () => setOpen(false));
  setOpen(false);
})();
