const dialog = document.querySelector('#platform-tutorial-dialog');

function focusSection(sectionId) {
  if (!dialog || !sectionId) return;
  const section = dialog.querySelector(`#tutorial-${CSS.escape(sectionId)}`);
  if (!section) return;
  section.scrollIntoView({ block: 'start' });
  section.focus({ preventScroll: true });
}

function openTutorial(button) {
  if (!dialog) return;
  if (!dialog.open) dialog.showModal();
  focusSection(button.dataset.platformTutorialSection);
}

document.querySelectorAll('[data-platform-tutorial-open]').forEach((button) => {
  button.addEventListener('click', () => openTutorial(button));
});

dialog?.querySelector('[data-platform-tutorial-close]')?.addEventListener('click', () => dialog.close());

dialog?.addEventListener('click', (event) => {
  if (event.target === dialog) dialog.close();
});
