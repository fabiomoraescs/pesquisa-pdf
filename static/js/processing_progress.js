/* Apresentação compartilhada da barra das três ferramentas. */
window.ProcessingProgress = {
  updateBar(bar, fill, percent) {
    const value = Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : null;
    bar.classList.toggle('is-indeterminate', value === null);
    if (value === null) {
      bar.removeAttribute('aria-valuenow');
      bar.setAttribute('aria-valuetext', 'Progresso ainda não disponível');
      fill.style.width = '';
    } else {
      bar.setAttribute('aria-valuenow', String(value));
      bar.setAttribute('aria-valuetext', `Progresso: ${value}%`);
      fill.style.width = `${value}%`;
    }
    return value;
  }
};
