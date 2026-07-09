(() => {
  const settings = window.StarAgentThemeSettings;
  const themeButton = document.querySelector(".brand-theme-button");
  const menu = document.querySelector(".theme-menu");
  if (!settings || !themeButton || !menu) {
    return;
  }

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
    for (const option of menu.querySelectorAll(".theme-option")) {
      option.classList.toggle("is-active", option.dataset.theme === theme);
    }
    if (persist) {
      localStorage.setItem(storageKey, theme);
      localStorage.removeItem(legacyKey);
    }
    window.dispatchEvent(new CustomEvent("staragent:themechange", {detail: {theme}}));
  };
  const setThemeStatus = (message) => {
    const status = menu.querySelector(".theme-upload-status");
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
    if (mode === "image" && !selectedBackground()) {
      setThemeStatus("Upload or select a background image first.");
      return;
    }
    document.documentElement.dataset.bgMode = mode;
    for (const button of menu.querySelectorAll("[data-bg-mode]")) {
      button.classList.toggle("is-active", button.dataset.bgMode === mode);
    }
    if (persist) {
      localStorage.setItem(backgroundModeKey, mode);
      localStorage.removeItem(backgroundImageKey);
    }
  };
  const renderBackgroundLibrary = () => {
    const library = menu.querySelector(".theme-background-library");
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
      item.setAttribute("aria-label", "Select background image");
      item.classList.toggle("is-active", background.id === selectedBackgroundId);

      const deleteButton = document.createElement("span");
      deleteButton.className = "theme-background-delete";
      deleteButton.dataset.bgDelete = background.id;
      deleteButton.setAttribute("role", "button");
      deleteButton.setAttribute("aria-label", "Delete background image");
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
    const clearButton = menu.querySelector(".theme-clear-background");
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
    for (const button of menu.querySelectorAll("[data-surface-mode]")) {
      button.classList.toggle("is-active", button.dataset.surfaceMode === mode);
    }
    if (persist) {
      setThemeStatus(`Surface: ${mode === "clear-glass" ? "Clear" : mode === "glass" ? "Glass" : "Solid"}`);
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
      setThemeStatus("Theme settings unavailable.");
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

  const closeMenu = () => {
    menu.hidden = true;
    themeButton.setAttribute("aria-expanded", "false");
  };
  const openMenu = () => {
    menu.hidden = false;
    themeButton.setAttribute("aria-expanded", "true");
    ensureThemeConfig();
  };

  themeButton.addEventListener("click", (event) => {
    event.stopPropagation();
    menu.hidden ? openMenu() : closeMenu();
  });
  menu.addEventListener("click", async (event) => {
    event.stopPropagation();
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
      setThemeStatus("Deleting background...");
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
        setThemeStatus("Background deleted.");
      } catch (error) {
        setThemeStatus(error.message || "Failed to delete background.");
      }
      return;
    }
    const backgroundButton = event.target.closest("[data-bg-id]");
    if (backgroundButton) {
      const backgroundId = backgroundButton.dataset.bgId;
      setThemeStatus("Selecting background...");
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
        setThemeStatus("Background selected.");
      } catch (error) {
        setThemeStatus(error.message || "Failed to select background.");
      }
      return;
    }
    const clearButton = event.target.closest(".theme-clear-background");
    if (!clearButton) {
      return;
    }
    clearButton.disabled = true;
    setThemeStatus("Clearing background...");
    try {
      const response = await fetch("/api/theme/background", {method: "DELETE"});
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || "clear failed");
      }
      applyThemeConfig(body);
      applyBackgroundMode("gradient", true);
      setThemeStatus("Background cleared.");
    } catch (error) {
      clearButton.disabled = false;
      setThemeStatus(error.message || "Failed to clear background.");
    }
  });
  const uploadInput = menu.querySelector(".theme-upload-button input");
  if (uploadInput) {
    uploadInput.addEventListener("change", async () => {
      const file = uploadInput.files && uploadInput.files[0];
      if (!file) {
        return;
      }
      if (file.size > 8 * 1024 * 1024) {
        setThemeStatus("Image must be 8 MiB or smaller.");
        uploadInput.value = "";
        return;
      }
      setThemeStatus("Uploading background...");
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
        setThemeStatus("Background uploaded.");
      } catch (error) {
        setThemeStatus(error.message || "Failed to upload background.");
      } finally {
        uploadInput.value = "";
      }
    });
  }
  for (const button of menu.querySelectorAll("[data-surface-mode]")) {
    const applyFromButton = (event) => {
      event.preventDefault();
      event.stopPropagation();
      applySurfaceMode(button.dataset.surfaceMode, true);
    };
    button.addEventListener("pointerdown", applyFromButton);
    button.addEventListener("click", applyFromButton);
  }
  document.addEventListener("click", closeMenu);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeMenu();
    }
  });
})();
