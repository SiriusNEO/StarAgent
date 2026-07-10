(() => {
  const sourceSelect = document.querySelector(".logs-source");
  const levelSelect = document.querySelector(".logs-level");
  const searchInput = document.querySelector(".logs-search");
  const refreshButton = document.querySelector(".logs-refresh");
  const status = document.querySelector(".logs-status");
  const stream = document.querySelector(".logs-stream");
  if (!sourceSelect || !levelSelect || !searchInput || !refreshButton || !status || !stream) {
    return;
  }

  const initialSource = new URLSearchParams(window.location.search).get("source");
  if (initialSource && Array.from(sourceSelect.options).some((option) => option.value === initialSource)) {
    sourceSelect.value = initialSource;
  }

  let controller = null;
  let searchTimer = null;

  const formatTime = (value) => {
    const parsed = new Date(value || "");
    if (Number.isNaN(parsed.getTime())) {
      return value || "Unknown time";
    }
    return parsed.toLocaleString([], {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  };

  const renderDetails = (details) => {
    if (!details || typeof details !== "object" || !Object.keys(details).length) {
      return null;
    }
    const disclosure = document.createElement("details");
    disclosure.className = "log-details";
    const summary = document.createElement("summary");
    summary.textContent = "Details";
    const content = document.createElement("pre");
    content.textContent = JSON.stringify(details, null, 2);
    disclosure.append(summary, content);
    return disclosure;
  };

  const renderEvents = (events) => {
    stream.replaceChildren();
    if (!events.length) {
      const empty = document.createElement("div");
      empty.className = "logs-empty";
      empty.textContent = "No matching log events.";
      stream.append(empty);
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const event of events) {
      const article = document.createElement("article");
      const level = String(event.level || "info").toLowerCase();
      article.className = `log-entry log-level-${level}`;

      const header = document.createElement("div");
      header.className = "log-entry-head";
      const time = document.createElement("time");
      time.dateTime = event.timestamp || "";
      time.textContent = formatTime(event.timestamp);
      const badge = document.createElement("span");
      badge.className = `log-level-badge log-level-${level}`;
      badge.textContent = level;
      const source = document.createElement("span");
      source.className = "log-source-name";
      source.textContent = event.source || "unknown";
      const eventName = document.createElement("code");
      eventName.className = "log-event-name";
      eventName.textContent = event.event || "log";
      header.append(time, badge, source, eventName);

      const message = document.createElement("p");
      message.className = "log-message";
      message.textContent = event.message || "";
      article.append(header, message);
      const details = renderDetails(event.details);
      if (details) {
        article.append(details);
      }
      fragment.append(article);
    }
    stream.append(fragment);
  };

  const updateLocation = () => {
    const url = new URL(window.location.href);
    if (sourceSelect.value === "hub") {
      url.searchParams.delete("source");
    } else {
      url.searchParams.set("source", sourceSelect.value);
    }
    window.history.replaceState(null, "", `${url.pathname}${url.search}`);
  };

  const loadLogs = async () => {
    if (controller) {
      controller.abort();
    }
    const currentController = new AbortController();
    controller = currentController;
    refreshButton.disabled = true;
    status.textContent = "Loading…";
    stream.setAttribute("aria-busy", "true");
    const params = new URLSearchParams({source: sourceSelect.value, limit: "200"});
    if (levelSelect.value) {
      params.set("level", levelSelect.value);
    }
    if (searchInput.value.trim()) {
      params.set("q", searchInput.value.trim());
    }
    try {
      const response = await fetch(`/api/logs?${params}`, {signal: currentController.signal});
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || "Failed to load logs");
      }
      const events = Array.isArray(body.events) ? body.events : [];
      renderEvents(events);
      status.textContent = `${events.length} event${events.length === 1 ? "" : "s"} · updated ${formatTime(body.generated_at)}`;
    } catch (error) {
      if (error.name !== "AbortError") {
        status.textContent = error.message || "Logs unavailable.";
        renderEvents([]);
      }
    } finally {
      if (controller === currentController) {
        refreshButton.disabled = false;
        stream.setAttribute("aria-busy", "false");
      }
    }
  };

  sourceSelect.addEventListener("change", () => {
    updateLocation();
    loadLogs();
  });
  levelSelect.addEventListener("change", loadLogs);
  searchInput.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(loadLogs, 300);
  });
  refreshButton.addEventListener("click", loadLogs);
  window.setInterval(() => {
    if (!document.hidden) {
      loadLogs();
    }
  }, 10_000);
  window.StarAgentAfterPaint(loadLogs);
})();
