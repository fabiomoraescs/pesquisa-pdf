"use strict";

// A gravação é restrita à configuração de visibilidade; o servidor continua
// responsável por CSRF, autorização, validação do catálogo e auditoria.
(() => {
  const forms = [...document.querySelectorAll("[data-tool-visibility-form]")];
  const refreshButton = document.querySelector("[data-tool-visibility-refresh]");
  if (!forms.length && !refreshButton) return;
  const refreshScrollKey = "analysis:tool-visibility-refresh-scroll";
  let pendingSaves = 0;

  const updateRefreshAvailability = () => {
    if (!refreshButton) return;
    refreshButton.disabled = pendingSaves > 0;
    refreshButton.setAttribute("aria-busy", String(pendingSaves > 0));
  };

  const restoreRefreshScroll = () => {
    try {
      const storedScroll = sessionStorage.getItem(refreshScrollKey);
      if (storedScroll === null) return;
      sessionStorage.removeItem(refreshScrollKey);
      const scrollY = Number(storedScroll);
      if (Number.isFinite(scrollY) && scrollY >= 0) {
        window.requestAnimationFrame(() => window.scrollTo({top: scrollY, behavior: "auto"}));
      }
    } catch (_error) {
      // A restauração de posição é uma melhoria progressiva; o reload nativo
      // continua seguro quando o armazenamento da sessão não está disponível.
    }
  };

  restoreRefreshScroll();

  if (refreshButton) {
    refreshButton.addEventListener("click", () => {
      if (pendingSaves > 0) return;
      try {
        sessionStorage.setItem(refreshScrollKey, String(window.scrollY));
      } catch (_error) {
        // O navegador poderá aplicar sua própria restauração de scroll.
      }
      refreshButton.disabled = true;
      refreshButton.setAttribute("aria-busy", "true");
      window.location.reload();
    });
  }

  const setFeedback = (form, message, visible = false) => {
    const feedback = form.querySelector("[data-tool-visibility-feedback]");
    if (!feedback) return;
    feedback.textContent = message;
    feedback.hidden = !visible;
  };

  const setState = (form, visible) => {
    const control = form.querySelector("[data-tool-visibility-control]");
    if (!control) return;
    control.checked = visible;
    control.dataset.visibilityCurrent = String(visible);

    const item = form.closest("article");
    const badge = item?.querySelector("[data-tool-visibility-badge]");
    if (badge) {
      badge.textContent = visible ? "Visível" : "Oculta";
      badge.classList.toggle("platform-badge-inactive", !visible);
    }

    if (control.dataset.visibilityKind === "group") {
      const groupId = control.dataset.visibilityId;
      const children = document.querySelector(`[data-tool-visibility-children="${groupId}"]`);
      if (children) children.hidden = !visible;
    }
  };

  forms.forEach((form) => {
    const control = form.querySelector("[data-tool-visibility-control]");
    const csrf = form.querySelector("[name=csrf_token]");
    if (!control || !csrf?.value) return;

    let pending = false;
    form.addEventListener("submit", (event) => event.preventDefault());
    control.addEventListener("change", async () => {
      if (pending) return;
      const previousVisible = control.dataset.visibilityCurrent === "true";
      const requestedVisible = control.checked;
      if (requestedVisible === previousVisible) return;

      pending = true;
      pendingSaves += 1;
      updateRefreshAvailability();
      control.disabled = true;
      form.setAttribute("aria-busy", "true");
      setFeedback(form, "Salvando visibilidade…");
      const formData = new FormData(form);
      formData.set("visible", String(requestedVisible));
      let confirmedFailure = false;

      try {
        const response = await fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          headers: {
            Accept: "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRFToken": csrf.value,
          },
          body: formData,
        });
        let payload = null;
        try {
          payload = await response.json();
        } catch (_error) {
          payload = null;
        }
        if (!response.ok || !payload || payload.ok !== true || typeof payload.visible !== "boolean") {
          confirmedFailure = !response.ok && payload !== null;
          throw new Error(payload?.message || payload?.erro || "save_failed");
        }
        setState(form, payload.visible);
        setFeedback(form, payload.changed ? "Visibilidade atualizada." : "A visibilidade já estava configurada dessa forma.");
      } catch (_error) {
        setState(form, previousVisible);
        setFeedback(
          form,
          confirmedFailure
            ? "Não foi possível atualizar a visibilidade. O estado anterior foi preservado."
            : "Não foi possível confirmar a atualização. Recarregue a página para conferir o estado salvo.",
          true,
        );
      } finally {
        pending = false;
        pendingSaves -= 1;
        updateRefreshAvailability();
        control.disabled = false;
        form.removeAttribute("aria-busy");
      }
    });
  });
})();
