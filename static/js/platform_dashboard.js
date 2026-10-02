(() => {
  const source = document.getElementById('dashboard-admin-chart-data');
  if (!source || !window.Plotly || !window.PesquisaPdfPlotTheme) return;

  let admin;
  try {
    admin = JSON.parse(source.textContent);
  } catch (_) {
    return;
  }

  const theme = window.PesquisaPdfPlotTheme;

  function draw(id, series, { horizontal = true, colorIndex = 0, label = 'registro(s)' } = {}) {
    const target = document.getElementById(id);
    if (!target || !series) return;
    const labels = series.labels || [];
    const values = series.values || [];
    target.classList.remove('empty-chart');
    Plotly.react(target, [{
      type: 'bar',
      x: horizontal ? values : labels,
      y: horizontal ? labels : values,
      orientation: horizontal ? 'h' : 'v',
      marker: { color: theme.palette()[colorIndex] },
      hovertemplate: horizontal
        ? `%{y}<br><b>%{x} ${label}</b><extra></extra>`
        : `%{x}<br><b>%{y} ${label}</b><extra></extra>`,
    }], {
      ...theme.layout({ l: horizontal ? 135 : 50, r: 20, t: 12, b: horizontal ? 45 : 70 }),
      xaxis: theme.axis(horizontal
        ? { title: label, rangemode: 'tozero', dtick: 1 }
        : { type: 'category', automargin: true, tickangle: -35 }),
      yaxis: theme.axis(horizontal
        ? { automargin: true, autorange: 'reversed' }
        : { title: label, rangemode: 'tozero', dtick: 1 }),
      autosize: true,
    }, { responsive: true, displaylogo: false });
  }

  function render() {
    const registrations = {
      labels: (admin.registrations?.labels || []).map(month => `${month.slice(5, 7)}/${month.slice(0, 4)}`),
      values: admin.registrations?.values || [],
    };
    draw('dashboard-chart-users', registrations,
      { horizontal: false, colorIndex: 2, label: 'cadastro(s)' });
    draw('dashboard-chart-usage', admin.usage,
      { colorIndex: 3, label: 'Base(s) concluída(s)' });
  }

  let resizeFrame;
  function resizeCharts() {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      document.querySelectorAll('.platform-dashboard-admin .js-plotly-plot')
        .forEach(chart => Plotly.Plots.resize(chart));
    });
  }

  render();
  document.addEventListener('tema-alterado', render);
  document.addEventListener('dashboard-redimensionar', resizeCharts);
  if ('ResizeObserver' in window) {
    const observer = new ResizeObserver(resizeCharts);
    document.querySelectorAll('.platform-dashboard-admin .platform-panel')
      .forEach(card => observer.observe(card));
  }
})();
