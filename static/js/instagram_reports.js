(() => {
  let observer;
  let frame;

  function reportChartData() {
    const source = document.getElementById('instagram-report-chart-data');
    if (!source) return null;
    try {
      return JSON.parse(source.textContent);
    } catch (_) {
      return null;
    }
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

  function drawReportChart(key, chart, palette, theme) {
    const target = document.getElementById(`instagram-report-chart-${key}`);
    if (!target) return;
    if (!chart?.values?.length) return empty(target, 'Não há dados suficientes para este gráfico.');
    if (!window.Plotly || !theme) return empty(target, 'O gráfico está indisponível neste momento. Os dados continuam no relatório.');

    const base = { ...theme.layout({ l: 72, r: 28, t: 20, b: 64 }), showlegend: false };
    let traces;
    let layout;
    if (chart.kind === 'pie') {
      traces = [{ type: 'pie', labels: chart.labels, values: chart.values, marker: { colors: palette }, textinfo: 'label+percent', hovertemplate: '%{label}<br><b>%{value:,.0f}</b> (%{percent})<extra></extra>' }];
      layout = { ...base, height: 340, showlegend: true, legend: theme.legend({ orientation: 'h', y: -0.15 }) };
    } else if (chart.kind === 'line') {
      traces = [{ type: 'scatter', mode: 'lines+markers', x: chart.labels, y: chart.values, line: { color: palette[0], width: 2 }, marker: { color: palette[0], size: 8 }, hovertemplate: `%{x}<br><b>%{y:,.2f}${chart.unit === '%' ? '%' : ''}</b><extra></extra>` }];
      layout = { ...base, height: 340, xaxis: theme.axis({ type: 'category', automargin: true, tickangle: -20 }), yaxis: theme.axis({ title: chart.unit === '%' ? 'Percentual' : 'Mediana', automargin: true, ticksuffix: chart.unit === '%' ? '%' : undefined }) };
    } else {
      const diverging = chart.kind === 'diverging_bar';
      traces = [{ type: 'bar', orientation: 'h', y: chart.labels, x: chart.values, marker: { color: chart.values.map((value, index) => diverging && value < 0 ? palette[4] || palette[index % palette.length] : palette[index % palette.length]) }, hovertemplate: `%{y}<br><b>%{x:,.2f}${chart.unit === '%' ? '%' : ''}</b><extra></extra>` }];
      layout = {
        ...theme.layout({ l: 178, r: 34, t: 20, b: 56 }), height: Math.max(340, chart.values.length * 48 + 104),
        xaxis: theme.axis({ title: diverging ? 'Variação relativa' : 'Mediana por conteúdo observado', automargin: true, rangemode: diverging ? undefined : 'tozero', ticksuffix: chart.unit === '%' ? '%' : undefined, zeroline: true }),
        yaxis: theme.axis({ type: 'category', automargin: true, autorange: 'reversed' }),
      };
    }
    Plotly.react(target, traces, layout, { responsive: true, displaylogo: false });
  }

  function resize() {
    if (!window.Plotly) return;
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => document.querySelectorAll('[data-report-chart-card] .js-plotly-plot')
      .forEach((chart) => Plotly.Plots.resize(chart)));
  }

  function renderCharts() {
    const charts = reportChartData();
    const theme = window.PesquisaPdfPlotTheme;
    if (!charts || !theme) return;
    const palette = theme.palette();
    Object.entries(charts).forEach(([key, chart]) => drawReportChart(key, chart, palette, theme));
    if (observer) observer.disconnect();
    if ('ResizeObserver' in window) {
      observer = new ResizeObserver(resize);
      document.querySelectorAll('[data-report-chart-card]').forEach((card) => observer.observe(card));
    }
  }

  renderCharts();
  document.addEventListener('tema-alterado', renderCharts);
})();
