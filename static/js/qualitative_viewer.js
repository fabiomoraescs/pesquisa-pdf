/* PDF.js 5.4.624 servido localmente. Apenas páginas próximas são renderizadas. */
import * as pdfjsLib from '../vendor/pdfjs/build/pdf.min.mjs';
import {startSemanticProgress} from './qualitative_semantic_progress.js';

pdfjsLib.GlobalWorkerOptions.workerSrc = new URL('../vendor/pdfjs/build/pdf.worker.min.mjs', import.meta.url).href;

const root = document.querySelector('[data-qualitative-viewer]');
if (root) {
  const scroll = root.querySelector('[data-pdf-scroll]');
  const status = root.querySelector('[data-viewer-status]');
  const pageOutput = root.querySelector('[data-current-page]');
  const zoom = root.querySelector('[data-zoom]');
  const searchForm = root.querySelector('[data-search-form]');
  const automaticToggle = searchForm.querySelector('[data-automatic-toggle]');
  const automaticOptions = searchForm.querySelector('[data-automatic-options]');
  const automaticSettings = searchForm.querySelector('[data-automatic-settings]');
  const searchControlRow = searchForm.querySelector('.platform-qualitative-search-control-row');
  const automaticModes = searchForm.querySelector('.platform-qualitative-automatic-modes');
  const automaticScope = searchForm.querySelector('.platform-qualitative-automatic-scope');
  const contextualRejection = searchForm.querySelector('[data-contextual-rejection]');
  const multipleTerms = searchForm.querySelector('[data-multiple-terms]');
  const multipleControl = multipleTerms?.closest('.platform-qualitative-search-control');
  const searchInput = searchForm.querySelector('[name="q"]');
  const separatorError = searchForm.querySelector('[data-separator-error]');
  const searchSubmit = searchForm.querySelector('[type="submit"]');
  const searchMessage = root.querySelector('[data-search-message]');
  const resultCount = root.querySelector('[data-result-count]');
  const resultSnippet = root.querySelector('[data-result-snippet]');
  const contextNotice = root.querySelector('[data-context-notice]');
  const contextMenu = root.querySelector('[data-context-menu]');
  const codeMenu = root.querySelector('[data-code-menu]');
  const contextDialog = root.querySelector('[data-context-dialog]');
  const recordsRoot = root.querySelector('[data-qualitative-records]');
  const margin = root.querySelector('.platform-qualitative-margin');
  const marginViewport = root.querySelector('[data-margin-viewport]');
  const marginTrack = root.querySelector('[data-margin-track]');
  const connectorLayer = root.querySelector('[data-connector-layer]');
  const prev = root.querySelector('[data-result-prev]');
  const next = root.querySelector('[data-result-next]');
  const searchResults = root.querySelector('.platform-qualitative-search-results');
  const focusNavigation = root.querySelector('[data-focus-navigation]');
  const focusPanel = root.querySelector('[data-focus-panel]');
  const focusToggle = root.querySelector('[data-focus-toggle]');
  const focusLabel = root.querySelector('[data-focus-label]');
  const focusPanelToggle = root.querySelector('[data-focus-panel-toggle]');
  const focusHandle = root.querySelector('[data-focus-handle]');
  const pageCount = Number(root.dataset.pageCount);
  // Corpus, rotas do leitor e PDF.js getPage() usam a mesma convenção: página 1-based.
  const firstPage = Number(root.dataset.initialPage);
  const nodes = new Map();
  const pending = new Set();
  const renderingPages = new Set();
  const tasks = new Map();
  let documentPdf;
  let observer;
  let rendering = 0;
  let generation = 0;
  let activePage = firstPage;
  let results = [];
  let resultIndex = -1;
  let scrollScheduled = false;
  let resizeAnchor;
  let panelDrag;
  let viewerWidth = 0;
  let wheelZoomTimer;
  let contextSnapshot;
  let contextAction;
  let contextRequest = 0;
  let automaticSnapshot;
  let automaticBusy = false;
  let searchRequest = 0;
  const removingCodings = new Set();
  let selectedCodeIds = new Set();
  let activeExcerptId = root.dataset.targetExcerptId || null;
  let excerptDrag;
  let excerptSaving = false;
  let marginScheduled = false;
  let connectorScheduled = false;
  let settleInitialPage;
  let records = JSON.parse(recordsRoot.querySelector('[data-records-initial]').textContent);
  document.addEventListener('qualitative:records-updated', (event) => {
    records = event.detail;
    for (const [number, node] of nodes) {
      if (node.rendered !== generation) continue;
      refreshPageExcerpts(number);
    }
  });
  document.addEventListener('qualitative:code-renamed', (event) => {
    const { codeId, name } = event.detail;
    for (const node of nodes.values()) {
      let changed = false;
      for (const excerpt of node.excerpts || []) {
        for (const code of excerpt.codes || []) {
          if (code.id !== codeId) continue;
          code.name = name;
          changed = true;
        }
      }
      if (changed && node.rendered === generation) renderExcerptOverlay(node);
    }
    scheduleMargin();
  });
  document.addEventListener('qualitative:code-colored', (event) => {
    const { codeId, color, color_hex: hex, color_text: text } = event.detail;
    for (const node of nodes.values()) {
      let changed = false;
      for (const excerpt of node.excerpts || []) {
        for (const code of excerpt.codes || []) {
          if (code.id !== codeId) continue;
          Object.assign(code, { color, color_hex: hex, color_text: text });
          changed = true;
        }
      }
      if (changed && node.rendered === generation) renderExcerptOverlay(node);
    }
    scheduleMargin();
  });
  document.addEventListener('qualitative:rename-code-error', (event) => {
    noticeContext(event.detail.message);
  });

  const pageUrl = (template, number) => template.replace('/paginas/0/', `/paginas/${number}/`);
  const createInitialPageSettler = (targetPage, navigate) => {
    // A página anterior pode ganhar altura ao ser renderizada depois do primeiro scroll.
    const pendingMeasurements = new Set([targetPage]);
    if (targetPage > 1) pendingMeasurements.add(targetPage - 1);
    return (pageNumber) => {
      if (!pendingMeasurements.delete(pageNumber) || pendingMeasurements.size) return;
      requestAnimationFrame(() => navigate(targetPage, false));
    };
  };
  const noticeContext = (message) => { contextNotice.textContent = message; };
  const closeContextMenu = () => { if (contextMenu) contextMenu.hidden = true; };

  const explorerPopovers = [...root.querySelectorAll('[data-explorer-popover]')];
  const positionExplorerPopover = (popover) => {
    const trigger = root.querySelector(`[aria-controls="${popover.id}"]`);
    const rect = trigger.getBoundingClientRect();
    const explorerRect = trigger.closest('#qualitative-explorer').getBoundingClientRect();
    const width = popover.offsetWidth;
    const height = popover.offsetHeight;
    const left = Math.min(Math.max(8, rect.left), Math.max(8, window.innerWidth - width - 8));
    // Keep sibling menu triggers unobstructed while the popover is open.
    const below = Math.max(rect.bottom, explorerRect.bottom) + 4;
    const top = below + height <= window.innerHeight - 8
      ? below : Math.max(8, explorerRect.top - height - 4);
    popover.style.left = `${left}px`;
    popover.style.top = `${top}px`;
  };
  const closeExplorerPopovers = () => {
    for (const popover of explorerPopovers) {
      if (popover.matches(':popover-open')) popover.hidePopover();
    }
  };
  for (const popover of explorerPopovers) {
    popover.addEventListener('toggle', () => {
      const open = popover.matches(':popover-open');
      root.querySelector(`[aria-controls="${popover.id}"]`).setAttribute('aria-expanded', String(open));
      if (open) positionExplorerPopover(popover);
    });
  }
  const repositionExplorerPopovers = () => {
    for (const popover of explorerPopovers) {
      if (popover.matches(':popover-open')) positionExplorerPopover(popover);
    }
  };
  window.addEventListener('scroll', repositionExplorerPopovers, true);

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
    nodes.set(number, { shell, surface, rendered: -1, layout: null, excerpts: [] });
    return shell;
  };

  const syncMarginScroll = () => {
    if (!marginTrack || window.matchMedia('(max-width: 950px)').matches) return;
    marginTrack.style.transform = `translateY(${-scroll.scrollTop}px)`;
  };
  const drawConnectorLines = () => {
    if (!connectorLayer) return;
    connectorLayer.replaceChildren();
    if (window.matchMedia('(max-width: 950px)').matches) return;
    const layerRect = connectorLayer.getBoundingClientRect();
    if (layerRect.width <= 0 || layerRect.height <= 0) return;
    connectorLayer.setAttribute('viewBox', `0 0 ${layerRect.width} ${layerRect.height}`);
    const pdfRect = scroll.getBoundingClientRect();
    const marginRect = margin.getBoundingClientRect();
    const highlights = [...scroll.querySelectorAll('.platform-qualitative-excerpt-box[data-coding-id]')];
    for (const tag of marginTrack.querySelectorAll('.platform-qualitative-coding-tag[data-coding-id]')) {
      const tagRect = tag.getBoundingClientRect();
      if (tagRect.width <= 0 || tagRect.height <= 0
          || tagRect.bottom <= marginRect.top || tagRect.top >= marginRect.bottom) continue;
      const matching = highlights.filter((box) => box.dataset.codingId === tag.dataset.codingId)
        .map((box) => ({ box, rect: box.getBoundingClientRect() }))
        .filter(({ rect }) => rect.width > 0 && rect.height > 0
          && rect.bottom > pdfRect.top && rect.top < pdfRect.bottom);
      if (!matching.length) continue;
      const target = matching.reduce((closest, candidate) => {
        const tagCenter = tagRect.top + tagRect.height / 2;
        const candidateDistance = Math.abs(candidate.rect.top + candidate.rect.height / 2 - tagCenter);
        const closestDistance = Math.abs(closest.rect.top + closest.rect.height / 2 - tagCenter);
        return candidateDistance < closestDistance
          || (candidateDistance === closestDistance && candidate.rect.right > closest.rect.right)
          ? candidate : closest;
      });
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.dataset.codingId = tag.dataset.codingId;
      line.dataset.excerptId = target.box.dataset.excerptId;
      line.setAttribute('x1', String(tagRect.left - layerRect.left + 1));
      line.setAttribute('y1', String(tagRect.top - layerRect.top + tagRect.height / 2));
      line.setAttribute('x2', String(target.rect.right - layerRect.left - 1));
      line.setAttribute('y2', String(target.rect.top - layerRect.top + target.rect.height / 2));
      line.setAttribute('stroke', getComputedStyle(tag).getPropertyValue('--qualitative-code-color').trim() || '#93C5FD');
      line.setAttribute('stroke-width', '1.5');
      line.setAttribute('stroke-linecap', 'round');
      line.setAttribute('stroke-opacity', '.78');
      connectorLayer.append(line);
    }
  };
  const scheduleConnectorLines = () => {
    if (!connectorLayer || connectorScheduled) return;
    connectorScheduled = true;
    requestAnimationFrame(() => {
      connectorScheduled = false;
      drawConnectorLines();
    });
  };
  const scheduleMargin = () => {
    if (!marginTrack || marginScheduled) return;
    marginScheduled = true;
    requestAnimationFrame(() => {
      marginScheduled = false;
      renderMargin();
      scheduleConnectorLines();
    });
  };

  const releaseDistant = () => {
    for (const [number, node] of nodes) {
      if (Math.abs(number - activePage) <= 4 || node.rendered < 0) continue;
      tasks.get(number)?.cancel();
      const canvas = node.surface.querySelector('canvas');
      if (canvas) { canvas.width = 0; canvas.height = 0; }
      node.surface.replaceChildren();
      node.rendered = -1;
      scheduleMargin();
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
    if (expectedGeneration === generation) settleInitialPage?.(number);
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
        await decoratePage(number, expectedGeneration);
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
        || renderingPages.has(`${generation}:${number}`)
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
      const key = `${item.generation}:${item.number}`;
      if (renderingPages.has(key)) continue;
      renderingPages.add(key);
      rendering += 1;
      renderPage(item.number, item.generation).finally(() => {
        renderingPages.delete(key); rendering -= 1; pump();
      });
    }
  };
  const updateCurrentPage = () => {
    const threshold = scroll.scrollTop + scroll.clientHeight * 0.35;
    let current = 1;
    for (const [number, node] of nodes) {
      if (node.shell.offsetTop <= threshold) current = number;
      else break;
    }
    if (current !== activePage) { closeContextMenu(); contextRequest += 1; }
    activePage = current;
    pageOutput.value = String(current);
    releaseDistant();
  };
  scroll.addEventListener('scroll', () => {
    syncMarginScroll();
    scheduleConnectorLines();
    if (scrollScheduled) return;
    scrollScheduled = true;
    requestAnimationFrame(() => { scrollScheduled = false; updateCurrentPage(); });
  }, { passive: true });

  const goToPage = (number, smooth = true) => {
    const node = nodes.get(number);
    if (!node) return;
    // scrollIntoView desloca também a página externa e esconde o cabeçalho.
    // A navegação do PDF deve mover somente a área de leitura.
    scroll.scrollTo({ top: node.shell.offsetTop, behavior: smooth ? 'smooth' : 'auto' });
    enqueue(number);
    enqueue(number + 1);
    enqueue(number - 1);
  };

  // Um layout antigo pode ter caixas por palavra. Jamais interpolamos uma
  // substring dentro dessa caixa: só limites exatos produzem destaque.
  const exactItems = (layout, start, end) => {
    if (!layout?.layout_available) return [];
    const items = layout.items.filter((item) => item.start < end && item.end > start);
    if (!items.length || items.some((item) => item.start < start || item.end > end)) return [];
    return items;
  };
  const mergeGeometry = (items) => {
    const runs = [];
    for (const item of items) {
      const previous = runs[runs.length - 1];
      if (previous && previous.end === item.start
          && Math.abs(previous.bbox[1] - item.bbox[1]) < 2
          && item.bbox[0] - previous.bbox[2] < 3) {
        previous.end = item.end;
        previous.bbox[2] = Math.max(previous.bbox[2], item.bbox[2]);
        previous.bbox[3] = Math.max(previous.bbox[3], item.bbox[3]);
      } else runs.push({ start: item.start, end: item.end, bbox: [...item.bbox] });
    }
    return runs;
  };
  const placeBox = (item, layout, className) => {
    const box = document.createElement('span');
    box.className = className;
    box.style.left = `${item.bbox[0] / layout.width * 100}%`;
    box.style.top = `${item.bbox[1] / layout.height * 100}%`;
    box.style.width = `${(item.bbox[2] - item.bbox[0]) / layout.width * 100}%`;
    box.style.height = `${(item.bbox[3] - item.bbox[1]) / layout.height * 100}%`;
    return box;
  };
  const selectableGroups = (items) => {
    const groups = [];
    for (const item of items) {
      const previous = groups[groups.length - 1];
      if (previous && !previous.quad && !item.quad && previous.end === item.start && previous.text.length < 40
          && Math.abs(previous.bbox[1] - item.bbox[1]) < 2
          && item.bbox[0] - previous.bbox[2] < 3) {
        previous.end = item.end;
        previous.text += item.text;
        previous.bbox[2] = Math.max(previous.bbox[2], item.bbox[2]);
        previous.bbox[3] = Math.max(previous.bbox[3], item.bbox[3]);
      } else groups.push({ start: item.start, end: item.end, text: item.text,
        bbox: [...item.bbox], quad: item.quad });
    }
    return groups;
  };
  const renderTextLayer = (node, layout) => {
    node.surface.querySelector('.platform-qualitative-text-layer')?.remove();
    if (!layout?.layout_available) return 0;
    const layer = document.createElement('div');
    layer.className = 'platform-qualitative-text-layer';
    layer.setAttribute('aria-label', 'Texto selecionável verificado contra o corpus');
    const scale = node.surface.clientWidth / layout.width;
    const measure = document.createElement('canvas').getContext('2d');
    const fontFamily = getComputedStyle(node.surface).fontFamily || 'sans-serif';
    let skipped = 0;
    for (const item of selectableGroups(layout.items)) {
      const span = placeBox(item, layout, 'platform-qualitative-text-item');
      span.dataset.start = String(item.start);
      span.dataset.end = String(item.end);
      span.textContent = item.text;
      const quad = item.quad || [[item.bbox[0], item.bbox[1]], [item.bbox[2], item.bbox[1]],
        [item.bbox[0], item.bbox[3]], [item.bbox[2], item.bbox[3]]];
      const height = Math.hypot(quad[2][0] - quad[0][0], quad[2][1] - quad[0][1]) * scale;
      const fontSize = Math.max(1, height);
      span.style.fontSize = `${fontSize}px`;
      span.style.fontFamily = fontFamily;
      span.style.width = 'max-content';
      span.style.height = `${fontSize}px`;
      measure.font = `${fontSize}px ${fontFamily}`;
      const naturalWidth = measure.measureText(item.text).width;
      // Texto transparente segue a geometria real, não as métricas da fonte
      // substituta do navegador. Não descartar fontes estreitas/largas por
      // um limite arbitrário de scaleX. Quad também preserva texto rotacionado.
      if (naturalWidth > 0 && Number.isFinite(naturalWidth) && height > 0) {
        span.style.left = `${quad[0][0] / layout.width * 100}%`;
        span.style.top = `${quad[0][1] / layout.height * 100}%`;
        span.style.transformOrigin = 'left top';
        span.style.transform = `matrix(${(quad[1][0] - quad[0][0]) * scale / naturalWidth},`
          + `${(quad[1][1] - quad[0][1]) * scale / naturalWidth},`
          + `${(quad[2][0] - quad[0][0]) * scale / fontSize},`
          + `${(quad[2][1] - quad[0][1]) * scale / fontSize},0,0)`;
        layer.append(span);
      } else skipped += 1;
    }
    node.surface.append(layer);
    if (skipped && !node.textLayerWarned) {
      console.warn('Camada textual incompleta.', { page: node.shell.dataset.pageNumber, skipped });
      node.textLayerWarned = true;
    }
    return layer.childElementCount;
  };
  const renderExcerptOverlay = (node) => {
    node.surface.querySelector('.platform-qualitative-excerpt-overlay')?.remove();
    node.surface.querySelectorAll('.platform-qualitative-excerpt-handle').forEach((item) => item.remove());
    if (!node.layout?.layout_available) return;
    const overlay = document.createElement('div');
    overlay.className = 'platform-qualitative-excerpt-overlay';
    for (const excerpt of node.excerpts) {
      if (!excerpt.codes.length && !excerpt.memo_count) continue;
      if (excerpt.page_hash !== node.layout.page_text_hash) continue;
      const spans = exactItems(node.layout, excerpt.start, excerpt.end);
      for (const item of mergeGeometry(spans)) {
        const layers = excerpt.codes.length ? excerpt.codes : [null];
        layers.forEach((code, index) => {
          const box = placeBox(item, node.layout, 'platform-qualitative-excerpt-box');
          box.dataset.excerptId = excerpt.id;
          box.classList.toggle('is-active', excerpt.id === activeExcerptId);
          box.classList.toggle('is-memo-only', !code);
          if (code) {
            box.dataset.codeId = code.id;
            box.dataset.codingId = code.coding_id;
            box.style.setProperty('--qualitative-code-color', code.color_hex || '#93C5FD');
            if (layers.length > 1) {
              box.style.clipPath = `inset(${index * 100 / layers.length}% 0 ${
                (layers.length - index - 1) * 100 / layers.length}% 0)`;
            }
          }
          overlay.append(box);
        });
      }
      if (excerpt.id === activeExcerptId && root.dataset.canAnnotate === '1'
          && spans.length && node.layout.items.every((item) => item.end - item.start === 1)) {
        for (const [kind, item, x] of [
          ['start', spans[0], spans[0].bbox[0]],
          ['end', spans[spans.length - 1], spans[spans.length - 1].bbox[2]],
        ]) {
          const handle = document.createElement('button');
          handle.type = 'button';
          handle.className = 'platform-qualitative-excerpt-handle';
          handle.dataset.handle = kind;
          handle.dataset.excerptId = excerpt.id;
          handle.setAttribute('aria-label', `Ajustar ${kind === 'start' ? 'início' : 'fim'} do trecho`);
          handle.title = handle.getAttribute('aria-label');
          handle.style.left = `${x / node.layout.width * 100}%`;
          handle.style.top = `${item.bbox[3] / node.layout.height * 100}%`;
          node.surface.append(handle);
        }
      }
    }
    node.surface.append(overlay);
  };
  const scrollToExcerpt = (number, excerptId) => {
    const node = nodes.get(number);
    const box = [...(node?.surface.querySelectorAll('.platform-qualitative-excerpt-box') || [])]
      .find((item) => item.dataset.excerptId === excerptId);
    if (!box) return;
    const top = box.getBoundingClientRect().top - scroll.getBoundingClientRect().top
      + scroll.scrollTop - scroll.clientHeight * .34;
    scroll.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
  };
  const activateExcerpt = (number, excerptId, move = false) => {
    const node = nodes.get(number);
    if (!node?.excerpts.some((item) => item.id === excerptId)) return;
    activeExcerptId = excerptId;
    for (const visible of nodes.values()) {
      if (visible.rendered === generation) renderExcerptOverlay(visible);
    }
    scheduleMargin();
    if (move) requestAnimationFrame(() => scrollToExcerpt(number, excerptId));
  };
  const renderMargin = () => {
    if (!marginTrack) return;
    const compact = window.matchMedia('(max-width: 950px)').matches;
    if (!compact) {
      const outer = margin.getBoundingClientRect();
      const pdf = scroll.getBoundingClientRect();
      margin.style.minHeight = `${Math.max(0, pdf.bottom - outer.top)}px`;
      marginViewport.style.top = `${pdf.top - outer.top}px`;
      marginViewport.style.height = `${scroll.clientHeight}px`;
      marginTrack.style.height = `${scroll.scrollHeight}px`;
    } else {
      margin.style.minHeight = '';
      marginViewport.style.top = '';
      marginViewport.style.height = '';
      marginTrack.style.height = '';
      marginTrack.style.transform = '';
    }
    const entries = [];
    for (const [number, node] of nodes) {
      if (node.rendered !== generation || !node.layout?.layout_available) continue;
      for (const excerpt of node.excerpts) {
        if (!excerpt.codes.length && !excerpt.memo_count) continue;
        const first = exactItems(node.layout, excerpt.start, excerpt.end)[0];
        if (!first) continue;
        const y = node.shell.offsetTop + node.surface.offsetTop
          + first.bbox[1] / node.layout.height * node.surface.clientHeight;
        entries.push({ number, excerpt, y });
      }
    }
    entries.sort((a, b) => a.y - b.y || a.excerpt.id.localeCompare(b.excerpt.id));
    marginTrack.replaceChildren();
    let bottom = 0;
    for (const { number, excerpt, y } of entries) {
      const card = document.createElement('div');
      card.setAttribute('role', 'group');
      card.className = 'platform-qualitative-margin-card';
      card.classList.toggle('is-active', excerpt.id === activeExcerptId);
      card.dataset.excerptId = excerpt.id;
      card.dataset.pageNumber = String(number);
      card.setAttribute('aria-label', `Ir ao trecho da página ${number}: ${excerpt.codes.map((c) => c.name).join(', ') || 'Memo'}`);
      for (const code of excerpt.codes) {
        const tag = document.createElement('span');
        tag.className = 'platform-qualitative-coding-tag';
        tag.dataset.codeId = code.id;
        tag.dataset.codingId = code.coding_id;
        tag.style.setProperty('--qualitative-code-color', code.color_hex || '#93C5FD');
        tag.style.setProperty('--qualitative-code-text', code.color_text || '#000000');
        const name = document.createElement('button');
        name.type = 'button';
        name.className = 'platform-qualitative-coding-name';
        name.dataset.codeId = code.id;
        name.textContent = code.name;
        name.setAttribute('aria-label', `Ir ao trecho do código ${code.name}`);
        tag.title = ({ manual: 'Manual', assisted: 'Assistida', automatic_literal: 'Automática · Literal',
          automatic_regex: 'Automática · Regex', automatic_lexical: 'Automática · Lexical',
          automatic_semantic: 'Automática · Semântica' })[code.origin] || 'Manual';
        tag.append(name);
        if (root.dataset.canAnnotate === '1' && code.coding_id) {
          const remove = document.createElement('button');
          remove.type = 'button';
          remove.className = 'platform-qualitative-coding-remove';
          remove.dataset.removeCoding = code.coding_id;
          remove.disabled = removingCodings.has(code.coding_id);
          remove.textContent = '×';
          remove.title = `Remover codificação ${code.name}`;
          remove.setAttribute('aria-label', remove.title);
          tag.append(remove);
        }
        card.append(tag);
      }
      if (excerpt.memo_count) {
        const memos = document.createElement('button');
        memos.type = 'button';
        memos.className = 'platform-qualitative-coding-name';
        memos.textContent = `${excerpt.memo_count} ${excerpt.memo_count === 1 ? 'memo' : 'memos'}`;
        card.append(memos);
      }
      marginTrack.append(card);
      if (!compact) {
        const top = Math.max(y, bottom + 6);
        card.style.top = `${top}px`;
        const displacement = top - y;
        card.style.setProperty('--margin-displacement', `${displacement}px`);
        bottom = top + card.offsetHeight;
      }
    }
    syncMarginScroll();
  };
  marginTrack?.addEventListener('click', async (event) => {
    const card = event.target.closest('[data-excerpt-id]');
    const remove = event.target.closest('[data-remove-coding]');
    if (remove) {
      const identifier = remove.dataset.removeCoding;
      if (removingCodings.has(identifier) || automaticBusy) return;
      event.stopPropagation();
      removingCodings.add(identifier);
      remove.disabled = true;
      try {
        const url = root.dataset.deleteCodingUrlTemplate.replace('00000000-0000-0000-0000-000000000000', identifier);
        const response = await fetch(url, { method: 'DELETE', credentials: 'same-origin',
          headers: { 'X-CSRFToken': root.querySelector('[data-context-csrf]').value, Accept: 'application/json' } });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || 'Não foi possível remover esta codificação.');
        const node = nodes.get(payload.page_number);
        if (node) {
          node.excerpts = payload.page.excerpts;
          if (!node.excerpts.some(item => item.id === activeExcerptId)) activeExcerptId = null;
          if (node.rendered === generation) renderExcerptOverlay(node);
        }
        scheduleMargin();
        document.dispatchEvent(new CustomEvent('qualitative:records-updated', { detail: payload.records }));
        noticeContext('Codificação removida. Os demais códigos e memos foram preservados.');
      } catch (error) { noticeContext(error.message); }
      finally { removingCodings.delete(identifier); remove.disabled = false; }
      return;
    }
    if (card) activateExcerpt(Number(card.dataset.pageNumber), card.dataset.excerptId, true);
  });
  if (codeMenu && marginTrack) {
    let codeToRename = null;
    let colorPending = false;
    const swatches = [...codeMenu.querySelectorAll('[data-code-color]')];
    const setColorBusy = (busy) => {
      colorPending = busy;
      swatches.forEach((swatch) => { swatch.disabled = busy; });
    };
    const openCodeMenu = (identifier, point) => {
      if (colorPending) return;
      codeToRename = identifier;
      closeContextMenu();
      codeMenu.querySelector('[data-code-menu-actions]').hidden = false;
      codeMenu.querySelector('[data-code-palette]').hidden = true;
      codeMenu.hidden = false;
      document.dispatchEvent(new CustomEvent('platform:popover-open', { detail: { panel: codeMenu } }));
      codeMenu.style.left = `${Math.max(8, Math.min(point.x, innerWidth - codeMenu.offsetWidth - 8))}px`;
      codeMenu.style.top = `${Math.max(8, Math.min(point.y, innerHeight - codeMenu.offsetHeight - 8))}px`;
      codeMenu.querySelector('[data-code-rename]').focus();
    };
    marginTrack.addEventListener('contextmenu', (event) => {
      const label = event.target.closest('.platform-qualitative-coding-tag[data-code-id]');
      if (!label || event.target.closest('[data-remove-coding]') || root.dataset.canAnnotate !== '1') return;
      event.preventDefault();
      openCodeMenu(label.dataset.codeId, { x: event.clientX, y: event.clientY });
    });
    marginTrack.addEventListener('keydown', (event) => {
      if (event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) return;
      const label = event.target.closest('.platform-qualitative-coding-tag[data-code-id]');
      if (!label || event.target.closest('[data-remove-coding]') || root.dataset.canAnnotate !== '1') return;
      event.preventDefault();
      const rect = label.getBoundingClientRect();
      openCodeMenu(label.dataset.codeId, { x: rect.left, y: rect.bottom });
    });
    codeMenu.addEventListener('click', (event) => {
      if (!codeToRename || colorPending) return;
      if (event.target.closest('[data-code-color-open]')) {
        const current = records.codes.find((code) => code.id === codeToRename)?.color || 'blue';
        swatches.forEach((swatch) => swatch.setAttribute('aria-checked', String(swatch.dataset.codeColor === current)));
        codeMenu.querySelector('[data-code-menu-actions]').hidden = true;
        codeMenu.querySelector('[data-code-palette]').hidden = false;
        codeMenu.style.left = `${Math.max(8, Math.min(parseFloat(codeMenu.style.left) || 8,
          innerWidth - codeMenu.offsetWidth - 8))}px`;
        codeMenu.style.top = `${Math.max(8, Math.min(parseFloat(codeMenu.style.top) || 8,
          innerHeight - codeMenu.offsetHeight - 8))}px`;
        swatches.find((swatch) => swatch.dataset.codeColor === current)?.focus();
        return;
      }
      const swatch = event.target.closest('[data-code-color]');
      if (swatch) {
        setColorBusy(true);
        document.dispatchEvent(new CustomEvent('qualitative:color-code-requested',
          { detail: { codeId: codeToRename, color: swatch.dataset.codeColor } }));
        return;
      }
      if (!event.target.closest('[data-code-rename]')) return;
      const identifier = codeToRename;
      codeToRename = null;
      codeMenu.hidden = true;
      document.dispatchEvent(new CustomEvent('qualitative:rename-code-requested',
        { detail: { codeId: identifier } }));
    });
    document.addEventListener('qualitative:code-colored', () => {
      setColorBusy(false);
      codeToRename = null;
      codeMenu.hidden = true;
    });
    document.addEventListener('qualitative:color-code-error', (event) => {
      setColorBusy(false);
      noticeContext(event.detail.message);
    });
  }
  const refreshPageExcerpts = async (number) => {
    const node = nodes.get(number);
    if (!node || node.rendered !== generation) return;
    const response = await fetch(pageUrl(root.dataset.excerptsUrlTemplate, number),
      { credentials: 'same-origin' });
    if (!response.ok) return;
    node.excerpts = (await response.json()).excerpts;
    if (node.rendered === generation) {
      renderExcerptOverlay(node);
      scheduleMargin();
    }
  };
  const nearestCanonicalBoundary = (node, clientX, clientY) => {
    if (!node.layout?.layout_available) return null;
    const rect = node.surface.getBoundingClientRect();
    const x = (clientX - rect.left) * node.layout.width / rect.width;
    const y = (clientY - rect.top) * node.layout.height / rect.height;
    let best = null;
    for (const item of node.layout.items) {
      if (item.end - item.start !== 1) continue;
      const midY = (item.bbox[1] + item.bbox[3]) / 2;
      for (const [offset, edgeX] of [[item.start, item.bbox[0]], [item.end, item.bbox[2]]]) {
        const score = (edgeX - x) ** 2 + ((midY - y) * 1.6) ** 2;
        if (!best || score < best.score) best = { offset, score };
      }
    }
    return best?.offset ?? null;
  };
  let suppressExcerptClick = false;
  scroll.addEventListener('click', (event) => {
    if (suppressExcerptClick) { suppressExcerptClick = false; return; }
    if (event.target.closest('[data-handle]')) return;
    const surface = event.target.closest('.platform-qualitative-pdf-surface');
    if (!surface) {
      if (activeExcerptId) { activeExcerptId = null; for (const node of nodes.values()) {
        if (node.rendered === generation) renderExcerptOverlay(node);
      } scheduleMargin(); }
      return;
    }
    const shell = surface.closest('[data-page-number]');
    const number = Number(shell.dataset.pageNumber);
    const boxes = [...surface.querySelectorAll('.platform-qualitative-excerpt-box')];
    const hit = boxes.reverse().find((box) => {
      const rect = box.getBoundingClientRect();
      return event.clientX >= rect.left && event.clientX <= rect.right
        && event.clientY >= rect.top && event.clientY <= rect.bottom;
    });
    if (hit) activateExcerpt(number, hit.dataset.excerptId);
    else if (activeExcerptId) {
      activeExcerptId = null;
      for (const node of nodes.values()) if (node.rendered === generation) renderExcerptOverlay(node);
      scheduleMargin();
    }
  });
  scroll.addEventListener('pointerdown', (event) => {
    const handle = event.target.closest('[data-handle]');
    if (!handle || excerptSaving || (event.pointerType === 'mouse' && event.button !== 0)) return;
    const shell = handle.closest('[data-page-number]');
    const number = Number(shell?.dataset.pageNumber);
    const node = nodes.get(number);
    const excerpt = node?.excerpts.find((item) => item.id === handle.dataset.excerptId);
    if (!excerpt || excerpt.id !== activeExcerptId) return;
    excerptDrag = { pointerId: event.pointerId, number, kind: handle.dataset.handle,
      excerpt, start: excerpt.start, end: excerpt.end };
    scroll.setPointerCapture(event.pointerId);
    event.preventDefault();
    event.stopPropagation();
  });
  scroll.addEventListener('pointermove', (event) => {
    if (!excerptDrag || excerptDrag.pointerId !== event.pointerId) return;
    const { number, excerpt, kind } = excerptDrag;
    const node = nodes.get(number);
    const boundary = nearestCanonicalBoundary(node, event.clientX, event.clientY);
    if (boundary === null) return;
    if (kind === 'start' && boundary < excerpt.end) excerpt.start = boundary;
    else if (kind === 'end' && boundary > excerpt.start) excerpt.end = boundary;
    else return;
    renderExcerptOverlay(node);
    scheduleMargin();
    event.preventDefault();
  });
  const finishExcerptDrag = async (event, cancelled = false) => {
    if (!excerptDrag || excerptDrag.pointerId !== event.pointerId) return;
    const drag = excerptDrag;
    excerptDrag = null;
    suppressExcerptClick = true;
    setTimeout(() => { suppressExcerptClick = false; }, 120);
    if (scroll.hasPointerCapture(event.pointerId)) scroll.releasePointerCapture(event.pointerId);
    const node = nodes.get(drag.number);
    const restore = () => {
      drag.excerpt.start = drag.start;
      drag.excerpt.end = drag.end;
      renderExcerptOverlay(node);
      scheduleMargin();
    };
    if (cancelled || (drag.start === drag.excerpt.start && drag.end === drag.excerpt.end)) {
      restore();
      return;
    }
    excerptSaving = true;
    try {
      const url = root.dataset.editExcerptUrlTemplate.replace(
        '00000000-0000-0000-0000-000000000000', drag.excerpt.id);
      const response = await fetch(url, { method: 'PATCH', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json',
          'X-CSRFToken': root.querySelector('[data-context-csrf]').value, Accept: 'application/json' },
        body: JSON.stringify({ start: drag.excerpt.start, end: drag.excerpt.end,
          page_hash: drag.excerpt.page_hash }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Não foi possível ajustar o trecho.');
      node.excerpts = payload.page.excerpts;
      renderExcerptOverlay(node);
      scheduleMargin();
      noticeContext('Limites do trecho salvos automaticamente.');
    } catch (error) {
      restore();
      noticeContext(error.message);
    } finally { excerptSaving = false; }
  };
  scroll.addEventListener('pointerup', (event) => { finishExcerptDrag(event); });
  scroll.addEventListener('pointercancel', (event) => { finishExcerptDrag(event, true); });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !activeExcerptId || excerptSaving) return;
    activeExcerptId = null;
    for (const node of nodes.values()) if (node.rendered === generation) renderExcerptOverlay(node);
    scheduleMargin();
  });
  const scrollToActive = (node) => {
    const box = node.surface.querySelector('.platform-qualitative-search-box.is-active');
    if (!box) return;
    const top = box.getBoundingClientRect().top - scroll.getBoundingClientRect().top
      + scroll.scrollTop - scroll.clientHeight * .32;
    scroll.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
  };
  const renderSearchOverlays = (number) => {
    const node = nodes.get(number);
    if (!node || node.rendered !== generation) return;
    node.surface.querySelector('.platform-qualitative-search-overlay')?.remove();
    const matches = results.map((result, index) => ({ result, index }))
      .filter(({ result }) => result.page_number === number);
    if (!matches.length) return;
    const overlay = document.createElement('div');
    overlay.className = 'platform-qualitative-search-overlay';
    let activeGeometry = false;
    for (const { result, index } of matches) {
      if (node.layout?.page_text_hash !== result.page_text_hash) continue;
      for (const item of mergeGeometry(exactItems(node.layout, result.start_offset, result.end_offset))) {
        const box = placeBox(item, node.layout,
          `platform-qualitative-search-box${index === resultIndex ? ' is-active' : ''}`);
        box.dataset.searchIndex = String(index);
        overlay.append(box);
        if (index === resultIndex) activeGeometry = true;
      }
    }
    node.surface.append(overlay);
    if (results[resultIndex]?.page_number === number) {
      searchNotice(activeGeometry ? '' : 'Destaque visual indisponível nesta página; consulte o trecho textual.');
      if (activeGeometry) requestAnimationFrame(() => scrollToActive(node));
    }
  };
  const decoratePage = async (number, expectedGeneration) => {
    const node = nodes.get(number);
    try {
      if (!node.layout) {
        const response = await fetch(pageUrl(root.dataset.layoutUrlTemplate, number),
          { credentials: 'same-origin' });
        if (!response.ok) throw new Error('Layout indisponível');
        node.layout = await response.json();
      }
    } catch (error) {
      if (!node.layoutWarned) console.warn('Layout da página indisponível.', { page: number, error });
      node.layoutWarned = true;
    }
    if (expectedGeneration !== generation || node.rendered !== generation) return;
    const selectableCount = renderTextLayer(node, node.layout);
    if (!selectableCount && !node.shell.querySelector('[data-layout-unavailable]')) {
      const notice = document.createElement('p');
      notice.className = 'platform-muted platform-qualitative-layout-notice';
      notice.dataset.layoutUnavailable = '';
      notice.textContent = 'Camada selecionável indisponível nesta página; o texto salvo ainda pode ser pesquisado.';
      node.shell.append(notice);
      if (!node.layoutWarned) console.warn('Página renderizada sem camada textual selecionável.', { page: number });
      node.layoutWarned = true;
    } else if (selectableCount) {
      node.shell.querySelector('[data-layout-unavailable]')?.remove();
    }
    // Uma falha de carregamento das anotações não pode retirar a seleção.
    try {
      const response = await fetch(pageUrl(root.dataset.excerptsUrlTemplate, number),
        { credentials: 'same-origin' });
      if (!response.ok) throw new Error('Trechos indisponíveis');
      node.excerpts = (await response.json()).excerpts;
    } catch (error) {
      if (!node.excerptsWarned) console.warn('Anotações da página indisponíveis.', { page: number, error });
      node.excerptsWarned = true;
    }
    if (expectedGeneration !== generation || node.rendered !== generation) return;
    renderExcerptOverlay(node);
    renderSearchOverlays(number);
    scheduleMargin();
    if (number === firstPage && activeExcerptId
        && node.excerpts.some((item) => item.id === activeExcerptId)) {
      requestAnimationFrame(() => {
        activateExcerpt(number, activeExcerptId, true);
      });
    }
  };

  const showResult = (index) => {
    if (!results.length) return;
    resultIndex = (index + results.length) % results.length;
    const result = results[resultIndex];
    resultCount.textContent = `${resultIndex + 1} de ${results.length}`;
    resultSnippet.textContent = `Página ${result.page_number}: …${result.snippet}…`;
    const node = nodes.get(result.page_number);
    if (node?.rendered === generation) renderSearchOverlays(result.page_number);
    else goToPage(result.page_number, false);
    for (const [number, visible] of nodes) {
      if (number !== result.page_number && visible.rendered === generation) renderSearchOverlays(number);
    }
  };
  prev.addEventListener('click', () => showResult(resultIndex - 1));
  next.addEventListener('click', () => showResult(resultIndex + 1));
  const regexInput = searchForm.querySelector('[name="grep"]');
  const selectedAutomaticMode = () => searchForm.querySelector('[name="automatic_mode"]:checked')?.value || 'literal';
  const validateAutomaticTerms = () => window.TermInput.validate(searchInput, separatorError,
    Boolean(automaticToggle?.checked && multipleTerms?.checked && !regexInput.checked));
  const syncRegexMode = () => {
    const unavailable = Boolean(automaticToggle?.checked && selectedAutomaticMode() !== 'literal');
    if (unavailable) regexInput.checked = false;
    regexInput.disabled = unavailable;
    validateAutomaticTerms();
  };
  searchForm.addEventListener('input', validateAutomaticTerms);
  searchForm.querySelectorAll('[name="automatic_mode"]').forEach((mode) => mode.addEventListener('change', syncRegexMode));
  automaticToggle?.addEventListener('change', () => {
    automaticOptions.hidden = !automaticToggle.checked;
    automaticSettings.hidden = !automaticToggle.checked;
    automaticOptions.disabled = !automaticToggle.checked;
    contextualRejection.disabled = !automaticToggle.checked;
    multipleTerms.disabled = !automaticToggle.checked;
    if (!automaticToggle.checked) contextualRejection.checked = multipleTerms.checked = false;
    automaticToggle.setAttribute('aria-expanded', String(automaticToggle.checked));
    syncRegexMode();
  });
  searchForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (automaticBusy || searchSubmit.disabled) return;
    if (!validateAutomaticTerms()) { searchInput.focus(); return; }
    const form = new FormData(searchForm);
    if (automaticToggle?.checked) {
      searchRequest += 1; // uma resposta de busca normal anterior não sobrescreve a operação
      automaticSnapshot = { q: form.get('q'), grep: form.has('grep'), case_sensitive: form.has('case'),
        mode: selectedAutomaticMode(), scope: form.get('automatic_scope') || 'document',
        contextual_rejection_enabled: contextualRejection.checked, multiple_terms: multipleTerms.checked };
      await sendAutomatic();
      return;
    }
    const requestId = ++searchRequest;
    searchSubmit.disabled = true;
    const params = new URLSearchParams({ q: form.get('q'), grep: form.has('grep') ? '1' : '0',
      case: form.has('case') ? '1' : '0' });
    results = [];
    resultIndex = -1;
    resultCount.textContent = '0 de 0';
    resultSnippet.textContent = '';
    prev.disabled = next.disabled = true;
    for (const node of nodes.values()) node.surface.querySelector('.platform-qualitative-search-overlay')?.remove();
    searchNotice('Pesquisando…');
    try {
      const response = await fetch(`${root.dataset.searchUrl}?${params}`, { credentials: 'same-origin' });
      const payload = await response.json();
      if (requestId !== searchRequest) return;
      if (!response.ok) throw new Error(payload.error || 'Não foi possível concluir a busca.');
      results = payload.results;
      prev.disabled = next.disabled = results.length === 0;
      searchNotice(results.length ? '' : 'Nenhuma correspondência neste documento.');
      if (results.length) showResult(0);
    } catch (error) { if (requestId === searchRequest) searchNotice(error.message); }
    finally { searchSubmit.disabled = false; }
  });

  const selectionBoundary = (container, offset, isEnd) => {
    if (container.nodeType === Node.TEXT_NODE) {
      const item = container.parentElement?.closest('.platform-qualitative-text-item');
      return item ? { item, offset: Array.from(container.textContent.slice(0, offset)).length } : null;
    }
    // Chromium também entrega limites no próprio span (antes/depois do
    // glifo) ou entre filhos da camada, não necessariamente em um Text node.
    if (container.nodeType !== Node.ELEMENT_NODE) return null;
    if (container.matches('.platform-qualitative-text-item')) {
      return { item: container, offset: offset ? Array.from(container.textContent).length : 0 };
    }
    if (!container.matches('.platform-qualitative-text-layer')) return null;
    const before = isEnd && offset > 0 || offset === container.childNodes.length;
    const item = container.childNodes[before ? offset - 1 : offset];
    if (!item?.matches?.('.platform-qualitative-text-item')) return null;
    return { item, offset: before ? Array.from(item.textContent).length : 0 };
  };
  const selectionAnchor = () => {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
    const range = selection.getRangeAt(0);
    const from = selectionBoundary(range.startContainer, range.startOffset, false);
    const to = selectionBoundary(range.endContainer, range.endOffset, true);
    const first = from?.item;
    const last = to?.item;
    if (!first || !last || !scroll.contains(first) || !scroll.contains(last)) return null;
    const firstPage = first.closest('.platform-qualitative-pdf-page');
    if (!firstPage || firstPage !== last.closest('.platform-qualitative-pdf-page')) {
      noticeContext('Nesta versão, o trecho selecionado deve estar contido em uma única página.');
      return null;
    }
    const number = Number(firstPage.dataset.pageNumber);
    const layout = nodes.get(number)?.layout;
    if (!layout?.layout_available) return null;
    // DOM Range mede UTF-16; Array.from conta code points, como Python.
    const start = Number(first.dataset.start) + from.offset;
    const end = Number(last.dataset.start) + to.offset;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start >= end
        || end - start > 10000 || !selection.toString().trim()) return null;
    return { number, start, end, page_hash: layout.page_text_hash, range };
  };
  const contextError = contextDialog?.querySelector('[data-context-error]');
  const memoText = contextDialog?.querySelector('[name="memo"]');
  const contextFilter = contextDialog?.querySelector('[data-context-filter]');
  const createInline = contextDialog?.querySelector('[data-context-create]');
  const renderCodeChoices = () => {
    const list = contextDialog.querySelector('[data-context-code-list]');
    const query = contextFilter.value.trim().toLocaleLowerCase();
    const node = nodes.get(contextSnapshot?.page_number);
    const existing = node?.excerpts.find((item) => item.start === contextSnapshot.start
      && item.end === contextSnapshot.end && item.page_hash === contextSnapshot.page_hash);
    list.replaceChildren();
    for (const code of records.codes) {
      if (!code.name.toLocaleLowerCase().includes(query)) continue;
      const label = document.createElement('label');
      label.title = code.name;
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.name = 'context_code';
      input.value = code.id;
      input.checked = selectedCodeIds.has(code.id);
      if (existing?.code_ids.includes(code.id)) {
        input.disabled = true;
        label.title = `${code.name} — já aplicado ao trecho`;
      }
      label.append(input, document.createTextNode(code.name));
      list.append(label);
    }
    if (!list.childElementCount) {
      const empty = document.createElement('p');
      empty.className = 'platform-muted';
      empty.textContent = records.codes.length ? 'Nenhum código corresponde ao filtro.'
        : 'Nenhum código criado. Digite um nome para criar e aplicar.';
      list.append(empty);
    }
    const typed = contextFilter.value.trim().replace(/\s+/g, ' ');
    const duplicate = records.codes.some((code) => code.name.normalize('NFKC').toLocaleLowerCase()
      === typed.normalize('NFKC').toLocaleLowerCase());
    createInline.hidden = !typed || typed.length > 160 || duplicate;
    if (!createInline.hidden) createInline.textContent = `Criar e aplicar “${typed}”`;
  };
  const openContextDialog = (action, suggestion = '') => {
    if (!contextDialog || !contextSnapshot) return;
    contextAction = action;
    closeContextMenu();
    contextError.hidden = true;
    contextError.textContent = '';
    contextDialog.querySelector('[data-context-title]').textContent = {
      apply_codes: 'Aplicar código', create_memo: 'Criar memo',
    }[action];
    contextDialog.querySelector('[data-context-quote]').textContent = `Trecho: “${contextSnapshot.selected_text.slice(0, 240)}${contextSnapshot.selected_text.length > 240 ? '…' : ''}”`;
    contextDialog.querySelector('[data-context-save]').textContent = 'Salvar';
    contextDialog.querySelector('[data-context-codes]').hidden = action !== 'apply_codes';
    contextDialog.querySelector('[data-context-memo-field]').hidden = action !== 'create_memo';
    memoText.required = action === 'create_memo';
    memoText.value = '';
    contextFilter.value = suggestion;
    if (action === 'apply_codes') {
      const node = nodes.get(contextSnapshot.page_number);
      const existing = node.excerpts.find((item) => item.start === contextSnapshot.start
        && item.end === contextSnapshot.end && item.page_hash === contextSnapshot.page_hash);
      selectedCodeIds = new Set(existing?.code_ids || []);
      renderCodeChoices();
    }
    contextDialog.showModal();
    if (action === 'create_memo') memoText.focus();
    else contextFilter.focus();
    if (suggestion.length > 160) {
      contextError.textContent = 'O trecho excede 160 caracteres. Reduza a sugestão para criar um código.';
      contextError.hidden = false;
    }
  };
  const sendContext = async (action, additional = {}) => {
    const snapshot = contextSnapshot;
    if (!snapshot) throw new Error('Selecione novamente o trecho.');
    const response = await fetch(pageUrl(root.dataset.contextUrlTemplate, snapshot.page_number), {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': root.querySelector('[data-context-csrf]').value,
        Accept: 'application/json' },
      body: JSON.stringify({ ...snapshot, action, ...additional }),
    });
    const payload = await response.json();
    if (!response.ok) {
      const error = new Error(payload.error || 'Não foi possível salvar o trecho.');
      error.suggestedName = payload.suggested_name;
      throw error;
    }
    const node = nodes.get(snapshot.page_number);
    node.excerpts = payload.page.excerpts;
    if (node.rendered === generation) renderExcerptOverlay(node);
    scheduleMargin();
    document.dispatchEvent(new CustomEvent('qualitative:records-updated', { detail: payload.records }));
    noticeContext('Trecho salvo no projeto.');
    window.getSelection()?.removeAllRanges();
    return payload;
  };
  const sendAutomatic = async () => {
    if (automaticBusy) throw new Error('Aguarde a codificação em andamento.');
    automaticBusy = true;
    searchSubmit.disabled = true;
    searchNotice('Buscando e codificando…');
    const progress = automaticSnapshot.mode === 'semantic' ? startSemanticProgress(root) : null;
    let succeeded = false;
    try {
      const response = await fetch(root.dataset.automaticUrl, { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': root.querySelector('[data-context-csrf]').value,
          Accept: 'application/json' }, body: JSON.stringify({ ...automaticSnapshot,
            ...(progress ? { progress_id: progress.id } : {}) }) });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || 'Não foi possível concluir a codificação. Nenhuma alteração foi confirmada.');
      // Navegação continua restrita ao PDF aberto; os demais documentos recebem os mesmos registros persistentes.
      results = payload.search.results.filter(item => item.document_id === root.dataset.documentId);
      resultIndex = -1;
      resultCount.textContent = '0 de 0';
      resultSnippet.textContent = '';
      prev.disabled = next.disabled = !results.length;
      for (const node of nodes.values()) node.surface.querySelector('.platform-qualitative-search-overlay')?.remove();
      if (results.length) showResult(0);
      document.dispatchEvent(new CustomEvent('qualitative:records-updated', { detail: payload.records }));
      if (!results.length) searchNotice('');
      succeeded = true;
      return payload;
    } catch (error) { searchNotice(error.message); }
    finally {
      if (progress) await progress.stop(succeeded);
      automaticBusy = false;
      searchSubmit.disabled = false;
    }
  };
  if (contextMenu && contextDialog) {
    const openContextAt = async (anchor, point) => {
      const requestId = ++contextRequest;
      const { number, start, end, page_hash } = anchor;
      try {
        const response = await fetch(pageUrl(root.dataset.pageDataUrlTemplate, number),
          { credentials: 'same-origin' });
        if (!response.ok) throw new Error('Texto preparado indisponível.');
        const page = await response.json();
        if (requestId !== contextRequest) return;
        if (page.sha256 !== page_hash || end > page.char_count) {
          throw new Error('O texto da página mudou. Reabra o documento.');
        }
        const selected_text = Array.from(page.text).slice(start, end).join('');
        if (!selected_text.trim()) throw new Error('Selecione texto, não apenas espaços.');
        contextSnapshot = { analysis_id: root.dataset.analysisId,
          document_id: root.dataset.documentId, page_number: number,
          start, end, selected_text, page_hash };
        contextMenu.hidden = false;
        document.dispatchEvent(new CustomEvent('platform:popover-open', { detail: { panel: contextMenu } }));
        contextMenu.style.left = `${Math.max(8, Math.min(point.x, innerWidth - contextMenu.offsetWidth - 8))}px`;
        contextMenu.style.top = `${Math.max(8, Math.min(point.y, innerHeight - contextMenu.offsetHeight - 8))}px`;
        contextMenu.querySelector('button').focus();
        noticeContext('');
      } catch (error) { noticeContext(error.message); }
    };
    scroll.addEventListener('contextmenu', (event) => {
      if (!event.target.closest('.platform-qualitative-text-item')) return;
      const anchor = selectionAnchor();
      if (!anchor || !anchor.range.getClientRects().length) return;
      const onSelection = [...anchor.range.getClientRects()].some((rect) =>
        event.clientX >= rect.left - 2 && event.clientX <= rect.right + 2
        && event.clientY >= rect.top - 2 && event.clientY <= rect.bottom + 2);
      if (!onSelection) return;
      event.preventDefault();
      openContextAt(anchor, { x: event.clientX, y: event.clientY });
    });
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) return;
      const anchor = selectionAnchor();
      if (!anchor) return;
      event.preventDefault();
      const rect = anchor.range.getClientRects()[0];
      openContextAt(anchor, { x: rect.left, y: rect.bottom });
    });
    contextMenu.addEventListener('click', async (event) => {
      const button = event.target.closest('[data-context-action]');
      if (!button) return;
      const action = button.dataset.contextAction;
      if (action !== 'in_vivo') return openContextDialog(action);
      closeContextMenu();
      try { await sendContext(action); }
      catch (error) {
        if (error.suggestedName) openContextDialog('apply_codes', error.suggestedName);
        else noticeContext(error.message);
      }
    });
    contextMenu.addEventListener('keydown', (event) => {
      const buttons = [...contextMenu.querySelectorAll('button')];
      const index = buttons.indexOf(document.activeElement);
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        buttons[(index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length].focus();
      }
    });
    document.addEventListener('pointerdown', (event) => {
      if (!contextMenu.hidden && !contextMenu.contains(event.target)) closeContextMenu();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeContextMenu();
    });
    contextFilter.addEventListener('input', renderCodeChoices);
    createInline.addEventListener('click', async () => {
      const name = contextFilter.value.trim().replace(/\s+/g, ' ');
      if (!name || name.length > 160) return;
      createInline.disabled = true;
      contextError.hidden = true;
      try {
        await sendContext('create_and_apply', { name, description: '' });
        contextDialog.close();
      } catch (error) {
        contextError.textContent = error.message;
        contextError.hidden = false;
      } finally { createInline.disabled = false; }
    });
    contextDialog.querySelector('[data-context-code-list]').addEventListener('change', (event) => {
      if (!['checkbox', 'radio'].includes(event.target.type)) return;
      if (event.target.checked) selectedCodeIds.add(event.target.value);
      else selectedCodeIds.delete(event.target.value);
    });
    contextDialog.querySelector('[data-context-cancel]').addEventListener('click', () => contextDialog.close());
    contextDialog.querySelector('[data-context-form]').addEventListener('submit', async (event) => {
      event.preventDefault();
      const save = contextDialog.querySelector('[data-context-save]');
      save.disabled = true;
      contextError.hidden = true;
      try {
        const additional = contextAction === 'apply_codes'
          ? { code_ids: [...selectedCodeIds] } : { text: memoText.value };
        await sendContext(contextAction, additional);
        contextDialog.close();
      } catch (error) {
        contextError.textContent = error.message;
        contextError.hidden = false;
      } finally { save.disabled = false; }
    });
  }

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
    scheduleMargin();
    enqueue(activePage);
    enqueue(activePage + 1);
    enqueue(activePage - 1);
  };
  zoom.addEventListener('change', () => {
    clearTimeout(wheelZoomTimer);
    rerenderVisiblePages();
  });
  scroll.addEventListener('wheel', (event) => {
    if (!event.ctrlKey || !documentPdf || !event.deltaY) return;
    event.preventDefault();
    const direction = event.deltaY < 0 ? 1 : -1;
    const index = Math.max(0, Math.min(zoom.options.length - 1, zoom.selectedIndex + direction));
    if (index === zoom.selectedIndex) return;
    zoom.selectedIndex = index;
    clearTimeout(wheelZoomTimer);
    wheelZoomTimer = setTimeout(rerenderVisiblePages, 90);
  }, { passive: false });

  const focusActive = () => document.body.classList.contains('platform-qualitative-focus');
  const movePanelTo = (left, top) => {
    const limitX = Math.max(8, window.innerWidth - focusPanel.offsetWidth - 8);
    const limitY = Math.max(8, window.innerHeight - focusPanel.offsetHeight - 8);
    focusPanel.style.left = `${Math.min(limitX, Math.max(8, left))}px`;
    focusPanel.style.top = `${Math.min(limitY, Math.max(8, top))}px`;
    repositionExplorerPopovers();
    scheduleConnectorLines();
  };
  const updateFocusPanel = (minimized) => {
    if (minimized) closeExplorerPopovers();
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
  const arrangeResultNavigation = (focused) => {
    // Reutiliza botões e contador com estado/listeners intactos, inclusive recolhido.
    if (focused) focusNavigation.append(prev, resultCount, next);
    else {
      for (const control of [prev, resultCount, next]) searchResults.insertBefore(control, searchMessage);
    }
  };
  const arrangeAutomaticControls = (focused) => {
    if (!automaticSettings) return;
    // Move os mesmos inputs: preserva valores/listeners e a ordem de Tab de cada layout.
    if (focused) {
      automaticModes.append(multipleControl);
      automaticOptions.insertBefore(automaticSettings, automaticScope);
      automaticSettings.append(automaticScope);
    } else {
      automaticSettings.insertBefore(multipleControl, automaticSettings.firstElementChild);
      automaticOptions.append(automaticScope);
      searchControlRow.append(automaticSettings);
    }
  };
  focusToggle.addEventListener('click', () => {
    closeExplorerPopovers();
    closeContextMenu();
    contextRequest += 1;
    const oldWidth = scroll.clientWidth;
    const focused = !focusActive();
    document.body.classList.toggle('platform-qualitative-focus', focused);
    arrangeResultNavigation(focused);
    arrangeAutomaticControls(focused);
    document.body.classList.remove('platform-menu-open');
    focusToggle.setAttribute('aria-pressed', String(focused));
    const action = focused ? 'Sair do modo foco' : 'Modo foco';
    focusToggle.setAttribute('aria-label', action);
    focusToggle.title = action;
    focusLabel.textContent = action;
    if (focused) updateFocusPanel(true);
    requestAnimationFrame(() => {
      if (focused && !window.matchMedia('(max-width: 700px)').matches) {
        movePanelTo(8, 8);
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
    repositionExplorerPopovers();
    requestAnimationFrame(() => {
      if (focusActive() && !window.matchMedia('(max-width: 700px)').matches) {
        movePanelTo(focusPanel.offsetLeft, focusPanel.offsetTop);
      }
      if (documentPdf && Math.abs(scroll.clientWidth - viewerWidth) > 2) rerenderVisiblePages();
      else scheduleMargin();
    });
  });
  window.addEventListener('scroll', scheduleConnectorLines, { capture: true, passive: true });
  if (connectorLayer && 'ResizeObserver' in window) {
    const connectorResizeObserver = new ResizeObserver(scheduleConnectorLines);
    connectorResizeObserver.observe(root.querySelector('.platform-qualitative-bottom'));
    connectorResizeObserver.observe(scroll);
    connectorResizeObserver.observe(margin);
  }
  const explorer = root.querySelector('#qualitative-explorer');
  const explorerToggle = root.querySelector('[data-explorer-toggle]');
  explorerToggle.addEventListener('click', () => {
    const open = !explorer.classList.contains('is-open');
    explorer.classList.toggle('is-open', open);
    explorerToggle.setAttribute('aria-expanded', String(open));
  });
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
    settleInitialPage = createInitialPageSettler(firstPage, goToPage);
    goToPage(firstPage, false);
  } catch (error) {
    console.error('Não foi possível abrir o PDF do projeto.', error);
    status.textContent = 'Não foi possível abrir o PDF original. Confira o documento do projeto.';
  }
}
