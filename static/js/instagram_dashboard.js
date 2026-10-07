(() => {
  let observer;
  let frame;

  function empty(target, message) {
    if (!target) return;
    if (window.Plotly && target.classList.contains('js-plotly-plot')) Plotly.purge(target);
    target.replaceChildren();
    const notice = document.createElement('p');
    notice.className = 'empty-chart';
    notice.textContent = message;
    target.appendChild(notice);
  }

  function chartData() {
    const source = document.getElementById('instagram-dashboard-chart-data');
    if (!source) return null;
    try {
      return JSON.parse(source.textContent);
    } catch (_) {
      return null;
    }
  }

  function taxonomyChartData() {
    const source = document.getElementById('instagram-taxonomy-chart-data');
    if (!source) return null;
    try {
      return JSON.parse(source.textContent);
    } catch (_) {
      return null;
    }
  }

  function draw(id, series, label, color, line = false) {
    const target = document.getElementById(id);
    if (!target) return;
    if (!series?.values?.length) return empty(target, `Não há dados de ${label.toLowerCase()} para este gráfico.`);
    const theme = window.PesquisaPdfPlotTheme;
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
      ...theme.layout({ l: 70, r: 24, t: 20, b: 105 }), height: 340,
      xaxis: theme.axis({ title: line ? 'Coleta' : 'Publicação', type: 'category', automargin: true, tickangle: -35 }),
      yaxis: theme.axis({ title: label, rangemode: 'tozero', automargin: true }), showlegend: false,
    }, { responsive: true, displaylogo: false });
  }

  function drawRate(id, series, color, taxonomy = false) {
    const target = document.getElementById(id);
    if (!target) return;
    if (!series?.values?.length) return empty(target, 'Não há taxas de interação por alcance conhecidas para este gráfico.');
    const theme = window.PesquisaPdfPlotTheme;
    if (!window.Plotly || !theme) return empty(target, 'O gráfico está indisponível neste momento. Os dados continuam na tabela.');
    const horizontal = taxonomy && Boolean(series.horizontal);
    Plotly.react(target, [{
      type: 'bar', orientation: horizontal ? 'h' : 'v',
      x: horizontal ? series.values : series.labels,
      y: horizontal ? series.labels : series.values,
      marker: { color },
      hovertemplate: horizontal
        ? '%{y}<br><b>%{x:.2f}% taxa mediana de interação por alcance</b><extra></extra>'
        : '%{x}<br><b>%{y:.2f}% taxa mediana de interação por alcance</b><extra></extra>',
    }], {
      ...theme.layout({ l: horizontal ? 155 : 70, r: 24, t: 20, b: horizontal ? 55 : 105 }),
      height: horizontal ? Math.max(340, series.values.length * 46 + 90) : 340,
      xaxis: horizontal
        ? theme.axis({ title: 'Taxa mediana de interação por alcance (%)', automargin: true, ticksuffix: '%' })
        : theme.axis({ title: taxonomy ? 'Categoria' : 'Tipo de conteúdo', type: 'category', automargin: true, tickangle: -25 }),
      yaxis: horizontal
        ? theme.axis({ title: 'Categoria', type: 'category', automargin: true })
        : theme.axis({ title: 'Taxa mediana de interação por alcance (%)', automargin: true, ticksuffix: '%' }),
      showlegend: false,
    }, { responsive: true, displaylogo: false });
  }

  function resize() {
    if (!window.Plotly) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => document.querySelectorAll('.dashboard-grid .js-plotly-plot')
      .forEach((chart) => Plotly.Plots.resize(chart)));
  }

  function observeCharts() {
    if (observer) observer.disconnect();
    if (!('ResizeObserver' in window)) return;
    observer = new ResizeObserver(resize);
    document.querySelectorAll('.dashboard-grid .chart-card').forEach((card) => observer.observe(card));
  }

  function dispose(target) {
    if (observer) observer.disconnect();
    if (!window.Plotly || !target) return;
    target.querySelectorAll('.js-plotly-plot').forEach((chart) => Plotly.purge(chart));
  }

  function render() {
    const charts = chartData();
    const theme = window.PesquisaPdfPlotTheme;
    if (!charts || !theme) return;
    const palette = theme.palette();
    draw('instagram-chart-followers', charts.followers, 'Seguidores', palette[0], true);
    draw('instagram-chart-reach', charts.reach, 'Alcance', palette[1]);
    draw('instagram-chart-interactions', charts.interactions, 'Interações', palette[2]);
    drawRate('instagram-chart-type-interaction-rate', charts.type_interaction_rate, palette[3]);
    drawRate('instagram-chart-taxonomy-interaction-rate', taxonomyChartData(), palette[4] || palette[0], true);
    observeCharts();
  }

  window.InstagramDashboard = { render };
  render();
  document.addEventListener('tema-alterado', render);
  document.addEventListener('dashboard-redimensionar', resize);
  document.addEventListener('platform:partial-before-update', (event) => {
    if (['#instagram-dashboard-content', '#instagram-taxonomy-performance'].includes(event.detail.target)) {
      dispose(event.detail.currentTarget);
    }
  });
  document.addEventListener('platform:partial-updated', (event) => {
    if (['#instagram-dashboard-content', '#instagram-taxonomy-performance'].includes(event.detail.target)) render();
  });
})();
