(() => {
  const formulario = document.getElementById('hr-form');
  if (!formulario) return;
  const arquivos = document.getElementById('pdfs-historico-racial');
  const duplicate = Boolean(formulario.querySelector('[name="duplicate_id"]'));
  const botao = document.getElementById('hr-analisar');
  const erro = document.getElementById('hr-erro');
  const overlay = document.getElementById('hr-overlay-processamento');
  const titulo = document.getElementById('hr-titulo-processamento');
  const mensagem = document.getElementById('hr-mensagem-processamento');
  const detalhe = document.getElementById('hr-detalhe-processamento');
  const barra = document.getElementById('hr-progresso-processamento');
  const barraValor = document.getElementById('hr-barra-progresso-processamento');
  const tempo = document.getElementById('hr-tempo-decorrido-processamento');
  const restante = document.getElementById('hr-tempo-restante-processamento');
  const fechar = document.getElementById('hr-fechar-processamento');
  const spinner = overlay.querySelector('.loading-spinner');
  const semanticControls = document.getElementById('hr-controles-semanticos');
  const semanticThreshold = document.getElementById('hr-limiar');
  const semanticThresholdValue = document.getElementById('hr-valor-limiar');
  semanticThreshold.addEventListener('input', () => {
    semanticThresholdValue.textContent = Number(semanticThreshold.value).toFixed(2).replace('.', ',');
  });
  document.querySelectorAll('input[name="metodo_analise"]').forEach((radio) => radio.addEventListener('change', () => {
    const hybrid = document.querySelector('input[name="metodo_analise"]:checked')?.value === 'hibrido';
    semanticControls.hidden = !hybrid;
    semanticThreshold.disabled = !hybrid;
  }));
  if (document.querySelector('input[name="metodo_analise"]:checked')?.value === 'hibrido') {
    semanticControls.hidden = false;
    semanticThreshold.disabled = false;
  }
  let ocupado = false;
  let temporizador = null;

  function selecaoValida() {
    return (duplicate || arquivos.files.length > 0) && [...arquivos.files].every((arquivo) => arquivo.name.toLowerCase().endsWith('.pdf'));
  }

  function mostrarErro(texto) {
    erro.textContent = texto;
    erro.hidden = !texto;
  }

  function atualizarBotao() {
    botao.disabled = ocupado || !selecaoValida();
  }

  function formatarDuracao(segundos) {
    const total = Math.max(0, Math.round(segundos));
    const minutos = Math.floor(total / 60);
    return minutos ? `${minutos} min ${total % 60} s` : `${total} s`;
  }

  function exibirOverlay(visivel) {
    overlay.hidden = !visivel;
    overlay.setAttribute('aria-hidden', String(!visivel));
    document.body.classList.toggle('is-processing', visivel);
  }

  function atualizarProgresso(dados) {
    const percentual = Number.isFinite(dados.percentual) ? Math.max(0, Math.min(100, dados.percentual)) : null;
    const etapa = dados.etapa || 'Preparando arquivos…';
    titulo.textContent = percentual === null ? 'Processando os arquivos...' : `Processando os arquivos — ${percentual}%`;
    mensagem.textContent = `Etapa: ${etapa}`;
    detalhe.textContent = dados.bloco_atual != null && dados.blocos_total
      ? `PDF ${dados.arquivo_indice} de ${dados.arquivos_total} · ${dados.bloco_atual} de ${dados.blocos_total} blocos processados`
      : dados.arquivo_indice && dados.paginas_total
      ? `PDF ${dados.arquivo_indice} de ${dados.arquivos_total} · página ${dados.pagina_atual} de ${dados.paginas_total}`
      : dados.arquivo_atual || 'Preparando análise…';
    if (percentual === null) {
      barra.classList.add('is-indeterminate');
      barra.removeAttribute('aria-valuenow');
      barra.setAttribute('aria-valuetext', 'Progresso ainda não disponível');
      barraValor.style.width = '';
    } else {
      barra.classList.remove('is-indeterminate');
      barra.setAttribute('aria-valuenow', String(percentual));
      barra.setAttribute('aria-valuetext', `Progresso: ${percentual}%`);
      barraValor.style.width = `${percentual}%`;
    }
    tempo.textContent = `Tempo decorrido: ${formatarDuracao(dados.tempo_decorrido || 0)}`;
    restante.textContent = Number.isFinite(dados.eta_segundos)
      ? `Tempo restante estimado: ~${formatarDuracao(dados.eta_segundos)}`
      : 'Calculando tempo restante…';
  }

  function falhaProcessamento(texto) {
    if (temporizador) window.clearTimeout(temporizador);
    temporizador = null;
    ocupado = false;
    atualizarBotao();
    mostrarErro(texto);
    titulo.textContent = 'Erro durante o processamento';
    mensagem.textContent = texto;
    detalhe.textContent = '';
    barra.classList.remove('is-indeterminate');
    barra.removeAttribute('aria-valuenow');
    barraValor.style.width = '0%';
    restante.textContent = '';
    spinner.hidden = true;
    fechar.hidden = false;
    fechar.focus();
  }

  arquivos.addEventListener('change', () => {
    mostrarErro(arquivos.files.length && !selecaoValida() ? 'Selecione apenas arquivos PDF.' : '');
    atualizarBotao();
  });
  fechar.addEventListener('click', () => exibirOverlay(false));

  async function consultarProgresso(url) {
    try {
      const resposta = await fetch(url, { cache: 'no-store' });
      const dados = await resposta.json();
      if (!resposta.ok) throw new Error(dados.erro || 'Não foi possível consultar o progresso.');
      atualizarProgresso(dados);
      if (dados.status === 'concluido') {
        titulo.textContent = 'Raspagem concluída — 100%';
        window.location.assign(dados.resultado_url);
        return;
      }
      if (dados.status === 'erro') {
        falhaProcessamento(dados.erro || 'Não foi possível processar os PDFs.');
        return;
      }
      temporizador = window.setTimeout(() => consultarProgresso(url), 1000);
    } catch (falha) {
      falhaProcessamento(falha.message || 'Não foi possível processar os PDFs.');
    }
  }

  formulario.addEventListener('submit', async (evento) => {
    evento.preventDefault();
    if (ocupado) return;
    if (!selecaoValida()) {
      mostrarErro('Selecione ao menos um arquivo PDF válido.');
      atualizarBotao();
      return;
    }
    ocupado = true;
    atualizarBotao();
    mostrarErro('');
    fechar.hidden = true;
    spinner.hidden = false;
    exibirOverlay(true);
    atualizarProgresso({ etapa: 'Preparando arquivos…', percentual: null });
    try {
      const resposta = await fetch(formulario.action, {
        method: 'POST', body: new FormData(formulario),
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      });
      const dados = await resposta.json();
      if (!resposta.ok) throw new Error(dados.erro || 'Não foi possível enviar os PDFs.');
      consultarProgresso(dados.progresso_url);
    } catch (falha) {
      falhaProcessamento(falha.message || 'Não foi possível enviar os PDFs.');
    }
  });
  atualizarBotao();
})();
