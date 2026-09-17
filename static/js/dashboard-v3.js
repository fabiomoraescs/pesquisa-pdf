(() => {
  const origem = document.getElementById('dados-dashboard');
  if (!origem || !window.Plotly) return;
  const dados = JSON.parse(origem.textContent);
  const coresClaras = ['#1f4e78', '#2a7f62', '#a05d20', '#7755a5', '#c0504d', '#3c8dad', '#748f34'];
  const coresEscuras = ['#78b7e5', '#66d0ad', '#f2b36d', '#b89bef', '#f18a87', '#72c8e5', '#b7cf6c'];
  const configBase = {
    responsive: true,
    displaylogo: false,
    toImageButtonOptions: { format: 'png', filename: 'varredura_v3', scale: 2 },
  };

  function cor(nome) {
    return getComputedStyle(document.documentElement).getPropertyValue(nome).trim();
  }

  function paleta() {
    return document.documentElement.dataset.theme === 'dark' ? coresEscuras : coresClaras;
  }

  function layoutBase() {
    return {
      margin: { l: 56, r: 26, t: 22, b: 82 },
      paper_bgcolor: cor('--plot-paper'),
      plot_bgcolor: cor('--plot-bg'),
      font: { color: cor('--plot-text') },
      hoverlabel: { bgcolor: cor('--surface-muted'), font: { color: cor('--plot-text') } },
    };
  }

  function eixo(opcoes = {}) {
    return {
      color: cor('--plot-text'),
      gridcolor: cor('--plot-grid'),
      zerolinecolor: cor('--plot-grid'),
      ...opcoes,
    };
  }

  function legenda(opcoes = {}) {
    return { font: { color: cor('--plot-text') }, ...opcoes };
  }

  function config(nome) {
    return { ...configBase, toImageButtonOptions: { ...configBase.toImageButtonOptions, filename: nome } };
  }

  function vazio(id, mensagem = 'Não há dados para exibir neste gráfico.') {
    const alvo = document.getElementById(id);
    if (!alvo) return;
    if (alvo.classList.contains('js-plotly-plot')) Plotly.purge(alvo);
    alvo.replaceChildren();
    const paragrafo = document.createElement('p');
    paragrafo.className = 'empty-chart';
    paragrafo.textContent = mensagem;
    alvo.appendChild(paragrafo);
  }

  function renderizarBarra(id, serie, opcoes) {
    const alvo = document.getElementById(id);
    if (!alvo || !serie.rotulos.length) return vazio(id);
    Plotly.react(alvo, [{
      type: 'bar',
      x: serie.rotulos,
      y: serie.valores,
      marker: { color: opcoes.cor || paleta()[0] },
      hovertemplate: `${opcoes.rotulo || '%{x}'}<br><b>%{y}</b>${opcoes.sufixo || ''}<extra></extra>`,
    }], {
      ...layoutBase(),
      xaxis: eixo({ automargin: true }),
      yaxis: eixo({ title: opcoes.eixoY || 'Quantidade', rangemode: 'tozero' }),
    }, config(opcoes.arquivo));
  }

  function indicadores() {
    const ids = {
      lexicais: 'indicador-v3-lexicais',
      semanticos: 'indicador-v3-semanticos',
      resultados: 'indicador-v3-resultados',
      livros: 'indicador-v3-livros',
      consultas: 'indicador-v3-consultas',
    };
    Object.entries(ids).forEach(([nome, id]) => {
      const alvo = document.getElementById(id);
      if (alvo) alvo.textContent = Number(dados.indicadores[nome] || 0).toLocaleString('pt-BR');
    });
  }

  function graficoLexical() {
    renderizarBarra('grafico-v3-lexical', dados.lexical_por_consulta, {
      cor: paleta()[0], eixoY: 'Ocorrências', sufixo: ' ocorrência(s)', arquivo: 'v3_ocorrencias_lexicais_por_consulta',
    });
  }

  function graficoSemantico() {
    renderizarBarra('grafico-v3-semantico', dados.semantico_por_consulta, {
      cor: paleta()[1], eixoY: 'Correspondências', sufixo: ' correspondência(s)', arquivo: 'v3_correspondencias_semanticas_por_consulta',
    });
  }

  function graficoMedia() {
    const serie = dados.similaridade_media_por_consulta;
    const alvo = document.getElementById('grafico-v3-media');
    if (!alvo || !serie.rotulos.length) return vazio('grafico-v3-media', 'Não há correspondências semânticas para calcular uma média.');
    Plotly.react(alvo, [{
      type: 'bar', x: serie.rotulos, y: serie.valores, marker: { color: paleta()[3] },
      hovertemplate: 'Consulta: %{x}<br><b>%{y:.4f}</b> de similaridade média<extra></extra>',
    }], {
      ...layoutBase(),
      xaxis: eixo({ automargin: true }),
      yaxis: eixo({ title: 'Similaridade média', range: [0, 1], tickformat: '.0%' }),
    }, config('v3_similaridade_media_por_consulta'));
  }

  function graficoLivros() {
    renderizarBarra('grafico-v3-livros', dados.resultados_por_livro, {
      cor: paleta()[5], eixoY: 'Resultados recuperados', sufixo: ' resultado(s)', arquivo: 'v3_resultados_por_livro',
    });
  }

  function graficoComparacao() {
    const comparacao = dados.comparacao;
    const alvo = document.getElementById('grafico-v3-comparacao');
    if (!alvo || !comparacao.consultas.length || !comparacao.livros.length) return vazio('grafico-v3-comparacao');
    Plotly.react(alvo, [{
      type: 'heatmap',
      x: comparacao.livros,
      y: comparacao.consultas,
      z: comparacao.matriz,
      colorscale: document.documentElement.dataset.theme === 'dark' ? 'Tealgrn' : 'Blues',
      colorbar: { title: { text: 'Resultados', font: { color: cor('--plot-text') } }, tickfont: { color: cor('--plot-text') } },
      hovertemplate: 'Livro: %{x}<br>Consulta: %{y}<br><b>%{z} resultado(s)</b><extra></extra>',
    }], {
      ...layoutBase(),
      xaxis: eixo({ title: 'Livros', automargin: true }),
      yaxis: eixo({ title: 'Consultas', automargin: true, autorange: 'reversed' }),
    }, config('v3_consulta_por_livro'));
  }

  function graficoContextos() {
    renderizarBarra('grafico-v3-contextos', dados.contextos, {
      cor: paleta()[2], eixoY: 'Resultados', sufixo: ' resultado(s)', arquivo: 'v3_contextos_sociologicos',
    });
  }

  function graficoDistribuicao() {
    const valores = dados.distribuicao_similaridade || [];
    const alvo = document.getElementById('grafico-v3-distribuicao');
    if (!alvo || !valores.length) return vazio('grafico-v3-distribuicao', 'Não há correspondências semânticas para distribuir.');
    Plotly.react(alvo, [{
      type: 'histogram', x: valores, nbinsx: 16, marker: { color: paleta()[4] },
      hovertemplate: 'Faixa: %{x}<br><b>%{y} correspondência(s)</b><extra></extra>',
    }], {
      ...layoutBase(),
      xaxis: eixo({ title: 'Similaridade semântica', range: [0.5, 1], tickformat: '.0%' }),
      yaxis: eixo({ title: 'Correspondências', rangemode: 'tozero' }),
    }, config('v3_distribuicao_similaridades'));
  }

  function graficoSankey() {
    const serie = dados.sankey;
    const aviso = document.getElementById('aviso-sankey-v3');
    if (aviso) {
      aviso.textContent = serie.aviso || '';
      aviso.hidden = !serie.aviso;
    }
    const alvo = document.getElementById('grafico-v3-sankey');
    if (!alvo || !serie.labels.length) return vazio('grafico-v3-sankey');
    const coresNos = serie.labels.map((rotulo) => {
      if (rotulo.startsWith('Consulta:')) return paleta()[0];
      if (rotulo.startsWith('Contexto:')) return paleta()[2];
      return paleta()[5];
    });
    Plotly.react(alvo, [{
      type: 'sankey', arrangement: 'snap',
      node: {
        label: serie.labels, color: coresNos, pad: 16, thickness: 18,
        line: { color: cor('--border'), width: 1 },
        hovertemplate: '%{label}<br><b>%{value} resultado(s)</b><extra></extra>',
      },
      link: {
        source: serie.sources, target: serie.targets, value: serie.values,
        color: cor('--sankey-link'),
        hovertemplate: '%{source.label} → %{target.label}<br><b>%{value} resultado(s)</b><extra></extra>',
      },
    }], { ...layoutBase(), margin: { l: 12, r: 12, t: 20, b: 20 } }, config('v3_sankey_consultas_contextos_livros'));
  }

  function renderizarTudo() {
    graficoLexical();
    graficoSemantico();
    graficoMedia();
    graficoLivros();
    graficoComparacao();
    graficoContextos();
    graficoDistribuicao();
    graficoSankey();
  }

  indicadores();
  renderizarTudo();

  let quadro;
  function redimensionar() {
    document.querySelectorAll('.js-plotly-plot').forEach((grafico) => Plotly.Plots.resize(grafico));
  }
  function agendarRedimensionamento() {
    cancelAnimationFrame(quadro);
    quadro = requestAnimationFrame(redimensionar);
  }
  document.addEventListener('tema-alterado', () => {
    renderizarTudo();
    agendarRedimensionamento();
  });
  document.addEventListener('dashboard-redimensionar', agendarRedimensionamento);
  if ('ResizeObserver' in window) {
    const observador = new ResizeObserver(agendarRedimensionamento);
    document.querySelectorAll('.chart-card').forEach((cartao) => observador.observe(cartao));
  }
})();
