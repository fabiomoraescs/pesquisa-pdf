(() => {
  document.querySelectorAll('[data-view-container]').forEach((container) => {
    const buttons = document.querySelectorAll(`[data-view-target="${container.id}"]`);
    const key = container.dataset.viewStorageKey || 'pesquisapdf-view-mode';
    let saved = 'list';
    try { saved = localStorage.getItem(key) === 'cardbox' ? 'cardbox' : 'list'; } catch (_) { /* armazenamento indisponível */ }
    const setView = (mode) => {
      container.classList.toggle('platform-view-list', mode === 'list');
      container.classList.toggle('platform-view-cardbox', mode === 'cardbox');
      buttons.forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.viewMode === mode)));
    };
    setView(saved);
    buttons.forEach((button) => button.addEventListener('click', () => {
      const mode = button.dataset.viewMode;
      setView(mode);
      try { localStorage.setItem(key, mode); } catch (_) { /* modo apenas nesta página */ }
    }));
  });
  const adminToggle = document.getElementById('platform-admin-toggle');
  const adminSubmenu = document.getElementById('platform-admin-submenu');
  if (adminToggle && adminSubmenu) {
    adminToggle.addEventListener('click', () => {
      const expanded = adminToggle.getAttribute('aria-expanded') !== 'true';
      adminToggle.setAttribute('aria-expanded', String(expanded));
      adminSubmenu.hidden = !expanded;
    });
  }
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

  const photoForm = document.querySelector('[data-profile-photo-form]');
  if (photoForm) {
    const fileInput = photoForm.querySelector('[data-profile-photo-file]');
    const cropPanel = photoForm.querySelector('[data-profile-photo-crop]');
    const canvas = photoForm.querySelector('[data-profile-photo-canvas]');
    const zoomInput = photoForm.querySelector('[data-profile-photo-zoom]');
    const status = photoForm.querySelector('[data-profile-photo-status]');
    const saveButton = photoForm.querySelector('button[type="submit"]');
    const cropX = photoForm.querySelector('[data-profile-crop-x]');
    const cropY = photoForm.querySelector('[data-profile-crop-y]');
    const cropSize = photoForm.querySelector('[data-profile-crop-size]');
    const context = canvas.getContext('2d');
    let image = null;
    let offsetX = 0;
    let offsetY = 0;
    let scale = 1;
    let pointer = null;
    let selection = 0;
    saveButton.disabled = true;

    const draw = () => {
      if (!image) return;
      const drawnWidth = image.naturalWidth * scale;
      const drawnHeight = image.naturalHeight * scale;
      offsetX = Math.min(0, Math.max(canvas.width - drawnWidth, offsetX));
      offsetY = Math.min(0, Math.max(canvas.height - drawnHeight, offsetY));
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(image, offsetX, offsetY, drawnWidth, drawnHeight);
      cropX.value = (-offsetX / scale).toFixed(3);
      cropY.value = (-offsetY / scale).toFixed(3);
      cropSize.value = (canvas.width / scale).toFixed(3);
    };
    const setZoom = () => {
      if (!image) return;
      const centerX = (canvas.width / 2 - offsetX) / scale;
      const centerY = (canvas.height / 2 - offsetY) / scale;
      scale = Math.max(canvas.width / image.naturalWidth, canvas.height / image.naturalHeight) * Number(zoomInput.value);
      offsetX = canvas.width / 2 - centerX * scale;
      offsetY = canvas.height / 2 - centerY * scale;
      draw();
    };
    fileInput.addEventListener('change', () => {
      const currentSelection = ++selection;
      const file = fileInput.files?.[0];
      image = null;
      cropPanel.hidden = true;
      saveButton.disabled = true;
      cropX.value = cropY.value = cropSize.value = '';
      if (!file) return;
      if (file.size > 5 * 1024 * 1024 || !['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
        status.textContent = 'Selecione uma foto JPEG, PNG ou WEBP de até 5 MB.';
        return;
      }
      const url = URL.createObjectURL(file);
      const selected = new Image();
      selected.onload = () => {
        URL.revokeObjectURL(url);
        if (currentSelection !== selection) return;
        if (selected.naturalWidth < 64 || selected.naturalHeight < 64 ||
            selected.naturalWidth * selected.naturalHeight > 20000000) {
          status.textContent = 'A foto precisa ter ao menos 64 px por lado e até 20 megapixels.';
          return;
        }
        image = selected;
        zoomInput.value = '1';
        scale = Math.max(canvas.width / image.naturalWidth, canvas.height / image.naturalHeight);
        offsetX = (canvas.width - image.naturalWidth * scale) / 2;
        offsetY = (canvas.height - image.naturalHeight * scale) / 2;
        cropPanel.hidden = false;
        status.textContent = 'Prévia pronta. Arraste para ajustar o recorte e use a ampliação, se desejar.';
        saveButton.disabled = false;
        draw();
      };
      selected.onerror = () => {
        URL.revokeObjectURL(url);
        if (currentSelection !== selection) return;
        status.textContent = 'Não foi possível abrir esta imagem.';
      };
      selected.src = url;
    });
    zoomInput.addEventListener('input', setZoom);
    canvas.addEventListener('pointerdown', (event) => {
      if (!image) return;
      canvas.setPointerCapture(event.pointerId);
      pointer = { id: event.pointerId, x: event.clientX, y: event.clientY };
    });
    canvas.addEventListener('pointermove', (event) => {
      if (!pointer || pointer.id !== event.pointerId) return;
      const ratio = canvas.width / canvas.getBoundingClientRect().width;
      offsetX += (event.clientX - pointer.x) * ratio;
      offsetY += (event.clientY - pointer.y) * ratio;
      pointer.x = event.clientX;
      pointer.y = event.clientY;
      draw();
    });
    canvas.addEventListener('keydown', (event) => {
      if (!image) return;
      const movement = { ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, -10], ArrowDown: [0, 10] }[event.key];
      if (!movement) return;
      event.preventDefault();
      offsetX += movement[0];
      offsetY += movement[1];
      draw();
    });
    const endPointer = () => { pointer = null; };
    canvas.addEventListener('pointerup', endPointer);
    canvas.addEventListener('pointercancel', endPointer);
  }

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
