(() => {
  const source = document.getElementById('structured-analysis-chart-data');
  if (!source || !window.Plotly || !window.PesquisaPdfPlotTheme) return;
  const dados = JSON.parse(source.textContent);
  const theme = window.PesquisaPdfPlotTheme;
  const config = { responsive: true, displaylogo: false };

  function vazio(target, mensagem = 'Não há ocorrências para exibir nesta visualização.') {
    if (!target) return;
    if (target.classList.contains('js-plotly-plot')) Plotly.purge(target);
    target.replaceChildren();
    const aviso = document.createElement('p');
    aviso.className = 'empty-chart';
    aviso.textContent = mensagem;
    target.appendChild(aviso);
  }

  function dimensoes(target, colunas, linhas, { compacta = false } = {}) {
    if (compacta) return theme.matrixDimensions(target, colunas, linhas, { left: 160, rowHeight: 42, minHeight: 300 });
    const largura = Math.max(target.parentElement?.clientWidth || target.clientWidth || 0, 280);
    target.style.width = '100%';
    target.style.minWidth = '0';
    return { largura, altura: Math.max(300, linhas * 42 + 155) };
  }

  function mapaDeCalor() {
    const alvo = document.getElementById('grafico-grupos-documentos');
    const serie = dados.grupos_documentos || {};
    const grupos = serie.grupos || [];
    const documentos = serie.documentos || [];
    if (!alvo || !grupos.length || !documentos.length) return vazio(alvo, 'Não há grupos e documentos suficientes para compor a matriz.');
    const tamanho = dimensoes(alvo, documentos.length, grupos.length, { compacta: true });
    const documentosVisiveis = documentos.map(documento => theme.abbreviateLabel(documento, 22));
    const gruposVisiveis = grupos.map(grupo => theme.abbreviateLabel(grupo, 30));
    const detalhes = grupos.map(grupo => documentos.map(documento => [documento, grupo]));
    Plotly.react(alvo, [{
      type: 'heatmap', x: documentosVisiveis, y: gruposVisiveis, z: serie.matriz || [], customdata: detalhes,
      colorscale: document.documentElement.dataset.theme === 'light' ? 'Blues' : 'Tealgrn',
      colorbar: { title: { text: 'Ocorrências', font: { color: theme.color('--plot-text') } }, tickfont: { color: theme.color('--plot-text') } },
      hovertemplate: 'Documento: %{customdata[0]}<br>Grupo: %{customdata[1]}<br><b>%{z} ocorrência(s)</b><extra></extra>',
    }], {
      ...theme.layout({ l: 160, r: 80, t: 20, b: 74 }), width: tamanho.largura, height: tamanho.altura,
      xaxis: theme.axis({ title: 'Documentos', automargin: true, tickangle: 0 }),
      yaxis: theme.axis({ title: 'Grupos', automargin: true, autorange: 'reversed' }),
    }, config);
  }

  function composicao() {
    const alvo = document.getElementById('grafico-composicao-grupos');
    const serie = dados.composicao_grupos || {};
    const grupos = serie.grupos || [];
    const entidades = serie.entidades || [];
    if (!alvo || !grupos.length || !entidades.length) return vazio(alvo, 'Não há entidades com ocorrência para compor os grupos.');
    const tamanho = dimensoes(alvo, entidades.length, grupos.length);
    const gruposVisiveis = grupos.map(grupo => theme.abbreviateLabel(grupo, 30));
    const traces = entidades.map((entidade, indice) => ({
      type: 'bar', orientation: 'h', name: theme.abbreviateLabel(entidade, 24), y: gruposVisiveis,
      x: (serie.percentuais || [])[indice] || grupos.map(() => 0),
      customdata: ((serie.contagens || [])[indice] || grupos.map(() => 0)).map((contagem, coluna) => [
        grupos[coluna],
        entidade,
        contagem,
        (((serie.percentuais || [])[indice] || [])[coluna] || 0).toLocaleString('pt-BR', { maximumFractionDigits: 2 }),
      ]),
      marker: { color: theme.palette()[indice % theme.palette().length] },
      hovertemplate: 'Grupo: %{customdata[0]}<br>Entidade: %{customdata[1]}<br>Ocorrências: %{customdata[2]}<br><b>%{customdata[3]}% da composição interna do grupo</b><extra></extra>',
    }));
    Plotly.react(alvo, traces, {
      ...theme.layout({ l: 160, r: 24, t: 20, b: 90 }), width: tamanho.largura, height: tamanho.altura,
      barmode: 'stack', legend: theme.legend({ orientation: 'h' }),
      xaxis: theme.axis({ title: 'Composição interna do grupo', range: [0, 100], ticksuffix: '%' }),
      yaxis: theme.axis({ title: 'Grupos', automargin: true, autorange: 'reversed' }),
    }, config);
  }

  function renderizar() {
    mapaDeCalor();
    composicao();
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
