(() => {
  'use strict';

  const assistant = document.querySelector('[data-assistant]');
  if (!assistant) return;
  const avatar = assistant.querySelector('[data-assistant-toggle]');
  const panel = assistant.querySelector('[data-assistant-panel]');
  const closeButton = assistant.querySelector('[data-assistant-close]');
  const messages = assistant.querySelector('[data-assistant-messages]');
  const body = assistant.querySelector('[data-assistant-body]');
  const payload = assistant.querySelector('[data-assistant-payload]');
  const form = assistant.querySelector('[data-assistant-form]');
  const input = assistant.querySelector('[data-assistant-input]');
  const sendButton = assistant.querySelector('[data-assistant-send]');
  const error = assistant.querySelector('[data-assistant-error]');
  const csrf = assistant.querySelector('[data-assistant-csrf]');
  if (!avatar || !panel || !closeButton || !messages || !body || !payload ||
      !form || !input || !sendButton || !error || !csrf) return;

  let context;
  try { context = JSON.parse(payload.textContent); }
  catch (_) { return; }
  if (!Array.isArray(context.suggestions) || context.suggestions.length !== 5) return;

  const setOpen = (open) => {
    panel.hidden = !open;
    avatar.setAttribute('aria-expanded', String(open));
    avatar.classList.toggle('is-active', open);
    if (open) closeButton.focus();
    else avatar.focus();
  };

  avatar.addEventListener('click', () => setOpen(panel.hidden));
  closeButton.addEventListener('click', () => setOpen(false));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !panel.hidden) {
      event.preventDefault();
      setOpen(false);
    }
  });

  const addMessage = (text, role) => {
    const message = document.createElement('p');
    message.className = `platform-assistant-message is-${role}`;
    message.textContent = text;
    messages.append(message);
    body.scrollTop = body.scrollHeight;
  };

  let busy = false;
  const showError = (message) => {
    error.textContent = message;
    error.hidden = !message;
  };
  const setBusy = (value) => {
    busy = value;
    form.setAttribute('aria-busy', String(value));
    input.disabled = value;
    sendButton.disabled = value;
    sendButton.textContent = value ? 'Enviando...' : 'Enviar';
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    const question = input.value.trim();
    if (!question || question.length > 1000) {
      showError('Informe uma pergunta de até 1000 caracteres.');
      input.focus();
      return;
    }
    showError('');
    addMessage(question, 'user');
    input.value = '';
    setBusy(true);
    try {
      const response = await fetch(form.action, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
          'X-CSRFToken': csrf.value,
        },
        body: JSON.stringify({question, context: context.key, page: context.page,
          reference: context.reference}),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || result.erro || 'Não foi possível enviar a pergunta.');
      if (!result || typeof result.answer !== 'string' || !result.answer.trim()) {
        throw new Error('Resposta inválida do Assistente.');
      }
      addMessage(result.answer, 'assistant');
    } catch (failure) {
      showError(failure.message || 'Não foi possível enviar a pergunta.');
    } finally {
      setBusy(false);
      if (!panel.hidden) input.focus();
    }
  });

  assistant.addEventListener('click', (event) => {
    const questionButton = event.target.closest('[data-assistant-question]');
    if (!questionButton || !assistant.contains(questionButton)) return;
    const index = Number(questionButton.dataset.assistantQuestion);
    const suggestion = context.suggestions[index];
    if (!Number.isInteger(index) || !suggestion) return;
    addMessage(suggestion.question, 'user');
    addMessage(suggestion.answer, 'assistant');
  });
})();
