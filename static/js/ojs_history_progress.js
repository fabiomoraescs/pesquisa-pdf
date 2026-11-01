/* Acompanha somente estado já persistido pelo OJS SEO; nunca aciona workers. */
(() => {
  const root = document.querySelector?.('[data-ojs-history-progress]');
  if (!root || root.dataset.progressPollingInitialized === 'true') return;

  const progressUrl = root.dataset.progressUrl;
  if (!progressUrl || root.dataset.progressActive !== 'true' || typeof window.fetch !== 'function') return;

  root.dataset.progressPollingInitialized = 'true';
  const pollInterval = 5000;
  const maxBackoff = 30000;
  let active = true;
  let inFlight = false;
  let timer = null;
  let retryCount = 0;
  let lastStage = root.dataset.progressStage || '';

  const number = (value) => Math.max(0, Number.isFinite(Number(value)) ? Number(value) : 0);
  const percentage = (value) => Math.min(100, number(value));
  const clearTimer = () => {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  };
  const stop = () => {
    active = false;
    clearTimer();
    root.dataset.progressActive = 'false';
  };
  const schedule = (delay) => {
    if (!active || timer !== null || document.visibilityState === 'hidden') return;
    timer = window.setTimeout(() => {
      timer = null;
      poll();
    }, delay);
  };
  const coverageLabel = (coverage) => (
    `Progresso da análise das páginas: ${coverage.verified_percentage}% efetivamente verificado; `
    + `${coverage.verified_article_count} páginas verificadas nesta referência; ${coverage.reused_evidence_article_count || 0} evidências históricas reaproveitadas; ${coverage.unverified_article_count} não verificadas; `
    + `${coverage.unprocessed_article_count} artigos não processados; ${coverage.eligible_article_count} elegíveis.`
  );
  const renderCoverage = (coverage) => {
    if (!coverage) return;
    const eligible = number(coverage.eligible_article_count);
    const values = {
      verified: number(coverage.verified_article_count),
      reused: number(coverage.reused_evidence_article_count),
      unverified: number(coverage.unverified_article_count),
      unprocessed: number(coverage.unprocessed_article_count),
      eligible,
    };
    const widths = {
      verified: eligible ? values.verified / eligible * 100 : 0,
      reused: eligible ? values.reused / eligible * 100 : 0,
      unverified: eligible ? values.unverified / eligible * 100 : 0,
      unprocessed: eligible ? values.unprocessed / eligible * 100 : 0,
    };
    document.querySelectorAll?.('[data-ojs-coverage-segment]').forEach((segment) => {
      segment.style.width = `${percentage(widths[segment.dataset.ojsCoverageSegment])}%`;
    });
    document.querySelectorAll?.('[data-ojs-coverage-value]').forEach((element) => {
      const key = element.dataset.ojsCoverageValue;
      if (Object.prototype.hasOwnProperty.call(values, key)) element.textContent = String(values[key]);
    });
    const verifiedPercentage = Math.round(percentage(coverage.verified_percentage));
    const percentageElement = document.querySelector?.('[data-ojs-coverage-percentage]');
    if (percentageElement) percentageElement.textContent = `${verifiedPercentage}% verificado nesta referência`;
    const availableElement = document.querySelector?.('[data-ojs-coverage-available]');
    if (availableElement) {
      const available = Math.round(percentage(coverage.available_coverage_percentage));
      availableElement.textContent = `${available}% com evidência disponível${values.reused ? ', incluindo evidências históricas compatíveis' : ''}`;
    }
    const progress = document.querySelector?.('[data-ojs-coverage-progress]');
    if (progress) progress.setAttribute('aria-label', coverageLabel({...coverage, ...values, verified_percentage: verifiedPercentage}));
  };
  const renderMonitoring = (payload) => {
    const monitoring = payload.monitoring || {};
    const status = document.querySelector?.('[data-ojs-progress-status]');
    if (status && monitoring.message) status.textContent = `${monitoring.label}. ${monitoring.message}`;
    const campaign = document.querySelector?.('[data-ojs-progress-campaign-status]');
    if (campaign) {
      if (payload.campaign) {
        campaign.hidden = false;
        campaign.textContent = `Campanha das páginas: ${payload.campaign.status_label}. ${payload.campaign.batch_count} lote(s) registrado(s).`;
      } else {
        campaign.hidden = true;
        campaign.textContent = '';
      }
    }
  };
  const shouldReloadForStage = (nextStage) => (
    lastStage.startsWith('oai_') && nextStage.startsWith('html_')
  );
  const poll = async () => {
    if (!active || inFlight || document.visibilityState === 'hidden') return;
    inFlight = true;
    try {
      const response = await window.fetch(progressUrl, {
        credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'},
      });
      if (response.status === 401 || response.status === 403 || response.status === 404) {
        stop();
        return;
      }
      if (!response.ok) throw new Error('progress_unavailable');
      const payload = await response.json();
      retryCount = 0;
      renderCoverage(payload.coverage);
      renderMonitoring(payload);
      const nextStage = payload.monitoring?.stage || '';
      if (nextStage && shouldReloadForStage(nextStage)) {
        stop();
        window.location.reload();
        return;
      }
      if (nextStage) lastStage = nextStage;
      if (!payload.monitoring?.active) {
        stop();
        // Uma única recarga revela as linhas históricas persistidas no lote final.
        window.location.reload();
        return;
      }
      schedule(pollInterval);
    } catch (_error) {
      // Indicadores existentes permanecem intactos; uma falha transitória não
      // deve parecer uma ausência de resultados persistidos.
      retryCount += 1;
      schedule(Math.min(maxBackoff, pollInterval * (2 ** Math.min(retryCount, 3))));
    } finally {
      inFlight = false;
    }
  };

  document.addEventListener('visibilitychange', () => {
    if (!active) return;
    if (document.visibilityState === 'hidden') {
      clearTimer();
      return;
    }
    retryCount = 0;
    poll();
  });
  window.addEventListener('pageshow', () => {
    if (active && document.visibilityState !== 'hidden') poll();
  });

  if (document.visibilityState !== 'hidden') poll();
})();
