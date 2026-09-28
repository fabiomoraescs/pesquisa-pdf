/* Usa a janela e a barra compartilhadas pelas outras ferramentas. */
export function startSemanticProgress(root) {
  const id = crypto.randomUUID();
  const overlay = document.getElementById('qualitative-semantic-overlay-processamento');
  const title = document.getElementById('qualitative-semantic-titulo-processamento');
  const message = document.getElementById('qualitative-semantic-mensagem-processamento');
  const detail = document.getElementById('qualitative-semantic-detalhe-processamento');
  const bar = document.getElementById('qualitative-semantic-progresso-processamento');
  const fill = document.getElementById('qualitative-semantic-barra-progresso-processamento');
  const elapsed = document.getElementById('qualitative-semantic-tempo-decorrido-processamento');
  const remaining = document.getElementById('qualitative-semantic-tempo-restante-processamento');
  const url = root.dataset.semanticProgressUrlTemplate.replace(
    '00000000-0000-0000-0000-000000000000', id);
  let active = true;
  let pollTimer;
  const started = Date.now();
  const render = (state) => {
    const percent = window.ProcessingProgress.updateBar(bar, fill, Number(state.percent));
    title.textContent = 'Processando busca semântica';
    message.textContent = state.document
      ? `${state.document}${state.page_number ? ` · página ${state.page_number}` : ''}`
      : 'Analisando os documentos do projeto…';
    detail.textContent = `${percent}% · ${state.stage}`;
    elapsed.textContent = `Tempo decorrido: ${Math.floor((Date.now() - started) / 1000)} s`;
  };
  remaining.hidden = true; // não inventar estimativa de conclusão
  overlay.hidden = false;
  overlay.setAttribute('aria-hidden', 'false');
  render({percent: 0, stage: 'Preparando busca semântica'});
  const poll = async () => {
    if (!active) return;
    try {
      const response = await fetch(url, {credentials: 'same-origin'});
      if (response.ok && active) render(await response.json());
      // 404 pode ocorrer antes de o POST registrar a operação.
    } catch (_) {
      // O POST continua sendo a fonte do erro exibido ao usuário.
    } finally {
      if (active) pollTimer = window.setTimeout(poll, 400);
    }
  };
  poll();
  return {
    id,
    async stop(succeeded) {
      active = false;
      window.clearTimeout(pollTimer);
      if (succeeded) {
        render({percent: 100, stage: 'Busca semântica concluída'});
        await new Promise(resolve => window.setTimeout(resolve, 180));
      }
      overlay.hidden = true;
      overlay.setAttribute('aria-hidden', 'true');
    },
  };
}
