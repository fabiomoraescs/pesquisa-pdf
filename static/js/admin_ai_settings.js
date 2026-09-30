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
  };

  strategy.addEventListener("change", refresh);
  primary.addEventListener("change", refresh);
  enabledControls.forEach((control) => control.addEventListener("change", refresh));
  refresh();
})();
