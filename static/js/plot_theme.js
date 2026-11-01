/* Identidade visual única para os gráficos das bases, baseada nos tokens do tema. */
window.PesquisaPdfPlotTheme = (() => {
  const fallback = ['#0b9fc7', '#c94cb9', '#7c5ce0', '#2b9b6d', '#b88805', '#d65c70', '#5e93e8', '#74834c'];
  const paletteTokens = [
    '--chart-cyan', '--chart-magenta', '--chart-violet', '--chart-green',
    '--chart-yellow', '--chart-coral', '--blue', '--muted',
  ];
  const themeFor = (scope = document.documentElement) => {
    const color = name => getComputedStyle(scope).getPropertyValue(name).trim();
    const palette = () => paletteTokens.map((token, index) => color(token) || fallback[index]);
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
  };
  const abbreviateLabel = (value, maximum = 26) => {
    const label = String(value || '').replace(/\s+/g, ' ').trim();
    if (label.length <= maximum) return label;
    return `${label.slice(0, Math.max(1, maximum - 1)).trimEnd()}…`;
  };
  const matrixDimensions = (target, columns, rows, options = {}) => {
    const { left = 145, right = 80, columnWidth = 102, rowHeight = 38, minHeight = 290 } = options;
    const available = Math.max(target?.parentElement?.clientWidth || target?.clientWidth || 0, 280);
    const desired = left + right + Math.max(1, columns) * columnWidth;
    // Uma matriz com poucos documentos não deve herdar a largura mínima de uma
    // tabela ampla; com muitas colunas, o próprio painel continua rolável.
    const width = columns <= 2 ? Math.min(available, Math.max(280, desired)) : Math.max(available, desired);
    target.style.width = `${width}px`;
    target.style.minWidth = width > available ? `${width}px` : '0';
    // Em matrizes pequenas, o próprio conjunto (rótulos, células e escala) é
    // centralizado dentro do painel; em matrizes largas a rolagem começa pela
    // primeira coluna, sem esconder conteúdo.
    target.style.marginInline = columns <= 2 ? 'auto' : '0';
    return { largura: width, altura: Math.max(minHeight, rows * rowHeight + 145) };
  };
  const rootTheme = themeFor();
  const forElement = element => themeFor(element?.closest?.('[data-dashboard-theme]') || document.documentElement);
  return { ...rootTheme, forElement, abbreviateLabel, matrixDimensions };
})();
