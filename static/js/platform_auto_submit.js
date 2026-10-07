(() => {
  const activeRequests = new Map();

  function nativeSubmit(form, url) {
    if ((form.method || 'get').toLowerCase() === 'get') {
      window.location.assign(url);
      return;
    }
    HTMLFormElement.prototype.submit.call(form);
  }

  function getSubmissionUrl(form) {
    const url = new URL(form.action || window.location.href, window.location.href);
    if ((form.method || 'get').toLowerCase() === 'get') {
      // A localized form normally carries hidden fallback values for the
      // neighboring filters.  During a partial update those fields may still
      // reflect the previous server-rendered fragment, so begin with the
      // canonical URL and let the control the user changed take precedence.
      const currentUrl = new URL(window.location.href);
      url.search = currentUrl.search;

      const formData = new FormData(form);
      const controlsByName = new Map();
      for (const control of form.elements) {
        if (!control.name || control.disabled) continue;
        const controls = controlsByName.get(control.name) || [];
        controls.push(control);
        controlsByName.set(control.name, controls);
      }

      for (const [name, controls] of controlsByName) {
        const hasInteractiveControl = controls.some((control) =>
          !(control instanceof HTMLInputElement && control.type === 'hidden'),
        );
        // Hidden inputs retain no-JavaScript behavior.  With JavaScript, they
        // only fill a parameter not already represented by the canonical URL.
        if (!hasInteractiveControl && url.searchParams.has(name)) continue;

        url.searchParams.delete(name);
        for (const value of formData.getAll(name)) url.searchParams.append(name, value);
      }
    }
    return url;
  }

  function replaceFlashes(documentFragment) {
    const incoming = documentFragment.querySelector('#platform-flashes');
    const current = document.getElementById('platform-flashes');
    if (incoming && current) current.replaceWith(incoming);
  }

  async function submitPartially(form) {
    const selector = form.dataset.partialTarget;
    const target = selector && document.querySelector(selector);
    const submissionUrl = getSubmissionUrl(form);
    if (!selector || !target) return nativeSubmit(form, submissionUrl);

    const previousRequest = activeRequests.get(selector);
    if (previousRequest) previousRequest.abort();
    const controller = new AbortController();
    activeRequests.set(selector, controller);

    const scrollY = window.scrollY;
    const focusedId = document.activeElement && document.activeElement.id;
    target.classList.add('platform-partial-target');
    target.setAttribute('aria-busy', 'true');
    form.setAttribute('aria-busy', 'true');

    try {
      const method = (form.method || 'get').toUpperCase();
      const response = await fetch(submissionUrl, {
        method,
        credentials: 'same-origin',
        redirect: 'follow',
        signal: controller.signal,
        headers: { Accept: 'text/html', 'X-Requested-With': 'XMLHttpRequest' },
        body: method === 'GET' ? undefined : new FormData(form),
      });
      if (!response.ok) throw new Error(`Unexpected response status: ${response.status}`);

      const responseDocument = new DOMParser().parseFromString(await response.text(), 'text/html');
      const replacement = responseDocument.querySelector(selector);
      if (!replacement) throw new Error(`Missing partial target: ${selector}`);
      if (controller.signal.aborted || activeRequests.get(selector) !== controller) return;

      document.dispatchEvent(new CustomEvent('platform:partial-before-update', {
        detail: { target: selector, source: form, method, currentTarget: target },
      }));
      target.replaceWith(replacement);
      replaceFlashes(responseDocument);
      if (method === 'GET' && form.dataset.partialHistory !== 'none') {
        const finalUrl = new URL(response.url || submissionUrl, window.location.href);
        finalUrl.hash = submissionUrl.hash;
        window.history.replaceState({}, '', finalUrl);
      }
      window.scrollTo({ top: scrollY, behavior: 'auto' });
      if (focusedId) {
        const replacementFocus = document.getElementById(focusedId);
        if (replacementFocus && !replacementFocus.disabled) replacementFocus.focus({ preventScroll: true });
      }
      document.dispatchEvent(new CustomEvent('platform:partial-updated', {
        detail: { target: selector, source: form, method },
      }));
    } catch (error) {
      if (error.name !== 'AbortError') nativeSubmit(form, submissionUrl);
    } finally {
      if (activeRequests.get(selector) === controller) activeRequests.delete(selector);
      const currentTarget = document.querySelector(selector);
      if (currentTarget) currentTarget.removeAttribute('aria-busy');
      form.removeAttribute('aria-busy');
    }
  }

  document.addEventListener('change', (event) => {
    const select = event.target.closest('select[data-auto-submit]');
    if (!select || select.disabled || !select.form || select.form.method.toLowerCase() !== 'get') return;
    if (typeof select.form.requestSubmit === 'function') select.form.requestSubmit();
    else select.form.submit();
  });

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !form.matches('form[data-partial-update]')) return;
    event.preventDefault();
    submitPartially(form);
  });
})();
