const deleteDialog = document.querySelector('[data-document-delete-dialog]');
const viewer = document.querySelector('[data-qualitative-viewer]');

if (deleteDialog && viewer) {
  const name = deleteDialog.querySelector('[data-delete-name]');
  const error = deleteDialog.querySelector('[data-delete-error]');
  const confirm = deleteDialog.querySelector('[data-delete-confirm]');
  const cancel = deleteDialog.querySelector('[data-delete-cancel]');
  let selected = null;

  viewer.addEventListener('click', (event) => {
    const button = event.target.closest('[data-delete-document]');
    if (!button) return;
    selected = button;
    name.textContent = button.dataset.documentName;
    error.textContent = '';
    error.hidden = true;
    deleteDialog.showModal();
  });
  cancel.addEventListener('click', () => deleteDialog.close());
  deleteDialog.addEventListener('close', () => { selected = null; });
  confirm.addEventListener('click', async () => {
    if (!selected || confirm.disabled) return;
    const button = selected;
    confirm.disabled = true;
    try {
      const response = await fetch(button.dataset.deleteUrl, {
        method: 'POST', credentials: 'same-origin',
        body: new URLSearchParams({ csrf_token: deleteDialog.querySelector('[data-delete-csrf]').value }),
        headers: { Accept: 'application/json' },
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Não foi possível excluir o documento.');
      if (button.dataset.documentId === viewer.dataset.documentId) {
        window.location.assign(result.next_url);
        return;
      }
      button.closest('[data-document-row]').remove();
      viewer.querySelector('[data-document-count]').textContent = `Documentos (${result.document_count})`;
      deleteDialog.close();
      viewer.querySelector('[data-explorer-popover]:popover-open')?.hidePopover();
    } catch (problem) {
      error.textContent = problem.message || 'Não foi possível excluir o documento.';
      error.hidden = false;
    } finally {
      confirm.disabled = false;
    }
  });
}
