/* Fechamento compartilhado, opt-in: nunca registra dialogs, editores ou sidebar. */
(() => {
  const entries = [];
  const contains = (entry, target) => Boolean(target && (
    entry.panel.contains(target) || entry.triggers.some((trigger) => trigger.contains(target))));
  const close = (entry, restoreFocus = false) => {
    if (!entry.isOpen() || entry.filePicker) return;
    const hadFocus = contains(entry, document.activeElement);
    entry.close();
    entry.triggers.forEach((trigger) => trigger.setAttribute('aria-expanded', 'false'));
    if (restoreFocus && hadFocus) entry.triggers[0]?.focus();
  };
  const opened = (entry) => {
    entries.forEach((other) => { if (other !== entry) close(other); });
    entry.triggers.forEach((trigger) => trigger.setAttribute('aria-expanded', 'true'));
  };
  document.querySelectorAll('[data-temporary-popover]').forEach((panel) => {
    const details = panel.tagName === 'DETAILS';
    const native = panel.hasAttribute('popover');
    const triggers = details ? [panel.querySelector('summary')] :
      [...document.querySelectorAll(`[aria-controls="${panel.id}"]`)];
    const entry = {
      panel, triggers: triggers.filter(Boolean), filePicker: false,
      isOpen: () => details ? panel.open : native ? panel.matches(':popover-open') : !panel.hidden,
      close: () => { if (details) panel.open = false; else if (native) panel.hidePopover(); else panel.hidden = true; },
    };
    entries.push(entry);
    panel.addEventListener('beforetoggle', (event) => {
      if (event.newState === 'open') opened(entry);
    });
    panel.addEventListener('toggle', () => {
      if (entry.isOpen()) opened(entry);
      else entry.triggers.forEach((trigger) => trigger.setAttribute('aria-expanded', 'false'));
    });
    panel.querySelectorAll('input[type="file"]').forEach((input) => {
      input.addEventListener('click', () => { entry.filePicker = true; });
      const finished = () => { entry.filePicker = false; };
      input.addEventListener('change', finished);
      input.addEventListener('cancel', finished);
    });
  });
  // Menus contextuais existentes podem anunciar abertura sem mudar sua lógica.
  document.addEventListener('platform:popover-open', (event) => {
    const entry = entries.find((item) => item.panel === event.detail.panel);
    if (entry) opened(entry);
  });
  const outside = (event) => entries.forEach((entry) => {
    if (!contains(entry, event.target)) close(entry);
  });
  document.addEventListener('pointerdown', outside);
  document.addEventListener('click', outside);
  document.addEventListener('focusin', outside);
  document.addEventListener('focusout', (event) => {
    // Um blur sem destino pode ser saída da janela ou foco voltando ao body.
    // Esperar a atualização do activeElement; o seletor de arquivo é protegido.
    if (!event.relatedTarget) {
      queueMicrotask(() => {
        if (!document.hasFocus()) return;
        entries.forEach((entry) => {
          if (contains(entry, event.target) && !contains(entry, document.activeElement)) close(entry);
        });
      });
      return;
    }
    entries.forEach((entry) => {
      if (contains(entry, event.target) && !contains(entry, event.relatedTarget)) close(entry);
    });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    entries.forEach((entry) => close(entry, true));
  });
})();
