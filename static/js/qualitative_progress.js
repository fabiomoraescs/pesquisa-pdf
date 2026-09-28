(() => {
  const panel = document.querySelector('[data-corpus-progress]');
  if (!panel) return;
  const title = panel.querySelector('h2');
  const stage = panel.querySelector('[data-progress-stage]');
  const detail = panel.querySelector('.loading-progress-detail');
  const error = panel.querySelector('[data-progress-error]');
  const bar = panel.querySelector('[role="progressbar"]');
  const fill = bar.querySelector('.loading-progress__bar');
  let percent = Number(bar.getAttribute('aria-valuenow')) || 0;
  const update = (data) => {
    percent = Math.max(percent, Math.min(100, Number(data.percentual) || 0));
    window.ProcessingProgress.updateBar(bar, fill, percent);
    title.textContent = `Preparando corpus textual — ${percent}%`;
    stage.textContent = data.stage || 'Aguardando início da extração…';
    detail.textContent = data.document_name
      ? `${data.document_name} · página ${data.page_number} de ${data.page_count} (documento ${data.document_index} de ${data.document_count})` : '';
  };
  const poll = async () => {
    try {
      const response = await fetch(panel.dataset.progressUrl, { credentials: 'same-origin', cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error('Não foi possível consultar o progresso.');
      update(data);
      error.hidden = !data.error;
      error.textContent = data.error || '';
      if (data.status === 'erro') return;
      if (data.status === 'concluida' && data.result_url) {
        setTimeout(() => location.assign(data.result_url), 400);
        return;
      }
    } catch (_) {
      error.hidden = false;
      error.textContent = 'Não foi possível atualizar o progresso. Tentando novamente…';
    }
    setTimeout(poll, 1000);
  };
  poll();
})();
