(() => {
  const source = document.getElementById('dados-dashboard');
  if (!source || !window.Plotly || !window.PesquisaPdfPlotTheme) return;
  const dados = JSON.parse(source.textContent);
  const theme = window.PesquisaPdfPlotTheme;
  const config = { responsive: true, displaylogo: false };

  Object.entries(dados.indicadores || {}).forEach(([nome, valor]) => {
    const indicador = document.getElementById(`indicador-${nome}`);
    if (indicador) indicador.textContent = Number(valor).toLocaleString('pt-BR');
  });

  function vazio(target, mensagem) {
    if (!target) return;
    if (target.classList.contains('js-plotly-plot')) Plotly.purge(target);
    target.replaceChildren();
    const aviso = document.createElement('p');
    aviso.className = 'empty-chart';
    aviso.textContent = mensagem;
    target.appendChild(aviso);
  }

  function dimensoes(target, colunas, linhas, { compacta = false } = {}) {
    if (compacta) return theme.matrixDimensions(target, colunas, linhas);
    const largura = Math.max(target.parentElement?.clientWidth || target.clientWidth || 0, 280);
    target.style.width = '100%';
    target.style.minWidth = '0';
    return { largura, altura: Math.max(290, linhas * 38 + 145) };
  }

  function mapaDeCalor() {
    const alvo = document.getElementById('grafico-termos-documentos');
    const serie = dados.termos_documentos || {};
    const termos = serie.termos || [];
    const documentos = serie.documentos || [];
    if (!alvo || !termos.length || !documentos.length) return vazio(alvo, 'Não há termos e documentos suficientes para compor a matriz.');
    const tamanho = dimensoes(alvo, documentos.length, termos.length, { compacta: true });
    const documentosVisiveis = documentos.map(documento => theme.abbreviateLabel(documento, 22));
    const termosVisiveis = termos.map(termo => theme.abbreviateLabel(termo, 28));
    const detalhes = termos.map(termo => documentos.map(documento => [documento, termo]));
    Plotly.react(alvo, [{
      type: 'heatmap', x: documentosVisiveis, y: termosVisiveis, z: serie.matriz || [], customdata: detalhes,
      colorscale: document.documentElement.dataset.theme === 'light' ? 'Blues' : 'Tealgrn',
      colorbar: { title: { text: 'Ocorrências', font: { color: theme.color('--plot-text') } }, tickfont: { color: theme.color('--plot-text') } },
      hovertemplate: 'Documento: %{customdata[0]}<br>Termo: %{customdata[1]}<br><b>%{z} ocorrência(s)</b><extra></extra>',
    }], {
      ...theme.layout({ l: 145, r: 80, t: 20, b: 74 }), width: tamanho.largura, height: tamanho.altura,
      xaxis: theme.axis({ title: 'Documentos', automargin: true, tickangle: 0 }),
      yaxis: theme.axis({ title: 'Termos', automargin: true, autorange: 'reversed' }),
    }, config);
  }

  function frequenciaRelativa() {
    const alvo = document.getElementById('grafico-frequencia-relativa');
    const serie = dados.frequencia_relativa || {};
    const documentos = serie.documentos || [];
    const porMil = serie.por_mil || [];
    if (!alvo || !documentos.length) return vazio(alvo, 'Não há documentos suficientes para calcular a frequência relativa.');
    if (!porMil.some(valor => Number.isFinite(valor))) {
      return vazio(alvo, 'A contagem de palavras não foi registrada nesta execução; não é possível normalizar sem reprocessar os documentos.');
    }
    const tamanho = dimensoes(alvo, 1, documentos.length);
    const ocorrencias = serie.ocorrencias || [];
    const palavras = serie.palavras || [];
    const valores = porMil.map(valor => Number.isFinite(valor) ? valor : 0);
    const documentosVisiveis = documentos.map(documento => theme.abbreviateLabel(documento, 28));
    const detalhes = documentos.map((documento, indice) => [
      documento,
      ocorrencias[indice] || 0,
      palavras[indice] || 0,
      Number.isFinite(porMil[indice]) ? Number(porMil[indice]).toLocaleString('pt-BR', { maximumFractionDigits: 2 }) : 'não disponível',
    ]);
    Plotly.react(alvo, [{
      type: 'bar', orientation: 'h', y: documentosVisiveis, x: valores, customdata: detalhes,
      marker: { color: theme.palette()[1] },
      hovertemplate: 'Documento: %{customdata[0]}<br>Ocorrências: %{customdata[1]}<br>Palavras consideradas: %{customdata[2]}<br><b>%{customdata[3]} por 1.000 palavras</b><extra></extra>',
    }], {
      ...theme.layout({ l: 145, r: 24, t: 20, b: 60 }), width: tamanho.largura, height: tamanho.altura,
      xaxis: theme.axis({ title: 'Ocorrências por 1.000 palavras', rangemode: 'tozero' }),
      yaxis: theme.axis({ title: 'Documentos', automargin: true, autorange: 'reversed' }),
    }, config);
  }

  function renderizar() {
    mapaDeCalor();
    frequenciaRelativa();
  }

  renderizar();
  document.addEventListener('tema-alterado', renderizar);
  let quadro;
  const redimensionar = () => {
    cancelAnimationFrame(quadro);
    quadro = requestAnimationFrame(() => document.querySelectorAll('.analysis-chart-scroll .js-plotly-plot')
      .forEach(grafico => Plotly.Plots.resize(grafico)));
  };
  window.addEventListener('resize', redimensionar, { passive: true });
})();
