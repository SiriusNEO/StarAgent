(() => {
  const root = document.querySelector("[data-agent-skills]");
  if (!root) {
    return;
  }

  const translate = (key, values = {}) => window.StarAgentI18n?.t(key, values) || key;
  const node = root.dataset.node || "local";
  const agent = root.dataset.agent || "";
  const list = root.querySelector(".agent-skills-list");
  const empty = root.querySelector(".agent-skills-empty");
  const summary = root.querySelector(".agent-skills-summary");
  const refresh = root.querySelector(".agent-skills-refresh");
  const filters = Array.from(root.querySelectorAll("[data-skill-filter]"));
  const search = root.querySelector(".agent-skills-search input");
  const roots = root.querySelector(".agent-skills-roots");
  let payload = null;
  let activeFilter = "all";
  let scanStarted = false;

  const localizedTime = (value) => {
    const date = new Date(value || "");
    return Number.isNaN(date.getTime())
      ? ""
      : date.toLocaleString(window.StarAgentI18n?.language || []);
  };

  const scopeLabel = (skill) => {
    if (skill.scope === "bundled") {
      return translate("agents.skills_bundled");
    }
    if (skill.scope === "plugin") {
      return translate("agents.skills_plugins");
    }
    return translate("agents.skills_personal");
  };

  const skillMatches = (skill) => {
    if (activeFilter !== "all" && skill.scope !== activeFilter) {
      return false;
    }
    const query = String(search?.value || "").trim().toLocaleLowerCase();
    if (!query) {
      return true;
    }
    return [skill.name, skill.description, skill.source, skill.plugin]
      .some((value) => String(value || "").toLocaleLowerCase().includes(query));
  };

  const renderSkill = (skill) => {
    const item = document.createElement("article");
    item.className = `agent-skill-row is-${skill.scope || "personal"}`;

    const mark = document.createElement("span");
    mark.className = "agent-skill-mark";
    mark.setAttribute("aria-hidden", "true");
    const initial = String(skill.name || "S").trim().charAt(0).toUpperCase();
    mark.textContent = initial || "S";

    const copy = document.createElement("div");
    copy.className = "agent-skill-copy";
    const titleLine = document.createElement("div");
    titleLine.className = "agent-skill-title";
    const title = document.createElement("strong");
    title.textContent = skill.name || "—";
    titleLine.appendChild(title);
    if (!skill.valid) {
      const warning = document.createElement("span");
      warning.className = "agent-skill-warning";
      warning.textContent = translate("agents.skills_invalid");
      titleLine.appendChild(warning);
    }
    const description = document.createElement("p");
    description.textContent = skill.description || translate("agents.skills_invalid");
    copy.append(titleLine, description);

    const metadata = document.createElement("div");
    metadata.className = "agent-skill-meta";
    const scope = document.createElement("span");
    scope.className = `agent-skill-scope is-${skill.scope || "personal"}`;
    scope.textContent = scopeLabel(skill);
    const source = document.createElement("span");
    source.className = "agent-skill-source";
    source.textContent = skill.plugin || skill.source || "—";
    metadata.append(scope, source);
    if (skill.kind === "compatible") {
      const compatible = document.createElement("span");
      compatible.className = "agent-skill-compatible";
      compatible.textContent = translate("agents.skills_compatible");
      metadata.appendChild(compatible);
    }
    item.append(mark, copy, metadata);
    return item;
  };

  const renderRoots = (items) => {
    roots.hidden = !items.length;
    const container = roots.querySelector("div");
    container.replaceChildren();
    for (const item of items) {
      const source = document.createElement("span");
      source.classList.toggle("is-missing", !item.exists);
      source.title = item.exists
        ? translate("agents.skills_count", {count: Number(item.count || 0)})
        : translate("common.missing");
      const label = document.createElement("strong");
      const directory = document.createElement("code");
      label.textContent = item.plugin || item.source || "—";
      directory.textContent = item.directory || "—";
      source.append(label, directory);
      container.appendChild(source);
    }
  };

  const render = () => {
    if (!payload) {
      return;
    }
    const skills = Array.isArray(payload.skills) ? payload.skills : [];
    const visible = skills.filter(skillMatches);
    list.replaceChildren(...visible.map(renderSkill));
    list.setAttribute("aria-busy", "false");
    empty.hidden = visible.length > 0;
    empty.textContent = skills.length
      ? translate("agents.skills_no_match")
      : translate("agents.skills_empty");

    const counts = payload.counts || {};
    for (const name of ["total", "bundled", "personal", "plugin"]) {
      const target = root.querySelector(`[data-skills-count="${name}"]`);
      if (target) {
        target.textContent = String(Number(counts[name] || 0));
      }
    }
    const rootItems = Array.isArray(payload.roots) ? payload.roots : [];
    const activeSources = rootItems.filter((item) => item.exists).length;
    if (!payload.supported) {
      summary.textContent = payload.error || translate("agents.skills_unavailable");
      root.dataset.state = "error";
    } else if (payload.error) {
      summary.textContent = payload.error;
      root.dataset.state = "error";
    } else {
      const found = translate("agents.skills_found", {
        count: Number(counts.total || 0),
        sources: activeSources,
      });
      const checked = localizedTime(payload.checked_at);
      summary.textContent = checked
        ? `${found} · ${translate("agents.skills_checked", {time: checked})}`
        : found;
      root.dataset.state = "ready";
    }
    if (payload.truncated) {
      const notice = document.createElement("span");
      notice.className = "agent-skills-truncated";
      notice.textContent = translate("agents.skills_truncated");
      summary.appendChild(notice);
    }
    renderRoots(rootItems);
  };

  const load = async (force = false) => {
    scanStarted = true;
    refresh.disabled = true;
    list.setAttribute("aria-busy", "true");
    summary.textContent = translate("agents.skills_scanning");
    try {
      const query = force ? "?refresh=true" : "";
      const response = await fetch(
        `/api/nodes/${encodeURIComponent(node)}/agent-tools/${encodeURIComponent(agent)}/skills${query}`,
        {cache: "no-store"},
      );
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || translate("agents.skills_unavailable"));
      }
      payload = body;
    } catch (error) {
      payload = {
        supported: false,
        error: error?.message || translate("agents.skills_unavailable"),
        roots: [],
        skills: [],
        counts: {},
      };
    } finally {
      refresh.disabled = false;
      render();
    }
  };

  filters.forEach((button) => {
    button.addEventListener("click", () => {
      activeFilter = button.dataset.skillFilter || "all";
      filters.forEach((item) => {
        const active = item === button;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-pressed", active ? "true" : "false");
      });
      render();
    });
  });
  search?.addEventListener("input", render);
  refresh.addEventListener("click", () => load(true));
  window.StarAgentAfterPaint(() => {
    if (!("IntersectionObserver" in window)) {
      load(false);
      return;
    }
    const observer = new IntersectionObserver((entries) => {
      if (scanStarted || !entries.some((entry) => entry.isIntersecting)) {
        if (scanStarted) {
          observer.disconnect();
        }
        return;
      }
      observer.disconnect();
      load(false);
    }, {rootMargin: "320px 0px"});
    observer.observe(root);
  });
})();
