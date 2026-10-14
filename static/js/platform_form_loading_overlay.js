/* Overlay compartilhado para formulários que iniciam processamento demorado. */
(() => {
  const forms = document.querySelectorAll('[data-platform-loading-overlay-form]');
  if (!forms.length) return;

  const stateFor = (form) => ({
    overlay: document.getElementById(form.dataset.loadingOverlayTarget),
    submit: form.querySelector('[data-loading-overlay-submit]'),
  });

  const show = (form) => {
    const { overlay, submit } = stateFor(form);
    form.setAttribute('aria-busy', 'true');
    if (submit) {
      submit.disabled = true;
      submit.setAttribute('aria-disabled', 'true');
    }
    if (overlay) {
      overlay.hidden = false;
      overlay.setAttribute('aria-hidden', 'false');
    }
    document.body.classList.add('is-processing');
  };

  const reset = (form) => {
    const { overlay, submit } = stateFor(form);
    form.removeAttribute('aria-busy');
    if (submit) {
      submit.disabled = false;
      submit.removeAttribute('aria-disabled');
    }
    if (overlay) {
      overlay.hidden = true;
      overlay.setAttribute('aria-hidden', 'true');
    }
    document.body.classList.remove('is-processing');
  };

  // Fluxos assíncronos podem reutilizar o mesmo painel visual sem recriar
  // spinner, foco ou proteção contra duplo envio no próprio módulo de negócio.
  window.PlatformFormLoadingOverlay = { show, reset };

  forms.forEach((form) => {
    if (form.hasAttribute?.('data-platform-async-form')) return;
    let submitting = false;

    const resetForm = () => {
      submitting = false;
      reset(form);
    };

    form.addEventListener('submit', (event) => {
      if (!form.checkValidity()) return;
      if (submitting) {
        event.preventDefault();
        return;
      }
      submitting = true;
      show(form);
    });

    // Quando o navegador restaura a página do cache, a requisição anterior já
    // terminou; devolver o controle evita um overlay residual.
    window.addEventListener('pageshow', resetForm);
  });
})();
