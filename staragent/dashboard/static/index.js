for (const button of document.querySelectorAll(".stop-session")) {
  button.addEventListener("click", async () => {
    const name = button.dataset.session;
    const node = button.dataset.node;
    if (!confirm(`Stop tmux session "${name}"?`)) {
      return;
    }
    button.disabled = true;
    button.textContent = "Stopping";
    const response = await fetch(`/api/nodes/${encodeURIComponent(node)}/sessions/${encodeURIComponent(name)}`, {method: "DELETE"});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      alert(body.detail || "Stop failed.");
      button.disabled = false;
      button.textContent = "Stop";
      return;
    }
    window.location.reload();
  });
}

const workerForm = document.querySelector(".worker-form");
if (workerForm) {
  const status = workerForm.querySelector(".worker-status");
  const cwdInput = workerForm.querySelector('input[name="cwd"]');
  const nodeSelect = workerForm.querySelector('select[name="node"]');
  const commandInput = workerForm.querySelector('input[name="command"]');
  const presetSelect = workerForm.querySelector('select[name="preset"]');
  const explorer = workerForm.querySelector(".explorer");

  presetSelect.addEventListener("change", () => {
    if (presetSelect.value) {
      commandInput.value = presetSelect.value;
    }
    commandInput.focus();
  });
  commandInput.addEventListener("input", () => {
    if (commandInput.value !== presetSelect.value) {
      presetSelect.value = "";
    }
  });

  const workerExplorer = window.StarAgentExplorer.mount(explorer, {
    node: () => nodeSelect.value,
    getPath: () => cwdInput.value,
    setPath: (path) => {
      cwdInput.value = path;
    },
    includeFiles: false,
    allowCreateDirectory: true,
    loadingText: "Loading folders...",
    emptyText: "No matching folders.",
  });

  cwdInput.addEventListener("change", () => workerExplorer.load(cwdInput.value));
    nodeSelect.addEventListener("change", () => {
      cwdInput.value = "";
      workerExplorer.load("");
    });
  window.StarAgentAfterPaint(() => workerExplorer.load(cwdInput.value));

  workerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(workerForm);
    const payload = {
      name: String(form.get("name") || "").trim(),
      node: String(form.get("node") || "local").trim(),
      cwd: String(form.get("cwd") || "").trim(),
      command: String(form.get("command") || "").trim()
    };
    if (!payload.name || !payload.cwd || !payload.command) {
      status.textContent = "Name, working directory, and command are required.";
      return;
    }
    status.textContent = "Creating...";
    const response = await fetch("/api/workers", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      status.textContent = body.detail || "Start failed.";
      return;
    }
    status.textContent = "Created.";
    setTimeout(() => window.location.reload(), 500);
  });
}

const adoptForm = document.querySelector(".adopt-form");
if (adoptForm) {
  let initialAdoptScanDone = false;
  const status = adoptForm.querySelector(".adopt-status");
  const sessionSelect = adoptForm.querySelector('select[name="name"]');
  const nodeSelect = adoptForm.querySelector('select[name="node"]');
  const list = adoptForm.querySelector(".adopt-list");
  const scanButton = adoptForm.querySelector(".adopt-scan");

  async function scanAdoptableSessions() {
    initialAdoptScanDone = true;
    status.textContent = "Scanning...";
    sessionSelect.innerHTML = '<option value="">Scanning...</option>';
    list.innerHTML = "";
    const response = await fetch(`/api/adoptable-sessions?node=${encodeURIComponent(nodeSelect.value)}`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      status.textContent = body.detail || "Scan failed.";
      sessionSelect.innerHTML = '<option value="">Scan failed</option>';
      return;
    }
    const data = await response.json();
    const sessions = data.sessions || [];
    sessionSelect.innerHTML = "";
    if (!sessions.length) {
      sessionSelect.innerHTML = '<option value="">No CLI tmux sessions</option>';
      list.innerHTML = '<div class="explorer-empty">No adoptable Codex, Claude, Gemini, or OpenCode tmux sessions found.</div>';
      status.textContent = "No sessions found.";
      return;
    }
    for (const item of sessions) {
      const option = document.createElement("option");
      option.value = item.name;
      option.textContent = `${item.name} · ${item.cli} · ${item.cwd || "-"}`;
      sessionSelect.appendChild(option);

      const row = document.createElement("button");
      row.type = "button";
      row.className = "directory-row";
      row.innerHTML = `<span>${item.cli}</span><strong>${item.name}</strong><small>${item.cwd || ""}</small>`;
      row.addEventListener("click", () => {
        sessionSelect.value = item.name;
      });
      list.appendChild(row);
    }
    status.textContent = `${sessions.length} session(s) found.`;
  }

  scanButton.addEventListener("click", scanAdoptableSessions);
  nodeSelect.addEventListener("change", scanAdoptableSessions);
  adoptForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const payload = {
      node: nodeSelect.value,
      name: sessionSelect.value
    };
    if (!payload.name) {
      status.textContent = "Choose a tmux session first.";
      return;
    }
    status.textContent = "Adopting...";
    const response = await fetch("/api/adopt", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      status.textContent = body.detail || "Adopt failed.";
      return;
    }
    status.textContent = "Adopted.";
    setTimeout(() => window.location.reload(), 500);
  });
  window.StarAgentAfterPaint(() => {
    if (!initialAdoptScanDone) {
      scanAdoptableSessions();
    }
  });
}
