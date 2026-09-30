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
  const status = assistant.querySelector('[data-assistant-status]');
  const suggestions = assistant.querySelector('[data-assistant-suggestions]');
  const suggestionsLoading = assistant.querySelector('[data-assistant-suggestions-loading]');
  const suggestionsLoadingText = assistant.querySelector('[data-assistant-suggestions-loading-text]');
  const suggestionsStatus = assistant.querySelector('[data-assistant-suggestions-status]');
  let suggestionButtons = assistant.querySelectorAll
    ? Array.from(assistant.querySelectorAll('[data-assistant-suggestion]')) : [];
  if (!avatar || !panel || !closeButton || !messages || !body || !payload ||
      !form || !input || !sendButton || !error || !csrf) return;

  let context;
  try { context = JSON.parse(payload.textContent); }
  catch (_) { return; }
  if (!context || typeof context.key !== 'string') return;

  const currentPageContext = () => {
    // Este estado é meramente visual. O servidor valida novamente documento,
    // análise e projeto antes de o aproveitar no resumo da resposta.
    const viewer = document.querySelector('[data-qualitative-viewer]');
    if (!viewer || !viewer.dataset || !viewer.dataset.analysisId || !viewer.dataset.documentId) return {};
    const page = viewer.querySelector?.('[data-current-page]');
    const currentPage = Number(page?.value || page?.textContent);
    const automaticMode = viewer.querySelector?.('[name="automatic_mode"]:checked')?.value;
    const pageContext = {
      document_id: viewer.dataset.documentId,
      ...(Number.isInteger(currentPage) && currentPage > 0 ? {current_page: currentPage} : {}),
      ...(automaticMode ? {selected_search_mode: automaticMode} : {}),
      ...(typeof document.body?.classList?.contains === 'function'
        ? {focus_mode: document.body.classList.contains('platform-qualitative-focus')} : {}),
    };
    return pageContext;
  };

  const setOpen = (open) => {
    panel.hidden = !open;
    avatar.setAttribute('aria-expanded', String(open));
    avatar.classList.toggle('is-active', open);
    if (open) {
      closeButton.focus();
      if (!suggestionsDismissed) requestDynamicSuggestions();
    }
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
    suggestionButtons.forEach((button) => { button.disabled = value; });
    sendButton.textContent = value ? 'Analisando…' : 'Enviar';
    if (status) {
      status.textContent = value ? 'Analisando sua pergunta…' : '';
      status.hidden = !value;
    }
  };

  const setSuggestionsStatus = (message) => {
    if (!suggestionsStatus) return;
    suggestionsStatus.textContent = message || '';
    suggestionsStatus.hidden = !message;
  };

  const onboardingPrompts = Array.isArray(context.onboarding_prompts)
    && context.onboarding_prompts.length === 3
    && context.onboarding_prompts.every((prompt) => typeof prompt === 'string' && prompt.trim())
    ? context.onboarding_prompts.map((prompt) => prompt.trim()) : [];

    const suggestionLoadingMessage = () => {
      if (context.key === 'qualitative_reader') return 'Analisando esta página para sugerir perguntas…';
      if (context.key === 'coding_report') return 'Analisando estes resultados para sugerir perguntas…';
      if (context.key === 'project') return 'Analisando este projeto para sugerir perguntas…';
      if (['qualitative', 'qualitative_search'].includes(context.key)) {
        return 'Analisando o corpus para sugerir perguntas…';
      }
      return 'Analisando esta página para sugerir perguntas…';
    };

  const setSuggestionsLoading = (visible) => {
    if (!suggestionsLoading) return;
    if (visible && suggestionsLoadingText) suggestionsLoadingText.textContent = suggestionLoadingMessage();
    suggestionsLoading.hidden = !visible;
  };

  const bindSuggestion = (button) => {
    button.addEventListener('click', () => submitQuestion(
      button.textContent.trim(), button.dataset.assistantSuggestionId || null,
    ));
  };

  const replaceSuggestions = (items, {dynamic = false} = {}) => {
    if (!suggestions || !Array.isArray(items) || items.length !== 3) return false;
    const normalized = items.map((item) => {
      if (dynamic) {
        if (!item || typeof item.id !== 'string' || !item.id
            || typeof item.text !== 'string' || !item.text.trim()) return null;
        return {id: item.id, text: item.text.trim()};
      }
      return typeof item === 'string' && item.trim() ? {id: '', text: item.trim()} : null;
    });
    if (normalized.some((item) => item === null)) return false;
    const buttons = normalized.map((item) => {
      const button = document.createElement('button');
      button.className = 'platform-assistant-suggestion';
      button.type = 'button';
      button.dataset.assistantSuggestion = '';
      if (item.id) button.dataset.assistantSuggestionId = item.id;
      button.textContent = item.text;
      bindSuggestion(button);
      return button;
    });
    suggestions.replaceChildren(...buttons);
    suggestionButtons = buttons;
    suggestions.hidden = false;
    return true;
  };

  const renderFallbackSuggestions = () => replaceSuggestions(onboardingPrompts);
  const isDynamicSuggestionSet = (items) => Array.isArray(items) && items.length === 3
    && items.every((item) => item && typeof item.id === 'string' && item.id
      && typeof item.text === 'string' && item.text.trim());
  const DYNAMIC_SUGGESTION_TIMEOUT_MS = 10000;
  const dynamicSuggestionCache = new Map();
  let dynamicSuggestionSequence = 0;
  let pendingDynamicSuggestionKey = '';
  let renderedDynamicSuggestionKey = '';
  let suggestionsDismissed = false;

  const dynamicRequest = () => {
    const pageContext = currentPageContext();
    const requestPayload = {context: context.key, page: context.page, reference: context.reference};
    if (Object.keys(pageContext).length) requestPayload.page_context = pageContext;
    return {payload: requestPayload, key: JSON.stringify(requestPayload)};
  };

  const showDynamicSuggestionLoading = () => {
    if (suggestions) {
      suggestions.hidden = true;
      suggestions.replaceChildren();
      suggestionButtons = [];
    }
    setSuggestionsStatus('');
    setSuggestionsLoading(true);
  };

  const dismissDynamicSuggestions = () => {
    suggestionsDismissed = true;
    dynamicSuggestionSequence += 1;
    pendingDynamicSuggestionKey = '';
    setSuggestionsLoading(false);
  };

  async function requestDynamicSuggestions(force = false) {
    if (!context.dynamic_suggestions || !assistant.dataset?.assistantSuggestionsUrl || !suggestions || panel.hidden) return;
    if (suggestionsDismissed) return;
    const request = dynamicRequest();
    if (pendingDynamicSuggestionKey === request.key) return;
    if (!force && renderedDynamicSuggestionKey === request.key && !suggestions.hidden) return;
    const cachedSuggestions = dynamicSuggestionCache.get(request.key);
    if (isDynamicSuggestionSet(cachedSuggestions)) {
      setSuggestionsLoading(false);
      replaceSuggestions(cachedSuggestions, {dynamic: true});
      renderedDynamicSuggestionKey = request.key;
      return;
    }

    const sequence = ++dynamicSuggestionSequence;
    pendingDynamicSuggestionKey = request.key;
    showDynamicSuggestionLoading();
    const fetchResult = fetch(assistant.dataset.assistantSuggestionsUrl, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRFToken': csrf.value},
      body: JSON.stringify(request.payload),
    }).then(async (response) => {
      let result = null;
      try { result = await response.json(); } catch (_) { /* fallback below */ }
      return {response, result};
    }, () => null);
    let timeoutId;
    try {
      const outcome = await Promise.race([
        fetchResult,
        new Promise((resolve) => {
          timeoutId = setTimeout(() => resolve(null), DYNAMIC_SUGGESTION_TIMEOUT_MS);
        }),
      ]);
      const stillCurrent = sequence === dynamicSuggestionSequence
        && !panel.hidden && !suggestionsDismissed && dynamicRequest().key === request.key;
      if (!stillCurrent) return;
      pendingDynamicSuggestionKey = '';
      if (outcome?.response?.ok && outcome.result?.dynamic === true
          && isDynamicSuggestionSet(outcome.result.suggestions)) {
        dynamicSuggestionCache.set(request.key, outcome.result.suggestions);
        while (dynamicSuggestionCache.size > 12) {
          dynamicSuggestionCache.delete(dynamicSuggestionCache.keys().next().value);
        }
        replaceSuggestions(outcome.result.suggestions, {dynamic: true});
      } else {
        // Quota, indisponibilidade, timeout e saída inválida permanecem um
        // detalhe de transporte: o onboarding funcional é o fallback visual.
        renderFallbackSuggestions();
      }
      renderedDynamicSuggestionKey = request.key;
    } finally {
      if (timeoutId) clearTimeout(timeoutId);
      if (sequence === dynamicSuggestionSequence) {
        if (pendingDynamicSuggestionKey === request.key) pendingDynamicSuggestionKey = '';
        setSuggestionsLoading(false);
      }
    }
  }

  const addEvidence = (evidence) => {
    if (!Array.isArray(evidence) || !evidence.length) return;
    const section = document.createElement('section');
    section.className = 'platform-assistant-evidence';
    section.setAttribute('aria-label', 'Fontes consultadas');
    const title = document.createElement('h3');
    title.textContent = 'Fontes consultadas';
    section.append(title);
    for (const item of evidence) {
      if (!item || typeof item.document_name !== 'string' || !Number.isInteger(item.page_number)
          || typeof item.preview !== 'string' || typeof item.url !== 'string') continue;
      const source = document.createElement('div');
      source.className = 'platform-assistant-evidence-item';
      const label = document.createElement('strong');
      label.textContent = `${item.document_name} · p. ${item.page_number}`;
      const preview = document.createElement('p');
      preview.textContent = `“${item.preview}”`;
      const link = document.createElement('a');
      link.href = item.url;
      link.textContent = 'Abrir página';
      source.append(label); source.append(preview); source.append(link);
      section.append(source);
    }
    if (section.children.length > 1) messages.append(section);
  };

  const submitQuestion = async (question, suggestionId = null) => {
    if (busy) return;
    if (!question || question.length > 1000) {
      showError('Informe uma pergunta de até 1000 caracteres.');
      input.focus();
      return;
    }
    showError('');
    dismissDynamicSuggestions();
    if (suggestions) suggestions.hidden = true;
    addMessage(question, 'user');
    input.value = '';
    setBusy(true);
    try {
      const pageContext = currentPageContext();
      const requestPayload = {question, context: context.key, page: context.page,
        reference: context.reference};
      if (typeof suggestionId === 'string' && suggestionId) requestPayload.suggestion_id = suggestionId;
      if (Object.keys(pageContext).length) requestPayload.page_context = pageContext;
      const response = await fetch(form.action, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
          'X-CSRFToken': csrf.value,
        },
        body: JSON.stringify(requestPayload),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || result.erro || 'Não foi possível enviar a pergunta.');
      if (!result || typeof result.answer !== 'string' || !result.answer.trim()) {
        throw new Error('Resposta inválida do Assistente.');
      }
      addMessage(result.answer, 'assistant');
      addEvidence(result.evidence);
    } catch (failure) {
      showError(failure.message || 'Não foi possível enviar a pergunta.');
    } finally {
      setBusy(false);
      if (!panel.hidden) input.focus();
    }
  };

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    return submitQuestion(input.value.trim());
  });

  suggestionButtons.forEach((button) => {
    bindSuggestion(button);
  });

  document.addEventListener('qualitative:page-changed', () => {
    if (!panel.hidden && !suggestionsDismissed) requestDynamicSuggestions();
  });

  document.addEventListener('assistant:reset', () => {
    if (!suggestions) return;
    suggestionsDismissed = false;
    renderedDynamicSuggestionKey = '';
    if (context.dynamic_suggestions) {
      suggestions.hidden = true;
      requestDynamicSuggestions(true);
    } else {
      renderFallbackSuggestions();
    }
  });

})();
