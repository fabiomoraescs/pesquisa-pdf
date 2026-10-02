"use strict";

// Conveniência visual apenas: o servidor revalida integralmente a estratégia,
// a cadeia e os providers antes de persistir qualquer alteração.
(() => {
  const form = document.querySelector("[data-ai-settings-form]");
  if (!form) return;
  const strategy = form.querySelector("[data-ai-strategy]");
  const primary = form.querySelector("[data-ai-primary]");
  const fallback = form.querySelector("[data-ai-fallback]");
  const enabledControls = [...form.querySelectorAll("[data-ai-enabled]")];
  const fallbackControls = [...form.querySelectorAll("[data-ai-fallback-provider]")];
  const enabledBadges = [...form.querySelectorAll("[data-ai-enabled-badge]")];
  const primaryBadges = [...form.querySelectorAll("[data-ai-primary-badge]")];
  const connectionTests = [...form.querySelectorAll("[data-ai-connection-test]")];
  const csrf = form.querySelector("[name=csrf_token]");

  const refresh = () => {
    const enabled = new Set(enabledControls.filter((control) => control.checked).map((control) => control.value));
    [...primary.options].forEach((option) => { option.disabled = !enabled.has(option.value); });
    if (primary.selectedOptions[0]?.disabled) {
      const available = [...primary.options].find((option) => !option.disabled);
      if (available) primary.value = available.value;
    }
    fallback.hidden = strategy.value !== "fallback";
    fallbackControls.forEach((control) => {
      const disabled = control.value === primary.value || !enabled.has(control.value);
      control.disabled = disabled;
      if (disabled) control.checked = false;
    });
    enabledBadges.forEach((badge) => {
      badge.textContent = enabled.has(badge.dataset.aiEnabledBadge) ? "Habilitado" : "Desabilitado";
    });
    primaryBadges.forEach((badge) => {
      badge.hidden = badge.dataset.aiPrimaryBadge !== primary.value;
    });
  };

  strategy.addEventListener("change", refresh);
  primary.addEventListener("change", refresh);
  enabledControls.forEach((control) => control.addEventListener("change", refresh));

  const updateConnectionStatus = (providerId, state, label) => {
    const status = form.querySelector(`[data-ai-connection-status="${providerId}"]`);
    if (!status || typeof label !== "string" || !label.trim()) return;
    status.textContent = label;
    if (typeof state === "string" && state) status.dataset.aiConnectionState = state;
  };

  connectionTests.forEach((button) => {
    button.addEventListener("click", async (event) => {
      // Sem JavaScript o próprio submit continua sendo um fallback acessível.
      event.preventDefault();
      if (!button.formAction || !csrf?.value) return;
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Testando…";
      try {
        const response = await fetch(button.formAction, {
          method: "POST",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRFToken": csrf.value,
          },
          body: `csrf_token=${encodeURIComponent(csrf.value)}`,
        });
        let payload = null;
        try {
          payload = await response.json();
        } catch (_error) {
          payload = null;
        }
        if (payload && typeof payload === "object") {
          updateConnectionStatus(
            button.dataset.aiConnectionTest,
            payload.connection_state,
            payload.connection_status,
          );
        } else {
          updateConnectionStatus(button.dataset.aiConnectionTest, "failed", "Falha na conexão");
        }
      } catch (_error) {
        updateConnectionStatus(button.dataset.aiConnectionTest, "failed", "Falha na conexão");
      } finally {
        button.disabled = false;
        button.textContent = original;
      }
    });
  });
  refresh();
})();
