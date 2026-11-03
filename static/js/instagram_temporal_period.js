/* Controlador reutilizável dos presets temporais do Analytics Instagram. */
(() => {
  const update = (control) => {
    const select = control.querySelector?.('[data-instagram-temporal-period-select]');
    const custom = control.querySelector?.('[data-instagram-temporal-period-custom]');
    if (!select || !custom) return;
    const customActive = select.value === 'custom';
    custom.hidden = !customActive;
    custom.querySelectorAll('[data-instagram-temporal-period-label]').forEach((label) => {
      label.classList.toggle('visually-hidden', !customActive);
      label.classList.toggle('form-label', customActive);
    });
    custom.querySelectorAll('input[type="date"]').forEach((input) => {
      input.disabled = !customActive || select.disabled;
      input.required = customActive;
    });
    const warning = control.querySelector?.('[data-instagram-temporal-all-history-warning]');
    if (warning) warning.hidden = !['all', 'all_history'].includes(select.value);
  };

  const requestFilterUpdate = (control) => {
    if (!control.hasAttribute('data-instagram-temporal-auto-submit')) return;
    const form = control.closest('form');
    if (!form || form.method.toLowerCase() !== 'get') return;
    form.dataset.resetPagination = 'true';
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else HTMLFormElement.prototype.submit.call(form);
  };

  const customDatesAreReady = (control) => {
    const custom = control.querySelector?.('[data-instagram-temporal-period-custom]');
    const dates = custom ? [...custom.querySelectorAll('input[type="date"]')] : [];
    return dates.length === 2 && dates.every((input) => !input.disabled && input.value && input.checkValidity());
  };

  const updateAll = () => document.querySelectorAll?.('[data-instagram-temporal-period]').forEach(update);

  document.addEventListener('change', (event) => {
    const select = event.target.closest?.('[data-instagram-temporal-period-select]');
    if (select) {
      const control = select.closest('[data-instagram-temporal-period]');
      if (!control) return;
      update(control);
      if (select.value !== 'custom') requestFilterUpdate(control);
      return;
    }
    const date = event.target.closest?.('[data-instagram-temporal-period-custom] input[type="date"]');
    if (date) {
      const control = date.closest('[data-instagram-temporal-period]');
      if (control && customDatesAreReady(control)) requestFilterUpdate(control);
    }
  });

  document.addEventListener('platform:partial-updated', updateAll);
  updateAll();
})();
