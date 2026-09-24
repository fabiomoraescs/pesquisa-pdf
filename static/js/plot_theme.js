/* Identidade visual única para os gráficos das bases, baseada nos tokens do tema. */
window.PesquisaPdfPlotTheme = (() => {
  const light = ['#1f4e78', '#2a7f62', '#a05d20', '#7755a5', '#c0504d', '#3c8dad', '#748f34', '#b36d2a', '#537a96', '#9a5a80'];
  const dark = ['#78b7e5', '#66d0ad', '#f2b36d', '#b89bef', '#f18a87', '#72c8e5', '#b7cf6c', '#f0a75c', '#91bbd9', '#e29ac3'];
  const color = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const palette = () => document.documentElement.dataset.theme === 'dark' ? dark : light;
  const layout = (margin = { l: 55, r: 24, t: 24, b: 80 }) => ({
    margin,
    paper_bgcolor: color('--plot-paper'),
    plot_bgcolor: color('--plot-bg'),
    font: { color: color('--plot-text') },
    hoverlabel: { bgcolor: color('--surface-muted'), font: { color: color('--plot-text') } },
  });
  const axis = (options = {}) => ({
    color: color('--plot-text'), gridcolor: color('--plot-grid'),
    zerolinecolor: color('--plot-grid'), ...options,
  });
  const legend = (options = {}) => ({ font: { color: color('--plot-text') }, ...options });
  return { color, palette, layout, axis, legend };
})();
