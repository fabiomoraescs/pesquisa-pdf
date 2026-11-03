(() => {
  let frame;

  function data() {
    const source = document.getElementById('instagram-comments-chart-data');
    if (!source) return null;
    try { return JSON.parse(source.textContent); } catch (_) { return null; }
  }

  function empty(target, message) {
    if (!target) return;
    if (window.Plotly && target.classList.contains('js-plotly-plot')) Plotly.purge(target);
    target.replaceChildren();
    const notice = document.createElement('p');
    notice.className = 'empty-chart';
    notice.textContent = message;
    target.appendChild(notice);
  }

  function draw(id, series, color) {
    const target = document.getElementById(id);
    if (!target) return;
    if (!series?.values?.length) return empty(target, 'Não há classificações conhecidas para este gráfico.');
    const theme = window.PesquisaPdfPlotTheme;
    if (!window.Plotly || !theme) return empty(target, 'O gráfico está indisponível neste momento. Os dados continuam na tabela.');
    const labels = series.labels || [];
    const horizontal = labels.length > 5 || labels.some(label => String(label).length > 16);
    const displayLabels = labels.map(label => String(label).length > 28 ? `${String(label).slice(0, 27).trimEnd()}…` : label);
    Plotly.react(target, [{
      type: 'bar', orientation: horizontal ? 'h' : 'v',
      x: horizontal ? series.values : displayLabels,
      y: horizontal ? displayLabels : series.values,
      customdata: horizontal ? labels : labels,
      marker: { color },
      hovertemplate: horizontal
        ? '%{customdata}<br><b>%{x:,.0f} comentário(s)</b><extra></extra>'
        : '%{customdata}<br><b>%{y:,.0f} comentário(s)</b><extra></extra>',
    }], {
      ...theme.layout({ l: horizontal ? 155 : 70, r: 24, t: 20, b: horizontal ? 55 : 92 }),
      height: horizontal ? Math.max(340, labels.length * 42 + 92) : 340,
      xaxis: horizontal
        ? theme.axis({ title: 'Comentários', rangemode: 'tozero', automargin: true })
        : theme.axis({ title: 'Classificação', type: 'category', automargin: true, tickangle: -25, tickfont: { size: 11 } }),
      yaxis: horizontal
        ? theme.axis({ title: 'Classificação', type: 'category', automargin: true, autorange: 'reversed', tickfont: { size: 11 } })
        : theme.axis({ title: 'Comentários', rangemode: 'tozero', automargin: true }),
      showlegend: false,
    }, { responsive: true, displaylogo: false });
  }

  function resize() {
    if (!window.Plotly) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => document.querySelectorAll('#instagram-comments-content .js-plotly-plot')
      .forEach((chart) => Plotly.Plots.resize(chart)));
  }

  function dispose(target) {
    if (window.Plotly && target) target.querySelectorAll('.js-plotly-plot').forEach((chart) => Plotly.purge(chart));
  }

  function render() {
    const charts = data();
    const theme = window.PesquisaPdfPlotTheme;
    if (!charts || !theme) return;
    const palette = theme.palette();
    draw('instagram-comments-chart-sentiment', charts.sentiment, palette[0]);
    draw('instagram-comments-chart-intent', charts.intent, palette[1]);
    draw('instagram-comments-chart-needs-response', charts.needs_response, palette[2]);
    draw('instagram-comments-chart-risk', charts.risk, palette[3]);
  }

  render();
  document.addEventListener('tema-alterado', render);
  document.addEventListener('dashboard-redimensionar', resize);
  window.addEventListener('resize', resize, { passive: true });
  document.addEventListener('platform:partial-before-update', (event) => {
    if (event.detail.target === '#instagram-comments-content') dispose(event.detail.currentTarget);
  });
  document.addEventListener('platform:partial-updated', (event) => {
    if (event.detail.target === '#instagram-comments-content') render();
  });
})();
