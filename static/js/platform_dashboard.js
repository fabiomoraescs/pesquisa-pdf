(() => {
  const source = document.getElementById('dashboard-chart-data');
  if (!source || !window.Plotly || !window.PesquisaPdfPlotTheme) return;
  const personal = JSON.parse(source.textContent);
  const adminSource = document.getElementById('dashboard-admin-chart-data');
  const admin = adminSource ? JSON.parse(adminSource.textContent) : null;
  const theme = window.PesquisaPdfPlotTheme;

  function draw(id, series, { horizontal = true, colorIndex = 0, label = 'Ocorrências' } = {}) {
    const target = document.getElementById(id);
    if (!target || !series) return;
    const labels = series.labels || [];
    const values = series.values || [];
    if (!labels.length || !values.some(value => Number(value) > 0)) {
      if (target.classList.contains('js-plotly-plot')) Plotly.purge(target);
      target.textContent = 'Ainda não há dados para exibir neste gráfico.';
      target.classList.add('empty-chart');
      return;
    }
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
      xaxis: theme.axis(horizontal ? { title: label, rangemode: 'tozero', dtick: 1 }
                                   : { type: 'category', automargin: true, tickangle: -35, tickmode: 'array',
                                       tickvals: labels.filter((_, index) => index % 3 === 0 || index === labels.length - 1) }),
      yaxis: theme.axis(horizontal ? { automargin: true, autorange: 'reversed' }
                                   : { title: label, rangemode: 'tozero', dtick: 1 }),
      autosize: true,
    }, { responsive: true, displaylogo: false });
  }

  function render() {
    draw('dashboard-chart-free', personal.free,
      { label: personal.free?.unit || 'ocorrência(s)' });
    draw('dashboard-chart-systematic', personal.systematic,
      { colorIndex: 1, label: 'ocorrência(s)' });
    if (admin) {
      const registrations = {
        labels: admin.registrations.labels.map(month => `${month.slice(5, 7)}/${month.slice(0, 4)}`),
        values: admin.registrations.values,
      };
      draw('dashboard-chart-users', registrations,
        { horizontal: false, colorIndex: 2, label: 'cadastro(s)' });
      draw('dashboard-chart-usage', admin.usage,
        { colorIndex: 3, label: 'Base(s) concluída(s)' });
    }
  }
  render();
  document.addEventListener('tema-alterado', render);
})();
