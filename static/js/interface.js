(() => {
  const chaveTema = 'varredura-pdf-theme';
  const raiz = document.documentElement;
  const botaoTema = document.getElementById('theme-toggle');
  const seletorVersao = document.getElementById('versao');
  const configuracoesV3 = document.getElementById('configuracoes-v3');
  const opcaoLexical = document.getElementById('incluir-lexical');
  const opcaoSemantica = document.getElementById('incluir-semantica');
  const limiarSemantico = document.getElementById('limiar-semantico');
  const valorLimiar = document.getElementById('valor-limiar');
  const controleLimiarSemantico = document.getElementById('controle-limiar-semantico');
  const avisoSemantico = document.getElementById('aviso-semantico');

  function temaAtual() {
    return raiz.dataset.theme === 'dark' ? 'dark' : 'light';
  }

  function atualizarBotaoTema() {
    if (!botaoTema) return;
    const escuro = temaAtual() === 'dark';
    const proximoTema = escuro ? 'claro' : 'escuro';
    const icone = escuro
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"></path></svg>'
      : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.8 15.6A8.5 8.5 0 0 1 8.4 3.2 8.5 8.5 0 1 0 20.8 15.6Z"></path></svg>';
    botaoTema.innerHTML = icone;
    botaoTema.setAttribute('aria-label', `Ativar tema ${proximoTema}`);
    botaoTema.setAttribute('title', `Ativar tema ${proximoTema}`);
    botaoTema.setAttribute('aria-pressed', String(escuro));
  }

  function aplicarTema(tema, salvar = true) {
    raiz.dataset.theme = tema === 'dark' ? 'dark' : 'light';
    if (salvar) {
      try { localStorage.setItem(chaveTema, raiz.dataset.theme); } catch (_) { /* preferência opcional */ }
    }
    atualizarBotaoTema();
    document.dispatchEvent(new CustomEvent('tema-alterado', { detail: { tema: raiz.dataset.theme } }));
  }

  if (botaoTema) {
    atualizarBotaoTema();
    botaoTema.addEventListener('click', () => aplicarTema(temaAtual() === 'dark' ? 'light' : 'dark'));
  }

  function atualizarConfiguracoesV3() {
    if (!configuracoesV3 || !seletorVersao) return;
    const ativa = seletorVersao.value === 'v3';
    configuracoesV3.hidden = !ativa;
    if (opcaoLexical) opcaoLexical.disabled = !ativa;
    if (opcaoSemantica) opcaoSemantica.disabled = !ativa;
    atualizarEstadoSemantico();
  }

  function atualizarEstadoSemantico() {
    const semanticaAtiva = Boolean(
      seletorVersao && seletorVersao.value === 'v3' && opcaoSemantica && opcaoSemantica.checked,
    );
    if (controleLimiarSemantico) controleLimiarSemantico.hidden = !semanticaAtiva;
    if (avisoSemantico) avisoSemantico.hidden = !semanticaAtiva;
    if (limiarSemantico) limiarSemantico.disabled = !semanticaAtiva;
  }

  function atualizarLimiar() {
    if (!limiarSemantico || !valorLimiar) return;
    valorLimiar.value = Number(limiarSemantico.value).toLocaleString('pt-BR', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function manterUmaModalidade(evento) {
    if (!opcaoLexical || !opcaoSemantica) return;
    if (!opcaoLexical.checked && !opcaoSemantica.checked) {
      evento.currentTarget.checked = true;
    }
    atualizarEstadoSemantico();
  }

  if (seletorVersao) {
    seletorVersao.addEventListener('change', atualizarConfiguracoesV3);
    atualizarConfiguracoesV3();
  }
  if (opcaoLexical && opcaoSemantica) {
    opcaoLexical.addEventListener('change', manterUmaModalidade);
    opcaoSemantica.addEventListener('change', manterUmaModalidade);
  }
  if (limiarSemantico) {
    limiarSemantico.addEventListener('input', atualizarLimiar);
    atualizarLimiar();
  }

  window.addEventListener('resize', () => document.dispatchEvent(new Event('dashboard-redimensionar')));
  aplicarTema(temaAtual(), false);
})();
