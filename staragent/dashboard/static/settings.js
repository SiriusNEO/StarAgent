(() => {
  const options = Array.from(document.querySelectorAll("[data-language]"));
  const status = document.querySelector(".settings-language-status");
  if (!options.length) {
    return;
  }

  const t = (key) => window.StarAgentI18n?.t(key) || key;
  const setStatus = (message, isError = false) => {
    if (!status) {
      return;
    }
    status.textContent = message;
    status.classList.toggle("is-error", isError);
  };
  const setBusy = (busy) => {
    for (const option of options) {
      option.disabled = busy;
    }
  };

  for (const option of options) {
    option.addEventListener("click", async () => {
      const language = option.dataset.language;
      if (!language || language === window.StarAgentI18n?.language) {
        return;
      }
      setBusy(true);
      setStatus(t("settings.language.saving"));
      try {
        const response = await fetch("/api/settings/language", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({language}),
        });
        if (!response.ok) {
          throw new Error("language preference request failed");
        }
        setStatus(t("settings.language.saved"));
        window.location.reload();
      } catch {
        setBusy(false);
        setStatus(t("settings.language.failed"), true);
      }
    });
  }
})();
