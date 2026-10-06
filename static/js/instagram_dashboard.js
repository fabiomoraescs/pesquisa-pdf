(() => {
  const source = document.getElementById('instagram-dashboard-chart-data');
  if (!source) return;
  const charts = JSON.parse(source.textContent);
  const theme = window.PesquisaPdfPlotTheme;
  const config = { responsive: true, displaylogo: false };

  function empty(target, message) {
    if (!target) return;
    if (window.Plotly && target.classList.contains('js-plotly-plot')) Plotly.purge(target);
    target.replaceChildren();
    const notice = document.createElement('p');
    notice.className = 'empty-chart';
    notice.textContent = message;
    target.appendChild(notice);
  }

  function draw(id, series, label, color, line = false) {
    const target = document.getElementById(id);
    if (!target) return;
    if (!series?.values?.length) return empty(target, `Não há dados de ${label.toLowerCase()} para este gráfico.`);
    if (!window.Plotly || !theme) return empty(target, 'O gráfico está indisponível neste momento. Os dados continuam na tabela.');
    Plotly.react(target, [{
      type: line ? 'scatter' : 'scattergl',
      mode: line ? 'lines+markers' : 'markers',
      x: series.labels,
      y: series.values,
      customdata: series.dates || null,
      line: line ? { color, width: 2 } : undefined,
      marker: { color, size: line ? 8 : 9 },
      hovertemplate: line
        ? `%{x}<br><b>%{y:,.0f} ${label.toLowerCase()}</b><extra></extra>`
        : `%{x}<br>Publicada em %{customdata}<br><b>%{y:,.0f} ${label.toLowerCase()}</b><extra></extra>`,
    }], {
      ...theme.layout({ l: 70, r: 24, t: 20, b: 105 }),
      height: 340,
      xaxis: theme.axis({ title: line ? 'Coleta' : 'Publicação', type: 'category', automargin: true, tickangle: -35 }),
      yaxis: theme.axis({ title: label, rangemode: 'tozero', automargin: true }),
      showlegend: false,
    }, config);
  }

  function drawTypeRate(series, color) {
    const target = document.getElementById('instagram-chart-type-interaction-rate');
    if (!target) return;
    if (!series?.values?.length) return empty(target, 'Não há taxas de interação por alcance conhecidas para este gráfico.');
    if (!window.Plotly || !theme) return empty(target, 'O gráfico está indisponível neste momento. Os dados continuam na tabela.');
    Plotly.react(target, [{
      type: 'bar',
      x: series.labels,
      y: series.values,
      marker: { color },
      hovertemplate: '%{x}<br><b>%{y:.2f}% taxa mediana de interação por alcance</b><extra></extra>',
    }], {
      ...theme.layout({ l: 70, r: 24, t: 20, b: 105 }),
      height: 340,
      xaxis: theme.axis({ title: 'Tipo de conteúdo', type: 'category', automargin: true, tickangle: -25 }),
      yaxis: theme.axis({ title: 'Taxa mediana de interação por alcance (%)', automargin: true, ticksuffix: '%' }),
      showlegend: false,
    }, config);
  }

  function render() {
    if (!theme) return;
    const palette = theme.palette();
    draw('instagram-chart-followers', charts.followers, 'Seguidores', palette[0], true);
    draw('instagram-chart-reach', charts.reach, 'Alcance', palette[1]);
    draw('instagram-chart-interactions', charts.interactions, 'Interações', palette[2]);
    drawTypeRate(charts.type_interaction_rate, palette[3]);
  }

  render();
  document.addEventListener('tema-alterado', render);
  let frame;
  function resize() {
    if (!window.Plotly) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => document.querySelectorAll('.dashboard-grid .js-plotly-plot')
      .forEach(chart => Plotly.Plots.resize(chart)));
  }
  document.addEventListener('dashboard-redimensionar', resize);
  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(resize);
    document.querySelectorAll('.dashboard-grid .chart-card').forEach(card => observer.observe(card));
  }
})();
