const sessionAssets = document.querySelector(".session-assets");
const loadScriptAsset = (url) => new Promise((resolve, reject) => {
  const existing = document.querySelector(`script[data-staragent-src="${CSS.escape(url)}"]`);
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
const loadStyleAsset = (url) => new Promise((resolve) => {
  const existing = Array.from(document.styleSheets).find((sheet) => sheet.href === new URL(url, window.location.href).href);
  if (existing) {
    resolve();
    return;
  }
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = url;
  link.addEventListener("load", resolve, {once: true});
  link.addEventListener("error", resolve, {once: true});
  document.head.appendChild(link);
});
let terminalAssetsPromise = null;
const ensureTerminalAssets = () => {
  if (window.Terminal && window.FitAddon) {
    return Promise.resolve();
  }
  if (!terminalAssetsPromise) {
    terminalAssetsPromise = loadScriptAsset(sessionAssets.dataset.xtermJs)
      .then(() => loadScriptAsset(sessionAssets.dataset.xtermFitJs));
  }
  return terminalAssetsPromise;
};
let highlightAssetsPromise = null;
const ensureHighlightAssets = () => {
  if (window.hljs) {
    return Promise.resolve();
  }
  if (!highlightAssetsPromise) {
    highlightAssetsPromise = Promise.all([
      loadStyleAsset(sessionAssets.dataset.highlightCss),
      loadScriptAsset(sessionAssets.dataset.highlightJs),
    ]);
  }
  return highlightAssetsPromise;
};

const sessionSwitcher = document.querySelector(".session-switcher");
if (sessionSwitcher) {
  const toggle = sessionSwitcher.querySelector(".session-switcher-toggle");
  const list = sessionSwitcher.querySelector(".session-switcher-list");

  const setOpen = (open) => {
    sessionSwitcher.classList.toggle("is-open", open);
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? "Hide" : "Browse";
  };

  toggle.addEventListener("click", () => {
    setOpen(!sessionSwitcher.classList.contains("is-open"));
  });

  window.StarAgentAfterPaint(() => {
    const current = list.querySelector(".session-switcher-item.is-current");
    if (!current) {
      return;
    }
    const listRect = list.getBoundingClientRect();
    const itemRect = current.getBoundingClientRect();
    list.scrollTop += itemRect.top - listRect.top - (list.clientHeight - itemRect.height) / 2;
  }, 400);
}

for (const button of document.querySelectorAll(".copy-button")) {
  button.addEventListener("click", async () => {
    const text = button.dataset.copy || "";
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
      button.textContent = "Copied";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 1200);
  });
}

const stopButton = document.querySelector(".detail-stop-session");
if (stopButton) {
  stopButton.addEventListener("click", async () => {
    const name = stopButton.dataset.session;
    const node = stopButton.dataset.node;
    if (!confirm(`Stop tmux session "${name}"?`)) {
      return;
    }
    stopButton.disabled = true;
    stopButton.textContent = "Stopping";
    const response = await fetch(`/api/nodes/${encodeURIComponent(node)}/sessions/${encodeURIComponent(name)}`, {method: "DELETE"});
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      alert(body.detail || "Stop failed.");
      stopButton.disabled = false;
      stopButton.textContent = "Stop";
      return;
    }
    window.location.href = "/";
  });
}

const sessionExplorer = document.querySelector(".session-explorer");
if (sessionExplorer) {
  const explorerNode = sessionExplorer.dataset.node;
  const workspaceRoot = sessionExplorer.dataset.path;
  const previewPanel = document.querySelector(".preview-panel");
  const previewMeta = previewPanel?.querySelector(".preview-meta");
  const previewSource = previewPanel?.querySelector(".file-preview");
  const previewBody = previewPanel?.querySelector(".file-preview-code");
  const previewMarkdown = previewPanel?.querySelector(".markdown-preview");
  const previewPdf = previewPanel?.querySelector(".pdf-preview");
  const previewActions = previewPanel?.querySelector(".preview-actions");
  const previewModeButtons = previewPanel ? Array.from(previewPanel.querySelectorAll(".preview-mode-button")) : [];
  let highlightedPath = "";
  let previewMode = "preview";
  let currentMarkdownText = "";
  const workspaceExplorer = window.StarAgentExplorer.mount(sessionExplorer, {
    node: () => explorerNode,
    rootPath: () => workspaceRoot,
    getPath: () => sessionExplorer.dataset.path || workspaceRoot,
    setPath: (path) => {
      sessionExplorer.dataset.path = path;
    },
    includeFiles: true,
    loadingText: "Loading workspace...",
    emptyText: "No matching files.",
    selectedPath: () => highlightedPath,
    onFileSelect: (path) => {
      highlightedPath = path;
      workspaceExplorer.render(highlightedPath);
      previewFile(path);
    },
  });

  async function previewFile(path) {
    if (!previewBody || !previewMeta || !previewSource) {
      return;
    }
    currentMarkdownText = "";
    setMarkdownControls(false);
    previewSource.hidden = false;
    if (previewMarkdown) {
      previewMarkdown.hidden = true;
      previewMarkdown.innerHTML = "";
    }
    if (previewPdf) {
      previewPdf.hidden = true;
      previewPdf.removeAttribute("src");
    }
    previewMeta.textContent = "Loading";
    previewBody.textContent = "";
    previewBody.className = "file-preview-code";
    if (previewLanguage(path) === "pdf") {
      const name = String(path || "").split("/").pop() || "PDF";
      previewMeta.textContent = name;
      await renderPdfFile(path);
      return;
    }
    const response = await fetch(`/api/files/preview?node=${encodeURIComponent(explorerNode)}&root=${encodeURIComponent(workspaceRoot)}&path=${encodeURIComponent(path)}`);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      previewMeta.textContent = "Error";
      previewBody.textContent = body.detail || "Could not preview file.";
      return;
    }
    const body = await response.json();
    if (highlightedPath !== path) {
      return;
    }
    previewMeta.textContent = `${body.name || "file"} · ${formatBytes(Number(body.size || 0))}`;
    if (body.error) {
      previewBody.textContent = body.error;
      return;
    }
    const language = previewLanguage(body.name || path);
    if (language === "markdown") {
      await renderMarkdownFile(body.text || "", path);
      return;
    }
    if (language === "pdf") {
      await renderPdfFile(path);
      return;
    }
    await renderHighlightedPreview(body.text || "", language, path);
  }

  async function renderHighlightedPreview(text, language, path) {
    currentMarkdownText = "";
    setMarkdownControls(false);
    if (previewSource) {
      previewSource.hidden = false;
    }
    if (previewMarkdown) {
      previewMarkdown.hidden = true;
      previewMarkdown.innerHTML = "";
    }
    if (previewPdf) {
      previewPdf.hidden = true;
      previewPdf.removeAttribute("src");
    }
    await renderHighlightedSource(text, language, path);
  }

  async function renderHighlightedSource(text, language, path = highlightedPath) {
    previewBody.className = language ? `file-preview-code hljs language-${language}` : "file-preview-code hljs";
    const local = localHighlight(text, language);
    if (local) {
      previewBody.innerHTML = local;
      return;
    }
    previewBody.textContent = text;
    try {
      await ensureHighlightAssets();
    } catch {
      return;
    }
    if (highlightedPath !== path || !window.hljs) {
      return;
    }
    try {
      const result = language
        ? window.hljs.highlight(text, {language, ignoreIllegals: true})
        : window.hljs.highlightAuto(text);
      previewBody.innerHTML = result.value;
    } catch {
      previewBody.textContent = text;
    }
  }

  async function renderMarkdownFile(text, path) {
    currentMarkdownText = text;
    previewMode = "preview";
    setMarkdownControls(true);
    await renderHighlightedSource(text, "markdown", path);
    if (highlightedPath !== path) {
      return;
    }
    if (previewPdf) {
      previewPdf.hidden = true;
      previewPdf.removeAttribute("src");
    }
    if (previewMarkdown) {
      previewMarkdown.innerHTML = renderMarkdown(text, path);
      const codeBlocks = Array.from(previewMarkdown.querySelectorAll("pre code"));
      if (codeBlocks.length) {
        try {
          await ensureHighlightAssets();
        } catch {
          // Keep escaped code visible if highlighting assets fail.
        }
      }
      if (highlightedPath !== path) {
        return;
      }
      codeBlocks.forEach((block) => {
        if (window.hljs) {
          try {
            window.hljs.highlightElement(block);
          } catch {
            // Keep escaped code visible if highlighting fails.
          }
        }
      });
    }
    applyPreviewMode();
  }

  async function renderPdfFile(path) {
    currentMarkdownText = "";
    setMarkdownControls(false);
    if (previewSource) {
      previewSource.hidden = true;
    }
    if (previewMarkdown) {
      previewMarkdown.hidden = true;
      previewMarkdown.innerHTML = "";
    }
    if (!previewPdf) {
      previewBody.textContent = "PDF preview is not available.";
      previewSource.hidden = false;
      return;
    }
    const rawUrl = `/api/files/raw?node=${encodeURIComponent(explorerNode)}&root=${encodeURIComponent(workspaceRoot)}&path=${encodeURIComponent(path)}`;
    const infoUrl = `/api/files/raw-info?node=${encodeURIComponent(explorerNode)}&root=${encodeURIComponent(workspaceRoot)}&path=${encodeURIComponent(path)}`;
    const response = await fetch(infoUrl);
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      previewPdf.hidden = true;
      previewPdf.removeAttribute("src");
      previewBody.className = "file-preview-code";
      previewBody.textContent = body.detail || "PDF preview is not available.";
      previewSource.hidden = false;
      previewMeta.textContent = "PDF preview error";
      return;
    }
    const info = await response.json();
    previewMeta.textContent = `${info.name || "PDF"} · ${formatBytes(Number(info.size || 0))}`;
    previewPdf.src = rawUrl;
    previewPdf.hidden = false;
  }

  function setMarkdownControls(enabled) {
    if (previewActions) {
      previewActions.hidden = !enabled;
    }
    if (!enabled) {
      previewModeButtons.forEach((button) => button.classList.toggle("is-active", button.dataset.previewMode === "preview"));
    }
  }

  function applyPreviewMode() {
    previewModeButtons.forEach((button) => button.classList.toggle("is-active", button.dataset.previewMode === previewMode));
    if (!previewSource || !previewMarkdown) {
      return;
    }
    previewSource.hidden = previewMode !== "source";
    previewMarkdown.hidden = previewMode !== "preview";
  }

  previewModeButtons.forEach((button) => {
    button.addEventListener("click", () => {
      previewMode = button.dataset.previewMode || "preview";
      if (currentMarkdownText) {
        applyPreviewMode();
      }
    });
  });

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function escapeAttribute(text) {
    return escapeHtml(text).replace(/"/g, "&quot;");
  }

  function renderMarkdown(text, markdownPath) {
    const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
    const html = [];
    let paragraph = [];
    let listItems = [];
    let quoteLines = [];
    let inFence = false;
    let fenceLanguage = "";
    let fenceLines = [];

    const flushParagraph = () => {
      if (!paragraph.length) return;
      html.push(`<p>${renderInlineMarkdown(paragraph.join(" "), markdownPath)}</p>`);
      paragraph = [];
    };
    const flushList = () => {
      if (!listItems.length) return;
      html.push(`<ul>${listItems.map((item) => `<li>${renderInlineMarkdown(item, markdownPath)}</li>`).join("")}</ul>`);
      listItems = [];
    };
    const flushQuote = () => {
      if (!quoteLines.length) return;
      html.push(`<blockquote>${quoteLines.map((item) => `<p>${renderInlineMarkdown(item, markdownPath)}</p>`).join("")}</blockquote>`);
      quoteLines = [];
    };
    const flushFlow = () => {
      flushParagraph();
      flushList();
      flushQuote();
    };
    const flushFence = () => {
      const lang = fenceLanguage ? ` language-${escapeAttribute(fenceLanguage)}` : "";
      html.push(`<pre><code class="hljs${lang}">${escapeHtml(fenceLines.join("\n"))}</code></pre>`);
      inFence = false;
      fenceLanguage = "";
      fenceLines = [];
    };

    for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
      const rawLine = lines[lineIndex];
      const line = rawLine.replace(/\s+$/, "");
      const fence = line.match(/^```([\w.+-]*)\s*$/);
      if (fence) {
        if (inFence) {
          flushFence();
        } else {
          flushFlow();
          inFence = true;
          fenceLanguage = fence[1] || "";
          fenceLines = [];
        }
        continue;
      }
      if (inFence) {
        fenceLines.push(rawLine);
        continue;
      }

      if (!line.trim()) {
        flushFlow();
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        flushFlow();
        const level = heading[1].length;
        html.push(`<h${level}>${renderInlineMarkdown(heading[2].trim(), markdownPath)}</h${level}>`);
        continue;
      }

      if (/^---+$/.test(line.trim())) {
        flushFlow();
        html.push("<hr>");
        continue;
      }

      if (line.includes("|") && isMarkdownTableDivider(lines[lineIndex + 1] || "")) {
        flushFlow();
        const headers = splitMarkdownTableRow(line);
        const alignments = splitMarkdownTableRow(lines[lineIndex + 1]).map(tableAlignment);
        const rows = [];
        lineIndex += 2;
        while (lineIndex < lines.length && lines[lineIndex].includes("|") && lines[lineIndex].trim()) {
          rows.push(splitMarkdownTableRow(lines[lineIndex]));
          lineIndex += 1;
        }
        lineIndex -= 1;
        html.push(renderMarkdownTable(headers, alignments, rows, markdownPath));
        continue;
      }

      const list = line.match(/^\s*[-*+]\s+(.+)$/);
      if (list) {
        flushParagraph();
        flushQuote();
        listItems.push(list[1].trim());
        continue;
      }

      const quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        flushParagraph();
        flushList();
        quoteLines.push(quote[1].trim());
        continue;
      }

      if (looksLikeHtmlLine(line)) {
        flushFlow();
        html.push(sanitizeHtml(line, markdownPath));
        continue;
      }

      flushList();
      flushQuote();
      paragraph.push(line.trim());
    }

    if (inFence) {
      flushFence();
    }
    flushFlow();
    return html.join("\n") || '<p class="empty">Empty Markdown file.</p>';
  }

  function isMarkdownTableDivider(line) {
    const cells = splitMarkdownTableRow(line);
    return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell.trim()));
  }

  function splitMarkdownTableRow(line) {
    const value = String(line || "").trim().replace(/^\|/, "").replace(/\|$/, "");
    const cells = [];
    let current = "";
    let escaped = false;
    for (const char of value) {
      if (escaped) {
        current += char;
        escaped = false;
        continue;
      }
      if (char === "\\") {
        escaped = true;
        continue;
      }
      if (char === "|") {
        cells.push(current.trim());
        current = "";
        continue;
      }
      current += char;
    }
    cells.push(current.trim());
    return cells;
  }

  function tableAlignment(cell) {
    const value = cell.trim();
    if (value.startsWith(":") && value.endsWith(":")) return "center";
    if (value.endsWith(":")) return "right";
    if (value.startsWith(":")) return "left";
    return "";
  }

  function renderMarkdownTable(headers, alignments, rows, markdownPath) {
    const th = headers.map((cell, index) => {
      const align = alignments[index] ? ` align="${alignments[index]}"` : "";
      return `<th${align}>${renderInlineMarkdown(cell, markdownPath)}</th>`;
    }).join("");
    const bodyRows = rows.map((row) => {
      const cells = headers.map((_, index) => {
        const align = alignments[index] ? ` align="${alignments[index]}"` : "";
        return `<td${align}>${renderInlineMarkdown(row[index] || "", markdownPath)}</td>`;
      }).join("");
      return `<tr>${cells}</tr>`;
    }).join("");
    return `<table><thead><tr>${th}</tr></thead><tbody>${bodyRows}</tbody></table>`;
  }

  function renderInlineMarkdown(text, markdownPath) {
    const placeholders = [];
    const store = (html) => {
      const key = `\u0000${placeholders.length}\u0000`;
      placeholders.push(html);
      return key;
    };
    let protectedText = String(text || "")
      .replace(/`([^`]+)`/g, (_, code) => store(`<code>${escapeHtml(code)}</code>`))
      .replace(/<[^>]+>/g, (tag) => store(sanitizeHtml(tag, markdownPath)))
      .replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_, alt, target) => {
        const parsed = parseMarkdownTarget(target);
        const src = markdownImageSrc(parsed.url, markdownPath);
        if (!src) {
          return escapeHtml(_);
        }
        const title = parsed.title ? ` title="${escapeAttribute(parsed.title)}"` : "";
        return store(`<img src="${escapeAttribute(src)}" alt="${escapeAttribute(alt)}"${title} loading="lazy">`);
      })
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, target) => {
        const parsed = parseMarkdownTarget(target);
        const href = markdownHref(parsed.url, markdownPath);
        if (!href) {
          return escapeHtml(label);
        }
        const title = parsed.title ? ` title="${escapeAttribute(parsed.title)}"` : "";
        return store(`<a href="${escapeAttribute(href)}"${title} target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`);
      })
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    let output = protectedText
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/_([^_]+)_/g, "<em>$1</em>");
    placeholders.forEach((html, index) => {
      output = output.replace(`\u0000${index}\u0000`, html);
    });
    return output;
  }

  function looksLikeHtmlLine(line) {
    return /^\s*<\/?[A-Za-z][^>]*>\s*$/.test(line);
  }

  function sanitizeHtml(html, markdownPath) {
    const allowedTags = new Set([
      "a", "b", "blockquote", "br", "code", "dd", "del", "details", "div", "dl", "dt",
      "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "kbd", "li", "ol",
      "p", "pre", "s", "span", "strong", "sub", "summary", "sup", "table", "tbody",
      "td", "th", "thead", "tr", "ul",
    ]);
    const source = String(html || "");
    const tagPattern = /<\/?\s*([A-Za-z][\w:-]*)([^>]*)>/g;
    let output = "";
    let lastIndex = 0;
    for (const match of source.matchAll(tagPattern)) {
      output += escapeHtml(source.slice(lastIndex, match.index));
      const [raw, tagName, rawAttrs] = match;
      const tag = tagName.toLowerCase();
      if (!allowedTags.has(tag)) {
        lastIndex = Number(match.index) + raw.length;
        continue;
      }
      const closing = /^<\//.test(raw);
      if (closing) {
        output += `</${tag}>`;
        lastIndex = Number(match.index) + raw.length;
        continue;
      }
      const attrs = sanitizeHtmlAttrs(tag, rawAttrs || "", markdownPath);
      const selfClosing = /\/\s*>$/.test(raw) || ["br", "hr", "img"].includes(tag);
      output += `<${tag}${attrs}${selfClosing ? ">" : ">"}`;
      lastIndex = Number(match.index) + raw.length;
    }
    output += escapeHtml(source.slice(lastIndex));
    return output;
  }

  function sanitizeHtmlAttrs(tag, rawAttrs, markdownPath) {
    const attrs = [];
    const allowed = {
      a: new Set(["href", "title"]),
      img: new Set(["src", "alt", "title", "width", "height"]),
      td: new Set(["colspan", "rowspan", "align"]),
      th: new Set(["colspan", "rowspan", "align"]),
      details: new Set(["open"]),
    };
    const global = new Set(["title"]);
    const attrPattern = /([A-Za-z_:][\w:.-]*)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>/]+)))?/g;
    for (const match of rawAttrs.matchAll(attrPattern)) {
      const name = match[1].toLowerCase();
      if (name.startsWith("on") || name === "style") {
        continue;
      }
      if (!(allowed[tag]?.has(name) || global.has(name))) {
        continue;
      }
      const rawValue = match[3] ?? match[4] ?? match[5] ?? "";
      if (tag === "a" && name === "href") {
        const href = markdownHref(rawValue, markdownPath);
        if (href) attrs.push(`href="${escapeAttribute(href)}" target="_blank" rel="noreferrer"`);
        continue;
      }
      if (tag === "img" && name === "src") {
        const src = markdownImageSrc(rawValue, markdownPath);
        if (src) attrs.push(`src="${escapeAttribute(src)}" loading="lazy"`);
        continue;
      }
      if (tag === "details" && name === "open") {
        attrs.push("open");
        continue;
      }
      if (["width", "height", "colspan", "rowspan"].includes(name) && !/^\d{1,4}$/.test(rawValue)) {
        continue;
      }
      if (name === "align" && !/^(left|center|right)$/i.test(rawValue)) {
        continue;
      }
      attrs.push(`${name}="${escapeAttribute(rawValue)}"`);
    }
    return attrs.length ? ` ${attrs.join(" ")}` : "";
  }

  function parseMarkdownTarget(raw) {
    const value = String(raw || "").trim();
    const titled = value.match(/^(\S+)\s+["']([^"']+)["']$/);
    if (titled) {
      return {url: titled[1], title: titled[2]};
    }
    return {url: value, title: ""};
  }

  function markdownHref(rawUrl, markdownPath) {
    const url = String(rawUrl || "").trim();
    if (!url || /^javascript:/i.test(url) || /^data:/i.test(url)) {
      return "";
    }
    if (/^(https?:|mailto:)/i.test(url) || url.startsWith("#")) {
      return url;
    }
    return resolveMarkdownPath(url, markdownPath);
  }

  function markdownImageSrc(rawUrl, markdownPath) {
    const url = String(rawUrl || "").trim();
    if (!url || /^javascript:/i.test(url)) {
      return "";
    }
    if (/^https?:/i.test(url)) {
      return url;
    }
    if (/^data:image\/(png|gif|jpe?g|webp|svg\+xml);base64,/i.test(url)) {
      return url;
    }
    const filePath = resolveMarkdownPath(url, markdownPath);
    if (!filePath) {
      return "";
    }
    return `/api/files/raw?node=${encodeURIComponent(explorerNode)}&root=${encodeURIComponent(workspaceRoot)}&path=${encodeURIComponent(filePath)}`;
  }

  function resolveMarkdownPath(rawUrl, markdownPath) {
    const value = String(rawUrl || "").trim();
    if (!value || value.startsWith("#")) {
      return value;
    }
    const [pathPart, suffix = ""] = value.split(/(?=[?#])/);
    const decodedPath = decodeURIComponent(pathPart || "");
    const base = String(markdownPath || workspaceRoot).split("/");
    base.pop();
    const parts = decodedPath.startsWith("/")
      ? decodedPath.split("/")
      : [...base, ...decodedPath.split("/")];
    const normalized = [];
    for (const part of parts) {
      if (!part || part === ".") continue;
      if (part === "..") {
        normalized.pop();
        continue;
      }
      normalized.push(part);
    }
    return `/${normalized.join("/")}${suffix}`;
  }

  function localHighlight(text, language) {
    const lang = String(language || "").toLowerCase();
    if (["javascript", "typescript", "python", "bash", "json", "toml", "yaml", "markdown", "css", "rust", "go", "java", "c", "cpp"].includes(lang)) {
      return highlightCodeLike(text, lang);
    }
    if (lang === "xml") {
      return escapeHtml(text).replace(/(&lt;\/?[\w:-]+)([^&]*?)(\/?&gt;)/g, '<span class="syntax-tag">$1</span><span class="syntax-attr">$2</span><span class="syntax-tag">$3</span>');
    }
    return "";
  }

  function highlightCodeLike(text, language) {
    const keywordSets = {
      javascript: "await async break case catch class const continue debugger default delete do else export extends finally for function if import in instanceof let new return switch throw try typeof var void while with yield true false null undefined",
      typescript: "await async break case catch class const continue debugger default delete do else enum export extends finally for function if implements import interface in instanceof let namespace new private protected public readonly return switch throw try type typeof var void while with yield true false null undefined",
      python: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield",
      bash: "case do done elif else esac export fi for function if in local readonly return set shift then unset until while",
      rust: "as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where while",
      go: "break case chan const continue default defer else fallthrough for func go goto if import interface map package range return select struct switch type var true false nil",
      java: "abstract assert boolean break byte case catch char class const continue default do double else enum extends final finally float for if implements import instanceof int interface long native new package private protected public return short static strictfp super switch synchronized this throw throws transient try void volatile while true false null",
      c: "auto break case char const continue default do double else enum extern float for goto if inline int long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while",
      cpp: "alignas alignof auto bool break case catch char class const constexpr continue default delete do double else enum explicit export extern false float for friend goto if inline int long mutable namespace new noexcept nullptr operator private protected public register reinterpret_cast return short signed sizeof static_cast struct switch template this throw true try typedef typeid typename union unsigned using virtual void volatile while",
    };
    const keywords = new Set((keywordSets[language] || "").split(/\s+/).filter(Boolean));
    const escaped = escapeHtml(text);
    const pattern = /(\/\*[\s\S]*?\*\/|\/\/[^\n]*|#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\b\d+(?:\.\d+)?\b|\b[A-Za-z_][A-Za-z0-9_]*\b)/g;
    return escaped.replace(pattern, (token) => {
      if (/^(\/\*|\/\/|#)/.test(token)) {
        return `<span class="syntax-comment">${token}</span>`;
      }
      if (/^["'`]/.test(token)) {
        return `<span class="syntax-string">${token}</span>`;
      }
      if (/^\d/.test(token)) {
        return `<span class="syntax-number">${token}</span>`;
      }
      if (keywords.has(token)) {
        return `<span class="syntax-keyword">${token}</span>`;
      }
      if (language === "json" && /^(true|false|null)$/.test(token)) {
        return `<span class="syntax-keyword">${token}</span>`;
      }
      return token;
    });
  }

  function previewLanguage(name) {
    const ext = String(name || "").split(".").pop().toLowerCase();
    const mapping = {
      js: "javascript",
      jsx: "javascript",
      ts: "typescript",
      tsx: "typescript",
      py: "python",
      sh: "bash",
      bash: "bash",
      zsh: "bash",
      json: "json",
      toml: "toml",
      yaml: "yaml",
      yml: "yaml",
      md: "markdown",
      markdown: "markdown",
      pdf: "pdf",
      html: "xml",
      css: "css",
      rs: "rust",
      go: "go",
      java: "java",
      c: "c",
      h: "c",
      cc: "cpp",
      cpp: "cpp",
      hpp: "cpp",
    };
    return mapping[ext] || "";
  }

  function formatBytes(size) {
    if (!size) {
      return "0 B";
    }
    if (size >= 1024 * 1024) {
      return `${(size / 1024 / 1024).toFixed(1)} MiB`;
    }
    if (size >= 1024) {
      return `${(size / 1024).toFixed(1)} KiB`;
    }
    return `${size} B`;
  }

  function parseChangedFile(raw) {
    let value = String(raw || "").trim();
    const status = value.slice(0, 2).trim() || "M";
    if (value.includes(" -> ")) {
      value = value.split(" -> ").pop().trim();
    }
    value = value.replace(/^([ MADRCU?!]{1,2})\s+/, "").trim();
    return {
      path: value,
      rawStatus: status,
      deleted: status.includes("D"),
      label: changeStatusLabel(status),
      shortLabel: changeStatusShortLabel(status),
    };
  }

  function changeStatusLabel(status) {
    if (status.includes("U")) return "Conflict";
    if (status.includes("R")) return "Renamed";
    if (status.includes("C")) return "Copied";
    if (status.includes("D")) return "Deleted";
    if (status.includes("A") || status.includes("?")) return "Added";
    if (status.includes("M")) return "Modified";
    return "Changed";
  }

  function changeStatusShortLabel(status) {
    if (status.includes("U")) return "!";
    if (status.includes("R")) return "R";
    if (status.includes("C")) return "C";
    if (status.includes("D")) return "D";
    if (status.includes("A") || status.includes("?")) return "A";
    if (status.includes("M")) return "M";
    return "*";
  }

  function renderChangedFileButton(button) {
    const parsed = parseChangedFile(button.dataset.file);
    const parts = parsed.path.split("/").filter(Boolean);
    const name = parts.pop() || parsed.path;
    const directory = parts.join("/");
    const fileName = button.querySelector(".changed-file-name");
    const fileDir = button.querySelector(".changed-file-dir");
    button.title = `${parsed.label}: ${parsed.path}`;
    if (fileName) {
      fileName.textContent = name;
    }
    if (fileDir) {
      fileDir.textContent = directory || ".";
    }
  }

  for (const button of document.querySelectorAll(".changed-file-list button[data-file]")) {
    renderChangedFileButton(button);
    button.addEventListener("click", () => {
      const parsed = parseChangedFile(button.dataset.file);
      if (parsed.deleted) {
        if (previewMeta && previewBody) {
          previewMeta.textContent = "Deleted";
          previewBody.textContent = `${parsed.path} was deleted in the working tree.`;
          previewBody.className = "file-preview-code";
        }
        return;
      }
      const relative = parsed.path;
      if (!relative) {
        return;
      }
      const root = workspaceRoot.replace(/\/$/, "");
      const parts = relative.split("/").filter(Boolean);
      parts.pop();
      const directory = `${root}/${parts.join("/")}`.replace(/\/$/, "");
      highlightedPath = `${root}/${relative}`;
      workspaceExplorer.load(directory || root, highlightedPath);
      previewFile(highlightedPath);
    });
  }

  window.StarAgentAfterPaint(() => workspaceExplorer.load(sessionExplorer.dataset.path));
}

const chat = document.querySelector(".mobile-chat");
if (chat) {
  const chatNode = chat.dataset.node;
  const chatSession = chat.dataset.session;
  const chatLog = chat.querySelector(".chat-log");
  const chatMeta = chat.querySelector(".chat-meta");
  const chatForm = chat.querySelector(".chat-form");
  const chatInput = chat.querySelector("textarea");
  const chatStatus = chat.querySelector(".chat-status");
  const chatRefresh = chat.querySelector(".chat-refresh");
  const tokenPanel = document.querySelector("[data-token-panel]");
  const chatStorageKey = `staragent.chat.${chatNode}.${chatSession}`;
  const chatPendingKey = `staragent.chat.pending.${chatNode}.${chatSession}`;
  let chatHistory = [];
  let lastChatSnapshot = "";
  let workingMessage = null;
  let workingStartedAt = 0;
  let transcriptSyncTimer = null;
  let userScrollPinned = false;
  let programmaticScroll = false;
  let lastRenderedChatSignature = "";
  let acknowledgedFinalKey = "";
  let pendingFinalKey = "";
  let acknowledgingFinal = false;
  const longMessageChars = 900;
  const longMessageLines = 14;

  const localChatHistory = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(chatStorageKey) || "[]");
      return Array.isArray(saved) ? saved.slice(-80) : [];
    } catch {
      return [];
    }
  };

  const saveChatHistory = () => {
    localStorage.setItem(chatStorageKey, JSON.stringify(chatHistory.slice(-80)));
  };

  const normalizeMessageText = (text) => String(text || "").trim().replace(/\s+/g, " ");
  const messageFingerprint = (text) => String(text || "").trim().replace(/\s+/g, "").toLowerCase();
  const looksLikeTranscriptFragment = (role, text) => {
    const first = String(text || "").split("\n").find((line) => line.trim())?.trim() || "";
    return role === "agent" && (first.startsWith("›") || first.startsWith("◦ Working"));
  };

  const saveChatMessageRemote = (message) => {
    fetch(`/api/nodes/${encodeURIComponent(chatNode)}/sessions/${encodeURIComponent(chatSession)}/chat-history`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(message)
    }).catch(() => {});
  };

  const formatTokenCount = (value) => {
    const count = Number(value || 0);
    if (!count) {
      return "--";
    }
    if (count >= 1000000) {
      return `${(count / 1000000).toFixed(count >= 10000000 ? 1 : 2)}M`;
    }
    if (count >= 1000) {
      return `${(count / 1000).toFixed(count >= 10000 ? 1 : 2)}k`;
    }
    return count.toLocaleString();
  };

  const setTokenText = (selector, text) => {
    const item = tokenPanel?.querySelector(selector);
    if (item) {
      item.textContent = text;
    }
  };

  const renderTokenUsage = (usage) => {
    if (!usage) {
      return;
    }
    const source = usage.source ? `${usage.source} native usage` : "CLI native usage";
    const rate = [
      Number(usage.primary_rate_used_percent || 0) ? `${Number(usage.primary_rate_used_percent).toFixed(0)}% primary` : "",
      Number(usage.secondary_rate_used_percent || 0) ? `${Number(usage.secondary_rate_used_percent).toFixed(0)}% weekly` : "",
    ].filter(Boolean).join(" · ");
    setTokenText("[data-token-total]", formatTokenCount(usage.total_tokens));
    setTokenText("[data-token-source]", source);
    setTokenText("[data-token-model]", usage.model || "--");
    setTokenText("[data-token-effort]", usage.reasoning_effort || "default");
    setTokenText("[data-token-plan]", usage.plan_type || "--");
    setTokenText("[data-token-context]", usage.context_window ? `${formatTokenCount(usage.context_window)} window` : "--");
    setTokenText("[data-token-rate]", rate || "--");
    setTokenText("[data-token-last]", formatTokenCount(usage.last_total_tokens));
    setTokenText("[data-token-cached]", formatTokenCount(usage.cached_input_tokens));
    setTokenText("[data-token-output]", formatTokenCount(usage.output_tokens));
    setTokenText("[data-token-reasoning]", formatTokenCount(usage.reasoning_output_tokens));
    chatMeta.textContent = `Tokens ${formatTokenCount(usage.total_tokens)} total · last ${formatTokenCount(usage.last_total_tokens)}`;
  };

  const mergeMessages = (...lists) => {
    const seenIds = new Set();
    const seenFingerprints = new Set();
    const merged = [];
    const candidates = [];
    for (const list of lists) {
      for (const message of list || []) {
        const role = ["user", "agent", "session"].includes(message.role) ? message.role : "agent";
        const text = String(message.text || "").trim();
        if (!text || looksLikeTranscriptFragment(role, text)) {
          continue;
        }
        const id = String(message.id || "");
        const fingerprint = `${role}\n${messageFingerprint(text)}`;
        candidates.push({role, text, time: Number(message.time || Date.now()), id, fingerprint});
      }
    }
    candidates.sort((a, b) => {
      if (a.fingerprint === b.fingerprint && Boolean(a.id) !== Boolean(b.id)) {
        return a.id ? -1 : 1;
      }
      return a.time - b.time;
    });
    for (const message of candidates) {
      if (message.id && seenIds.has(message.id)) {
        continue;
      }
      if (seenFingerprints.has(message.fingerprint)) {
        continue;
      }
      if (message.id) {
        seenIds.add(message.id);
      }
      seenFingerprints.add(message.fingerprint);
      merged.push({
        role: message.role,
        text: message.text,
        time: message.time,
        id: message.id,
      });
    }
    return merged
      .sort((a, b) => a.time - b.time)
      .slice(-80);
  };

  const pendingLocalUserMessages = (local, remote) => {
    const latestRemoteTime = (remote || []).reduce((latest, message) => Math.max(latest, Number(message.time || 0)), 0);
    const remoteUserFingerprints = new Set((remote || [])
      .filter((message) => message.role === "user")
      .map((message) => messageFingerprint(message.text || "")));
    return (local || []).filter((message) => {
      if (message.role !== "user") {
        return false;
      }
      if (Number(message.time || 0) <= latestRemoteTime) {
        return false;
      }
      return !remoteUserFingerprints.has(messageFingerprint(message.text || ""));
    });
  };

  const loadPendingChat = () => {
    try {
      const pending = JSON.parse(localStorage.getItem(chatPendingKey) || "null");
      if (!pending || !pending.baseline || !pending.startedAt) {
        return null;
      }
      if (Date.now() - Number(pending.startedAt) > 15 * 60 * 1000) {
        localStorage.removeItem(chatPendingKey);
        return null;
      }
      return pending;
    } catch {
      return null;
    }
  };

  const savePendingChat = (baseline, startedAt = Date.now()) => {
    localStorage.setItem(chatPendingKey, JSON.stringify({baseline, startedAt}));
  };

  const clearPendingChat = () => {
    localStorage.removeItem(chatPendingKey);
  };

  const loadChatHistory = async () => {
    const local = localChatHistory();
    let remote = [];
    try {
      const response = await fetch(`/api/nodes/${encodeURIComponent(chatNode)}/sessions/${encodeURIComponent(chatSession)}/chat-sync`);
      if (response.ok) {
        const body = await response.json();
        remote = Array.isArray(body.messages) ? body.messages : [];
        applyTranscriptState(body);
      }
    } catch {
      remote = [];
    }
    chatHistory = remote.length
      ? mergeMessages(remote, pendingLocalUserMessages(local, remote))
      : mergeMessages(remote, local);
    saveChatHistory();
    if (!remote.length) {
      for (const message of local) {
        saveChatMessageRemote(message);
      }
    }
    lastChatSnapshot = [...chatHistory].reverse().find((item) => item.role === "agent" || item.role === "session")?.text || "";
    renderChatHistory();
  };

  const messageTitle = (message) => {
    const value = String(message.text || "");
    let firstLine = "";
    let offset = 0;
    while (offset <= value.length) {
      const newline = value.indexOf("\n", offset);
      const line = value.slice(offset, newline < 0 ? value.length : newline).trim();
      if (line) {
        firstLine = line;
        break;
      }
      if (newline < 0) {
        break;
      }
      offset = newline + 1;
    }
    const compact = firstLine.trim().slice(0, 72);
    return `${messageLabel(message.role)} · ${compact || "(empty)"}`;
  };

  const messageLabel = (role) => {
    if (role === "user") {
      return "You";
    }
    if (role === "session") {
      return "Session";
    }
    return "Agent";
  };

  const isLongMessage = (text) => {
    const value = String(text || "");
    if (value.length > longMessageChars) {
      return true;
    }
    let offset = 0;
    for (let line = 0; line < longMessageLines; line += 1) {
      const newline = value.indexOf("\n", offset);
      if (newline < 0) {
        return false;
      }
      offset = newline + 1;
    }
    return offset < value.length;
  };

  const compactMessageText = (text) => {
    const value = String(text || "").trim();
    let offset = 0;
    for (let line = 0; line < longMessageLines; line += 1) {
      const newline = value.indexOf("\n", offset);
      if (newline < 0) {
        offset = value.length;
        break;
      }
      offset = newline + 1;
    }
    const linesTruncated = offset < value.length;
    let compact = value.slice(0, offset || value.length);
    if (compact.length > longMessageChars) {
      compact = `${compact.slice(0, longMessageChars).trimEnd()}...`;
    }
    if (linesTruncated) {
      compact = `${compact.trimEnd()}\n...`;
    }
    return compact || "(empty)";
  };

  const messageKey = (message) => {
    const prefix = String(message.text || "").slice(0, 512);
    return `${message.role}:${message.time || 0}:${normalizeMessageText(prefix).slice(0, 120)}`;
  };

  const chatRenderSignature = (visibleHistory) => {
    const messages = visibleHistory
      .map((message) => `${messageKey(message)}:${message.id || ""}:${String(message.text || "").length}`)
      .join("|");
    return `${messages}|working:${Boolean(workingMessage)}`;
  };

  const openDetailKeys = () => new Set(
    Array.from(chatLog.querySelectorAll("details[data-detail-key][open]"))
      .map((item) => item.dataset.detailKey)
      .filter(Boolean)
  );

  const appendMessageBody = (bodyWrap, message, key, opened) => {
    const text = (message.text || "").trim() || "(empty)";
    if (!isLongMessage(text)) {
      const body = document.createElement("pre");
      body.textContent = text;
      bodyWrap.appendChild(body);
      return;
    }
    const preview = document.createElement("pre");
    preview.className = "chat-preview";
    preview.textContent = compactMessageText(text);
    const full = document.createElement("details");
    full.className = "chat-full-message";
    full.dataset.detailKey = `${key}:full`;
    const summary = document.createElement("summary");
    summary.textContent = "Show full message";
    const appendFullBody = () => {
      if (full.dataset.bodyLoaded) {
        return;
      }
      full.dataset.bodyLoaded = "true";
      const body = document.createElement("pre");
      body.textContent = text;
      full.appendChild(body);
    };
    full.appendChild(summary);
    full.open = opened.has(full.dataset.detailKey);
    if (full.open) {
      appendFullBody();
    }
    full.addEventListener("toggle", () => {
      if (full.open) {
        appendFullBody();
      }
    });
    bodyWrap.append(preview, full);
  };

  const isChatNearBottom = () => chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight < 80;

  const updateUserScrollPin = () => {
    if (!programmaticScroll) {
      userScrollPinned = !isChatNearBottom();
    }
  };

  const scheduleUserScrollPinUpdate = () => {
    requestAnimationFrame(updateUserScrollPin);
  };

  const releaseUserScrollPin = () => {
    userScrollPinned = false;
  };

  const isUserScrollLocked = () => userScrollPinned && !isChatNearBottom();

  const setChatScrollTop = (value) => {
    programmaticScroll = true;
    chatLog.scrollTop = value;
    setTimeout(() => {
      programmaticScroll = false;
    }, 80);
  };

  const scrollChatToBottom = () => {
    releaseUserScrollPin();
    setChatScrollTop(chatLog.scrollHeight);
  };

  const currentScrollAnchor = () => {
    const logTop = chatLog.getBoundingClientRect().top;
    const messages = Array.from(chatLog.querySelectorAll(".chat-message[data-scroll-key]"));
    for (const item of messages) {
      const rect = item.getBoundingClientRect();
      if (rect.bottom > logTop + 1) {
        return {
          key: item.dataset.scrollKey,
          offset: rect.top - logTop,
        };
      }
    }
    return null;
  };

  const restoreScrollAnchor = (anchor) => {
    if (!anchor?.key) {
      return false;
    }
    const target = chatLog.querySelector(`.chat-message[data-scroll-key="${CSS.escape(anchor.key)}"]`);
    if (!target) {
      return false;
    }
    const logTop = chatLog.getBoundingClientRect().top;
    const targetTop = target.getBoundingClientRect().top;
    setChatScrollTop(chatLog.scrollTop + targetTop - logTop - anchor.offset);
    return true;
  };

  const renderChatHistory = ({preserveScroll = true, forceBottom = false} = {}) => {
    const scrollLocked = preserveScroll && isUserScrollLocked() && !forceBottom;
    const previousScrollTop = chatLog.scrollTop;
    const shouldStickToBottom = !scrollLocked && (forceBottom || (preserveScroll && isChatNearBottom()));
    const previousBottomOffset = chatLog.scrollHeight - chatLog.scrollTop;
    const anchor = !shouldStickToBottom && preserveScroll ? currentScrollAnchor() : null;
    const opened = openDetailKeys();
    const visibleHistory = chatHistory.filter((message) => !message.transient);
    const nextSignature = chatRenderSignature(visibleHistory);
    if (preserveScroll && !forceBottom && nextSignature === lastRenderedChatSignature) {
      return;
    }
    lastRenderedChatSignature = nextSignature;
    chatLog.innerHTML = "";
    const expandedStart = Math.max(0, visibleHistory.length - 4);
    visibleHistory.forEach((message, index) => {
      const collapsed = index < expandedStart;
      const key = messageKey(message);
      const item = document.createElement(collapsed ? "details" : "div");
      item.className = `chat-message chat-${message.role}`;
      item.dataset.scrollKey = key;
      if (collapsed) {
        item.dataset.detailKey = `${key}:thread`;
        item.open = opened.has(item.dataset.detailKey);
        const summary = document.createElement("summary");
        summary.textContent = messageTitle(message);
        item.appendChild(summary);
      }
      const appendBody = () => {
        if (item.dataset.bodyLoaded) {
          return;
        }
        item.dataset.bodyLoaded = "true";
        const bodyWrap = document.createElement("div");
        bodyWrap.className = "chat-bubble";
        if (!collapsed) {
          const label = document.createElement("strong");
          label.textContent = messageLabel(message.role);
          bodyWrap.appendChild(label);
        }
        appendMessageBody(bodyWrap, message, key, opened);
        item.appendChild(bodyWrap);
      };
      if (collapsed) {
        if (item.open) {
          appendBody();
        }
        item.addEventListener("toggle", () => {
          if (item.open) {
            appendBody();
          }
        });
      } else {
        appendBody();
      }
      chatLog.appendChild(item);
    });
    if (workingMessage) {
      chatLog.appendChild(workingMessage);
    }
    if (shouldStickToBottom) {
      scrollChatToBottom();
    } else if (scrollLocked) {
      setChatScrollTop(previousScrollTop);
    } else if (preserveScroll) {
      if (!restoreScrollAnchor(anchor)) {
        setChatScrollTop(Math.max(0, chatLog.scrollHeight - previousBottomOffset));
      }
    }
  };

  const appendChat = (role, text) => {
    role = ["user", "agent", "session"].includes(role) ? role : "agent";
    const normalized = messageFingerprint(text);
    if (looksLikeTranscriptFragment(role, text) || chatHistory.some((message) => message.role === role && messageFingerprint(message.text) === normalized)) {
      return;
    }
    const message = {role, text: text.trim(), time: Date.now()};
    chatHistory.push(message);
    saveChatHistory();
    saveChatMessageRemote(message);
    renderChatHistory({forceBottom: role === "user"});
  };

  const showWorking = ({forceBottom = false, label = "Working", startedAt = 0} = {}) => {
    const scrollLocked = isUserScrollLocked() && !forceBottom;
    const shouldStickToBottom = !scrollLocked && (forceBottom || isChatNearBottom());
    if (!workingMessage) {
      workingStartedAt = startedAt || Date.now();
      workingMessage = document.createElement("div");
      workingMessage.className = "chat-message chat-agent chat-working";
      const bodyWrap = document.createElement("div");
      bodyWrap.className = "chat-bubble";
      const label = document.createElement("strong");
      label.textContent = "Agent";
      const body = document.createElement("div");
      body.className = "working-pill";
      body.innerHTML = '<span class="chat-spinner"></span><span class="working-text">Working</span>';
      bodyWrap.append(label, body);
      workingMessage.appendChild(bodyWrap);
    } else if (startedAt && (!workingStartedAt || startedAt < workingStartedAt)) {
      workingStartedAt = startedAt;
    }
    const elapsed = Math.max(1, Math.round((Date.now() - workingStartedAt) / 1000));
    const text = workingMessage.querySelector(".working-text");
    if (text) {
      text.textContent = label === "Working" ? `Working · ${elapsed}s` : label;
    }
    if (!workingMessage.isConnected) {
      renderChatHistory({forceBottom: shouldStickToBottom});
    }
    if (shouldStickToBottom) {
      scrollChatToBottom();
    }
  };

  const clearWorking = () => {
    if (workingMessage) {
      workingMessage.remove();
      workingMessage = null;
    }
    clearPendingChat();
  };

  chatLog.addEventListener("wheel", scheduleUserScrollPinUpdate, {passive: true});
  chatLog.addEventListener("touchstart", scheduleUserScrollPinUpdate, {passive: true});
  chatLog.addEventListener("pointerdown", scheduleUserScrollPinUpdate);
  chatLog.addEventListener("scroll", () => {
    updateUserScrollPin();
  }, {passive: true});

  const finalStateKey = (body) => {
    const messages = Array.isArray(body.messages) ? body.messages : [];
    const latestAgent = [...messages].reverse().find((message) => message.role === "agent");
    return String(latestAgent?.id || messageFingerprint(body.completed_reply || body.reply || ""));
  };

  const acknowledgePendingFinal = async () => {
    if (
      acknowledgingFinal
      || document.visibilityState !== "visible"
      || !pendingFinalKey
      || pendingFinalKey === acknowledgedFinalKey
    ) {
      return;
    }
    const key = pendingFinalKey;
    acknowledgingFinal = true;
    try {
      const response = await fetch(
        `/api/nodes/${encodeURIComponent(chatNode)}/sessions/${encodeURIComponent(chatSession)}/seen`,
        {method: "POST"},
      );
      const body = await response.json().catch(() => ({}));
      if (response.ok && body.acknowledged) {
        acknowledgedFinalKey = key;
        if (pendingFinalKey === key) {
          pendingFinalKey = "";
        }
      }
    } catch {
      // The normal transcript poll will retry while this completion remains visible.
    } finally {
      acknowledgingFinal = false;
    }
  };

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      acknowledgePendingFinal();
    }
  });

  const applyTranscriptState = (body) => {
    renderTokenUsage(body.token_usage);
    if (Array.isArray(body.messages)) {
      const transcriptMessages = body.messages;
      const latestTranscriptTime = transcriptMessages.reduce((latest, message) => Math.max(latest, Number(message.time || 0)), 0);
      const transcriptFingerprints = new Set(
        transcriptMessages.map((message) => {
          const role = ["user", "agent", "session"].includes(message.role) ? message.role : "agent";
          return `${role}\n${messageFingerprint(message.text || "")}`;
        })
      );
      const pendingLocalUsers = chatHistory.filter((message) => {
        if (message.id || message.role !== "user" || Number(message.time || 0) <= latestTranscriptTime) {
          return false;
        }
        return !transcriptFingerprints.has(`user\n${messageFingerprint(message.text || "")}`);
      });
      chatHistory = mergeMessages(transcriptMessages, pendingLocalUsers);
      saveChatHistory();
      lastChatSnapshot = [...chatHistory].reverse().find((item) => item.role === "agent")?.text || lastChatSnapshot;
    }
    if (body.working) {
      pendingFinalKey = "";
      showWorking({
        label: body.working_label || "Working",
        startedAt: Number(body.working_since_ms || 0),
      });
      const pending = loadPendingChat();
      savePendingChat(
        lastChatSnapshot || String(body.reply || ""),
        Number(pending?.startedAt || body.working_since_ms || Date.now()),
      );
      chatStatus.textContent = "Agent working";
      return;
    }
    if (body.final) {
      const key = finalStateKey(body);
      if (key && key !== acknowledgedFinalKey) {
        pendingFinalKey = key;
        acknowledgePendingFinal();
      }
    }
    if (body.final || body.reply || !loadPendingChat()) {
      clearWorking();
    }
  };

  const syncChatFromTranscript = async ({silent = true} = {}) => {
    if (!silent) {
      chatStatus.textContent = "Syncing";
    }
    try {
      const response = await fetch(`/api/nodes/${encodeURIComponent(chatNode)}/sessions/${encodeURIComponent(chatSession)}/chat-sync`);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || "Sync failed");
      }
      const body = await response.json();
      applyTranscriptState(body);
      renderChatHistory();
      if (!body.working && !silent) {
        chatStatus.textContent = "Updated";
      }
      return body;
    } catch (error) {
      if (!silent) {
        chatStatus.textContent = error.message;
      }
    }
    return null;
  };

  const scheduleTranscriptSync = (delay = 3500) => {
    if (transcriptSyncTimer) {
      clearTimeout(transcriptSyncTimer);
    }
    transcriptSyncTimer = setTimeout(async () => {
      transcriptSyncTimer = null;
      const body = await syncChatFromTranscript({silent: true});
      const pending = loadPendingChat();
      const pollDelay = body?.working || pending ? 1800 : 3500;
      scheduleTranscriptSync(pollDelay);
    }, Math.max(0, delay));
  };

  const monitorAgentReply = (baseline, startedAt = Date.now()) => {
    savePendingChat(baseline, startedAt);
    showWorking({forceBottom: true, startedAt});
    chatStatus.textContent = "Agent working";
    scheduleTranscriptSync(1800);
  };

  window.StarAgentAfterPaint(() => {
    loadChatHistory().then(() => {
      const pending = loadPendingChat();
      if (pending) {
        monitorAgentReply(pending.baseline, Number(pending.startedAt));
      } else {
        chatStatus.textContent = "Loaded";
        scheduleTranscriptSync();
      }
    });
  }, 500);

  chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = chatInput.value.trim();
    if (!text) {
      chatInput.focus();
      return;
    }
    appendChat("user", text);
    chatInput.value = "";
    chatStatus.textContent = "Sending";
    const response = await fetch(`/api/nodes/${encodeURIComponent(chatNode)}/sessions/${encodeURIComponent(chatSession)}/send`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text})
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      chatStatus.textContent = body.detail || "Send failed";
      return;
    }
    chatStatus.textContent = "Sent";
    const startedAt = Date.now();
    savePendingChat(lastChatSnapshot, startedAt);
    monitorAgentReply(lastChatSnapshot, startedAt);
  });

  chatRefresh.addEventListener("click", () => syncChatFromTranscript({silent: false}));
}

const terminalBand = document.querySelector(".terminal-band");
if (terminalBand) {
  const terminalToggle = terminalBand.querySelector(".terminal-toggle");
  const prefersCollapsed = window.matchMedia("(max-width: 820px)").matches;
  if (!prefersCollapsed) {
    terminalBand.classList.add("is-open");
    terminalToggle.textContent = "Hide Terminal";
  }
  terminalToggle.addEventListener("click", () => {
    terminalBand.classList.toggle("is-open");
    terminalToggle.textContent = terminalBand.classList.contains("is-open") ? "Hide Terminal" : "Show Terminal";
    window.dispatchEvent(new Event("resize"));
  });
}

const terminal = document.querySelector(".web-terminal");
if (terminal) {
  const initializeTerminal = async () => {
  await ensureTerminalAssets();
  const session = terminal.dataset.session;
  const node = terminal.dataset.node;
  const parentTerminalBand = terminal.closest(".terminal-band");
  const parentTerminalToggle = parentTerminalBand ? parentTerminalBand.querySelector(".terminal-toggle") : null;
  const screenEl = terminal.querySelector(".terminal-screen");
  const status = terminal.querySelector(".terminal-connection-state");
  const transportValue = terminal.querySelector(".terminal-transport-value");
  const inputLockButton = parentTerminalBand.querySelector(".terminal-input-lock");
  const inputLockLabel = inputLockButton.querySelector(".terminal-lock-label");
  const cssVar = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  const terminalTheme = () => ({
    background: cssVar("--terminal-bg", "#1e1e1e"),
    foreground: cssVar("--terminal-fg", "#d4d4d4"),
    cursor: cssVar("--terminal-cursor", "#aeafad"),
    selectionBackground: cssVar("--terminal-selection", "#264f78"),
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
    brightWhite: "#ffffff"
  });
  const term = new Terminal({
    allowProposedApi: false,
    convertEol: true,
    cursorBlink: false,
    disableStdin: true,
    fontFamily: '"Cascadia Mono", "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace',
    fontSize: 13,
    lineHeight: 1.15,
    fastScrollModifier: "alt",
    fastScrollSensitivity: 5,
    scrollSensitivity: 1.2,
    scrollback: 10000,
    theme: terminalTheme()
  });
  window.addEventListener("staragent:themechange", () => {
    term.options.theme = terminalTheme();
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(screenEl);
  const terminalScrollbar = document.createElement("div");
  terminalScrollbar.className = "terminal-scrollbar";
  terminalScrollbar.setAttribute("aria-hidden", "true");
  const terminalScrollbarThumb = document.createElement("div");
  terminalScrollbarThumb.className = "terminal-scrollbar-thumb";
  terminalScrollbar.appendChild(terminalScrollbarThumb);
  screenEl.appendChild(terminalScrollbar);
  let socket = null;
  let reconnectTimer = null;
  let keepaliveTimer = null;
  let reconnectAttempts = 0;
  let closedByPage = false;
  let terminalPaused = false;
  let transport = "websocket";
  let httpTerminalId = "";
  let httpPolling = false;
  let terminalInputUnlocked = false;
  let terminalHistoryLoaded = false;
  let terminalHistoryPromise = null;
  let suppressInitialTerminalPaint = false;
  let terminalInitialPaintIdleTimer = null;
  let terminalInitialPaintDeadlineTimer = null;
  const terminalDecoder = new TextDecoder("utf-8");
  const terminalResetPattern = /\x1b\[\?(?:47|1047|1048|1049)[hl]|\x1b\[(?:22|23);0;0t|\x1b\[3J|\x1b\[(?:H|1;1H)\x1b\[2J|\x1b\[2J|\x1bc/g;
  const preferHttpTerminal = window.matchMedia("(max-width: 820px)").matches;
  const updateTerminalTransport = () => {
    transportValue.textContent = transport === "http" ? "HTTP polling" : "WebSocket";
  };
  updateTerminalTransport();

  const sendTerminalMessage = (payload) => {
    if (transport === "websocket" && socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
      return true;
    }
    if (transport === "http" && httpTerminalId) {
      if (payload.type === "resize") {
        fetch(`/api/terminal-http/${encodeURIComponent(httpTerminalId)}/resize`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({cols: payload.cols, rows: payload.rows})
        }).catch(() => {});
        return true;
      }
      if (payload.type === "input") {
        fetch(`/api/terminal-http/${encodeURIComponent(httpTerminalId)}/input`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({data: payload.data || ""})
        }).then((response) => {
          if (!response.ok) {
            status.textContent = "Terminal input failed";
          }
        }).catch(() => {
          status.textContent = "Terminal input failed";
        });
        return true;
      }
    }
    return false;
  };

  const terminalTextarea = term.textarea;
  const terminalTextareaTabIndex = terminalTextarea?.getAttribute("tabindex");
  const terminalTextareaInputMode = terminalTextarea?.getAttribute("inputmode");
  const setTerminalInputUnlocked = (unlocked, {focus = false} = {}) => {
    terminalInputUnlocked = Boolean(unlocked);
    term.options.disableStdin = !terminalInputUnlocked;
    terminal.classList.toggle("is-input-locked", !terminalInputUnlocked);
    terminal.classList.toggle("is-input-unlocked", terminalInputUnlocked);
    inputLockButton.classList.toggle("is-locked", !terminalInputUnlocked);
    inputLockButton.classList.toggle("is-unlocked", terminalInputUnlocked);
    inputLockButton.setAttribute("aria-pressed", String(terminalInputUnlocked));
    inputLockButton.setAttribute(
      "aria-label",
      terminalInputUnlocked ? "Lock terminal input" : "Unlock terminal input",
    );
    inputLockButton.title = terminalInputUnlocked
      ? "Terminal input is unlocked"
      : "Terminal input is locked";
    inputLockLabel.textContent = terminalInputUnlocked ? "Unlocked" : "Locked";
    if (terminalTextarea) {
      terminalTextarea.readOnly = !terminalInputUnlocked;
      terminalTextarea.setAttribute("aria-readonly", String(!terminalInputUnlocked));
      if (terminalInputUnlocked) {
        if (terminalTextareaTabIndex === null) {
          terminalTextarea.removeAttribute("tabindex");
        } else {
          terminalTextarea.setAttribute("tabindex", terminalTextareaTabIndex);
        }
        if (terminalTextareaInputMode === null) {
          terminalTextarea.removeAttribute("inputmode");
        } else {
          terminalTextarea.setAttribute("inputmode", terminalTextareaInputMode);
        }
      } else {
        terminalTextarea.setAttribute("tabindex", "-1");
        terminalTextarea.setAttribute("inputmode", "none");
      }
    }
    if (terminalInputUnlocked && focus) {
      term.focus();
    } else if (!terminalInputUnlocked) {
      term.blur();
    }
  };

  term.onData((data) => {
    if (!terminalInputUnlocked) {
      return;
    }
    if (!sendTerminalMessage({type: "input", data})) {
      status.textContent = "Terminal input not connected";
    }
  });

  const fit = () => {
    fitAddon.fit();
    sendTerminalMessage({type: "resize", cols: term.cols, rows: term.rows});
  };
  setTerminalInputUnlocked(false);
  inputLockButton.disabled = false;
  inputLockButton.addEventListener("click", () => {
    const nextUnlocked = !terminalInputUnlocked;
    if (nextUnlocked && parentTerminalBand && !parentTerminalBand.classList.contains("is-open")) {
      parentTerminalToggle?.click();
    }
    setTerminalInputUnlocked(nextUnlocked, {focus: nextUnlocked});
  });
  screenEl.addEventListener("pointerdown", () => {
    if (terminalInputUnlocked) {
      term.focus();
      return;
    }
    requestAnimationFrame(() => term.blur());
  });
  document.addEventListener("pointerdown", (event) => {
    if (!terminal.contains(event.target)) {
      term.blur();
    }
  });
  const activeTerminalBuffer = () => term.buffer?.active || term._core?._bufferService?.buffer || null;
  const terminalLineCount = () => {
    const buffer = activeTerminalBuffer();
    return Number(buffer?.length ?? buffer?.lines?.length ?? term.rows);
  };
  const terminalMaxScroll = () => Math.max(0, terminalLineCount() - Math.max(1, term.rows));
  const terminalViewportY = () => Number(activeTerminalBuffer()?.viewportY ?? activeTerminalBuffer()?.ydisp ?? 0);
  const updateTerminalScrollbar = () => {
    const maxScroll = terminalMaxScroll();
    const totalRows = maxScroll + Math.max(1, term.rows);
    const thumbHeight = maxScroll > 0
      ? Math.max(24, terminalScrollbar.clientHeight * term.rows / totalRows)
      : terminalScrollbar.clientHeight;
    const travel = Math.max(0, terminalScrollbar.clientHeight - thumbHeight);
    const top = maxScroll > 0 ? travel * terminalViewportY() / maxScroll : 0;
    terminalScrollbar.classList.toggle("is-scrollable", maxScroll > 0);
    terminalScrollbarThumb.style.height = `${Math.max(0, thumbHeight)}px`;
    terminalScrollbarThumb.style.transform = `translateY(${Math.max(0, Math.min(travel, top))}px)`;
  };
  const scrollTerminalFromPointer = (clientY) => {
    const maxScroll = terminalMaxScroll();
    if (!maxScroll) {
      return;
    }
    const rect = terminalScrollbar.getBoundingClientRect();
    const thumbHeight = terminalScrollbarThumb.getBoundingClientRect().height || 24;
    const travel = Math.max(1, rect.height - thumbHeight);
    const offset = Math.max(0, Math.min(travel, clientY - rect.top - thumbHeight / 2));
    term.scrollToLine(Math.round(maxScroll * offset / travel));
    updateTerminalScrollbar();
  };
  terminalScrollbar.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    event.stopPropagation();
    terminalScrollbar.setPointerCapture(event.pointerId);
    terminalScrollbar.classList.add("is-dragging");
    scrollTerminalFromPointer(event.clientY);
  });
  terminalScrollbar.addEventListener("pointermove", (event) => {
    if (!terminalScrollbar.classList.contains("is-dragging")) {
      return;
    }
    event.preventDefault();
    scrollTerminalFromPointer(event.clientY);
  });
  terminalScrollbar.addEventListener("pointerup", (event) => {
    terminalScrollbar.releasePointerCapture(event.pointerId);
    terminalScrollbar.classList.remove("is-dragging");
  });
  terminalScrollbar.addEventListener("pointercancel", () => {
    terminalScrollbar.classList.remove("is-dragging");
  });
  term.onScroll(updateTerminalScrollbar);
  term.onResize(updateTerminalScrollbar);
  fit();
  window.addEventListener("resize", () => {
    fit();
    requestAnimationFrame(updateTerminalScrollbar);
  });
  requestAnimationFrame(updateTerminalScrollbar);
  let wheelRemainder = 0;
  screenEl.addEventListener("wheel", (event) => {
    if (event.ctrlKey) {
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    const sampleRow = screenEl.querySelector(".xterm-rows > div");
    const lineHeight = sampleRow ? sampleRow.getBoundingClientRect().height || 16 : 16;
    const pageLines = Math.max(1, Math.floor(term.rows * 0.85));
    let lines = 0;
    if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) {
      lines = event.deltaY;
    } else if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) {
      lines = event.deltaY * pageLines;
    } else {
      wheelRemainder += event.deltaY / lineHeight;
      lines = wheelRemainder > 0 ? Math.floor(wheelRemainder) : Math.ceil(wheelRemainder);
      wheelRemainder -= lines;
    }
    if (lines !== 0) {
      term.scrollLines(lines);
      updateTerminalScrollbar();
    }
  }, {capture: true, passive: false});

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const terminalUrl = `${protocol}//${window.location.host}/ws/nodes/${encodeURIComponent(node)}/sessions/${encodeURIComponent(session)}/terminal`;

  const terminalHistoryText = (text) => String(text || "").replace(/\r?\n/g, "\r\n");
  const filterTerminalOutput = (text) => String(text || "").replace(terminalResetPattern, "");

  const stopSuppressingInitialTerminalPaint = () => {
    suppressInitialTerminalPaint = false;
    if (terminalInitialPaintIdleTimer) {
      clearTimeout(terminalInitialPaintIdleTimer);
      terminalInitialPaintIdleTimer = null;
    }
    if (terminalInitialPaintDeadlineTimer) {
      clearTimeout(terminalInitialPaintDeadlineTimer);
      terminalInitialPaintDeadlineTimer = null;
    }
    updateTerminalScrollbar();
  };

  const beginSuppressingInitialTerminalPaint = () => {
    stopSuppressingInitialTerminalPaint();
    suppressInitialTerminalPaint = terminalHistoryLoaded && terminalMaxScroll() > 0;
    if (suppressInitialTerminalPaint) {
      terminalInitialPaintDeadlineTimer = setTimeout(stopSuppressingInitialTerminalPaint, 1200);
    }
  };

  const shouldSuppressTerminalLiveOutput = () => {
    if (!suppressInitialTerminalPaint) {
      return false;
    }
    if (terminalInitialPaintIdleTimer) {
      clearTimeout(terminalInitialPaintIdleTimer);
    }
    terminalInitialPaintIdleTimer = setTimeout(stopSuppressingInitialTerminalPaint, 180);
    updateTerminalScrollbar();
    return true;
  };

  const writeTerminalOutput = (data) => {
    const text = typeof data === "string"
      ? data
      : terminalDecoder.decode(data instanceof Uint8Array ? data : new Uint8Array(data), {stream: true});
    const filtered = filterTerminalOutput(text);
    if (!filtered) {
      updateTerminalScrollbar();
      return;
    }
    if (shouldSuppressTerminalLiveOutput()) {
      return;
    }
    term.write(filtered, updateTerminalScrollbar);
  };

  const loadTerminalHistory = () => {
    if (terminalHistoryLoaded) {
      return Promise.resolve();
    }
    if (terminalHistoryPromise) {
      return terminalHistoryPromise;
    }
    const previousStatus = status.textContent;
    status.textContent = "Loading terminal history";
    terminalHistoryPromise = fetch(`/api/nodes/${encodeURIComponent(node)}/sessions/${encodeURIComponent(session)}/output?lines=2500`)
      .then(async (response) => {
        if (!response.ok) {
          return;
        }
        const body = await response.json();
        const output = terminalHistoryText(body.output || "");
        if (output.trim()) {
          await new Promise((resolve) => term.write(`${output}\r\n`, resolve));
          term.scrollToBottom();
          updateTerminalScrollbar();
        }
        terminalHistoryLoaded = true;
      })
      .catch(() => {})
      .finally(() => {
        if (status.textContent === "Loading terminal history") {
          status.textContent = previousStatus || "Connecting";
        }
        terminalHistoryPromise = null;
      });
    return terminalHistoryPromise;
  };

  const clearKeepalive = () => {
    if (keepaliveTimer) {
      clearInterval(keepaliveTimer);
      keepaliveTimer = null;
    }
  };

  const scheduleReconnect = (event) => {
    if (closedByPage || terminalPaused || reconnectTimer) {
      return;
    }
    clearKeepalive();
    if (reconnectAttempts >= 2) {
      startHttpTerminal();
      return;
    }
    const reason = event && event.reason ? ` · ${event.reason}` : "";
    const code = event && event.code ? event.code : "closed";
    const delay = Math.min(10000, 1000 * Math.max(1, 2 ** reconnectAttempts));
    reconnectAttempts += 1;
    status.textContent = `Disconnected (${code})${reason} · reconnecting in ${Math.round(delay / 1000)}s`;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connectTerminal();
    }, delay);
  };

  const connectTerminal = () => {
    if (terminalPaused || transport !== "websocket") {
      return;
    }
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    status.textContent = reconnectAttempts ? "Reconnecting" : "Connecting";
    socket = new WebSocket(terminalUrl);
    socket.binaryType = "arraybuffer";

    socket.addEventListener("open", () => {
      reconnectAttempts = 0;
      status.textContent = "Connected";
      fit();
      beginSuppressingInitialTerminalPaint();
      clearKeepalive();
      keepaliveTimer = setInterval(() => {
        sendTerminalMessage({type: "ping", time: Date.now()});
      }, 20000);
    });
    socket.addEventListener("message", (event) => {
      writeTerminalOutput(event.data);
    });
    socket.addEventListener("close", scheduleReconnect);
    socket.addEventListener("error", () => {
      status.textContent = "Terminal connection error";
    });
  };

  const startHttpTerminal = async () => {
    if (terminalPaused || transport === "http") {
      return;
    }
    transport = "http";
    updateTerminalTransport();
    clearKeepalive();
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    status.textContent = "Switching to HTTP terminal";
    try {
      const response = await fetch(`/api/nodes/${encodeURIComponent(node)}/sessions/${encodeURIComponent(session)}/terminal-http`, {
        method: "POST"
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        status.textContent = body.detail || "HTTP terminal failed";
        transport = "websocket";
        updateTerminalTransport();
        return;
      }
      const body = await response.json();
      httpTerminalId = body.terminal_id;
      status.textContent = "Connected · HTTP fallback";
      fit();
      beginSuppressingInitialTerminalPaint();
      pollHttpTerminal();
    } catch {
      status.textContent = "HTTP terminal connection error";
      transport = "websocket";
      updateTerminalTransport();
    }
  };

  const writeBase64Chunk = (chunk) => {
    const raw = atob(chunk);
    const bytes = new Uint8Array(raw.length);
    for (let index = 0; index < raw.length; index += 1) {
      bytes[index] = raw.charCodeAt(index);
    }
    writeTerminalOutput(bytes);
  };

  const pollHttpTerminal = async () => {
    if (!httpTerminalId || httpPolling) {
      return;
    }
    httpPolling = true;
    while (!closedByPage && transport === "http" && httpTerminalId) {
      try {
        const response = await fetch(`/api/terminal-http/${encodeURIComponent(httpTerminalId)}/output?timeout=1.5`);
        if (!response.ok) {
          status.textContent = "HTTP terminal disconnected";
          break;
        }
        const body = await response.json();
        for (const chunk of body.chunks || []) {
          writeBase64Chunk(chunk);
        }
        if (body.closed) {
          status.textContent = "HTTP terminal closed";
          break;
        }
      } catch {
        status.textContent = "HTTP terminal reconnecting";
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
    httpPolling = false;
  };

  const connectPreferredTerminal = async () => {
    terminalPaused = false;
    await loadTerminalHistory();
    if (terminalPaused) {
      return;
    }
    if (preferHttpTerminal) {
      startHttpTerminal();
    } else {
      connectTerminal();
    }
  };

  const pauseTerminal = () => {
    terminalPaused = true;
    setTerminalInputUnlocked(false);
    stopSuppressingInitialTerminalPaint();
    clearKeepalive();
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (socket) {
      socket.close(1000, "terminal collapsed");
      socket = null;
    }
    if (httpTerminalId) {
      fetch(`/api/terminal-http/${encodeURIComponent(httpTerminalId)}`, {method: "DELETE", keepalive: true}).catch(() => {});
      httpTerminalId = "";
    }
    transport = "websocket";
    updateTerminalTransport();
    httpPolling = false;
    reconnectAttempts = 0;
    status.textContent = "Collapsed · terminal paused";
  };

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !terminalPaused && (!socket || socket.readyState === WebSocket.CLOSED)) {
      connectPreferredTerminal();
    }
  });

  window.addEventListener("beforeunload", () => {
    closedByPage = true;
    clearKeepalive();
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
    }
    if (socket) {
      socket.close(1000, "page unload");
    }
    if (httpTerminalId) {
      fetch(`/api/terminal-http/${encodeURIComponent(httpTerminalId)}`, {method: "DELETE", keepalive: true}).catch(() => {});
    }
  });

  if (parentTerminalBand && !parentTerminalBand.classList.contains("is-open")) {
    pauseTerminal();
  } else {
    connectPreferredTerminal();
  }
  if (parentTerminalToggle) {
    parentTerminalToggle.addEventListener("click", () => {
      if (parentTerminalBand.classList.contains("is-open")) {
        setTimeout(() => {
          fit();
          connectPreferredTerminal();
        }, 0);
      } else {
        pauseTerminal();
      }
    });
  }
  };
  window.StarAgentAfterPaint(() => {
    initializeTerminal().catch((error) => {
      const status = terminal.querySelector(".terminal-connection-state");
      if (status) {
        status.textContent = error?.message || "Terminal assets failed to load";
      }
    });
  }, 500);
}
