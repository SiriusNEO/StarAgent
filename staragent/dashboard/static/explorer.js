(() => {
  function optionValue(value, fallback = "") {
    return typeof value === "function" ? value() : (value ?? fallback);
  }

  function createCrumb(label, path, load) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.title = path;
    button.addEventListener("click", () => load(path));
    return button;
  }

  function renderBreadcrumb(pathEl, path, load) {
    pathEl.innerHTML = "";
    const parts = String(path || "/").split("/").filter(Boolean);
    pathEl.appendChild(createCrumb("/", "/", load));
    let current = "";
    for (const part of parts) {
      current += `/${part}`;
      const separator = document.createElement("span");
      separator.className = "breadcrumb-separator";
      separator.textContent = "/";
      pathEl.append(separator, createCrumb(part, current, load));
    }
  }

  function renderRoots(rootsEl, roots, activePath, load) {
    rootsEl.innerHTML = "";
    for (const root of roots) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = root.label;
      button.title = root.path;
      button.className = root.path === activePath ? "is-active" : "";
      button.addEventListener("click", () => load(root.path));
      rootsEl.appendChild(button);
    }
  }

  function createEntry(entry, options, load, selectedPath) {
    const type = entry.type || "directory";
    const isParent = Boolean(entry.parent);
    const button = document.createElement("button");
    button.type = "button";
    button.className = isParent
      ? "directory-row parent-row"
      : `directory-row ${type === "file" ? "file-row" : ""}`;
    if (selectedPath && entry.path === selectedPath) {
      button.classList.add("is-selected");
    }

    const icon = document.createElement("span");
    icon.className = type === "file" ? "file-icon" : "directory-icon";
    icon.textContent = isParent ? ".." : "";

    const text = document.createElement("div");
    text.className = "explorer-entry-text";
    const name = document.createElement("strong");
    name.className = "explorer-entry-name";
    name.textContent = entry.name;
    const detail = document.createElement("small");
    detail.className = "explorer-entry-detail";
    detail.textContent = entry.path;
    button.title = entry.path;
    text.append(name, detail);
    button.append(icon, text);

    if (type === "file") {
      button.addEventListener("click", () => options.onFileSelect?.(entry.path, entry));
    } else {
      button.addEventListener("click", () => {
        options.onDirectorySelect?.(entry.path, entry);
        load(entry.path);
      });
    }
    return button;
  }

  function mountExplorer(root, options = {}) {
    const listEl = root.querySelector(".explorer-list");
    const pathEl = root.querySelector(".explorer-path");
    const rootsEl = root.querySelector(".explorer-roots");
    const filterEl = root.querySelector(".explorer-filter");
    const refreshEl = root.querySelector(".explorer-refresh");
    const newFolderEl = root.querySelector(".explorer-new-folder");
    const selectedEl = root.querySelector(".explorer-selected");
    let data = null;
    let loadingPath = "";

    async function load(path, selectedPath = optionValue(options.selectedPath)) {
      const targetPath = path || optionValue(options.getPath, root.dataset.path || "/");
      loadingPath = targetPath;
      listEl.innerHTML = `<div class="explorer-empty">${options.loadingText || "Loading..."}</div>`;
      const node = optionValue(options.node, "local");
      const includeFiles = options.includeFiles ? "&include_files=true" : "";
      const rootPath = optionValue(options.rootPath, "");
      const rootQuery = rootPath ? `&root=${encodeURIComponent(rootPath)}` : "";
      const response = await fetch(
        `/api/directories?node=${encodeURIComponent(node)}${includeFiles}${rootQuery}&path=${encodeURIComponent(targetPath)}`,
      );
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        listEl.innerHTML = `<div class="explorer-empty">${body.detail || options.errorText || "Could not load directory."}</div>`;
        return;
      }
      const nextData = await response.json();
      if (loadingPath !== targetPath) {
        return;
      }
      data = nextData;
      options.setPath?.(data.path);
      root.dataset.path = data.path;
      if (selectedEl) {
        selectedEl.textContent = data.path;
      }
      renderBreadcrumb(pathEl, data.path, load);
      renderRoots(rootsEl, data.roots || [], data.path, load);
      renderEntries(selectedPath);
    }

    function renderEntries(selectedPath = optionValue(options.selectedPath)) {
      if (!data) {
        return;
      }
      const filter = (filterEl.value || "").trim().toLowerCase();
      const entries = (data.entries || [])
        .filter((entry) => options.includeFiles || (entry.type || "directory") === "directory")
        .filter((entry) => entry.name.toLowerCase().includes(filter));
      listEl.innerHTML = "";
      if (data.parent) {
        listEl.appendChild(
          createEntry(
            {name: "Parent directory", path: data.parent, type: "directory", parent: true},
            options,
            load,
            selectedPath,
          ),
        );
      }
      for (const entry of entries) {
        listEl.appendChild(createEntry(entry, options, load, selectedPath));
      }
      if (!entries.length) {
        const empty = document.createElement("div");
        empty.className = "explorer-empty";
        empty.textContent = options.emptyText || "No matching entries.";
        listEl.appendChild(empty);
      }
    }

    filterEl.addEventListener("input", () => renderEntries());
    refreshEl.addEventListener("click", () => load(optionValue(options.getPath, root.dataset.path)));
    if (newFolderEl && options.allowCreateDirectory) {
      newFolderEl.addEventListener("click", async () => {
        if (!data) {
          return;
        }
        const name = window.prompt("New folder name");
        if (!name) {
          return;
        }
        newFolderEl.disabled = true;
        try {
          const node = optionValue(options.node, "local");
          const rootPath = optionValue(options.rootPath, "");
          const rootQuery = rootPath ? `&root=${encodeURIComponent(rootPath)}` : "";
          const response = await fetch(`/api/directories?node=${encodeURIComponent(node)}${rootQuery}`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({path: data.path, name}),
          });
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            window.alert(body.detail || "Could not create folder.");
            return;
          }
          const body = await response.json();
          await load(body.path || data.path);
        } finally {
          newFolderEl.disabled = false;
        }
      });
    }

    return {
      load,
      render: renderEntries,
      get data() {
        return data;
      },
    };
  }

  window.StarAgentExplorer = {mount: mountExplorer};
})();
