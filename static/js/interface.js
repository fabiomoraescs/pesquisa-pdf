(() => {
  const chaveTema = 'varredura-pdf-theme';
  const raiz = document.documentElement;
  const botaoTema = document.getElementById('theme-toggle');
  const seletorTema = document.querySelector('[data-theme-selector]');
  const menuTema = seletorTema?.querySelector('[data-theme-menu]');
  const opcoesTema = [...(seletorTema?.querySelectorAll('[data-theme-option]') || [])];
  const temasValidos = new Set(['light', 'dark', 'black']);
  const opcoesVersao = [...document.querySelectorAll('input[name="versao"]')];
  const versaoSelecionada = () => opcoesVersao.find((opcao) => opcao.checked)?.value || '';
  const configuracoesV3 = document.getElementById('configuracoes-v3');
  const limiarSemantico = document.getElementById('limiar-semantico');
  const valorLimiar = document.getElementById('valor-limiar');
  const controleLimiarSemantico = document.getElementById('controle-limiar-semantico');
  const avisoSemantico = document.getElementById('aviso-semantico');

  function temaAtual() {
    return temasValidos.has(raiz.dataset.theme) ? raiz.dataset.theme : 'dark';
  }

  function atualizarBotaoTema() {
    if (!botaoTema) return;
    const tema = temaAtual();
    const nomes = { light: 'Claro', dark: 'Escuro azul', black: 'Preto' };
    const icone = tema === 'light'
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"></path></svg>'
      : tema === 'black'
        ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a9 9 0 1 0 9 9c0-1.3-.3-2.5-.8-3.6A7 7 0 0 1 12 3Z"></path></svg>'
        : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.8 15.6A8.5 8.5 0 0 1 8.4 3.2 8.5 8.5 0 1 0 20.8 15.6Z"></path></svg>';
    botaoTema.innerHTML = icone;
    botaoTema.setAttribute('aria-label', `Aparência: ${nomes[tema]}. Escolher tema`);
    botaoTema.setAttribute('title', `Aparência: ${nomes[tema]}`);
    botaoTema.setAttribute('aria-expanded', String(menuTema && !menuTema.hidden));
    opcoesTema.forEach((opcao) => {
      const selecionada = opcao.dataset.themeOption === tema;
      opcao.setAttribute('aria-checked', String(selecionada));
      const check = opcao.querySelector('[data-theme-check]');
      if (check) check.hidden = !selecionada;
    });
  }

  function aplicarTema(tema, salvar = true) {
    raiz.dataset.theme = temasValidos.has(tema) ? tema : 'dark';
    if (salvar) {
      try { localStorage.setItem(chaveTema, raiz.dataset.theme); } catch (_) { /* preferência opcional */ }
    }
    atualizarBotaoTema();
    document.dispatchEvent(new CustomEvent('tema-alterado', { detail: { tema: raiz.dataset.theme } }));
  }

  if (botaoTema) {
    atualizarBotaoTema();
    botaoTema.addEventListener('click', () => {
      if (!menuTema) return;
      menuTema.hidden = !menuTema.hidden;
      atualizarBotaoTema();
      if (!menuTema.hidden) opcoesTema.find((opcao) => opcao.dataset.themeOption === temaAtual())?.focus();
    });
    opcoesTema.forEach((opcao, indice) => {
      opcao.addEventListener('click', () => {
        aplicarTema(opcao.dataset.themeOption);
        if (menuTema) menuTema.hidden = true;
        atualizarBotaoTema();
        botaoTema.focus();
      });
      opcao.addEventListener('keydown', (event) => {
        if (!menuTema || menuTema.hidden || !opcoesTema.length) return;
        let destino = null;
        if (event.key === 'ArrowDown') destino = opcoesTema[(indice + 1) % opcoesTema.length];
        if (event.key === 'ArrowUp') destino = opcoesTema[(indice - 1 + opcoesTema.length) % opcoesTema.length];
        if (event.key === 'Home') destino = opcoesTema[0];
        if (event.key === 'End') destino = opcoesTema.at(-1);
        if (destino) {
          event.preventDefault();
          destino.focus();
        }
      });
    });
    document.addEventListener('pointerdown', (event) => {
      if (menuTema && !menuTema.hidden && !seletorTema.contains(event.target)) {
        menuTema.hidden = true;
        atualizarBotaoTema();
      }
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && menuTema && !menuTema.hidden) {
        menuTema.hidden = true;
        atualizarBotaoTema();
        botaoTema.focus();
      }
    });
  }

  function atualizarConfiguracoesV3() {
    if (!configuracoesV3 || !opcoesVersao.length) return;
    const ativa = versaoSelecionada() === 'v3';
    configuracoesV3.hidden = !ativa;
    if (controleLimiarSemantico) controleLimiarSemantico.hidden = !ativa;
    if (avisoSemantico) avisoSemantico.hidden = !ativa;
    if (limiarSemantico) limiarSemantico.disabled = !ativa;
  }

  function atualizarLimiar() {
    if (!limiarSemantico || !valorLimiar) return;
    valorLimiar.value = Number(limiarSemantico.value).toLocaleString('pt-BR', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  if (opcoesVersao.length) {
    opcoesVersao.forEach((opcao) => opcao.addEventListener('change', atualizarConfiguracoesV3));
    atualizarConfiguracoesV3();
  }
  if (limiarSemantico) {
    limiarSemantico.addEventListener('input', atualizarLimiar);
    atualizarLimiar();
  }

  window.addEventListener('resize', () => document.dispatchEvent(new Event('dashboard-redimensionar')));
  aplicarTema(temaAtual(), false);
})();
