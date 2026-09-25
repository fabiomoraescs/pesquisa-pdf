const info = document.querySelector('[data-qualitative-info]');
const open = document.querySelector('[data-qualitative-info-open]');

if (info && open) {
  open.addEventListener('click', () => info.showModal());
  info.querySelector('[data-qualitative-info-close]')?.addEventListener('click', () => info.close());
}
