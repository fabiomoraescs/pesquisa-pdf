(() => {
  const formulario = document.getElementById('hr-form');
  if (!formulario) return;
  const arquivos = document.getElementById('pdfs-historico-racial');
  const botao = document.getElementById('hr-analisar');
  const erro = document.getElementById('hr-erro');
  const progresso = document.getElementById('hr-progress');
  const etapa = document.getElementById('hr-etapa');
  const detalhe = document.getElementById('hr-detalhe');
  const barra = document.getElementById('hr-barra');
  const barraValor = document.getElementById('hr-barra-valor');
  const tempo = document.getElementById('hr-tempo');
  let ocupado = false;
  let temporizador = null;

  function selecaoValida() {
    return arquivos.files.length > 0 && [...arquivos.files].every((arquivo) => arquivo.name.toLowerCase().endsWith('.pdf'));
  }

  function mostrarErro(mensagem) {
    erro.textContent = mensagem;
    erro.hidden = !mensagem;
  }

  function atualizarBotao() {
    botao.disabled = ocupado || !selecaoValida();
  }

  arquivos.addEventListener('change', () => {
    mostrarErro(arquivos.files.length && !selecaoValida() ? 'Selecione apenas arquivos PDF.' : '');
    atualizarBotao();
  });

  function atualizarProgresso(dados) {
    etapa.textContent = dados.etapa || 'Preparando arquivos…';
    detalhe.textContent = dados.arquivo_indice && dados.paginas_total
      ? `PDF ${dados.arquivo_indice} de ${dados.arquivos_total} · página ${dados.pagina_atual} de ${dados.paginas_total}`
      : 'Preparando análise…';
    if (Number.isFinite(dados.percentual)) {
      barra.setAttribute('aria-valuenow', String(dados.percentual));
      barraValor.style.width = `${dados.percentual}%`;
      etapa.textContent += ` — ${dados.percentual}%`;
    } else {
      barra.removeAttribute('aria-valuenow');
      barraValor.style.width = '0%';
    }
    tempo.textContent = `Tempo decorrido: ${dados.tempo_decorrido || 0} s`;
  }

  async function consultarProgresso(url) {
    try {
      const resposta = await fetch(url, { cache: 'no-store' });
      const dados = await resposta.json();
      if (!resposta.ok) throw new Error(dados.erro || 'Não foi possível consultar o progresso.');
      atualizarProgresso(dados);
      if (dados.status === 'concluido') {
        etapa.textContent = 'Processamento concluído — 100%';
        window.location.assign(dados.resultado_url);
        return;
      }
      if (dados.status === 'erro') throw new Error(dados.erro || 'Não foi possível processar os PDFs.');
      temporizador = window.setTimeout(() => consultarProgresso(url), 1000);
    } catch (falha) {
      ocupado = false;
      progresso.hidden = true;
      atualizarBotao();
      mostrarErro(falha.message || 'Não foi possível processar os PDFs.');
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
    progresso.hidden = false;
    etapa.textContent = 'Preparando arquivos…';
    detalhe.textContent = 'Enviando PDFs…';
    barra.removeAttribute('aria-valuenow');
    barraValor.style.width = '0%';
    try {
      const resposta = await fetch(formulario.action, {
        method: 'POST', body: new FormData(formulario),
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      });
      const dados = await resposta.json();
      if (!resposta.ok) throw new Error(dados.erro || 'Não foi possível enviar os PDFs.');
      consultarProgresso(dados.progresso_url);
    } catch (falha) {
      if (temporizador) window.clearTimeout(temporizador);
      ocupado = false;
      progresso.hidden = true;
      atualizarBotao();
      mostrarErro(falha.message || 'Não foi possível enviar os PDFs.');
    }
  });
  atualizarBotao();
})();
