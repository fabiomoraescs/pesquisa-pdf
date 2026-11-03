/* Acompanha somente o job Meta; o painel visual continua sendo compartilhado. */
(() => {
  const form = document.querySelector?.('[data-instagram-meta-sync-form]');
  if (!form) return;

  const feedback = document.getElementById('instagram-meta-sync-feedback');
  const activeNotice = document.getElementById('instagram-meta-sync-active');
  const periodSelect = form.querySelector?.('[data-meta-sync-period-select]');
  const customPeriod = form.querySelector?.('[data-meta-sync-custom-period]');
  const customDates = customPeriod?.querySelectorAll?.('input[type="date"]') || [];
  const allHistoryWarning = form.querySelector?.('[data-meta-sync-all-history-warning]');
  const overlay = window.PlatformFormLoadingOverlay;
  let statusUrl = form.dataset.metaSyncStatusUrl || activeNotice?.dataset.instagramMetaSyncStatusUrl || '';
  let completionUrl = form.dataset.metaSyncCompletionUrl || activeNotice?.dataset.instagramMetaSyncCompletionUrl || '';
  let timer = null;
  let polling = false;

  const showFeedback = (message, kind) => {
    if (!feedback) return;
    feedback.textContent = message;
    feedback.className = `alert alert-${kind}`;
    feedback.hidden = false;
  };

  const updatePeriodControls = () => {
    if (!periodSelect) return;
    const isCustom = periodSelect.value === 'custom';
    if (customPeriod) customPeriod.hidden = !isCustom;
    customDates.forEach((input) => {
      input.disabled = !isCustom;
      input.required = isCustom;
    });
    if (allHistoryWarning) allHistoryWarning.hidden = periodSelect.value !== 'all_history';
  };

  periodSelect?.addEventListener('change', updatePeriodControls);
  updatePeriodControls();

  const stopPolling = () => {
    polling = false;
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  };

  const finishFailure = (message) => {
    stopPolling();
    overlay?.reset(form);
    showFeedback(message || 'Não foi possível concluir a sincronização. Nenhum dado foi salvo.', 'danger');
  };

  const schedule = () => {
    timer = window.setTimeout(poll, 3000);
  };

  const poll = async () => {
    if (!polling || !statusUrl) return;
    try {
      const response = await window.fetch(statusUrl, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) throw new Error('status_unavailable');
      const payload = await response.json();
      if (payload.status === 'pending' || payload.status === 'running') {
        schedule();
        return;
      }
      if (payload.status === 'completed') {
        stopPolling();
        overlay?.reset(form);
        if (completionUrl) {
          window.location.assign(completionUrl);
        } else {
          showFeedback(payload.message || 'Sincronização concluída.', 'success');
        }
        return;
      }
      if (payload.status === 'failed') {
        finishFailure(payload.message);
        return;
      }
      finishFailure('Não foi possível acompanhar a sincronização. Atualize a página para consultar o estado.');
    } catch (_error) {
      finishFailure('Não foi possível acompanhar a sincronização. Atualize a página para consultar o estado.');
    }
  };

  const beginPolling = (nextStatusUrl, nextCompletionUrl) => {
    if (!nextStatusUrl) return;
    statusUrl = nextStatusUrl;
    completionUrl = nextCompletionUrl || completionUrl;
    if (polling) return;
    polling = true;
    overlay?.show(form);
    poll();
  };

  form.addEventListener('submit', async (event) => {
    if (typeof window.fetch !== 'function' || !form.checkValidity()) return;
    event.preventDefault();
    if (polling) return;
    overlay?.show(form);
    try {
      const response = await window.fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        credentials: 'same-origin',
        headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
      });
      if (response.status !== 202) {
        const payload = await response.json().catch(() => null);
        throw new Error(payload?.message || 'Não foi possível iniciar a sincronização. Tente novamente.');
      }
      const payload = await response.json();
      beginPolling(payload.status_url, payload.completion_url);
    } catch (error) {
      finishFailure(error?.message || 'Não foi possível iniciar a sincronização. Tente novamente.');
    }
  });

  if (statusUrl) beginPolling(statusUrl, completionUrl);

  // BFCache pode restaurar uma página com job ainda ativo; o run no banco é a
  // fonte de verdade e o polling é retomado sem depender do DOM anterior.
  window.addEventListener('pageshow', () => {
    if (statusUrl) beginPolling(statusUrl, completionUrl);
  });
})();
