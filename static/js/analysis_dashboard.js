(() => {
  const source = document.getElementById('analysis-chart-data');
  if (!source || !window.Plotly) return;
  const counts = JSON.parse(source.textContent);
  const theme = window.PesquisaPdfPlotTheme;
  const charts = [
    ['chart-entities', 'entities', 'Ocorrências por entidade'],
    ['chart-methods', 'methods', 'Tipo de correspondência'],
  ];
  function render() {
    for (const [id, field, title] of charts) {
      const element = document.getElementById(id);
      const values = counts[field] || [];
      if (!element) continue;
      if (!values.length) {
        if (element.classList.contains('js-plotly-plot')) window.Plotly.purge(element);
        element.textContent = 'Não há ocorrências para exibir neste gráfico.';
        element.classList.add('empty-chart');
        continue;
      }
      element.classList.remove('empty-chart');
      window.Plotly.react(element, [{
        type: 'bar', x: values.map(([name]) => name), y: values.map(([, count]) => count),
        marker: { color: theme.palette()[field === 'entities' ? 0 : 1] },
      }], {
        ...theme.layout({ l: 55, r: 24, t: 54, b: 90 }),
        title: { text: title, font: { color: theme.color('--plot-text'), size: 16 } },
        xaxis: theme.axis({ automargin: true }),
        yaxis: theme.axis({ title: 'Ocorrências', rangemode: 'tozero' }),
        autosize: true,
      }, { responsive: true, displaylogo: false });
    }
  }
  render();
  document.addEventListener('tema-alterado', render);
})();
