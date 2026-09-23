(() => {
  document.querySelectorAll('[data-password-toggle]').forEach((button) => {
    const input = document.getElementById(button.dataset.passwordToggle);
    if (!input) return;
    button.addEventListener('click', () => {
      const visible = input.type === 'password';
      input.type = visible ? 'text' : 'password';
      const label = visible ? 'Ocultar senha' : 'Mostrar senha';
      button.setAttribute('aria-label', label);
      button.setAttribute('title', label);
      button.setAttribute('aria-pressed', String(visible));
    });
  });

  const menu = document.getElementById('platform-sidebar');
  const toggle = document.getElementById('platform-menu-toggle');
  const backdrop = document.getElementById('platform-menu-backdrop');
  if (menu && toggle && backdrop) {
    const mobile = window.matchMedia('(max-width: 900px)');
    const setOpen = (open, restoreFocus = false) => {
      const visible = mobile.matches && open;
      document.body.classList.toggle('platform-menu-open', visible);
      menu.inert = mobile.matches && !visible;
      backdrop.hidden = !visible;
      toggle.setAttribute('aria-expanded', String(visible));
      toggle.setAttribute('aria-label', visible ? 'Fechar menu' : 'Abrir menu');
      if (visible) menu.querySelector('a')?.focus();
      else if (restoreFocus) toggle.focus();
    };
    setOpen(false);
    toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true', true));
    backdrop.addEventListener('click', () => setOpen(false, true));
    menu.querySelectorAll('a').forEach((link) => link.addEventListener('click', () => setOpen(false)));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && document.body.classList.contains('platform-menu-open')) setOpen(false, true);
    });
    mobile.addEventListener('change', () => setOpen(false));
  }

  document.querySelectorAll('[data-delete-confirm]').forEach((input) => {
    const button = document.getElementById(input.dataset.deleteConfirm);
    if (!button) return;
    const update = () => { button.disabled = input.value.trim() !== 'deletar'; };
    input.addEventListener('input', update);
    update();
  });
})();
