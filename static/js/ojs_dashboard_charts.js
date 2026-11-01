/* Histórico leve do painel OJS SEO. As distribuições e a rosca são HTML/CSS
 * renderizados no servidor; este SVG só existe quando há três ou mais pontos. */
(() => {
  const source = document.getElementById('ojs-dashboard-history-chart-data');
  const target = document.getElementById('ojs-dashboard-chart-history');
  if (!source || !target || window.__ojsSeoHistoryChartInitialized) return;
  window.__ojsSeoHistoryChartInitialized = true;
  let series;
  try { series = JSON.parse(source.textContent); } catch (_) { return; }
  const labels = Array.isArray(series.labels) ? series.labels : [];
  const values = Array.isArray(series.active_records) ? series.active_records : [];
  const records = Array.isArray(series.records) ? series.records : [];
  const statuses = Array.isArray(series.statuses) ? series.statuses : [];
  if (labels.length < 3 || values.length !== labels.length) return;

  const createSvg = (name, attributes = {}) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
    return element;
  };
  function renderHistory() {
    const width = 680, height = 235, left = 48, right = 20, top = 16, bottom = 40;
    const plotWidth = width - left - right, plotHeight = height - top - bottom;
    const maximum = Math.max(...values, 1);
    const styles = getComputedStyle(target);
    const text = styles.getPropertyValue('--plot-text').trim() || '#f1f2fa';
    const muted = styles.getPropertyValue('--muted').trim() || '#aab1cb';
    const grid = styles.getPropertyValue('--plot-grid').trim() || '#3d4475';
    const line = styles.getPropertyValue('--chart-cyan').trim() || '#58cff7';
    const point = styles.getPropertyValue('--chart-magenta').trim() || '#ef70d0';
    const x = index => left + (plotWidth * index / (values.length - 1));
    const y = value => top + plotHeight - (value / maximum * plotHeight);
    const svg = createSvg('svg', { class: 'ojs-history-svg', viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Evolução das publicações ativas por auditoria' });
    for (let tick = 0; tick <= 4; tick += 1) {
      const value = Math.round(maximum * tick / 4), coordinate = y(value);
      svg.append(createSvg('line', { x1: left, x2: width - right, y1: coordinate, y2: coordinate, stroke: grid, 'stroke-width': 1 }));
      const label = createSvg('text', { x: left - 8, y: coordinate + 4, fill: muted, 'font-size': 11, 'text-anchor': 'end' });
      label.textContent = value;
      svg.append(label);
    }
    svg.append(createSvg('path', { d: values.map((value, index) => `${index ? 'L' : 'M'} ${x(index)} ${y(value)}`).join(' '), fill: 'none', stroke: line, 'stroke-width': 3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));
    const step = Math.max(1, Math.ceil(labels.length / 6));
    values.forEach((value, index) => {
      const circle = createSvg('circle', { cx: x(index), cy: y(value), r: 4, fill: point, stroke: line, 'stroke-width': 1.5 });
      const title = createSvg('title');
      title.textContent = `${labels[index]}: ${value} publicações ativas; ${records[index] || 0} registros recuperados; ${statuses[index] || '—'}.`;
      circle.append(title); svg.append(circle);
      if (index % step === 0 || index === labels.length - 1) {
        const label = createSvg('text', { x: x(index), y: height - 14, fill: muted, 'font-size': 11, 'text-anchor': 'middle' });
        label.textContent = String(labels[index]).slice(0, 10);
        svg.append(label);
      }
    });
    target.replaceChildren(svg);
    target.dataset.chartRendered = 'true';
  }
  renderHistory();
  document.addEventListener('tema-alterado', renderHistory);
})();
