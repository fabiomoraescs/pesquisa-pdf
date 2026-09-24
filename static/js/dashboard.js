(() => {
  const dados = JSON.parse(document.getElementById('dados-dashboard').textContent);
  const configBase = {
    responsive: true,
    displaylogo: false,
    toImageButtonOptions: { format: 'png', filename: 'varredura_de_pdfs', scale: 2 }
  };
  const plotTheme = window.PesquisaPdfPlotTheme;
  const limiteCategorias = 8;

  const corVariavel = plotTheme.color;
  const paleta = plotTheme.palette;
  const layoutBase = plotTheme.layout;
  const eixo = plotTheme.axis;
  const legenda = plotTheme.legend;

  function configPara(nome) {
    return {
      ...configBase,
      toImageButtonOptions: { ...configBase.toImageButtonOptions, filename: nome }
    };
  }

  Object.entries(dados.indicadores).forEach(([nome, valor]) => {
    document.getElementById(`indicador-${nome}`).textContent = Number(valor).toLocaleString('pt-BR');
  });

  function desenharVazio(elemento) {
    if (elemento.classList.contains('js-plotly-plot')) Plotly.purge(elemento);
    elemento.replaceChildren();
    const aviso = document.createElement('p');
    aviso.className = 'empty-chart';
    aviso.textContent = 'Não há ocorrências para exibir neste gráfico.';
    elemento.appendChild(aviso);
  }

  function atualizarAviso(id, tipo, quantidade) {
    const aviso = document.getElementById(id);
    if (aviso) aviso.hidden = !(['pizza', 'rosca'].includes(tipo) && quantidade > limiteCategorias);
  }

  function renderizarSerie({ id, avisoId, serie, tipo, cor, arquivo }) {
    const alvo = document.getElementById(id);
    atualizarAviso(avisoId, tipo, serie.rotulos.length);
    if (!serie.rotulos.length) return desenharVazio(alvo);

    let traces;
    let layout;
    if (tipo === 'pizza' || tipo === 'rosca') {
      traces = [{
        type: 'pie',
        labels: serie.rotulos,
        values: serie.valores,
        hole: tipo === 'rosca' ? 0.48 : 0,
        marker: { colors: paleta() },
        textinfo: 'label+value',
        textposition: 'auto',
        hovertemplate: '%{label}<br><b>%{value} ocorrência(s)</b><br>%{percent:.1%}<extra></extra>'
      }];
      layout = { ...layoutBase(), margin: { l: 24, r: 24, t: 24, b: 24 }, showlegend: true, legend: legenda({ orientation: 'h' }) };
    } else if (tipo === 'treemap') {
      traces = [{
        type: 'treemap',
        labels: serie.rotulos,
        parents: serie.rotulos.map(() => ''),
        values: serie.valores,
        marker: { colors: paleta() },
        textinfo: 'label+value',
        hovertemplate: '%{label}<br><b>%{value} ocorrência(s)</b><extra></extra>'
      }];
      layout = { ...layoutBase(), margin: { l: 12, r: 12, t: 12, b: 12 } };
    } else {
      traces = [{
        type: 'bar',
        x: serie.rotulos,
        y: serie.valores,
        marker: { color: cor },
        hovertemplate: '%{x}<br><b>%{y} ocorrência(s)</b><extra></extra>'
      }];
      layout = {
        ...layoutBase(),
        xaxis: eixo({ automargin: true }),
        yaxis: eixo({ title: 'Ocorrências', rangemode: 'tozero' })
      };
    }
    Plotly.react(alvo, traces, layout, configPara(arquivo));
  }

  function renderizarTermo() {
    renderizarSerie({
      id: 'grafico-termo', avisoId: 'aviso-termo', serie: dados.por_termo,
      tipo: document.getElementById('tipo-termo').value, cor: paleta()[0], arquivo: 'ocorrencias_por_termo'
    });
  }

  function renderizarLivro() {
    renderizarSerie({
      id: 'grafico-livro', avisoId: 'aviso-livro', serie: dados.por_livro,
      tipo: document.getElementById('tipo-livro').value, cor: paleta()[1], arquivo: 'ocorrencias_por_livro'
    });
  }

  function renderizarContexto() {
    renderizarSerie({
      id: 'grafico-contexto', avisoId: 'aviso-contexto', serie: dados.por_contexto,
      tipo: document.getElementById('tipo-contexto').value, cor: paleta()[2], arquivo: 'contextos_sociologicos'
    });
  }

  function renderizarComparacao() {
    const alvo = document.getElementById('grafico-comparacao');
    const tipo = document.getElementById('tipo-comparacao').value;
    const comparacao = dados.comparacao;
    if (!comparacao.termos.length || !comparacao.livros.length) return desenharVazio(alvo);

    let traces;
    let layout;
    if (tipo === 'heatmap') {
      traces = [{
        type: 'heatmap', x: comparacao.livros, y: comparacao.termos, z: comparacao.matriz,
        colorscale: document.documentElement.dataset.theme === 'dark' ? 'Tealgrn' : 'Blues',
        colorbar: { title: { text: 'Ocorrências', font: { color: corVariavel('--plot-text') } }, tickfont: { color: corVariavel('--plot-text') } },
        hovertemplate: 'Livro: %{x}<br>Termo: %{y}<br><b>%{z} ocorrência(s)</b><extra></extra>'
      }];
      layout = { ...layoutBase(), xaxis: eixo({ title: 'Livros', automargin: true }), yaxis: eixo({ title: 'Termos', automargin: true, autorange: 'reversed' }) };
    } else if (tipo === 'empilhadas') {
      traces = comparacao.termos.map((termo, linha) => ({
        type: 'bar', name: termo, x: comparacao.livros, y: comparacao.matriz[linha],
        marker: { color: paleta()[linha % paleta().length] },
        hovertemplate: 'Livro: %{x}<br>Termo: %{fullData.name}<br><b>%{y} ocorrência(s)</b><extra></extra>'
      }));
      layout = { ...layoutBase(), barmode: 'stack', legend: legenda({ orientation: 'h' }), xaxis: eixo({ title: 'Livros', automargin: true }), yaxis: eixo({ title: 'Ocorrências', rangemode: 'tozero' }) };
    } else {
      traces = comparacao.livros.map((livro, coluna) => ({
        type: 'bar', name: livro, x: comparacao.termos, y: comparacao.matriz.map(linha => linha[coluna]),
        marker: { color: paleta()[coluna % paleta().length] },
        hovertemplate: 'Livro: %{fullData.name}<br>Termo: %{x}<br><b>%{y} ocorrência(s)</b><extra></extra>'
      }));
      layout = { ...layoutBase(), barmode: 'group', legend: legenda({ orientation: 'h' }), xaxis: eixo({ title: 'Termos', automargin: true }), yaxis: eixo({ title: 'Ocorrências', rangemode: 'tozero' }) };
    }
    Plotly.react(alvo, traces, layout, configPara('termo_por_livro'));
  }

  function renderizarContextosPorLivro() {
    const alvo = document.getElementById('grafico-contextos-livro');
    const serie = dados.contextos_por_livro;
    if (!serie.livros.length || !serie.contextos.length) return desenharVazio(alvo);
    const traces = serie.contextos.map((contexto, indice) => ({
      type: 'bar', name: contexto, x: serie.livros, y: serie.percentuais[indice],
      customdata: serie.contagens[indice], marker: { color: paleta()[indice % paleta().length] },
      hovertemplate: 'Livro: %{x}<br>Contexto: %{fullData.name}<br><b>%{customdata} ocorrência(s)</b><br>%{y:.1f}% no livro<extra></extra>'
    }));
    Plotly.react(alvo, traces, {
      ...layoutBase(), barmode: 'stack', legend: legenda({ orientation: 'h' }),
      xaxis: eixo({ title: 'Livros', automargin: true }),
      yaxis: eixo({ title: 'Percentual de ocorrências', range: [0, 100], ticksuffix: '%', fixedrange: true })
    }, configPara('contextos_por_livro_percentual'));
  }

  function renderizarPareto() {
    const alvo = document.getElementById('grafico-pareto');
    const linhas = dados.pareto.linhas;
    if (!linhas.length) return desenharVazio(alvo);
    const termos = linhas.map(linha => linha.termo);
    const ocorrencias = linhas.map(linha => linha.ocorrencias);
    const acumulados = linhas.map(linha => linha.acumulado);
    const detalhes = linhas.map(linha => [linha.percentual, linha.acumulado]);
    const traces = [
      {
        type: 'bar', name: 'Ocorrências', x: termos, y: ocorrencias, marker: { color: paleta()[0] }, customdata: detalhes,
        hovertemplate: 'Termo: %{x}<br><b>%{y} ocorrência(s)</b><br>%{customdata[0]:.1f}% individual<br>%{customdata[1]:.1f}% acumulado<extra></extra>'
      },
      {
        type: 'scatter', mode: 'lines+markers', name: 'Percentual acumulado', x: termos, y: acumulados, yaxis: 'y2',
        line: { color: paleta()[3], width: 3 }, marker: { size: 7 }, customdata: detalhes,
        hovertemplate: 'Termo: %{x}<br>%{customdata[0]:.1f}% individual<br><b>%{y:.1f}% acumulado</b><extra></extra>'
      }
    ];
    Plotly.react(alvo, traces, {
      ...layoutBase(), legend: legenda({ orientation: 'h' }),
      xaxis: eixo({ title: 'Termos', automargin: true }),
      yaxis: eixo({ title: 'Ocorrências', rangemode: 'tozero' }),
      yaxis2: eixo({ title: 'Percentual acumulado', overlaying: 'y', side: 'right', range: [0, 100], ticksuffix: '%', showgrid: false })
    }, configPara('pareto_termos'));
  }

  function renderizarSankey() {
    const alvo = document.getElementById('grafico-sankey');
    const serie = dados.sankey;
    const aviso = document.getElementById('aviso-sankey');
    aviso.textContent = serie.aviso;
    aviso.hidden = !serie.aviso;
    if (!serie.labels.length) return desenharVazio(alvo);
    const coresNos = serie.labels.map(rotulo => {
      if (rotulo.startsWith('Termo:')) return paleta()[0];
      if (rotulo.startsWith('Contexto:')) return paleta()[2];
      return paleta()[5];
    });
    Plotly.react(alvo, [{
      type: 'sankey', arrangement: 'snap',
      node: { label: serie.labels, color: coresNos, pad: 16, thickness: 18, line: { color: corVariavel('--border'), width: 1 }, hovertemplate: '%{label}<br><b>%{value} ocorrência(s)</b><extra></extra>' },
      link: { source: serie.sources, target: serie.targets, value: serie.values, color: corVariavel('--sankey-link'), hovertemplate: '%{source.label} → %{target.label}<br><b>%{value} ocorrência(s)</b><extra></extra>' }
    }], {
      ...layoutBase(), margin: { l: 12, r: 12, t: 20, b: 20 }
    }, configPara('sankey_termos_contextos_livros'));
  }

  function renderizarTudo() {
    renderizarTermo();
    renderizarLivro();
    renderizarContexto();
    renderizarComparacao();
    renderizarContextosPorLivro();
    renderizarPareto();
    renderizarSankey();
  }

  renderizarTudo();

  document.getElementById('tipo-termo').addEventListener('change', renderizarTermo);
  document.getElementById('tipo-livro').addEventListener('change', renderizarLivro);
  document.getElementById('tipo-contexto').addEventListener('change', renderizarContexto);
  document.getElementById('tipo-comparacao').addEventListener('change', renderizarComparacao);

  let quadroResize;
  function redimensionarGraficos() {
    document.querySelectorAll('.js-plotly-plot').forEach(grafico => Plotly.Plots.resize(grafico));
  }
  function agendarRedimensionamento() {
    cancelAnimationFrame(quadroResize);
    quadroResize = requestAnimationFrame(redimensionarGraficos);
  }

  document.addEventListener('tema-alterado', () => {
    renderizarTudo();
    agendarRedimensionamento();
  });
  document.addEventListener('dashboard-redimensionar', agendarRedimensionamento);
  if ('ResizeObserver' in window) {
    const observador = new ResizeObserver(agendarRedimensionamento);
    document.querySelectorAll('.chart-card').forEach(cartao => observador.observe(cartao));
  }
})();
