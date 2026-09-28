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
  if (!avatar || !panel || !closeButton || !messages || !body || !payload) return;

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
