(() => {
  let observer;
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
    Plotly.react(target, [{
      type: 'bar', x: series.labels, y: series.values, marker: { color },
      hovertemplate: '%{x}<br><b>%{y:,.0f} comentário(s)</b><extra></extra>',
    }], {
      ...theme.layout({ l: 70, r: 24, t: 20, b: 105 }), height: 340,
      xaxis: theme.axis({ title: 'Classificação', type: 'category', automargin: true, tickangle: -25 }),
      yaxis: theme.axis({ title: 'Comentários', rangemode: 'tozero', automargin: true }), showlegend: false,
    }, { responsive: true, displaylogo: false });
  }

  function resize() {
    if (!window.Plotly) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => document.querySelectorAll('#instagram-comments-content .js-plotly-plot')
      .forEach((chart) => Plotly.Plots.resize(chart)));
  }

  function observe() {
    if (observer) observer.disconnect();
    if (!('ResizeObserver' in window)) return;
    observer = new ResizeObserver(resize);
    document.querySelectorAll('#instagram-comments-content .chart-card').forEach((card) => observer.observe(card));
  }

  function dispose(target) {
    if (observer) observer.disconnect();
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
    observe();
  }

  render();
  document.addEventListener('tema-alterado', render);
  document.addEventListener('dashboard-redimensionar', resize);
  document.addEventListener('platform:partial-before-update', (event) => {
    if (event.detail.target === '#instagram-comments-content') dispose(event.detail.currentTarget);
  });
  document.addEventListener('platform:partial-updated', (event) => {
    if (event.detail.target === '#instagram-comments-content') render();
  });
})();
