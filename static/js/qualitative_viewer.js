/* PDF.js 5.4.624 servido localmente. Apenas páginas próximas são renderizadas. */
import * as pdfjsLib from '../vendor/pdfjs/build/pdf.min.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL('../vendor/pdfjs/build/pdf.worker.min.mjs', import.meta.url).href;

const root = document.querySelector('[data-qualitative-viewer]');
if (root) {
  const scroll = root.querySelector('[data-pdf-scroll]');
  const status = root.querySelector('[data-viewer-status]');
  const pageOutput = root.querySelector('[data-current-page]');
  const zoom = root.querySelector('[data-zoom]');
  const searchForm = root.querySelector('[data-search-form]');
  const searchMessage = root.querySelector('[data-search-message]');
  const resultCount = root.querySelector('[data-result-count]');
  const resultSnippet = root.querySelector('[data-result-snippet]');
  const prev = root.querySelector('[data-result-prev]');
  const next = root.querySelector('[data-result-next]');
  const focusPanel = root.querySelector('[data-focus-panel]');
  const focusToggle = root.querySelector('[data-focus-toggle]');
  const focusLabel = root.querySelector('[data-focus-label]');
  const focusPanelToggle = root.querySelector('[data-focus-panel-toggle]');
  const focusHandle = root.querySelector('[data-focus-handle]');
  const pageCount = Number(root.dataset.pageCount);
  const firstPage = Number(root.dataset.initialPage);
  const nodes = new Map();
  const pending = new Set();
  const tasks = new Map();
  let documentPdf;
  let observer;
  let rendering = 0;
  let generation = 0;
  let activePage = firstPage;
  let results = [];
  let resultIndex = -1;
  let overlayRequest = 0;
  let scrollScheduled = false;
  let resizeAnchor;
  let panelDrag;
  let viewerWidth = 0;

  const searchNotice = (message) => { searchMessage.textContent = message; };
  const makePage = (number) => {
    const shell = document.createElement('section');
    shell.className = 'platform-qualitative-pdf-page';
    shell.dataset.pageNumber = String(number);
    shell.setAttribute('aria-label', `Página ${number}`);
    const label = document.createElement('span');
    label.className = 'platform-qualitative-pdf-page-label';
    label.textContent = `Página ${number}`;
    const surface = document.createElement('div');
    surface.className = 'platform-qualitative-pdf-surface';
    shell.append(label, surface);
    scroll.append(shell);
    nodes.set(number, { shell, surface, rendered: -1 });
    return shell;
  };

  const releaseDistant = () => {
    for (const [number, node] of nodes) {
      if (Math.abs(number - activePage) <= 4 || node.rendered < 0) continue;
      tasks.get(number)?.cancel();
      const canvas = node.surface.querySelector('canvas');
      if (canvas) { canvas.width = 0; canvas.height = 0; }
      node.surface.replaceChildren();
      node.rendered = -1;
    }
  };

  const renderPage = async (number, expectedGeneration) => {
    if (!documentPdf || expectedGeneration !== generation) return;
    const node = nodes.get(number);
    if (!node || node.rendered === generation) return;
    const page = await documentPdf.getPage(number);
    if (expectedGeneration !== generation) return;
    const unit = page.getViewport({ scale: 1 });
    const fit = Math.min(1.6, Math.max(260, scroll.clientWidth - 34) / unit.width);
    const viewport = page.getViewport({ scale: fit * Number(zoom.value) });
    const ratio = Math.min(window.devicePixelRatio || 1, 2,
      Math.sqrt(12000000 / (viewport.width * viewport.height)));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.floor(viewport.width * ratio));
    canvas.height = Math.max(1, Math.floor(viewport.height * ratio));
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;
    node.surface.style.width = `${viewport.width}px`;
    node.surface.style.height = `${viewport.height}px`;
    node.shell.style.minHeight = `${viewport.height + 35}px`;
    node.surface.replaceChildren(canvas);
    const task = page.render({ canvasContext: canvas.getContext('2d'), viewport,
      transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0] });
    tasks.set(number, task);
    try {
      await task.promise;
      if (expectedGeneration === generation) {
        node.rendered = generation;
        if (resizeAnchor?.generation === expectedGeneration && resizeAnchor.page === number) {
          scroll.scrollTop = node.shell.offsetTop + resizeAnchor.fraction * node.shell.offsetHeight;
          resizeAnchor = undefined;
        }
        if (resultIndex >= 0 && results[resultIndex].page_number === number) {
          await showOverlay(results[resultIndex]);
        }
      }
    } catch (error) {
      if (error?.name !== 'RenderingCancelledException') {
        console.error('Não foi possível renderizar a página do PDF.', error);
        node.surface.textContent = 'Página indisponível para visualização.';
      }
    } finally { tasks.delete(number); page.cleanup(); }
  };

  const queue = [];
  const enqueue = (number) => {
    if (!documentPdf || number < 1 || number > pageCount || pending.has(number)
        || nodes.get(number)?.rendered === generation) return;
    pending.add(number);
    queue.push({ number, generation });
    pump();
  };
  const pump = () => {
    while (rendering < 2 && queue.length) {
      const item = queue.shift();
      pending.delete(item.number);
      if (item.generation !== generation) continue;
      rendering += 1;
      renderPage(item.number, item.generation).finally(() => { rendering -= 1; pump(); });
    }
  };
  const updateCurrentPage = () => {
    const threshold = scroll.scrollTop + scroll.clientHeight * 0.35;
    let current = 1;
    for (const [number, node] of nodes) {
      if (node.shell.offsetTop <= threshold) current = number;
      else break;
    }
    activePage = current;
    pageOutput.value = String(current);
    releaseDistant();
  };
  scroll.addEventListener('scroll', () => {
    if (scrollScheduled) return;
    scrollScheduled = true;
    requestAnimationFrame(() => { scrollScheduled = false; updateCurrentPage(); });
  }, { passive: true });

  const goToPage = (number) => {
    const node = nodes.get(number);
    if (!node) return;
    // scrollIntoView desloca também a página externa e esconde o cabeçalho.
    // A navegação do PDF deve mover somente a área de leitura.
    scroll.scrollTo({ top: node.shell.offsetTop, behavior: 'smooth' });
    enqueue(number);
    enqueue(number + 1);
    enqueue(number - 1);
  };

  const showOverlay = async (result) => {
    const requestNumber = ++overlayRequest;
    for (const node of nodes.values()) node.surface.querySelector('.platform-qualitative-search-overlay')?.remove();
    const node = nodes.get(result.page_number);
    if (!node || node.rendered !== generation) return;
    const layoutUrl = root.dataset.layoutUrlTemplate.replace('/paginas/0/layout',
      `/paginas/${result.page_number}/layout`);
    try {
      const response = await fetch(layoutUrl, { credentials: 'same-origin' });
      if (!response.ok) throw new Error('Layout indisponível');
      const layout = await response.json();
      if (requestNumber !== overlayRequest || result !== results[resultIndex]
          || node.rendered !== generation) return;
      if (!layout.layout_available || layout.page_text_hash !== result.page_text_hash) {
        searchNotice('Posição visual indisponível nesta página; consulte o trecho textual.');
        return;
      }
      const matching = layout.items.filter((item) => item.start < result.end_offset
        && item.end > result.start_offset);
      if (!matching.length) {
        searchNotice('Posição visual indisponível nesta página; consulte o trecho textual.');
        return;
      }
      const overlay = document.createElement('div');
      overlay.className = 'platform-qualitative-search-overlay';
      for (const item of matching) {
        const box = document.createElement('span');
        box.className = 'platform-qualitative-search-box';
        box.style.left = `${item.bbox[0] / layout.width * 100}%`;
        box.style.top = `${item.bbox[1] / layout.height * 100}%`;
        box.style.width = `${(item.bbox[2] - item.bbox[0]) / layout.width * 100}%`;
        box.style.height = `${(item.bbox[3] - item.bbox[1]) / layout.height * 100}%`;
        overlay.append(box);
      }
      node.surface.append(overlay);
      searchNotice('');
    } catch (_) {
      if (requestNumber === overlayRequest) searchNotice('Posição visual indisponível; consulte o trecho textual.');
    }
  };

  const showResult = (index) => {
    if (!results.length) return;
    resultIndex = (index + results.length) % results.length;
    const result = results[resultIndex];
    resultCount.textContent = `${resultIndex + 1} de ${results.length}`;
    resultSnippet.textContent = `Página ${result.page_number}: …${result.snippet}…`;
    goToPage(result.page_number);
    showOverlay(result);
  };
  prev.addEventListener('click', () => showResult(resultIndex - 1));
  next.addEventListener('click', () => showResult(resultIndex + 1));
  searchForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = new FormData(searchForm);
    const params = new URLSearchParams({ q: form.get('q'), grep: form.has('grep') ? '1' : '0',
      case: form.has('case') ? '1' : '0' });
    results = [];
    resultIndex = -1;
    overlayRequest += 1;
    resultCount.textContent = '0 de 0';
    resultSnippet.textContent = '';
    prev.disabled = next.disabled = true;
    for (const node of nodes.values()) node.surface.querySelector('.platform-qualitative-search-overlay')?.remove();
    searchNotice('Pesquisando…');
    try {
      const response = await fetch(`${root.dataset.searchUrl}?${params}`, { credentials: 'same-origin' });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Não foi possível concluir a busca.');
      results = payload.results;
      prev.disabled = next.disabled = results.length === 0;
      searchNotice(payload.truncated ? 'Limite de 200 resultados; refine a busca.'
        : results.length ? '' : 'Nenhuma correspondência neste documento.');
      if (results.length) showResult(0);
    } catch (error) { searchNotice(error.message); }
  });

  const rerenderVisiblePages = () => {
    viewerWidth = scroll.clientWidth;
    const anchor = nodes.get(activePage);
    const fraction = anchor ? Math.max(0, Math.min(1,
      (scroll.scrollTop - anchor.shell.offsetTop) / Math.max(1, anchor.shell.offsetHeight))) : 0;
    generation += 1;
    resizeAnchor = anchor ? { page: activePage, fraction, generation } : undefined;
    queue.length = 0;
    pending.clear();
    for (const [number, node] of nodes) {
      tasks.get(number)?.cancel();
      const canvas = node.surface.querySelector('canvas');
      if (canvas) { canvas.width = 0; canvas.height = 0; }
      node.surface.replaceChildren();
      node.rendered = -1;
    }
    enqueue(activePage);
    enqueue(activePage + 1);
    enqueue(activePage - 1);
  };
  zoom.addEventListener('change', rerenderVisiblePages);

  const focusActive = () => document.body.classList.contains('platform-qualitative-focus');
  const movePanelTo = (left, top) => {
    const limitX = Math.max(8, window.innerWidth - focusPanel.offsetWidth - 8);
    const limitY = Math.max(8, window.innerHeight - focusPanel.offsetHeight - 8);
    focusPanel.style.left = `${Math.min(limitX, Math.max(8, left))}px`;
    focusPanel.style.top = `${Math.min(limitY, Math.max(8, top))}px`;
  };
  const updateFocusPanel = (minimized) => {
    focusPanel.classList.toggle('is-minimized', minimized);
    focusPanelToggle.setAttribute('aria-expanded', String(!minimized));
    const action = minimized ? 'Restaurar painel' : 'Minimizar painel';
    focusPanelToggle.setAttribute('aria-label', action);
    focusPanelToggle.title = action;
    focusPanelToggle.textContent = minimized ? 'Expandir' : 'Recolher';
    if (focusActive() && !window.matchMedia('(max-width: 700px)').matches) {
      requestAnimationFrame(() => movePanelTo(focusPanel.offsetLeft, focusPanel.offsetTop));
    }
  };
  focusPanelToggle.addEventListener('click', () => updateFocusPanel(!focusPanel.classList.contains('is-minimized')));
  focusToggle.addEventListener('click', () => {
    const oldWidth = scroll.clientWidth;
    const focused = !focusActive();
    document.body.classList.toggle('platform-qualitative-focus', focused);
    document.body.classList.remove('platform-menu-open');
    focusToggle.setAttribute('aria-pressed', String(focused));
    const action = focused ? 'Sair do modo foco' : 'Modo foco';
    focusToggle.setAttribute('aria-label', action);
    focusToggle.title = action;
    focusLabel.textContent = action;
    if (focused) updateFocusPanel(window.matchMedia('(max-width: 700px)').matches);
    requestAnimationFrame(() => {
      if (focused && !window.matchMedia('(max-width: 700px)').matches) {
        if (focusPanel.style.left) movePanelTo(focusPanel.offsetLeft, focusPanel.offsetTop);
        else {
          const rect = root.querySelector('.platform-qualitative-document').getBoundingClientRect();
          movePanelTo(rect.left + 16, rect.top + 55);
        }
      }
      if (documentPdf && Math.abs(scroll.clientWidth - oldWidth) > 2) rerenderVisiblePages();
    });
  });
  focusHandle.addEventListener('pointerdown', (event) => {
    if (!focusActive() || window.matchMedia('(max-width: 700px)').matches || event.button !== 0) return;
    panelDrag = { id: event.pointerId, x: event.clientX, y: event.clientY,
      left: focusPanel.offsetLeft, top: focusPanel.offsetTop };
    focusHandle.setPointerCapture(event.pointerId);
    event.preventDefault();
  });
  focusHandle.addEventListener('pointermove', (event) => {
    if (!panelDrag || panelDrag.id !== event.pointerId) return;
    movePanelTo(panelDrag.left + event.clientX - panelDrag.x,
      panelDrag.top + event.clientY - panelDrag.y);
  });
  const stopPanelDrag = (event) => {
    if (panelDrag?.id === event.pointerId) panelDrag = undefined;
  };
  focusHandle.addEventListener('pointerup', stopPanelDrag);
  focusHandle.addEventListener('pointercancel', stopPanelDrag);
  focusHandle.addEventListener('keydown', (event) => {
    if (!focusActive() || window.matchMedia('(max-width: 700px)').matches) return;
    const direction = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
    if (!direction) return;
    event.preventDefault();
    const step = event.shiftKey ? 48 : 16;
    movePanelTo(focusPanel.offsetLeft + direction[0] * step,
      focusPanel.offsetTop + direction[1] * step);
  });
  window.addEventListener('resize', () => {
    if (!focusActive()) return;
    requestAnimationFrame(() => {
      if (!window.matchMedia('(max-width: 700px)').matches) movePanelTo(focusPanel.offsetLeft, focusPanel.offsetTop);
      if (documentPdf && Math.abs(scroll.clientWidth - viewerWidth) > 2) rerenderVisiblePages();
    });
  });
  const explorer = root.querySelector('#qualitative-explorer');
  const explorerToggle = root.querySelector('[data-explorer-toggle]');
  explorerToggle.addEventListener('click', () => {
    const open = !explorer.classList.contains('is-open');
    explorer.classList.toggle('is-open', open);
    explorerToggle.setAttribute('aria-expanded', String(open));
  });
  const info = document.querySelector('[data-qualitative-info]');
  document.querySelector('[data-qualitative-info-open]')?.addEventListener('click', () => info.showModal());
  info.querySelector('[data-qualitative-info-close]')?.addEventListener('click', () => info.close());

  try {
    const base = root.dataset.pdfjsBase;
    const loading = pdfjsLib.getDocument({ url: root.dataset.pdfUrl, cMapUrl: `${base}cmaps/`,
      cMapPacked: true, standardFontDataUrl: `${base}standard_fonts/`, wasmUrl: `${base}wasm/` });
    documentPdf = await loading.promise;
    if (documentPdf.numPages !== pageCount) throw new Error('O PDF não corresponde ao corpus preparado.');
    viewerWidth = scroll.clientWidth;
    status.remove();
    for (let number = 1; number <= pageCount; number += 1) makePage(number);
    observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const number = Number(entry.target.dataset.pageNumber);
        enqueue(number);
        enqueue(number + 1);
      }
    }, { root: scroll, rootMargin: '500px 0px', threshold: 0 });
    for (const node of nodes.values()) observer.observe(node.shell);
    goToPage(firstPage);
  } catch (error) {
    console.error('Não foi possível abrir o PDF da Base.', error);
    status.textContent = 'Não foi possível abrir o PDF original. Confira o arquivo da Base.';
  }
}
