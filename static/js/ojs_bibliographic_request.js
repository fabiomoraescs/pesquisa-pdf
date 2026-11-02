/* Solicita e acompanha auditorias OAI-PMH já persistidas; nunca executa workers. */
(() => {
  const root = document.querySelector?.('[data-ojs-history-progress]');
  const forms = Array.from(document.querySelectorAll?.('[data-ojs-bibliographic-request]') || []);
  if (!root || !forms.length || typeof window.fetch !== 'function') return;

  const progressUrl = root.dataset.progressUrl;
  const journalName = root.dataset.ojsJournalName || '';
  const feedback = document.querySelector?.('[data-ojs-async-progress]');
  const feedbackStatus = document.querySelector?.('[data-ojs-async-progress-status]');
  const overlay = document.getElementById('ojs-seo-overlay-processamento');
  const overlayTitle = document.getElementById('ojs-seo-titulo-processamento');
  const overlayContext = document.getElementById('ojs-seo-contexto-processamento');
  const overlayMessage = document.getElementById('ojs-seo-mensagem-processamento');
  const overlayDetail = document.getElementById('ojs-seo-detalhe-processamento');
  const overlayClose = document.getElementById('ojs-seo-fechar-processamento');
  const sharedOverlay = window.PlatformFormLoadingOverlay;
  const pollInterval = 5000;
  let activeForm = null;
  let polling = false;
  let inFlight = false;
  let submitting = false;
  let timer = null;

  const setControlsDisabled = (disabled) => {
    forms.forEach((form) => {
      form.setAttribute('aria-busy', disabled ? 'true' : 'false');
      form.querySelectorAll?.('[data-loading-overlay-submit]').forEach((button) => {
        button.disabled = disabled;
        button.setAttribute('aria-disabled', disabled ? 'true' : 'false');
      });
    });
  };

  const updateOverlay = ({ title, message, detail }) => {
    if (overlayTitle && title) overlayTitle.textContent = title;
    if (overlayContext && journalName) overlayContext.textContent = journalName;
    if (overlayMessage && message) overlayMessage.textContent = message;
    if (overlayDetail) overlayDetail.textContent = detail || '';
  };

  const showOverlay = () => {
    if (activeForm && sharedOverlay?.show) {
      sharedOverlay.show(activeForm);
    } else if (overlay) {
      overlay.hidden = false;
      overlay.setAttribute('aria-hidden', 'false');
      document.body.classList.add('is-processing');
    }
  };

  const hideOverlay = () => {
    if (activeForm && sharedOverlay?.hide) {
      sharedOverlay.hide(activeForm);
    } else if (overlay) {
      overlay.hidden = true;
      overlay.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('is-processing');
    }
  };

  const showFeedback = (label, message, kind = '') => {
    if (!feedback || !feedbackStatus) return;
    feedback.hidden = false;
    feedback.classList.remove('alert', 'alert-success', 'alert-danger', 'alert-warning');
    if (kind) feedback.classList.add('alert', `alert-${kind}`);
    feedbackStatus.textContent = `${label}. ${message}`;
  };

  const clearTimer = () => {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  };

  const stopPolling = () => {
    polling = false;
    clearTimer();
  };

  const schedule = () => {
    if (!polling || timer !== null || document.visibilityState === 'hidden') return;
    timer = window.setTimeout(() => {
      timer = null;
      poll();
    }, pollInterval);
  };

  const completionMessage = (audit) => {
    if (audit?.status === 'completed' || audit?.status === 'partial') {
      return ['Análise concluída', 'Os resultados bibliográficos foram registrados.'];
    }
    if (audit?.status === 'failed') {
      const error = Array.isArray(audit.errors) ? audit.errors.at(-1)?.message : '';
      return ['Não foi possível concluir a análise', error || 'A falha foi registrada no histórico da auditoria.'];
    }
    if (audit?.status === 'canceled') {
      return ['Análise cancelada', 'A solicitação foi cancelada antes da conclusão.'];
    }
    return ['Análise finalizada', 'Atualizando o histórico da auditoria.'];
  };

  const refreshHistory = () => {
    // A recarga é programática: ela revela tabela, indicadores e erros já
    // persistidos sem exigir F5 e sem iniciar qualquer nova operação.
    window.location.reload();
  };

  const renderProgress = (payload) => {
    const monitoring = payload.monitoring || {};
    if (monitoring.active) {
      const label = monitoring.label || 'Processamento em andamento';
      const message = monitoring.message || 'A análise está sendo acompanhada.';
      updateOverlay({ title: label, message, detail: '' });
      showFeedback(label, message);
      if (overlayClose) {
        overlayClose.hidden = false;
        overlayClose.textContent = 'Continuar navegando';
      }
      return true;
    }

    const [label, message] = completionMessage(payload.audit);
    updateOverlay({ title: label, message, detail: '' });
    showFeedback(label, message, payload.audit?.status === 'failed' ? 'danger' : 'success');
    if (overlayClose) overlayClose.hidden = true;
    stopPolling();
    window.setTimeout(refreshHistory, 750);
    return false;
  };

  const poll = async () => {
    if (!polling || inFlight || !progressUrl || document.visibilityState === 'hidden') return;
    inFlight = true;
    try {
      const response = await window.fetch(progressUrl, {
        credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' },
      });
      if (!response.ok) throw new Error('progress_unavailable');
      const payload = await response.json();
      if (renderProgress(payload)) schedule();
    } catch (_error) {
      updateOverlay({
        title: 'Acompanhamento indisponível',
        message: 'Não foi possível atualizar o andamento agora. O histórico mantém o estado registrado.',
        detail: '',
      });
      showFeedback('Acompanhamento indisponível', 'Atualize a página para consultar o estado persistido.', 'warning');
      schedule();
    } finally {
      inFlight = false;
    }
  };

  const beginPolling = () => {
    if (polling) return;
    polling = true;
    poll();
  };

  overlayClose?.addEventListener('click', () => hideOverlay());
  document.addEventListener('visibilitychange', () => {
    if (!polling) return;
    if (document.visibilityState === 'hidden') {
      clearTimer();
      return;
    }
    poll();
  });

  forms.forEach((form) => form.addEventListener('submit', async (event) => {
    if (!form.checkValidity()) return;
    event.preventDefault();
    if (submitting || polling || inFlight) return;

    submitting = true;
    activeForm = form;
    setControlsDisabled(true);
    updateOverlay({
      title: 'Preparando análise bibliográfica',
      message: 'A solicitação está sendo enviada.',
      detail: 'Aguardando resposta segura.',
    });
    if (overlayClose) overlayClose.hidden = true;
    showOverlay();
    try {
      const response = await window.fetch(form.action, {
        method: 'POST', body: new FormData(form), credentials: 'same-origin',
        headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      });
      const payload = await response.json().catch(() => null);
      if (response.status !== 202 || !payload?.progress_url) {
        throw new Error(payload?.message || 'Não foi possível registrar a análise bibliográfica.');
      }
      root.dataset.progressActive = 'true';
      root.dataset.progressStage = payload.stage || 'oai_pending';
      updateOverlay({
        title: 'Aguardando início do processamento',
        message: payload.message,
        detail: '',
      });
      showFeedback('Aguardando início do processamento', payload.message);
      if (overlayClose) {
        overlayClose.hidden = false;
        overlayClose.textContent = 'Continuar navegando';
      }
      beginPolling();
    } catch (error) {
      const message = error?.message || 'Não foi possível registrar a análise bibliográfica.';
      updateOverlay({ title: 'Não foi possível concluir a solicitação', message, detail: '' });
      showFeedback('Não foi possível concluir a solicitação', message, 'danger');
      if (activeForm && sharedOverlay?.reset) sharedOverlay.reset(activeForm);
      else hideOverlay();
      setControlsDisabled(false);
      activeForm = null;
      submitting = false;
    }
  }));
})();
