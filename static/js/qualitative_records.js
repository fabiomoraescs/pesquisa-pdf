/* Registros manuais do projeto. Não altera o viewer nem o corpus. */
const recordsRoot = document.querySelector('[data-qualitative-records]');

if (recordsRoot) {
  let records = JSON.parse(recordsRoot.querySelector('[data-records-initial]').textContent);
  const dialog = recordsRoot.querySelector('[data-record-dialog]');
  const confirmDialog = recordsRoot.querySelector('[data-record-confirm-dialog]');
  const rowTemplate = recordsRoot.querySelector('[data-record-row-template]');
  const csrf = recordsRoot.querySelector('[data-record-csrf]')?.value;
  const kinds = { code: { key: 'codes', label: 'código', plural: 'Códigos', url: recordsRoot.dataset.codesUrl },
    memo: { key: 'memos', label: 'memo', plural: 'Memos', url: recordsRoot.dataset.memosUrl } };
  let editing = null;
  let removing = null;

  const labelFor = (kind, item) => kind === 'code' ? item.name : item.text.split(/\r?\n/, 1)[0];
  const render = (kind) => {
    const config = kinds[kind];
    const items = records[config.key];
    recordsRoot.querySelector(`[data-record-count="${kind}"]`).textContent = `${config.plural} (${items.length})`;
    recordsRoot.querySelector(`[data-record-empty="${kind}"]`).hidden = items.length > 0;
    const list = recordsRoot.querySelector(`[data-record-list="${kind}"]`);
    if (!rowTemplate) return;
    const fragment = document.createDocumentFragment();
    for (const item of items) {
      const row = rowTemplate.content.firstElementChild.cloneNode(true);
      const name = labelFor(kind, item);
      const label = row.querySelector('[data-record-label], .platform-qualitative-record-label');
      label.textContent = kind === 'code' ? `${name} (${item.excerpt_count ?? 0})` : name;
      label.title = name;
      const context = row.querySelector('[data-record-context]');
      if (kind === 'memo' && item.context && item.context !== 'geral') {
        context.textContent = `Vinculado a ${item.context}`;
        context.hidden = false;
      }
      for (const action of ['edit', 'delete']) {
        const button = row.querySelector(`[data-record-${action}]`);
        button.dataset[`record${action[0].toUpperCase()}${action.slice(1)}`] = kind;
        button.dataset.recordId = item.id;
        button.title = `${action === 'edit' ? 'Editar' : 'Excluir'} ${config.label} ${name}`;
        button.setAttribute('aria-label', button.title);
      }
      fragment.append(row);
    }
    list.replaceChildren(fragment);
  };
  const update = (payload, announce = true) => {
    records = payload;
    render('code');
    render('memo');
    if (announce) document.dispatchEvent(new CustomEvent('qualitative:records-updated', { detail: payload }));
  };
  document.addEventListener('qualitative:records-updated', (event) => {
    if (event.detail !== records) update(event.detail, false);
  });
  const request = async (url, method, body) => {
    const response = await fetch(url, { method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf, Accept: 'application/json' },
      body: body ? JSON.stringify(body) : undefined });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || 'Não foi possível salvar a alteração.');
    return payload;
  };
  const editError = recordsRoot.querySelector('[data-record-error]');
  const deleteError = recordsRoot.querySelector('[data-record-confirm-error]');
  const codeFields = recordsRoot.querySelector('[data-code-fields]');
  const memoFields = recordsRoot.querySelector('[data-memo-fields]');
  const codeName = recordsRoot.querySelector('[name="name"]');
  const codeDescription = recordsRoot.querySelector('[name="description"]');
  const memoText = recordsRoot.querySelector('[name="text"]');
  const form = recordsRoot.querySelector('[data-record-form]');

  if (dialog && confirmDialog && rowTemplate && csrf) {
    const openEditor = (kind, item = null) => {
      editing = { kind, id: item?.id };
      codeFields.hidden = kind !== 'code';
      memoFields.hidden = kind !== 'memo';
      codeName.disabled = codeDescription.disabled = kind !== 'code';
      memoText.disabled = kind !== 'memo';
      memoText.required = kind === 'memo';
      codeName.value = item?.name || '';
      codeDescription.value = item?.description || '';
      memoText.value = item?.text || '';
      recordsRoot.querySelector('[data-record-dialog-title]').textContent =
        `${item ? 'Editar' : 'Novo'} ${kinds[kind].label}`;
      editError.hidden = true;
      editError.textContent = '';
      dialog.showModal();
      (kind === 'code' ? codeName : memoText).focus();
    };
    recordsRoot.addEventListener('click', (event) => {
      const add = event.target.closest('[data-record-new]');
      if (add && recordsRoot.contains(add)) return openEditor(add.dataset.recordNew);
      const edit = event.target.closest('[data-record-edit]');
      if (edit && recordsRoot.contains(edit)) {
        const kind = edit.dataset.recordEdit;
        return openEditor(kind, records[kinds[kind].key].find((item) => item.id === edit.dataset.recordId));
      }
      const remove = event.target.closest('[data-record-delete]');
      if (remove && recordsRoot.contains(remove)) {
        const kind = remove.dataset.recordDelete;
        const item = records[kinds[kind].key].find((entry) => entry.id === remove.dataset.recordId);
        if (!item) return;
        removing = { kind, item };
        const name = labelFor(kind, item);
        recordsRoot.querySelector('[data-record-confirm-title]').textContent = `Excluir ${kinds[kind].label}?`;
        recordsRoot.querySelector('[data-record-confirm-message]').textContent = kind === 'code'
          ? `Excluir o código “${name}”? As associações desse código serão removidas. Esta ação não pode ser desfeita.`
          : `Excluir o memo “${name}”? Esta ação não pode ser desfeita.`;
        deleteError.hidden = true;
        deleteError.textContent = '';
        confirmDialog.showModal();
      }
    });
    recordsRoot.querySelector('[data-record-cancel]').addEventListener('click', () => dialog.close());
    recordsRoot.querySelector('[data-record-confirm-cancel]').addEventListener('click', () => confirmDialog.close());
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!editing) return;
      const save = recordsRoot.querySelector('[data-record-save]');
      save.disabled = true;
      try {
        const { kind, id } = editing;
        const url = id ? `${kinds[kind].url}/${id}` : kinds[kind].url;
        const body = kind === 'code' ? { name: codeName.value, description: codeDescription.value }
          : { text: memoText.value };
        update(await request(url, id ? 'PATCH' : 'POST', body));
        dialog.close();
      } catch (error) {
        editError.textContent = error.message;
        editError.hidden = false;
      } finally { save.disabled = false; }
    });
    recordsRoot.querySelector('[data-record-confirm-delete]').addEventListener('click', async (event) => {
      if (!removing) return;
      event.currentTarget.disabled = true;
      try {
        const { kind, item } = removing;
        update(await request(`${kinds[kind].url}/${item.id}`, 'DELETE'));
        confirmDialog.close();
      } catch (error) {
        deleteError.textContent = error.message;
        deleteError.hidden = false;
      } finally { event.currentTarget.disabled = false; }
    });
  }

  // O ambiente sem documentos não carrega o viewer; ancora os mesmos popovers aqui.
  if (!document.querySelector('[data-qualitative-viewer]')) {
    const position = (popover) => {
      const trigger = recordsRoot.querySelector(`[aria-controls="${popover.id}"]`);
      const rect = trigger.getBoundingClientRect();
      const left = Math.min(Math.max(8, rect.left), Math.max(8, innerWidth - popover.offsetWidth - 8));
      const below = rect.bottom + 4;
      popover.style.left = `${left}px`;
      popover.style.top = `${below + popover.offsetHeight <= innerHeight - 8 ? below
        : Math.max(8, rect.top - popover.offsetHeight - 4)}px`;
    };
    recordsRoot.querySelectorAll('[data-explorer-popover]').forEach((popover) => {
      popover.addEventListener('toggle', () => {
        const open = popover.matches(':popover-open');
        recordsRoot.querySelector(`[aria-controls="${popover.id}"]`).setAttribute('aria-expanded', String(open));
        if (open) position(popover);
      });
    });
  }
}
