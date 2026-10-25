/* ================================================================
 *  Citation graph (notebook, library paper, research scope)
 *
 *  Two layouts, both structured by time:
 *  - one paper: its references on the left (the past), the paper in the
 *    middle, the works citing it on the right (the future);
 *  - several papers: three horizontal bands (references / corpus / citing
 *    works) on a shared year axis.
 *  In both, a light spring simulation lets nodes be pulled and fall back to
 *  their place: a node's year and side never change.
 *  Labels, year ticks and the tooltip are HTML laid over the SVG.
 * ================================================================ */
const _NB_CG_NOW = new Date().getFullYear();

const _nbCg = {
  data: null,
  model: null,
  layout: null,
  selectedNodeId: '',
  hoveredId: '',
  filters: { refs: true, cits: true, internal: true },
  query: '',
  showAll: { refs: false, cits: false },
  expandedIds: new Set(),
  expansions: new Map(),
  expandingIds: new Set(),
  addedIds: new Set(),
  addingIds: new Set(),
  stage: { w: 0, h: 0 },
  pos: new Map(),
  drag: null,
  suppressClick: false,
  layoutSig: '',
  rafId: null,
  loopFrames: 0,
  relayoutRaf: null,
  resizeObserver: null,
  dom: { nodes: new Map(), links: [], arcs: [] },
  uiBound: false,
  requestUrl: '',
  requestOptions: {},
  abortController: null,
  returnFocusEl: null,
  embedded: false,
  suspended: false,
  scope: { kind: 'notebook', title: '' },
  notebookId: '',
};

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    _nbCgStopLoop();
    return;
  }
  if (_nbCgIsOpen() && _nbCg.layout) _nbCgStartLoop();
});

function _nbCgIsOpen() {
  return !!document.getElementById('nb-citation-graph-modal')?.classList.contains('open');
}

function _nbCgEscapeAttr(value) {
  return String(value || '')
    .replaceAll('&', '&amp;')
    .replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

function _nbCgEscapeHtml(value) {
  return String(value || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');
}

function _nbCgFormatAuthors(authors) {
  if (!Array.isArray(authors) || !authors.length) return '';
  if (authors.length === 1) return authors[0];
  if (authors.length === 2) return `${authors[0]} & ${authors[1]}`;
  return `${authors[0]} et al.`;
}

function _nbCgSurname(name) {
  const full = String(name || '').trim();
  if (!full) return '';
  if (full.includes(',')) return full.split(',')[0].trim();
  const parts = full.split(/\s+/).filter(Boolean);
  // "Zhou D", "Li X-Y": the surname comes first, initials after.
  const names = parts.filter(part => !/^(?:[A-Z]\.?-?){1,3}$/.test(part));
  return names[names.length - 1] || parts[parts.length - 1] || full;
}

function _nbCgAuthorLastName(node) {
  return _nbCgSurname((node?.authors || [])[0]) || '?';
}

function _nbCgYear(node) {
  const year = Number(node?.year);
  return Number.isFinite(year) && year > 0 ? Math.round(year) : 0;
}

function _nbCgClamp(value, lo, hi) {
  return Math.max(lo, Math.min(hi, value));
}

function _nbCgNumber(value) {
  const locale = String(document.documentElement.lang || 'fr').startsWith('en') ? 'en-US' : 'fr-FR';
  return Number(value || 0).toLocaleString(locale);
}

// "Tomita 2003", or "Tomita ’03" when space is short. Without an author,
// the first words of the title stand in.
function _nbCgNodeLabel(node, compact) {
  const author = _nbCgSurname((node?.authors || [])[0]) || _nbCgTitleLead(node);
  const year = _nbCgYear(node);
  if (!year) return `${author} ${_ti('citationGraph.noDate')}`;
  return `${author} ${compact ? `’${String(year).slice(2)}` : year}`;
}

function _nbCgTitleLead(node) {
  const title = String(node?.title || '').trim();
  if (!title) return '?';
  return title.length > 14 ? `${title.slice(0, 13).trimEnd()}…` : title;
}

// "Li et al. 2016"
function _nbCgShortRef(node) {
  const author = _nbCgAuthorLastName(node);
  const lead = (node?.authors || []).length > 1 ? `${author} et al.` : author;
  return [lead, _nbCgYear(node) || ''].filter(Boolean).join(' ');
}

// "Li, Fujishiro, Naito, Ainslie"
function _nbCgSurnames(node, max = 4) {
  const authors = (node?.authors || []).map(_nbCgSurname).filter(Boolean);
  const shown = authors.slice(0, max).join(', ');
  return authors.length > max ? `${shown} et al.` : shown;
}

function _nbCgMotionEnabled() {
  if (typeof isLightPerformanceMode === 'function' && isLightPerformanceMode()) return false;
  return !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

const _researchScope = {
  bound: false,
  results: [],
  selected: null,
  abortController: null,
  returnFocusEl: null,
};

function _researchScopeSetStatus(message, kind = '') {
  const status = document.getElementById('research-scope-status');
  if (!status) return;
  status.textContent = String(message || '');
  status.dataset.kind = kind;
}

function _researchScopeRenderResults() {
  const container = document.getElementById('research-scope-results');
  if (!container) return;
  if (!_researchScope.results.length) {
    container.innerHTML = `<div class="research-scope-empty">${_nbCgEscapeHtml(_ti('researchScope.emptyInitial'))}</div>`;
    return;
  }
  container.innerHTML = _researchScope.results.map((entity) => {
    const entityId = String(entity?.id || '');
    const selected = entityId && entityId === String(_researchScope.selected?.id || '');
    const details = [entity?.subtitle, entity?.external_id].filter(Boolean).join(' · ');
    const stats = _ti('researchScope.resultStats', {
      works: Number(entity?.works_count || 0).toLocaleString(),
      citations: Number(entity?.cited_by_count || 0).toLocaleString(),
    });
    return `<button type="button" class="research-scope-result${selected ? ' is-selected' : ''}" role="option" aria-selected="${selected ? 'true' : 'false'}" data-entity-id="${_nbCgEscapeAttr(entityId)}">
      <span class="research-scope-result-main">
        <span class="research-scope-result-title">${_nbCgEscapeHtml(entity?.label || entityId)}</span>
        <span class="research-scope-result-subtitle">${_nbCgEscapeHtml(details || entityId)}</span>
      </span>
      <span class="research-scope-result-stats">${_nbCgEscapeHtml(stats)}</span>
    </button>`;
  }).join('');
}

function _researchScopeSelectEntity(entityId) {
  _researchScope.selected = _researchScope.results.find(
    entity => String(entity?.id || '') === String(entityId || ''),
  ) || null;
  const build = document.getElementById('research-scope-build');
  if (build) build.disabled = !_researchScope.selected;
  _researchScopeRenderResults();
  if (_researchScope.selected) {
    _researchScopeSetStatus(
      _ti('researchScope.selected', { label: _researchScope.selected.label || _researchScope.selected.id }),
      'success',
    );
  }
}

async function _researchScopeSearch() {
  const query = String(document.getElementById('research-scope-query')?.value || '').trim();
  const entityType = String(document.getElementById('research-scope-type')?.value || 'author');
  if (query.length < 2) {
    _researchScopeSetStatus(_ti('researchScope.queryTooShort'), 'error');
    document.getElementById('research-scope-query')?.focus();
    return;
  }
  _researchScope.abortController?.abort();
  const abortController = new AbortController();
  _researchScope.abortController = abortController;
  _researchScope.selected = null;
  _researchScope.results = [];
  const searchButton = document.getElementById('research-scope-search-btn');
  const buildButton = document.getElementById('research-scope-build');
  if (searchButton) searchButton.disabled = true;
  if (buildButton) buildButton.disabled = true;
  _researchScopeSetStatus(_ti('researchScope.searching'));
  _researchScopeRenderResults();
  try {
    const response = await fetch('/api/citation-graph/entities', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_type: entityType, query, limit: 8 }),
      signal: abortController.signal,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload?.ok === false) {
      throw new Error(payload?.msg || payload?.message || `${_ti('researchScope.serverError')} ${response.status}`);
    }
    _researchScope.results = Array.isArray(payload?.entities) ? payload.entities : [];
    _researchScopeRenderResults();
    _researchScopeSetStatus(
      _researchScope.results.length
        ? _ti('researchScope.resultsFound', { count: _researchScope.results.length })
        : _ti('researchScope.noResults'),
      _researchScope.results.length ? '' : 'warning',
    );
  } catch (error) {
    if (error?.name !== 'AbortError') {
      _researchScopeSetStatus(error?.message || _ti('researchScope.searchFailed'), 'error');
    }
  } finally {
    if (_researchScope.abortController === abortController) {
      _researchScope.abortController = null;
      if (searchButton) searchButton.disabled = false;
    }
  }
}

function _researchScopeYearValue(id) {
  const raw = String(document.getElementById(id)?.value || '').trim();
  return raw ? Number(raw) : null;
}

async function _researchScopeBuildGraph() {
  const entity = _researchScope.selected;
  if (!entity) return;
  const fromYear = _researchScopeYearValue('research-scope-from-year');
  const toYear = _researchScopeYearValue('research-scope-to-year');
  if (fromYear && toYear && fromYear > toYear) {
    _researchScopeSetStatus(_ti('researchScope.invalidYears'), 'error');
    document.getElementById('research-scope-from-year')?.focus();
    return;
  }
  const body = {
    entity_type: String(document.getElementById('research-scope-type')?.value || 'author'),
    openalex_id: String(entity.id || ''),
    from_year: fromYear,
    to_year: toYear,
    max_works: Number(document.getElementById('research-scope-max-works')?.value || 80),
    strategy: String(document.getElementById('research-scope-strategy')?.value || 'balanced'),
  };
  await _openCitationGraphFrom('/api/citation-graph/scope', {
    emptyMsg: _ti('researchScope.noWorks'),
    scope: {
      kind: body.entity_type === 'author' ? 'author' : 'institution',
      title: String(entity.label || entity.id),
    },
    returnFocusEl: document.getElementById('research-scope-build'),
    embedded: true,
    fetchOptions: {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  });
}

async function _researchScopeClearCache() {
  const button = document.getElementById('research-scope-clear-cache');
  if (button) button.disabled = true;
  try {
    const response = await fetch('/api/citation-graph/search-cache', { method: 'DELETE' });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload?.ok === false) throw new Error();
    _researchScopeSetStatus(_ti('researchScope.cacheCleared'), 'success');
  } catch (_error) {
    _researchScopeSetStatus(_ti('researchScope.cacheClearFailed'), 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

function _researchScopeBindUiOnce() {
  if (_researchScope.bound) return;
  _researchScope.bound = true;
  document.getElementById('research-scope-form')?.addEventListener('submit', (event) => {
    event.preventDefault();
    _researchScopeSearch();
  });
  document.getElementById('research-scope-results')?.addEventListener('click', (event) => {
    const button = event.target?.closest?.('[data-entity-id]');
    if (button) _researchScopeSelectEntity(button.dataset.entityId);
  });
  document.getElementById('research-scope-build')?.addEventListener('click', _researchScopeBuildGraph);
  document.getElementById('research-scope-clear-cache')?.addEventListener('click', _researchScopeClearCache);
  document.getElementById('research-scope-type')?.addEventListener('change', () => {
    _researchScope.results = [];
    _researchScope.selected = null;
    const build = document.getElementById('research-scope-build');
    if (build) build.disabled = true;
    _researchScopeRenderResults();
    _researchScopeSetStatus('');
  });
}

async function openNbCitationGraph() {
  if (!_nb.id) {
    showToast(_ti('citationGraph.openNotebookFirst'), 2400);
    return;
  }
  await _openCitationGraphFrom(`/api/notebooks/${_nb.id}/citation-graph`, {
    emptyMsg: _ti('citationGraph.emptyNotebook'),
    scope: { kind: 'notebook', title: String(_nb.data?.title || '') },
    notebookId: _nb.id,
  });
}

async function openLibraryCitationGraph() {
  const relPath = _currentSelectedArticlePath();
  if (!relPath) {
    showToast(_ti('citationGraph.selectPaperFirst'), 2400);
    return;
  }
  await _openCitationGraphFrom(
    `/api/library/citation-graph?path=${encodeURIComponent(relPath)}`,
    {
      emptyMsg: _ti('citationGraph.emptyArticle'),
      scope: { kind: 'article', title: String(state.libRows[state.selectedRow]?.[0] || relPath) },
    },
  );
}

// In the one-paper view the paper itself stands for "nothing selected".
function _nbCgHasUserSelection() {
  if (!_nbCg.selectedNodeId) return false;
  return !(_nbCg.layout?.mode === 'single' && _nbCg.selectedNodeId === _nbCg.layout.source.id);
}

function _nbCgEscapeKeyHandler(event) {
  const modal = document.getElementById('nb-citation-graph-modal');
  if (!modal?.classList.contains('open')) return;
  // A notebook picker opened from the graph sits on top: let it handle keys.
  if (document.querySelector('dialog[open]')) return;
  if (_nbCg.embedded && !modal.contains(document.activeElement)) return;
  if (event.key === 'Escape') {
    if (event.target?.id === 'nb-cg-search' && event.target.value) return;
    event.preventDefault();
    if (_nbCgHasUserSelection()) {
      _nbCgSelectNode('');
      return;
    }
    closeNbCitationGraph();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = Array.from(modal.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
  )).filter(el => el.getClientRects().length > 0);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function _nbCgMount(embedded = false) {
  const modal = document.getElementById('nb-citation-graph-modal');
  const overlay = document.getElementById('nb-citation-graph-overlay');
  const workspace = document.getElementById('cartography-citation-workspace');
  if (!modal) return;
  _nbCg.embedded = Boolean(embedded && workspace);
  modal.classList.toggle('is-embedded', _nbCg.embedded);
  if (_nbCg.embedded) {
    workspace.hidden = false;
    document.getElementById('research-scope-builder')?.setAttribute('hidden', '');
    workspace.appendChild(modal);
    modal.setAttribute('role', 'region');
    modal.removeAttribute('aria-modal');
    overlay?.classList.remove('open');
  } else {
    overlay?.after(modal);
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
  }
  const closeButton = document.getElementById('nb-cg-close');
  if (closeButton) {
    closeButton.textContent = _nbCg.embedded ? `← ${_ti('researchScope.changeIdentity')}` : '×';
    closeButton.setAttribute('aria-label', _nbCg.embedded ? _ti('researchScope.changeIdentity') : _ti('researchScope.close'));
  }
}

function _nbCgResetState() {
  _nbCgStopLoop();
  _nbCg.data = null;
  _nbCg.model = null;
  _nbCg.layout = null;
  _nbCg.selectedNodeId = '';
  _nbCg.hoveredId = '';
  _nbCg.filters = { refs: true, cits: true, internal: true };
  _nbCg.query = '';
  _nbCg.showAll = { refs: false, cits: false };
  _nbCg.expandedIds.clear();
  _nbCg.expansions.clear();
  _nbCg.expandingIds.clear();
  _nbCg.addedIds.clear();
  _nbCg.addingIds.clear();
  _nbCg.pos.clear();
  _nbCg.drag = null;
  _nbCg.layoutSig = '';
  _nbCg.dom = { nodes: new Map(), links: [], arcs: [] };
}

async function _openCitationGraphFrom(url, { emptyMsg, scope = null, notebookId = '', fetchOptions = {}, returnFocusEl = null, embedded = false } = {}) {
  _nbCg.abortController?.abort();
  const abortController = new AbortController();
  _nbCg.abortController = abortController;
  _nbCg.requestUrl = url;
  _nbCg.requestOptions = { emptyMsg, scope, notebookId, fetchOptions, returnFocusEl, embedded };
  _nbCg.returnFocusEl = returnFocusEl || document.activeElement;
  _nbCgHideResume();
  _nbCgResetState();
  _nbCg.scope = scope || { kind: 'notebook', title: '' };
  _nbCg.notebookId = String(notebookId || '');
  const search = document.getElementById('nb-cg-search');
  if (search) search.value = '';
  _nbCgMount(embedded);
  if (!_nbCg.embedded) document.getElementById('nb-citation-graph-overlay')?.classList.add('open');
  const modal = document.getElementById('nb-citation-graph-modal');
  modal?.classList.add('open');
  modal?.removeAttribute('hidden');
  document.body.classList.toggle('citation-graph-open', !_nbCg.embedded);
  _nbCgBindUiOnce();
  document.addEventListener('keydown', _nbCgEscapeKeyHandler);
  _nbCgObserveStage();
  _nbCgShowLoading(true);
  _nbCgClearStage();
  _nbCgRenderHeader();
  document.getElementById('nb-cg-close')?.focus();
  try {
    const r = await fetch(url, { ...fetchOptions, signal: abortController.signal });
    if (!r.ok) {
      const errorPayload = await r.json().catch(() => ({}));
      throw new Error(errorPayload?.msg || errorPayload?.message || _ti('citationGraph.serverError', { status: r.status }));
    }
    const d = await r.json();
    _nbCg.data = d;
    if (!d || d.ok === false) {
      _nbCgShowEmpty(d?.msg || _ti('citationGraph.computeError'), d?.skipped_sources);
      return;
    }
    const nodes = Array.isArray(d.nodes) ? d.nodes : [];
    if (!nodes.length) {
      _nbCgShowEmpty(d.msg || emptyMsg || _ti('citationGraph.noIdentifiable'), d.skipped_sources);
      return;
    }
    _nbCgRebuild();
  } catch (e) {
    if (e?.name !== 'AbortError') {
      _nbCgShowEmpty(`${_ti('citationGraph.loadFailed')} ${e?.message || _ti('citationGraph.unknownError')}`);
    }
  } finally {
    if (_nbCg.abortController === abortController) {
      _nbCg.abortController = null;
      _nbCgShowLoading(false);
    }
  }
}

function _nbCgRetry() {
  if (_nbCg.requestUrl) _openCitationGraphFrom(_nbCg.requestUrl, _nbCg.requestOptions);
}

function closeNbCitationGraph() {
  const wasEmbedded = _nbCg.embedded;
  document.removeEventListener('keydown', _nbCgEscapeKeyHandler);
  _nbCg.abortController?.abort();
  _nbCg.abortController = null;
  document.getElementById('nb-citation-graph-overlay')?.classList.remove('open');
  const modal = document.getElementById('nb-citation-graph-modal');
  if (modal) {
    modal.classList.remove('open');
    modal.setAttribute('hidden', '');
  }
  document.body.classList.remove('citation-graph-open');
  _nbCg.resizeObserver?.disconnect();
  _nbCg.stage = { w: 0, h: 0 };
  _nbCgHideResume();
  _nbCgResetState();
  _nbCgClearStage();
  const returnFocus = _nbCg.returnFocusEl;
  _nbCg.returnFocusEl = null;
  if (wasEmbedded) {
    document.getElementById('cartography-citation-workspace')?.setAttribute('hidden', '');
    document.getElementById('research-scope-builder')?.removeAttribute('hidden');
    _nbCg.embedded = false;
    window.setTimeout(() => document.getElementById('research-scope-query')?.focus(), 0);
    return;
  }
  if (returnFocus && typeof returnFocus.focus === 'function') returnFocus.focus();
}

// Leaving the graph for another screen (Discovery, the library) keeps its
// state, explorations included; a floating button brings it back.
function _nbCgSuspend() {
  if (_nbCg.embedded) return;
  _nbCg.suspended = true;
  _nbCgStopLoop();
  _nbCg.drag = null;
  _nbCg.hoveredId = '';
  _nbCgUpdateTooltip();
  document.removeEventListener('keydown', _nbCgEscapeKeyHandler);
  document.getElementById('nb-citation-graph-overlay')?.classList.remove('open');
  const modal = document.getElementById('nb-citation-graph-modal');
  if (modal) {
    modal.classList.remove('open');
    modal.setAttribute('hidden', '');
  }
  document.body.classList.remove('citation-graph-open');
  _nbCg.resizeObserver?.disconnect();
  _nbCg.stage = { w: 0, h: 0 };
  const resume = document.getElementById('nb-cg-resume');
  if (resume) resume.hidden = false;
}

function _nbCgResume() {
  if (!_nbCg.suspended) return;
  _nbCgHideResume();
  document.getElementById('nb-citation-graph-overlay')?.classList.add('open');
  const modal = document.getElementById('nb-citation-graph-modal');
  modal?.classList.add('open');
  modal?.removeAttribute('hidden');
  document.body.classList.add('citation-graph-open');
  document.addEventListener('keydown', _nbCgEscapeKeyHandler);
  _nbCgObserveStage();
  document.getElementById('nb-cg-close')?.focus();
}

function _nbCgHideResume() {
  _nbCg.suspended = false;
  const resume = document.getElementById('nb-cg-resume');
  if (resume) resume.hidden = true;
}

function _nbCgShowLoading(show) {
  const el = document.getElementById('nb-cg-loading');
  const stage = document.getElementById('nb-cg-stage');
  if (el) {
    el.hidden = !show;
    el.setAttribute('aria-hidden', show ? 'false' : 'true');
  }
  stage?.setAttribute('aria-busy', show ? 'true' : 'false');
}

function _nbCgShowEmpty(message, skippedSources) {
  const el = document.getElementById('nb-cg-empty');
  if (!el) return;
  if (!message) {
    el.innerHTML = '';
    el.hidden = true;
    return;
  }
  let html = `
    <div class="nb-cg-empty-icon" aria-hidden="true">↗</div>
    <div class="nb-cg-empty-title">${_nbCgEscapeHtml(message)}</div>
    ${_nbCg.requestUrl ? `<button type="button" class="nb-cg-retry-btn" data-cg-action="retry">${_nbCgEscapeHtml(_ti('citationGraph.retry'))}</button>` : ''}
  `;
  const skipped = Array.isArray(skippedSources) ? skippedSources : [];
  if (skipped.length) {
    const items = skipped.slice(0, 30).map(s =>
      `<li><strong>${_nbCgEscapeHtml(s.title || _ti('citationGraph.untitled'))}</strong> - <em>${_nbCgEscapeHtml(s.reason || _ti('citationGraph.unknownReason'))}</em></li>`
    ).join('');
    const more = skipped.length > 30 ? `<li>${_nbCgEscapeHtml(_ti('citationGraph.skippedMore', { count: skipped.length - 30 }))}</li>` : '';
    html += `
      <details class="nb-cg-skipped-block" open>
        <summary class="disclosure">${_nbCgEscapeHtml(_ti('citationGraph.skippedSummary', { count: skipped.length }))}</summary>
        <ul>${items}${more}</ul>
      </details>
    `;
  }
  el.innerHTML = html;
  el.hidden = false;
}

function _nbCgClearStage() {
  ['nb-cg-svg', 'nb-cg-bands', 'nb-cg-overlay', 'nb-cg-gutters', 'nb-cg-detail', 'nb-cg-explored-chips'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '';
  });
  _nbCgShowEmpty('');
  _nbCgUpdateTooltip();
  const stage = document.getElementById('nb-cg-stage');
  stage?.classList.remove('is-single', 'is-bands', 'is-draggable', 'is-dense', 'is-dragging');
  const topbar = document.getElementById('nb-cg-topbar');
  if (topbar) topbar.hidden = true;
}

/* ---------------- Data model ---------------- */

// Strip the fields the model adds, so a node can be stored back as a work.
function _nbCgPlainWork(work) {
  const { _kind, links, refs, cits, internal, explored, degree, ...rest } = work || {};
  return rest;
}

function _nbCgWorkIndex() {
  const data = _nbCg.data || {};
  const works = new Map();
  const remember = (work) => {
    if (work?.id && !works.has(work.id)) works.set(work.id, work);
  };
  (data.nodes || []).forEach(remember);
  for (const expansion of _nbCg.expansions.values()) remember(expansion.center);
  (data.prior_works || []).forEach(remember);
  (data.derivative_works || []).forEach(remember);
  for (const expansion of _nbCg.expansions.values()) {
    (expansion.prior_works || []).forEach(remember);
    (expansion.derivative_works || []).forEach(remember);
  }
  return works;
}

// One graph model for both layouts. Every citation is read as "citer cites
// cited"; where the two ends fall (corpus or not) decides the relation:
// corpus to corpus is an internal link, a corpus paper citing an outside work
// makes it a reference, an outside work citing the corpus makes it a citing
// work. An explored paper is promoted into the corpus.
function _nbCgBuildModel() {
  const data = _nbCg.data || {};
  const works = _nbCgWorkIndex();
  const baseIds = new Set((data.nodes || []).map(node => node.id).filter(Boolean));
  const corpus = new Map();
  [...baseIds, ..._nbCg.expandedIds].forEach((id) => {
    const work = works.get(id);
    if (!work || corpus.has(id)) return;
    corpus.set(id, {
      ..._nbCgPlainWork(work),
      id,
      _kind: 'corpus',
      explored: !baseIds.has(id),
      refs: new Set(),
      cits: new Set(),
      internal: new Set(),
    });
  });

  const cites = [];
  const addCitation = (citer, cited) => {
    if (citer && cited && citer !== cited) cites.push([citer, cited]);
  };
  (data.edges || []).forEach(edge => addCitation(edge.source, edge.target));
  (data.prior_works || []).forEach(work => (work.cited_by_notebook_ids || []).forEach(id => addCitation(id, work.id)));
  (data.derivative_works || []).forEach(work => (work.cites_notebook_ids || []).forEach(id => addCitation(work.id, id)));
  for (const [centerId, expansion] of _nbCg.expansions) {
    (expansion.prior_works || []).forEach(work => addCitation(centerId, work.id));
    (expansion.derivative_works || []).forEach(work => addCitation(work.id, centerId));
  }

  const internal = [];
  const internalKeys = new Set();
  const outside = new Map();
  const outsideEntry = (id) => {
    if (!outside.has(id)) outside.set(id, { refLinks: new Set(), citLinks: new Set() });
    return outside.get(id);
  };
  cites.forEach(([citer, cited]) => {
    const citerIn = corpus.has(citer);
    const citedIn = corpus.has(cited);
    if (citerIn && citedIn) {
      corpus.get(citer).internal.add(cited);
      corpus.get(cited).internal.add(citer);
      const key = citer < cited ? `${citer}|${cited}` : `${cited}|${citer}`;
      if (!internalKeys.has(key)) {
        internalKeys.add(key);
        internal.push({ a: citer, b: cited });
      }
    } else if (citerIn) {
      outsideEntry(cited).refLinks.add(citer);
    } else if (citedIn) {
      outsideEntry(citer).citLinks.add(cited);
    }
  });

  const refs = [];
  const cits = [];
  for (const [id, entry] of outside) {
    const work = works.get(id);
    if (!work) continue;
    // A work both cited by and citing the corpus goes where it has more links.
    const asRef = entry.refLinks.size >= entry.citLinks.size;
    const links = [...(asRef ? entry.refLinks : entry.citLinks)];
    const node = { ..._nbCgPlainWork(work), id, _kind: asRef ? 'prior' : 'derivative', links };
    (asRef ? refs : cits).push(node);
    links.forEach(corpusId => corpus.get(corpusId)?.[asRef ? 'refs' : 'cits'].add(id));
  }

  const corpusNodes = [...corpus.values()].map((node) => {
    const refsList = [...node.refs];
    const citsList = [...node.cits];
    const internalList = [...node.internal];
    return {
      ...node,
      refs: refsList,
      cits: citsList,
      internal: internalList,
      degree: refsList.length + citsList.length + internalList.length,
    };
  });
  const byLinks = (a, b) => b.links.length - a.links.length
    || Number(b.cited_by_count || 0) - Number(a.cited_by_count || 0);
  const byId = new Map();
  [...corpusNodes, ...refs, ...cits].forEach(node => byId.set(node.id, node));
  return {
    corpus: corpusNodes,
    corpusRanked: corpusNodes.slice().sort((a, b) => b.degree - a.degree
      || Number(b.cited_by_count || 0) - Number(a.cited_by_count || 0)),
    refs,
    cits,
    refsRanked: refs.slice().sort(byLinks),
    citsRanked: cits.slice().sort(byLinks),
    internal,
    byId,
  };
}

function _nbCgIsSingle(model = _nbCg.model) {
  return !!model && model.corpus.length === 1 && _nbCg.expandedIds.size === 0;
}

function _nbCgRebuild() {
  _nbCg.model = _nbCgBuildModel();
  const selected = _nbCg.selectedNodeId;
  if (selected && !_nbCg.model.byId.has(selected)) _nbCg.selectedNodeId = '';
  if (_nbCg.hoveredId && !_nbCg.model.byId.has(_nbCg.hoveredId)) _nbCg.hoveredId = '';
  _nbCgRenderHeader();
  _nbCgRelayout();
  _nbCgRenderPanel();
}

/* ---------------- Layouts ---------------- */

// Greedy anti-collision placement: each node tries the middle of its zone,
// then one step above, one below, two above… The first slot where the dot
// and its label overlap nothing wins. When there is none, the dot is placed
// alone and its label only shows on hover or selection.
function _nbCgPlacer(reserved = []) {
  const boxes = reserved.slice();
  const overlap = (box) => {
    let total = 0;
    for (const other of boxes) {
      const ox = Math.min(box[2], other[2] + 3) - Math.max(box[0], other[0] - 3);
      const oy = Math.min(box[3], other[3] + 2) - Math.max(box[1], other[1] - 2);
      if (ox > 0 && oy > 0) total += ox * oy;
    }
    return total;
  };
  const search = (mid, lo, hi, step, boxAt) => {
    let best = null;
    for (let k = 0; k < 400; k += 1) {
      const offset = Math.ceil(k / 2) * step;
      if (mid - offset < lo && mid + offset > hi) break;
      const y = mid + (k % 2 ? offset : -offset);
      if (y < lo || y > hi) continue;
      const box = boxAt(y);
      const o = overlap(box);
      if (!best || o < best.o) best = { y, box, o };
      if (o === 0) break;
    }
    return best || { y: mid, box: boxAt(mid), o: Infinity };
  };
  return {
    place(mid, lo, hi, step, fullBoxAt, dotBoxAt) {
      let best = search(mid, lo, hi, step, fullBoxAt);
      let showLabel = true;
      if (best.o > 0) {
        best = search(mid, lo, hi, step, dotBoxAt);
        showLabel = false;
      }
      boxes.push(best.box);
      return { y: best.y, showLabel };
    },
  };
}

function _nbCgLayoutSingle(model, stageW, stageH) {
  const W = Math.max(stageW, 480);
  const H = Math.max(stageH, 380);
  const source = model.corpus[0];
  const top = 92;
  const axisY = H - 40;
  const bottom = axisY - 16;
  const midY = Math.round((top + bottom) / 2);
  const narrow = W < 760;
  const padL = narrow ? 92 : 112;
  const padR = narrow ? 96 : 120;
  // The paper sits right of centre: there are usually far more references
  // than citing works.
  const cx = Math.round(padL + (W - padL - padR) * 0.64);
  const leftA = padL;
  const leftB = cx - 70;
  const rightA = cx + 80;
  const rightB = W - padR;
  const refYears = model.refs.map(_nbCgYear).filter(Boolean);
  const citYears = model.cits.map(_nbCgYear).filter(Boolean);
  const srcYear = _nbCgYear(source)
    || (refYears.length ? Math.max(...refYears) + 1 : 0)
    || (citYears.length ? Math.min(...citYears) - 1 : 0)
    || _NB_CG_NOW;
  const olderYears = refYears.filter(year => year <= srcYear);
  const refMin = (olderYears.length ? Math.min(...olderYears) : srcYear - 10) - 1;
  const rightEnd = Math.max(_NB_CG_NOW, srcYear + 1);
  const leftX = year => leftA + (_nbCgClamp(year || refMin, refMin, srcYear) - refMin)
    / Math.max(1, srcYear - refMin) * (leftB - leftA);
  const rightX = year => rightA + (_nbCgClamp(year || rightEnd, srcYear + 1, rightEnd) - srcYear - 1)
    / Math.max(1, rightEnd - srcYear - 1) * (rightB - rightA);
  const compact = W < 760 || H < 520;
  const radius = node => Math.min(compact ? 11 : 16, 4 + Math.log2(Number(node.cited_by_count || 0) + 2) * (compact ? 0.8 : 1.15));
  const step = _nbCgClamp((bottom - top) / 14, 15, 23);
  const placer = _nbCgPlacer([[cx - 95, midY - 22, cx + 95, midY + 52]]);
  const targets = new Map();
  const placeSide = (list, xOf, side) => {
    list.slice()
      .sort((a, b) => Number(b.cited_by_count || 0) - Number(a.cited_by_count || 0))
      .forEach((node) => {
        const x = xOf(_nbCgYear(node));
        const r = radius(node);
        const label = _nbCgNodeLabel(node, compact);
        const labelW = label.length * (compact ? 5.9 : 6.3) + 4;
        const hh = Math.max(7, r);
        const full = side === 'L'
          ? y => [x - r - 5 - labelW, y - hh, x + r, y + hh]
          : y => [x - r, y - hh, x + r + 5 + labelW, y + hh];
        const spot = placer.place(midY, top, bottom, step, full, y => [x - r, y - hh, x + r, y + hh]);
        targets.set(node.id, { node, x, y: spot.y, r, label, showLabel: spot.showLabel, side });
      });
  };
  // Undated works cannot be placed on a time axis: the panel counts them.
  if (_nbCg.filters.refs) placeSide(model.refs.filter(_nbCgYear), leftX, 'L');
  if (_nbCg.filters.cits) placeSide(model.cits.filter(_nbCgYear), rightX, 'R');
  // The paper is drawn last, on top of everything.
  targets.set(source.id, { node: source, x: cx, y: midY, r: 16, label: '', showLabel: false, source: true });

  const ticks = [];
  const leftStep = (leftB - leftA) < 260 ? 10 : 5;
  for (let year = Math.ceil(refMin / leftStep) * leftStep; year < srcYear; year += leftStep) {
    ticks.push({ x: leftX(year), label: String(year) });
  }
  const rightWidth = rightB - rightA;
  if (rightEnd > srcYear + 1 && rightWidth >= 80) {
    const perYear = rightWidth / Math.max(1, rightEnd - srcYear - 1);
    const rightStep = perYear < 22 ? 5 : perYear < 40 ? 2 : 1;
    for (let year = Math.ceil((srcYear + 1) / rightStep) * rightStep; year <= rightEnd; year += rightStep) {
      ticks.push({ x: rightX(year), label: String(year) });
    }
  }
  ticks.push({ x: cx, label: String(srcYear), source: true });
  return { mode: 'single', W, H, compact, top, midY, axisY, cx, leftA, leftB, rightA, rightB, srcYear, source, targets, ticks };
}

function _nbCgLayoutBands(model, stageW, stageH) {
  const W = Math.max(stageW, 520);
  const H = Math.max(stageH, 380);
  const compact = W < 820 || H < 560;
  const gutterW = compact ? 92 : 112;
  const xL = gutterW + 30;
  const xR = W - (compact ? 84 : 110);
  const bandsTop = 50;
  const axisY = H - 30;
  const avail = axisY - 8 - bandsTop;
  // References 38 %, corpus 24 %, citing works 38 %; a large corpus (an
  // author's whole output) takes up to 44 % so its papers are not stacked.
  const corpusShare = 0.24 + _nbCgClamp((model.corpus.length - 16) * 0.004, 0, 0.2);
  const hCor = Math.round(avail * corpusShare);
  const hRef = Math.round((avail - hCor) / 2);
  const hCit = avail - hRef - hCor;
  const band = {
    prior: [bandsTop, hRef],
    corpus: [bandsTop + hRef, hCor],
    derivative: [bandsTop + hRef + hCor, hCit],
  };

  const step = compact ? 16 : 20;
  const cap = h => _nbCgClamp(Math.floor((h - 16) / step) * Math.floor((xR - xL) / (compact ? 120 : 140)), 6, 40);
  const capRef = cap(hRef);
  const capCit = cap(hCit);
  // Undated works cannot be placed on a time axis: the panel counts them.
  const datedRefs = model.refsRanked.filter(_nbCgYear);
  const datedCits = model.citsRanked.filter(_nbCgYear);
  const visRefs = _nbCg.filters.refs ? datedRefs.slice(0, _nbCg.showAll.refs ? undefined : capRef) : [];
  const visCits = _nbCg.filters.cits ? datedCits.slice(0, _nbCg.showAll.cits ? undefined : capCit) : [];

  // Two-segment year axis: the distant past is compressed, the corpus and
  // citing-works period is stretched. It starts at the oldest paper on
  // screen, not at the oldest reference hidden behind "Show all".
  const corpusYears = model.corpus.map(_nbCgYear).filter(Boolean).sort((a, b) => a - b);
  const refYears = visRefs.map(_nbCgYear).filter(Boolean);
  const minCorpus = corpusYears.length ? corpusYears[0] : _NB_CG_NOW - 10;
  const medianCorpus = corpusYears.length ? corpusYears[Math.floor(corpusYears.length / 2)] : minCorpus;
  const yBreak = minCorpus - 1;
  const y0 = Math.min(Math.floor(Math.min(minCorpus, ...refYears) / 5) * 5, yBreak - 1);
  const end = Math.max(_NB_CG_NOW, yBreak + 2);
  const xBreak = xL + (xR - xL) * (yBreak - y0 > 12 ? 0.36 : 0.15);
  const xOf = (year) => {
    const v = _nbCgClamp(year, y0, end);
    return v <= yBreak
      ? xL + (v - y0) / Math.max(1, yBreak - y0) * (xBreak - xL)
      : xBreak + (v - yBreak) / Math.max(1, end - yBreak) * (xR - xBreak);
  };

  // Size = links in this graph, not total citations. Smaller corpus circles
  // when there are many of them.
  const corpusMax = (compact ? 11 : 14) * (model.corpus.length > 40 ? 0.7 : 1);
  const radius = node => (node._kind === 'corpus'
    ? Math.min(corpusMax, 5 + Math.sqrt(node.degree) * 1.2)
    : Math.min(compact ? 9 : 12, 3 + node.links.length * 1.6));
  const fallbackYear = { corpus: medianCorpus, prior: y0, derivative: end };
  const placer = _nbCgPlacer();
  const targets = new Map();
  const place = (list, kind) => {
    const [top, h] = band[kind];
    const mid = top + h / 2;
    const lo = top + 14;
    const hi = top + h - 12;
    list.forEach((node) => {
      const x = xOf(_nbCgYear(node) || fallbackYear[kind]);
      const r = radius(node);
      const shared = node._kind !== 'corpus' && node.links.length > 1 ? ` · ${node.links.length}×` : '';
      const label = `${_nbCgNodeLabel(node, compact)}${shared}`;
      const labelW = label.length * (compact ? 5.9 : 6.3) + 4;
      const hh = Math.max(7, r);
      const spot = placer.place(
        mid, lo, hi, step,
        y => [x - r, y - hh, x + r + 5 + labelW, y + hh],
        y => [x - r, y - hh, x + r, y + hh],
      );
      targets.set(node.id, { node, x, y: spot.y, r, label, showLabel: spot.showLabel, band: kind });
    });
  };
  place(model.corpusRanked, 'corpus');
  place(visRefs, 'prior');
  place(visCits, 'derivative');

  const ticks = [];
  const oldStep = (xBreak - xL) < 200 ? 20 : 10;
  for (let year = Math.ceil(y0 / oldStep) * oldStep; year <= yBreak - 3; year += oldStep) {
    ticks.push({ x: xOf(year), label: String(year) });
  }
  const perYear = (xR - xBreak) / Math.max(1, end - yBreak);
  const newStep = perYear < 22 ? 5 : perYear < 40 ? 2 : 1;
  for (let year = Math.ceil((yBreak + 1) / newStep) * newStep; year <= end; year += newStep) {
    ticks.push({ x: xOf(year), label: String(year) });
  }
  return {
    mode: 'bands',
    W, H, compact, gutterW, xL, xR, bandsTop, axisY, band, targets, ticks,
    shown: { refs: visRefs.length, cits: visCits.length },
    dated: { refs: datedRefs.length, cits: datedCits.length },
    more: {
      refs: _nbCg.filters.refs && datedRefs.length > capRef,
      cits: _nbCg.filters.cits && datedCits.length > capCit,
    },
  };
}

function _nbCgObserveStage() {
  const stage = document.getElementById('nb-cg-stage');
  if (!stage) return;
  if (typeof ResizeObserver !== 'undefined') {
    if (!_nbCg.resizeObserver) _nbCg.resizeObserver = new ResizeObserver(() => _nbCgMeasureStage());
    _nbCg.resizeObserver.observe(stage);
  }
  _nbCgMeasureStage();
}

function _nbCgMeasureStage() {
  const stage = document.getElementById('nb-cg-stage');
  if (!stage) return;
  const w = Math.round(stage.clientWidth);
  const h = Math.round(stage.clientHeight);
  // A hidden or not yet laid out stage measures 0: keep the last size.
  if (!w || !h || (w === _nbCg.stage.w && h === _nbCg.stage.h)) return;
  _nbCg.stage = { w, h };
  if (_nbCg.relayoutRaf) cancelAnimationFrame(_nbCg.relayoutRaf);
  _nbCg.relayoutRaf = requestAnimationFrame(() => {
    _nbCg.relayoutRaf = null;
    _nbCgRelayout();
  });
}

function _nbCgRelayout() {
  const model = _nbCg.model;
  if (!model) return;
  const { w, h } = _nbCg.stage;
  if (!w || !h) return; // the ResizeObserver calls back once the stage has a size
  _nbCg.layout = _nbCgIsSingle(model)
    ? _nbCgLayoutSingle(model, w, h)
    : _nbCgLayoutBands(model, w, h);
  _nbCgSyncPositions();
  _nbCgRenderStage();
  _nbCgApplyFocus();
  _nbCgUpdateTooltip();
}

/* ---------------- Motion ---------------- */

function _nbCgSyncPositions() {
  const layout = _nbCg.layout;
  if (!_nbCgMotionEnabled()) {
    _nbCgStopLoop();
    _nbCg.pos = new Map([...layout.targets].map(([id, t]) => [id, { x: t.x, y: t.y, vx: 0, vy: 0 }]));
    _nbCg.layoutSig = '';
    return;
  }
  // New nodes flow out of what they are linked to: the corpus band, or the
  // source paper in the one-paper view.
  const jitter = span => (Math.random() - 0.5) * span;
  for (const [id, t] of layout.targets) {
    if (_nbCg.pos.has(id)) continue;
    if (layout.mode === 'single') {
      _nbCg.pos.set(id, t.source
        ? { x: t.x, y: t.y, vx: 0, vy: 0 }
        : { x: layout.cx + (t.x - layout.cx) * 0.2 + jitter(20), y: layout.midY + jitter(30), vx: 0, vy: 0 });
    } else {
      const corpusMid = layout.band.corpus[0] + layout.band.corpus[1] / 2;
      _nbCg.pos.set(id, { x: t.x + jitter(40), y: corpusMid + jitter(30), vx: 0, vy: 0 });
    }
  }
  for (const id of [..._nbCg.pos.keys()]) {
    if (!layout.targets.has(id)) _nbCg.pos.delete(id);
  }
  // Only restart the loop when the targets moved: restarting it on every
  // render would never let it settle.
  const sig = [...layout.targets].map(([id, t]) => `${id}:${Math.round(t.x)}:${Math.round(t.y)}`).join('|');
  if (sig !== _nbCg.layoutSig) {
    _nbCg.layoutSig = sig;
    _nbCgStartLoop();
  }
}

// A crowded band can leave collisions and springs jostling forever: after a
// few seconds without a drag the loop stops anyway.
const _NB_CG_MAX_SETTLE_FRAMES = 420;

function _nbCgStartLoop() {
  _nbCg.loopFrames = 0;
  if (_nbCg.rafId || document.hidden) return;
  const tick = () => {
    _nbCg.rafId = null;
    if (!_nbCgIsOpen() || document.hidden || !_nbCg.layout) return;
    const moving = _nbCgStep();
    _nbCgUpdatePositionsDom();
    _nbCg.loopFrames += 1;
    if (_nbCg.drag?.moved || (moving && _nbCg.loopFrames < _NB_CG_MAX_SETTLE_FRAMES)) {
      _nbCg.rafId = requestAnimationFrame(tick);
    }
  };
  _nbCg.rafId = requestAnimationFrame(tick);
}

function _nbCgStopLoop() {
  if (_nbCg.rafId) cancelAnimationFrame(_nbCg.rafId);
  _nbCg.rafId = null;
}

function _nbCgStep() {
  const targets = _nbCg.layout?.targets;
  if (!targets) return false;
  const ids = [...targets.keys()];
  const pos = _nbCg.pos;
  const dragId = _nbCg.drag?.moved ? _nbCg.drag.id : '';
  const near = dragId ? _nbCgRelatedIds(dragId) : null;
  const dragged = dragId ? pos.get(dragId) : null;
  for (const id of ids) {
    if (id === dragId) continue;
    const p = pos.get(id);
    const t = targets.get(id);
    if (!p) continue;
    let ax = (t.x - p.x) * 0.05;
    let ay = (t.y - p.y) * 0.035;
    if (dragged && near.has(id)) {
      ax += (dragged.x - p.x) * 0.018;
      ay += (dragged.y - p.y) * 0.018;
    }
    p.vx = (p.vx + ax) * 0.8;
    p.vy = (p.vy + ay) * 0.8;
  }
  for (let i = 0; i < ids.length; i += 1) {
    const a = pos.get(ids[i]);
    const ra = targets.get(ids[i]).r;
    for (let j = i + 1; j < ids.length; j += 1) {
      const b = pos.get(ids[j]);
      const rb = targets.get(ids[j]).r;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.hypot(dx, dy) || 0.01;
      const min = ra + rb + 5;
      if (d >= min) continue;
      const push = (min - d) * 0.12;
      const ux = dx / d;
      const uy = dy / d;
      if (ids[i] !== dragId) { a.vx -= ux * push; a.vy -= uy * push; }
      if (ids[j] !== dragId) { b.vx += ux * push; b.vy += uy * push; }
    }
  }
  let maxV = 0;
  for (const id of ids) {
    if (id === dragId) continue;
    const p = pos.get(id);
    p.x += p.vx;
    p.y += p.vy;
    maxV = Math.max(maxV, Math.abs(p.vx) + Math.abs(p.vy));
  }
  return maxV > 0.05;
}

/* ---------------- Rendering ---------------- */

function _nbCgPos(id) {
  return _nbCg.pos.get(id) || _nbCg.layout?.targets.get(id) || null;
}

// Horizontal curve between a work and the side of the source paper; it
// follows the source when the source is dragged.
function _nbCgSingleLinkD(source, outer, side) {
  const ey = source.y + _nbCgClamp((outer.y - source.y) * 0.08, -12, 12);
  let x1; let y1; let x2; let y2;
  if (side === 'L') {
    x1 = outer.x + outer.r; y1 = outer.y; x2 = source.x - 19; y2 = ey;
  } else {
    x1 = source.x + 19; y1 = ey; x2 = outer.x - outer.r; y2 = outer.y;
  }
  const mx = ((x1 + x2) / 2).toFixed(1);
  return `M ${x1.toFixed(1)} ${y1.toFixed(1)} C ${mx} ${y1.toFixed(1)}, ${mx} ${y2.toFixed(1)}, ${x2.toFixed(1)} ${y2.toFixed(1)}`;
}

// Vertical curve between bands: from the bottom of the upper node to the top
// of the lower one.
function _nbCgBandLinkD(outer, corpus, kind) {
  const a = kind === 'prior' ? { x: outer.x, y: outer.y + outer.r } : { x: corpus.x, y: corpus.y + corpus.r };
  const b = kind === 'prior' ? { x: corpus.x, y: corpus.y - corpus.r } : { x: outer.x, y: outer.y - outer.r };
  const my = ((a.y + b.y) / 2).toFixed(1);
  return `M ${a.x.toFixed(1)} ${a.y.toFixed(1)} C ${a.x.toFixed(1)} ${my}, ${b.x.toFixed(1)} ${my}, ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;
}

// Internal links arc above the corpus band.
function _nbCgArcD(a, b) {
  const mx = (a.x + b.x) / 2;
  const lift = 18 + Math.abs(a.x - b.x) * 0.12;
  return `M ${a.x.toFixed(1)} ${(a.y - a.r).toFixed(1)} Q ${mx.toFixed(1)} ${(Math.min(a.y, b.y) - lift).toFixed(1)} ${b.x.toFixed(1)} ${(b.y - b.r).toFixed(1)}`;
}

function _nbCgRenderStage() {
  const layout = _nbCg.layout;
  const model = _nbCg.model;
  const stage = document.getElementById('nb-cg-stage');
  const svg = document.getElementById('nb-cg-svg');
  const bandsEl = document.getElementById('nb-cg-bands');
  const overlay = document.getElementById('nb-cg-overlay');
  const gutters = document.getElementById('nb-cg-gutters');
  if (!layout || !model || !stage || !svg || !bandsEl || !overlay || !gutters) return;
  const single = layout.mode === 'single';
  const f = value => Number(value).toFixed(1);
  stage.classList.toggle('is-single', single);
  stage.classList.toggle('is-bands', !single);
  stage.classList.toggle('is-draggable', _nbCgMotionEnabled());
  stage.classList.toggle('is-dense', !single && layout.targets.size > 60);
  [svg, bandsEl, overlay, gutters].forEach((el) => {
    el.style.width = `${layout.W}px`;
    el.style.height = `${layout.H}px`;
  });
  svg.setAttribute('width', String(layout.W));
  svg.setAttribute('height', String(layout.H));
  svg.setAttribute('viewBox', `0 0 ${layout.W} ${layout.H}`);
  const topbar = document.getElementById('nb-cg-topbar');
  if (topbar) topbar.hidden = false;
  const legend = document.getElementById('nb-cg-legend-text');
  if (legend) legend.textContent = _ti(single ? 'citationGraph.legendSingle' : 'citationGraph.legendBands');

  const gridTop = single ? 80 : layout.bandsTop;
  const grid = layout.ticks.map(t => `<line class="nb-cg-grid-line" x1="${f(t.x)}" y1="${gridTop}" x2="${f(t.x)}" y2="${layout.axisY}"></line>`
    + (single ? `<line class="nb-cg-tick-mark" x1="${f(t.x)}" y1="${layout.axisY}" x2="${f(t.x)}" y2="${layout.axisY + 5}"></line>` : '')).join('');
  const axisL = single ? layout.leftA - 10 : layout.xL - 10;
  const axisR = single ? layout.rightB + 10 : layout.xR + 10;
  const guide = single
    ? `<line class="nb-cg-source-guide" x1="${layout.cx}" y1="76" x2="${layout.cx}" y2="${layout.axisY}"></line>`
    : '';

  const linkItems = [];
  for (const [id, target] of layout.targets) {
    if (target.node._kind === 'corpus') continue;
    target.node.links.forEach((corpusId) => {
      const corpusTarget = layout.targets.get(corpusId);
      if (corpusTarget) {
        linkItems.push({ outerId: id, corpusId, kind: target.node._kind, side: target.side, outerR: target.r, corpusR: corpusTarget.r });
      }
    });
  }
  const arcItems = !single && _nbCg.filters.internal
    ? model.internal.filter(arc => layout.targets.has(arc.a) && layout.targets.has(arc.b))
      .map(arc => ({ ...arc, aR: layout.targets.get(arc.a).r, bR: layout.targets.get(arc.b).r }))
    : [];
  const nodesSvg = [...layout.targets].map(([id, target]) => {
    const node = target.node;
    const classes = ['nb-cg-node-wrap', `kind-${node._kind}`];
    if (node.explored) classes.push('is-explored');
    if (target.source) classes.push('is-source');
    const year = _nbCgYear(node);
    const aria = `${node.title || _ti('citationGraph.unknownTitle')}${year ? ` (${year})` : ''}`;
    return `<g class="${classes.join(' ')}" data-id="${_nbCgEscapeAttr(id)}" role="button" tabindex="0" aria-label="${_nbCgEscapeAttr(aria)}">`
      + `<circle class="nb-cg-node-halo" r="${f(target.source ? 24 : target.r + 6)}"></circle>`
      + `<circle class="nb-cg-node" r="${f(target.r)}"></circle></g>`;
  }).join('');
  svg.innerHTML = `<g class="nb-cg-grid">${grid}</g>`
    + `<line class="nb-cg-axis" x1="${f(axisL)}" y1="${layout.axisY}" x2="${f(axisR)}" y2="${layout.axisY}"></line>`
    + guide
    + `<g class="nb-cg-links">${linkItems.map(link => `<path class="nb-cg-link kind-${link.kind}"></path>`).join('')}</g>`
    + `<g class="nb-cg-arcs">${arcItems.map(() => '<path class="nb-cg-arc"></path>').join('')}</g>`
    + `<g class="nb-cg-nodes">${nodesSvg}</g>`;

  const ticksHtml = layout.ticks.map(t => `<div class="nb-cg-tick${t.source ? ' is-source' : ''}" style="left:${f(t.x)}px;top:${layout.axisY + (single ? 9 : 7)}px">${_nbCgEscapeHtml(t.label)}</div>`).join('');
  const labelsHtml = [...layout.targets].filter(([, target]) => !target.source).map(([id, target]) => (
    `<div class="nb-cg-label kind-${target.node._kind}${target.side === 'L' ? ' is-left' : ''}" data-id="${_nbCgEscapeAttr(id)}">${_nbCgEscapeHtml(target.label)}</div>`
  )).join('');
  let singleHtml = '';
  if (single) {
    singleHtml = `<div class="nb-cg-col-title kind-prior" style="left:${layout.leftA}px;width:${layout.leftB - layout.leftA}px">${_nbCgEscapeHtml(_ti('citationGraph.colRefs'))}</div>`
      + `<div class="nb-cg-col-title kind-derivative" style="left:${layout.rightA}px;width:${Math.max(80, layout.rightB - layout.rightA)}px">${_nbCgEscapeHtml(_ti('citationGraph.colCits'))}</div>`
      + `<div class="nb-cg-source-card" style="left:${layout.cx - 95}px;top:${layout.midY + 28}px">`
      + `<div class="nb-cg-source-eyebrow">${_nbCgEscapeHtml(_ti('citationGraph.kindSource'))}</div>`
      + `<div class="nb-cg-source-name">${_nbCgEscapeHtml(_nbCgShortRef(layout.source))}</div></div>`;
  }
  overlay.innerHTML = ticksHtml + singleHtml + labelsHtml;

  if (single) {
    bandsEl.innerHTML = '';
    gutters.innerHTML = '';
  } else {
    const exploredCount = model.corpus.filter(node => node.explored).length;
    const baseCount = model.corpus.length - exploredCount;
    const bands = [
      {
        kind: 'prior', key: 'refs', name: _ti('citationGraph.bandRefs'),
        sub: _ti('citationGraph.bandRefsSub', { shown: layout.shown.refs, total: layout.dated.refs }),
        more: layout.more.refs,
      },
      {
        kind: 'corpus', key: '', name: _ti(exploredCount ? 'citationGraph.bandSources' : 'citationGraph.bandCorpus'),
        sub: exploredCount
          ? _ti('citationGraph.bandSourcesSub', { base: baseCount, explored: exploredCount })
          : _ti(model.corpus.length === 1 ? 'citationGraph.papersOne' : 'citationGraph.papersMany', { count: model.corpus.length }),
        more: false,
      },
      {
        kind: 'derivative', key: 'cits', name: _ti('citationGraph.bandCits'),
        sub: _ti('citationGraph.bandCitsSub', { shown: layout.shown.cits, total: layout.dated.cits }),
        more: layout.more.cits,
      },
    ];
    bandsEl.innerHTML = bands.map(b => `<div class="nb-cg-band kind-${b.kind}" style="top:${layout.band[b.kind][0]}px;height:${layout.band[b.kind][1]}px"></div>`).join('');
    gutters.innerHTML = bands.map(b => `<div class="nb-cg-gutter kind-${b.kind}" style="top:${layout.band[b.kind][0] + 10}px;width:${layout.gutterW}px">`
      + `<span class="nb-cg-gutter-name">${_nbCgEscapeHtml(b.name)}</span>`
      + `<span class="nb-cg-gutter-sub">${_nbCgEscapeHtml(b.sub)}</span>`
      + (b.more ? `<button type="button" class="nb-cg-more-btn" data-cg-action="show-all" data-band="${b.key}">${_nbCgEscapeHtml(_ti(_nbCg.showAll[b.key] ? 'citationGraph.showLess' : 'citationGraph.showAll'))}</button>` : '')
      + '</div>').join('');
  }

  // Cache element references: the animation only touches attributes.
  const labels = new Map();
  overlay.querySelectorAll('.nb-cg-label').forEach(el => labels.set(el.dataset.id, el));
  _nbCg.dom.nodes = new Map();
  svg.querySelectorAll('.nb-cg-node-wrap').forEach((g) => {
    const id = g.dataset.id;
    _nbCg.dom.nodes.set(id, { g, label: labels.get(id) || null, target: layout.targets.get(id) });
  });
  const linkPaths = svg.querySelectorAll('.nb-cg-link');
  _nbCg.dom.links = linkItems.map((link, i) => ({ ...link, path: linkPaths[i] }));
  const arcPaths = svg.querySelectorAll('.nb-cg-arc');
  _nbCg.dom.arcs = arcItems.map((arc, i) => ({ ...arc, path: arcPaths[i] }));
  _nbCg.dom.sourceCard = overlay.querySelector('.nb-cg-source-card');
  _nbCgUpdatePositionsDom();
}

function _nbCgUpdatePositionsDom() {
  const layout = _nbCg.layout;
  if (!layout) return;
  const single = layout.mode === 'single';
  for (const [id, ref] of _nbCg.dom.nodes) {
    const p = _nbCgPos(id);
    if (!p) continue;
    ref.g.setAttribute('transform', `translate(${p.x.toFixed(1)} ${p.y.toFixed(1)})`);
    if (!ref.label) continue;
    const target = ref.target;
    // Left-side labels are anchored by their right edge (CSS translateX(-100%)),
    // which never lets the text run past the stage's left edge.
    const left = target.side === 'L'
      ? Math.max(4 + target.label.length * 6.3, p.x - target.r - 5)
      : p.x + target.r + 5;
    ref.label.style.left = `${Math.round(left)}px`;
    ref.label.style.top = `${Math.round(p.y - 7)}px`;
  }
  if (single && _nbCg.dom.sourceCard) {
    const source = _nbCgPos(layout.source.id);
    _nbCg.dom.sourceCard.style.left = `${Math.round(source.x - 95)}px`;
    _nbCg.dom.sourceCard.style.top = `${Math.round(source.y + 28)}px`;
  }
  for (const link of _nbCg.dom.links) {
    const outer = _nbCgPos(link.outerId);
    const corpus = _nbCgPos(link.corpusId);
    if (!outer || !corpus || !link.path) continue;
    link.path.setAttribute('d', single
      ? _nbCgSingleLinkD(corpus, { x: outer.x, y: outer.y, r: link.outerR }, link.side)
      : _nbCgBandLinkD({ x: outer.x, y: outer.y, r: link.outerR }, { x: corpus.x, y: corpus.y, r: link.corpusR }, link.kind));
  }
  for (const arc of _nbCg.dom.arcs) {
    const a = _nbCgPos(arc.a);
    const b = _nbCgPos(arc.b);
    if (!a || !b || !arc.path) continue;
    arc.path.setAttribute('d', _nbCgArcD({ x: a.x, y: a.y, r: arc.aR }, { x: b.x, y: b.y, r: arc.bR }));
  }
  _nbCgPositionTooltip();
}

/* ---------------- Focus, search, tooltip ---------------- */

function _nbCgRelatedIds(id) {
  const related = new Set();
  const node = _nbCg.model?.byId.get(id);
  if (!node) return related;
  related.add(id);
  if (node._kind === 'corpus') [...node.refs, ...node.cits, ...node.internal].forEach(other => related.add(other));
  else node.links.forEach(other => related.add(other));
  return related;
}

function _nbCgEffectiveSelection() {
  const layout = _nbCg.layout;
  if (layout?.mode === 'single') return _nbCg.selectedNodeId || layout.source.id;
  return _nbCg.selectedNodeId;
}

// Focus = node being dragged, else hovered, else selected.
function _nbCgFocusId() {
  if (_nbCg.drag?.moved) return _nbCg.drag.id;
  if (_nbCg.hoveredId) return _nbCg.hoveredId;
  return _nbCgHasUserSelection() ? _nbCg.selectedNodeId : '';
}

function _nbCgMatchesQuery(node) {
  const query = _nbCg.query.trim().toLowerCase();
  if (!query) return true;
  return `${(node.authors || []).join(' ')} ${node.title || ''} ${node.year || ''}`.toLowerCase().includes(query);
}

function _nbCgApplyFocus() {
  if (!_nbCg.layout) return;
  const focus = _nbCgFocusId();
  const related = focus ? _nbCgRelatedIds(focus) : null;
  const selected = _nbCgEffectiveSelection();
  for (const [id, ref] of _nbCg.dom.nodes) {
    const node = ref.target.node;
    const dim = (related && !related.has(id)) || !_nbCgMatchesQuery(node);
    const active = id === selected || id === focus;
    ref.g.classList.toggle('is-dim', dim);
    ref.g.classList.toggle('is-selected', id === selected);
    ref.g.classList.toggle('is-focus', id === focus);
    if (!ref.label) continue;
    ref.label.classList.toggle('is-dim', dim);
    ref.label.classList.toggle('is-active', active);
    ref.label.classList.toggle('is-label-hidden', !ref.target.showLabel && !active);
  }
  for (const link of _nbCg.dom.links) {
    const on = !!focus && (focus === link.outerId || focus === link.corpusId);
    const outerNode = _nbCg.model.byId.get(link.outerId);
    link.path?.classList.toggle('is-on', on);
    link.path?.classList.toggle('is-off', focus ? !on : !_nbCgMatchesQuery(outerNode || {}));
  }
  for (const arc of _nbCg.dom.arcs) {
    const on = !!focus && (focus === arc.a || focus === arc.b);
    arc.path?.classList.toggle('is-on', on);
    arc.path?.classList.toggle('is-off', !!focus && !on);
  }
}

function _nbCgUpdateTooltip() {
  const tip = document.getElementById('nb-cg-tooltip');
  if (!tip) return;
  const id = _nbCg.drag?.moved ? '' : _nbCg.hoveredId;
  const target = id && _nbCg.layout?.targets.get(id);
  if (!target) {
    tip.hidden = true;
    tip.dataset.id = '';
    return;
  }
  if (tip.dataset.id !== id) {
    const node = target.node;
    const meta = [node.journal, _ti('citationGraph.tooltipCitations', { count: _nbCgNumber(node.cited_by_count) })]
      .filter(Boolean).join(' · ');
    tip.innerHTML = `<div class="nb-cg-tooltip-title">${_nbCgEscapeHtml(node.title || _ti('citationGraph.unknownTitle'))}</div>`
      + `<div class="nb-cg-tooltip-meta">${_nbCgEscapeHtml(meta)}</div>`;
    tip.dataset.id = id;
  }
  tip.hidden = false;
  _nbCgPositionTooltip();
}

// Below the node, or above it in the lower 40 % of the stage.
function _nbCgPositionTooltip() {
  const tip = document.getElementById('nb-cg-tooltip');
  const layout = _nbCg.layout;
  const id = tip?.dataset.id;
  if (!tip || tip.hidden || !id || !layout) return;
  const p = _nbCgPos(id);
  const target = layout.targets.get(id);
  if (!p || !target) return;
  const height = tip.offsetHeight || 60;
  const left = _nbCgClamp(p.x - 130, 8, layout.W - 268);
  const top = p.y > layout.H * 0.6 ? p.y - target.r - 10 - height : p.y + target.r + 10;
  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;
}

function _nbCgSetHovered(id) {
  const next = String(id || '');
  if (next === _nbCg.hoveredId) return;
  _nbCg.hoveredId = next;
  _nbCgApplyFocus();
  _nbCgUpdateTooltip();
}

function _nbCgSelectNode(nodeId) {
  let id = String(nodeId || '');
  if (_nbCg.layout?.mode === 'single' && id === _nbCg.layout.source.id) id = '';
  _nbCg.selectedNodeId = id;
  _nbCgApplyFocus();
  _nbCgRenderPanel();
}

/* ---------------- Header ---------------- */

function _nbCgRenderHeader() {
  const data = _nbCg.data;
  const model = _nbCg.model;
  const baseNodes = Array.isArray(data?.nodes) ? data.nodes : [];
  let eyebrow = _ti('citationGraph.eyebrow');
  let title = _nbCg.scope?.title || _ti('citationGraph.title');
  let meta = '';
  if (model && baseNodes.length === 1) {
    // One paper, explored or not: the header names the paper in full.
    const source = model.byId.get(baseNodes[0].id) || baseNodes[0];
    eyebrow = _ti('citationGraph.eyebrowArticle');
    title = source.title || title;
    meta = [_nbCgSurnames(source), _nbCgYear(source) || '', source.journal].filter(Boolean).join(' · ');
  } else {
    const eyebrowKeys = {
      notebook: 'citationGraph.eyebrowNotebook',
      article: 'citationGraph.eyebrowArticle',
      author: 'citationGraph.eyebrowAuthor',
      institution: 'citationGraph.eyebrowInstitution',
    };
    eyebrow = _ti(eyebrowKeys[_nbCg.scope?.kind] || 'citationGraph.eyebrow');
    if (model) {
      const years = baseNodes.map(_nbCgYear).filter(Boolean);
      const period = years.length ? `${Math.min(...years)}–${Math.max(...years)}` : '';
      const count = _ti(baseNodes.length === 1 ? 'citationGraph.papersOne' : 'citationGraph.papersMany', { count: baseNodes.length });
      meta = [count, period, 'OpenAlex'].filter(Boolean).join(' · ');
    }
  }
  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  };
  setText('nb-cg-eyebrow', eyebrow);
  setText('nb-cg-title', title);
  setText('nb-cg-context', meta);
  const titleEl = document.getElementById('nb-cg-title');
  if (titleEl) titleEl.title = title;

  const chips = document.getElementById('nb-cg-explored-chips');
  if (chips) {
    chips.innerHTML = model ? [..._nbCg.expandedIds].map((id) => {
      const node = model.byId.get(id);
      if (!node) return '';
      return `<span class="nb-cg-explored-chip">${_nbCgEscapeHtml(_ti('citationGraph.exploredChip', { label: _nbCgNodeLabel(node, false) }))}`
        + `<button type="button" data-cg-action="unexplore" data-id="${_nbCgEscapeAttr(id)}" aria-label="${_nbCgEscapeAttr(_ti('citationGraph.removeExploration'))}" title="${_nbCgEscapeAttr(_ti('citationGraph.removeExploration'))}">×</button></span>`;
    }).join('') : '';
  }

  const filters = document.getElementById('nb-cg-filters');
  if (filters) filters.hidden = !model;
  if (!model) return;
  const single = _nbCgIsSingle(model);
  const chip = (key, count, labelKey, hidden = false) => {
    const button = document.getElementById(`nb-cg-filter-${key}`);
    if (!button) return;
    button.hidden = hidden;
    button.setAttribute('aria-pressed', _nbCg.filters[key] ? 'true' : 'false');
    setText(`nb-cg-count-${key}`, _nbCgNumber(count));
    setText(`nb-cg-filter-${key}-label`, _ti(labelKey));
  };
  chip('refs', model.refs.length, single ? 'citationGraph.filterRefsSingle' : 'citationGraph.filterRefs');
  chip('cits', model.cits.length, single ? 'citationGraph.filterCitsSingle' : 'citationGraph.filterCits');
  chip('internal', model.internal.length, 'citationGraph.filterInternal', single || !model.internal.length);
}

/* ---------------- Side panel ---------------- */

function _nbCgPill(kind, label) {
  return `<span class="nb-cg-pill kind-${kind}">${_nbCgEscapeHtml(label)}</span>`;
}

function _nbCgRows(rows) {
  return `<div class="nb-cg-rows">${rows.map(([key, value]) => (
    `<div class="nb-cg-row"><span>${_nbCgEscapeHtml(key)}</span><span>${_nbCgEscapeHtml(value)}</span></div>`
  )).join('')}</div>`;
}

function _nbCgWorkHeading(node, { allAuthors = false } = {}) {
  const authors = allAuthors ? _nbCgSurnames(node) : _nbCgFormatAuthors(node.authors);
  const meta = [authors, _nbCgYear(node) || ''].filter(Boolean).join(' · ');
  return `<div class="nb-cg-work">
    <div class="nb-cg-detail-title">${_nbCgEscapeHtml(node.title || _ti('citationGraph.unknownTitle'))}</div>
    ${meta ? `<div class="nb-cg-detail-meta">${_nbCgEscapeHtml(meta)}</div>` : ''}
    ${node.journal ? `<div class="nb-cg-detail-journal">${_nbCgEscapeHtml(node.journal)}</div>` : ''}
  </div>`;
}

// Where the paper sits in the library, if it is there: the path a library
// graph gives, else the same normalised-title match as Discovery's "in your
// library" badge. Very short titles never match, too many false hits.
function _nbCgLibraryPath(node) {
  const paths = Array.isArray(state.libPaths) ? state.libPaths : [];
  if (node.source_path && paths.includes(node.source_path)) return node.source_path;
  if (typeof discoveryTitleKey !== 'function') return '';
  const rows = Array.isArray(state.libRows) ? state.libRows : [];
  if (_nbCg.libraryIndex?.rows !== rows) {
    const byTitle = new Map();
    rows.forEach((row, index) => {
      const key = discoveryTitleKey(row?.[0]);
      if (key && paths[index] && !byTitle.has(key)) byTitle.set(key, paths[index]);
    });
    _nbCg.libraryIndex = { rows, byTitle };
  }
  const key = discoveryTitleKey(node.title);
  return key.length >= 12 ? _nbCg.libraryIndex.byTitle.get(key) || '' : '';
}

function _nbCgPublisherUrl(node) {
  const page = node.landing_page_url || (node.doi ? `https://doi.org/${encodeURI(String(node.doi).trim())}` : '');
  return /^https?:\/\//i.test(page) ? page : '';
}

// One main action: the paper in the library when it is there, else the
// publisher's page.
function _nbCgActions(node, { explore = true } = {}) {
  const buttons = [];
  const libraryPath = _nbCgLibraryPath(node);
  const page = _nbCgPublisherUrl(node);
  if (libraryPath) {
    buttons.push(`<button type="button" class="nb-cg-btn-primary" data-cg-action="open-library" data-id="${_nbCgEscapeAttr(node.id)}">${_nbCgEscapeHtml(_ti('citationGraph.openInLibrary'))}</button>`);
  } else if (page) {
    buttons.push(`<a class="nb-cg-btn-primary" href="${_nbCgEscapeAttr(page)}" target="_blank" rel="noopener">${_nbCgEscapeHtml(_ti('citationGraph.viewAtPublisher'))}</a>`);
  }
  if (explore && node.openalex_id) {
    const expanding = _nbCg.expandingIds.has(node.id);
    const expanded = _nbCg.expandedIds.has(node.id);
    const labelKey = expanding ? 'citationGraph.expanding' : expanded ? 'citationGraph.expanded' : 'citationGraph.explore';
    buttons.push(`<button type="button" class="nb-cg-btn-secondary" data-cg-action="explore" data-id="${_nbCgEscapeAttr(node.id)}"${expanding || expanded ? ' disabled' : ''}>${_nbCgEscapeHtml(_ti(labelKey))}</button>`);
  }
  return buttons.length ? `<div class="nb-cg-actions">${buttons.join('')}</div>` : '';
}

function _nbCgExternalLinks(node) {
  const links = [];
  const inLibrary = Boolean(_nbCgLibraryPath(node));
  if (node.openalex_id) {
    links.push(`<a href="https://openalex.org/${encodeURIComponent(node.openalex_id)}" target="_blank" rel="noopener">${_nbCgEscapeHtml(_ti('citationGraph.openAlexLink'))}</a>`);
  }
  // The publisher's page is the main button unless the paper is in the library.
  const page = _nbCgPublisherUrl(node);
  if (inLibrary && page) {
    links.push(`<a href="${_nbCgEscapeAttr(page)}" target="_blank" rel="noopener">${_nbCgEscapeHtml(_ti('citationGraph.publisherPage'))}</a>`);
  }
  // A corpus paper of a notebook graph is already in that notebook.
  const inThisNotebook = node._kind === 'corpus' && !node.explored && Boolean(_nbCg.notebookId);
  if (!inThisNotebook) {
    const added = _nbCg.addedIds.has(node.id);
    const adding = _nbCg.addingIds.has(node.id);
    const labelKey = added ? 'citationGraph.inNotebook' : adding ? 'citationGraph.adding' : 'citationGraph.addToNotebook';
    links.push(`<button type="button" class="nb-cg-text-btn${added ? ' is-done' : ''}" data-cg-action="add" data-id="${_nbCgEscapeAttr(node.id)}"${adding ? ' disabled' : ''}${added ? ' aria-disabled="true"' : ''}>${_nbCgEscapeHtml(_ti(labelKey))}</button>`);
  }
  if (!inLibrary && node.title) {
    links.push(`<button type="button" class="nb-cg-text-btn" data-cg-action="discovery" data-id="${_nbCgEscapeAttr(node.id)}">${_nbCgEscapeHtml(_ti('citationGraph.searchDiscovery'))}</button>`);
  }
  return links.length ? `<div class="nb-cg-links">${links.join('')}</div>` : '';
}

function _nbCgAbstract(node) {
  const abstract = String(node.abstract || '').trim();
  return abstract
    ? `<div class="nb-cg-abstract">${_nbCgEscapeHtml(abstract)}</div>`
    : `<div class="nb-cg-abstract is-empty">${_nbCgEscapeHtml(_ti('citationGraph.noAbstract'))}</div>`;
}

function _nbCgDataNotes() {
  const data = _nbCg.data || {};
  const notes = [];
  const withoutDoi = Number(data.stats?.papers_without_doi || 0);
  if (withoutDoi) notes.push(_ti('citationGraph.noDoiNote', { count: withoutDoi }));
  const model = _nbCg.model;
  const undated = model ? [...model.refs, ...model.cits].filter(node => !_nbCgYear(node)).length : 0;
  if (undated) notes.push(_ti(undated > 1 ? 'citationGraph.undatedNoteMany' : 'citationGraph.undatedNoteOne', { count: undated }));
  (Array.isArray(data.warnings) ? data.warnings : []).forEach(warning => notes.push(String(warning)));
  return notes.length
    ? `<div class="nb-cg-notes">${notes.map(note => `<p>${_nbCgEscapeHtml(note)}</p>`).join('')}</div>`
    : '';
}

function _nbCgKindPill(node) {
  if (node._kind === 'corpus') {
    return node.explored
      ? _nbCgPill('explored', _ti('citationGraph.kindExplored'))
      : _nbCgPill('corpus', _ti('citationGraph.kindCorpus'));
  }
  return _nbCgPill(node._kind, _ti(node._kind === 'prior' ? 'citationGraph.kindPrior' : 'citationGraph.kindDerivative'));
}

function _nbCgGapText(node, source) {
  const a = _nbCgYear(node);
  const b = _nbCgYear(source);
  if (!a || !b) return '';
  const diff = Math.abs(a - b);
  if (!diff) return _ti('citationGraph.gapSame');
  return _ti(`citationGraph.gap${a < b ? 'Before' : 'After'}${diff > 1 ? 'Many' : 'One'}`, { count: diff });
}

function _nbCgPanelSource(source) {
  const model = _nbCg.model;
  return `
    ${_nbCgPill('source', _ti('citationGraph.kindSource'))}
    ${_nbCgWorkHeading(source, { allAuthors: true })}
    ${_nbCgRows([
      [_ti('citationGraph.rowRefsCited'), _nbCgNumber(model.refs.length)],
      [_ti('citationGraph.rowCitingWorks'), _nbCgNumber(model.cits.length)],
      [_ti('citationGraph.rowCitedTotalOA'), _nbCgNumber(source.cited_by_count)],
    ])}
    ${_nbCgActions(source, { explore: false })}
    ${_nbCgExternalLinks(source)}
    ${_nbCgDataNotes()}
    <div class="nb-cg-hint">${_nbCgEscapeHtml(_ti('citationGraph.sourceHint'))}</div>
    ${String(source.abstract || '').trim() ? _nbCgAbstract(source) : ''}
  `;
}

function _nbCgPanelSingleNode(node, source) {
  const rows = [
    [_ti('citationGraph.rowCitedTotal'), _nbCgNumber(node.cited_by_count)],
    [_ti('citationGraph.rowRelation'), _ti(node._kind === 'prior' ? 'citationGraph.relCitedBy' : 'citationGraph.relCites')],
  ];
  const gap = _nbCgGapText(node, source);
  if (gap) rows.push([_ti('citationGraph.rowGap'), gap]);
  return `
    ${_nbCgKindPill(node)}
    ${_nbCgWorkHeading(node)}
    ${_nbCgRows(rows)}
    ${_nbCgActions(node)}
    ${_nbCgExternalLinks(node)}
    ${_nbCgAbstract(node)}
  `;
}

function _nbCgPanelBandsNode(node) {
  const byId = _nbCg.model.byId;
  let rows;
  let linkedIds;
  let linkedTitle;
  if (node._kind === 'corpus') {
    rows = [
      [_ti('citationGraph.rowCitedTotal'), _nbCgNumber(node.cited_by_count)],
      [_ti('citationGraph.rowRefsInGraph'), _nbCgNumber(node.refs.length)],
      [_ti('citationGraph.rowSharedRefs'), _nbCgNumber(node.refs.filter(id => (byId.get(id)?.links.length || 0) > 1).length)],
      [_ti('citationGraph.rowCitsInGraph'), _nbCgNumber(node.cits.length)],
    ];
    linkedIds = node.internal;
    linkedTitle = linkedIds.length ? _ti('citationGraph.linkedInternal') : '';
  } else {
    const prior = node._kind === 'prior';
    rows = [
      [_ti('citationGraph.rowCitedTotal'), _nbCgNumber(node.cited_by_count)],
      [_ti(prior ? 'citationGraph.rowCitedByCorpus' : 'citationGraph.rowCitesCorpus'), _ti('citationGraph.rowPapers', { count: node.links.length })],
    ];
    linkedIds = node.links;
    linkedTitle = _ti(prior ? 'citationGraph.linkedCitedBy' : 'citationGraph.linkedCites');
  }
  const linked = linkedIds.map(id => byId.get(id)).filter(Boolean).map(other => (
    `<button type="button" class="nb-cg-linked-item" data-cg-action="select" data-id="${_nbCgEscapeAttr(other.id)}">`
    + `<span class="nb-cg-dot kind-${other._kind}${other.explored ? ' is-explored' : ''}" aria-hidden="true"></span>`
    + `<span class="nb-cg-linked-label">${_nbCgEscapeHtml(`${_nbCgAuthorLastName(other)} ${_nbCgYear(other) || ''} — ${other.title || ''}`)}</span></button>`
  )).join('');
  return `
    <button type="button" class="nb-cg-back" data-cg-action="overview">${_nbCgEscapeHtml(_ti('citationGraph.backToOverview'))}</button>
    ${_nbCgKindPill(node)}
    ${_nbCgWorkHeading(node)}
    ${_nbCgRows(rows)}
    ${linkedTitle ? `<section class="nb-cg-section"><div class="nb-cg-section-label">${_nbCgEscapeHtml(linkedTitle)}</div><div class="nb-cg-linked">${linked}</div></section>` : ''}
    ${_nbCgActions(node)}
    ${_nbCgExternalLinks(node)}
    ${_nbCgAbstract(node)}
  `;
}

// The overview doubles as a list view of the graph for screen readers.
function _nbCgPanelOverview() {
  const model = _nbCg.model;
  const explored = model.corpus.some(node => node.explored);
  const years = model.corpus.map(_nbCgYear).filter(Boolean);
  const period = years.length ? `${Math.min(...years)}–${Math.max(...years)}` : '—';
  const shared = model.refs.filter(node => node.links.length > 1).length;
  const rank = (list, kind) => list.slice(0, 5).map(node => (
    `<button type="button" class="nb-cg-rank-item" data-cg-action="select" data-id="${_nbCgEscapeAttr(node.id)}">`
    + '<span class="nb-cg-rank-copy">'
    + `<span class="nb-cg-rank-title">${_nbCgEscapeHtml(node.title || _ti('citationGraph.unknownTitle'))}</span>`
    + `<span class="nb-cg-rank-meta">${_nbCgEscapeHtml([_nbCgAuthorLastName(node), _nbCgYear(node) || ''].filter(Boolean).join(' · '))}</span>`
    + '</span>'
    + `<span class="nb-cg-badge kind-${kind}">${node.links.length}×</span></button>`
  )).join('');
  return `
    <section class="nb-cg-section">
      <div class="nb-cg-section-label kind-corpus">${_nbCgEscapeHtml(_ti('citationGraph.overview'))}</div>
      ${_nbCgRows([
        [_ti(explored ? 'citationGraph.ovSources' : 'citationGraph.ovCorpus'), _nbCgNumber(model.corpus.length)],
        [_ti('citationGraph.ovPeriod'), period],
        [_ti('citationGraph.ovShared'), `${_nbCgNumber(shared)} / ${_nbCgNumber(model.refs.length)}`],
        [_ti('citationGraph.ovCits'), _nbCgNumber(model.cits.length)],
      ])}
    </section>
    ${model.refs.length ? `<section class="nb-cg-section"><div class="nb-cg-section-label kind-prior">${_nbCgEscapeHtml(_ti('citationGraph.topRefs'))}</div><div class="nb-cg-rank">${rank(model.refsRanked, 'prior')}</div></section>` : ''}
    ${model.cits.length ? `<section class="nb-cg-section"><div class="nb-cg-section-label kind-derivative">${_nbCgEscapeHtml(_ti('citationGraph.topCits'))}</div><div class="nb-cg-rank">${rank(model.citsRanked, 'derivative')}</div></section>` : ''}
    ${_nbCgDataNotes()}
  `;
}

// The panel is never empty: the paper or the overview stands in when nothing
// is selected.
function _nbCgRenderPanel() {
  const detail = document.getElementById('nb-cg-detail');
  if (!detail) return;
  const model = _nbCg.model;
  if (!model) {
    detail.innerHTML = '';
    return;
  }
  const selected = _nbCg.selectedNodeId ? model.byId.get(_nbCg.selectedNodeId) : null;
  if (_nbCgIsSingle(model)) {
    const source = model.corpus[0];
    detail.innerHTML = selected && selected.id !== source.id
      ? _nbCgPanelSingleNode(selected, source)
      : _nbCgPanelSource(source);
  } else {
    detail.innerHTML = selected ? _nbCgPanelBandsNode(selected) : _nbCgPanelOverview();
  }
}

/* ---------------- Actions ---------------- */

function _nbCgHandleAction(button) {
  const action = button.dataset.cgAction;
  const id = button.dataset.id || '';
  if (action === 'select') _nbCgSelectNode(id);
  else if (action === 'overview') _nbCgSelectNode('');
  else if (action === 'add') void _nbCgAddToNotebook(id);
  else if (action === 'explore') void _nbCgExpandNode(id);
  else if (action === 'unexplore') _nbCgRemoveExpansion(id);
  else if (action === 'open-library') _nbCgOpenInLibrary(id);
  else if (action === 'discovery') _nbCgSearchInDiscovery(id);
  else if (action === 'retry') _nbCgRetry();
  else if (action === 'show-all') {
    const key = button.dataset.band;
    if (key === 'refs' || key === 'cits') {
      _nbCg.showAll[key] = !_nbCg.showAll[key];
      _nbCgRelayout();
    }
  }
}

function _nbCgToggleFilter(key) {
  if (!_nbCg.model || !(key in _nbCg.filters)) return;
  _nbCg.filters[key] = !_nbCg.filters[key];
  if (_nbCg.hoveredId && key !== 'internal') _nbCg.hoveredId = '';
  _nbCgRenderHeader();
  _nbCgRelayout();
}

function _nbCgNotebookItem(node) {
  return {
    title: node.title || '',
    authors_line: (node.authors || []).join(', '),
    year: node.year || '',
    venue: node.journal || '',
    doi: node.doi || '',
    publisher_url: node.landing_page_url || '',
    abstract: node.abstract || '',
  };
}

// A graph opened from a notebook adds to that notebook; elsewhere the user
// picks one, as in Discovery.
async function _nbCgAddToNotebook(nodeId) {
  const node = _nbCg.model?.byId.get(nodeId);
  if (!node || _nbCg.addedIds.has(nodeId) || _nbCg.addingIds.has(nodeId)) return;
  const item = _nbCgNotebookItem(node);
  if (!_nbCg.notebookId) {
    if (typeof openNotebookPickerForItems === 'function') {
      void openNotebookPickerForItems([item], () => {
        _nbCg.addedIds.add(nodeId);
        _nbCgRenderPanel();
      });
    }
    return;
  }
  const notebookId = _nbCg.notebookId;
  _nbCg.addingIds.add(nodeId);
  _nbCgRenderPanel();
  try {
    const [path] = (await _discoveryLibraryPaths([item])) || [''];
    if (!path) {
      showToast(_ti('discovery.notInLibraryToast'), 5000, 'error');
      return;
    }
    const body = await _discoveryNotebookSourceBody(item, path);
    if (!body) throw new Error('extract');
    const response = await fetch(`/api/notebooks/${encodeURIComponent(notebookId)}/sources`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(String(response.status));
    const payload = await response.json().catch(() => ({}));
    if (typeof _nb !== 'undefined' && _nb.id === notebookId && payload?.source && Array.isArray(_nb.data?.sources)) {
      _nb.data.sources.push(payload.source);
      _renderNbSources();
    }
    _nbCg.addedIds.add(nodeId);
    showToast(_ti('discovery.addedToNotebook', { count: 1, title: _nbCg.scope?.title || '' }), 3000, 'success');
  } catch (_error) {
    showToast(_ti('discovery.addToNotebookFailed'), 4000, 'error');
  } finally {
    _nbCg.addingIds.delete(nodeId);
    if (_nbCg.model) _nbCgRenderPanel();
  }
}

function _nbCgOpenInLibrary(nodeId) {
  const node = _nbCg.model?.byId.get(nodeId);
  const path = node ? _nbCgLibraryPath(node) : '';
  if (!path) return;
  if (!state.libPaths.includes(path)) {
    showToast(_tr('Article non trouvé dans la bibliothèque.'), 2800);
    return;
  }
  _nbCgSuspend();
  void selectRowByPath(path);
}

function _nbCgSearchInDiscovery(nodeId) {
  const title = _nbCg.model?.byId.get(nodeId)?.title;
  if (!title) return;
  // The graph is kept, not closed: its explorations survive the detour.
  _nbCgSuspend();
  switchTab('discovery');
  switchDiscoveryWorkspace('articles', { focus: false });
  setTimeout(() => {
    const input = document.getElementById('discovery-input');
    if (input) {
      input.value = title;
      input.focus();
    }
  }, 150);
}

/* ---------------- Exploration ---------------- */

// Exploring a paper promotes it into the corpus band (dashed circle); its
// references and citing works join the bands, and those it shares with the
// rest of the corpus get their "2×" badge.
async function _nbCgExpandNode(nodeId) {
  const node = _nbCg.model?.byId.get(nodeId);
  if (!node?.openalex_id || _nbCg.expandingIds.has(nodeId) || _nbCg.expandedIds.has(nodeId)) return;
  _nbCg.expandingIds.add(nodeId);
  _nbCgRenderPanel();
  try {
    const url = `/api/citation-graph/expand?openalex_id=${encodeURIComponent(node.openalex_id)}&limit=18`;
    const response = await fetch(url);
    // A desktop server already running an older backend does not know the new
    // route yet. Keep the feature operational immediately through OpenAlex's
    // public API; the server route remains the preferred, privacy-gated path.
    const payload = response.status === 404
      ? await _nbCgExpandDirectlyFromOpenAlex(node.openalex_id, 18)
      : await response.json();
    if (!response.ok && response.status !== 404) throw new Error(_ti('citationGraph.serverError', { status: response.status }));
    if (!payload?.ok) throw new Error(payload?.msg || _ti('citationGraph.badOpenAlex'));
    if (!_nbCg.model) return; // closed meanwhile
    const before = new Set(_nbCg.model.byId.keys());
    _nbCg.expansions.set(nodeId, {
      center: _nbCgPlainWork(node),
      prior_works: Array.isArray(payload.prior_works) ? payload.prior_works : [],
      derivative_works: Array.isArray(payload.derivative_works) ? payload.derivative_works : [],
    });
    _nbCg.expandedIds.add(nodeId);
    _nbCg.selectedNodeId = nodeId;
    _nbCg.hoveredId = '';
    _nbCgRebuild();
    const added = [..._nbCg.model.byId.keys()].filter(id => !before.has(id)).length;
    const linkCount = (payload.prior_works || []).length + (payload.derivative_works || []).length;
    showToast(
      _ti('citationGraph.expandAdded', {
        papers: _ti(added > 1 ? 'citationGraph.expandPapersMany' : 'citationGraph.expandPapersOne', { count: added }),
        links: _ti(linkCount > 1 ? 'citationGraph.expandLinksMany' : 'citationGraph.expandLinksOne', { count: linkCount }),
      }),
      3400,
      'success',
    );
  } catch (error) {
    showToast(_ti('citationGraph.expandFailed', { error: error?.message || _ti('citationGraph.unknownErrorLower') }), 4800, 'error');
  } finally {
    _nbCg.expandingIds.delete(nodeId);
    if (_nbCg.model) _nbCgRenderPanel();
  }
}

// Undo an exploration, and the explorations that only hung on it.
function _nbCgRemoveExpansion(nodeId) {
  if (!_nbCg.expansions.has(nodeId)) return;
  _nbCg.expansions.delete(nodeId);
  _nbCg.expandedIds.delete(nodeId);
  const data = _nbCg.data || {};
  let changed = true;
  while (changed) {
    changed = false;
    const reachable = new Set([
      ...(data.nodes || []).map(work => work.id),
      ...(data.prior_works || []).map(work => work.id),
      ...(data.derivative_works || []).map(work => work.id),
    ]);
    for (const expansion of _nbCg.expansions.values()) {
      (expansion.prior_works || []).forEach(work => reachable.add(work.id));
      (expansion.derivative_works || []).forEach(work => reachable.add(work.id));
    }
    for (const id of [..._nbCg.expandedIds]) {
      if (reachable.has(id)) continue;
      _nbCg.expansions.delete(id);
      _nbCg.expandedIds.delete(id);
      changed = true;
    }
  }
  _nbCgRebuild();
}

/* ---------------- Events ---------------- */

function _nbCgStagePoint(event) {
  const stage = document.getElementById('nb-cg-stage');
  const rect = stage.getBoundingClientRect();
  return { x: event.clientX - rect.left + stage.scrollLeft, y: event.clientY - rect.top + stage.scrollTop };
}

function _nbCgBindUiOnce() {
  if (_nbCg.uiBound) return;
  _nbCg.uiBound = true;
  document.getElementById('nb-cg-close')?.addEventListener('click', closeNbCitationGraph);
  document.getElementById('nb-cg-resume-open')?.addEventListener('click', _nbCgResume);
  document.getElementById('nb-cg-resume-close')?.addEventListener('click', closeNbCitationGraph);
  ['refs', 'cits', 'internal'].forEach((key) => {
    document.getElementById(`nb-cg-filter-${key}`)?.addEventListener('click', () => _nbCgToggleFilter(key));
  });
  document.getElementById('nb-cg-header')?.addEventListener('click', (event) => {
    const button = event.target.closest?.('[data-cg-action]');
    if (button) _nbCgHandleAction(button);
  });
  document.getElementById('nb-cg-sidepanel')?.addEventListener('click', (event) => {
    const button = event.target.closest?.('[data-cg-action]');
    if (button && !button.disabled && button.getAttribute('aria-disabled') !== 'true') _nbCgHandleAction(button);
  });
  document.getElementById('nb-cg-search')?.addEventListener('input', (event) => {
    _nbCg.query = String(event.target.value || '');
    _nbCgApplyFocus();
  });

  const stage = document.getElementById('nb-cg-stage');
  const svg = document.getElementById('nb-cg-svg');
  if (!stage || !svg) return;
  stage.addEventListener('click', (event) => {
    if (_nbCg.suppressClick) return;
    const target = event.target;
    const action = target.closest?.('[data-cg-action]');
    if (action) {
      _nbCgHandleAction(action);
      return;
    }
    const node = target.closest?.('.nb-cg-node-wrap, .nb-cg-label');
    if (node) {
      _nbCgSelectNode(node.dataset.id);
      return;
    }
    if (!_nbCg.model || target.closest?.('#nb-cg-topbar, #nb-cg-empty, #nb-cg-loading')) return;
    _nbCgSelectNode('');
  });
  svg.addEventListener('mouseover', (event) => {
    if (_nbCg.drag?.moved) return;
    _nbCgSetHovered(event.target.closest?.('.nb-cg-node-wrap')?.dataset.id || '');
  });
  svg.addEventListener('mouseleave', (event) => {
    if (!_nbCg.drag?.moved && !event.relatedTarget?.closest?.('.nb-cg-label')) _nbCgSetHovered('');
  });
  // Labels are clickable too: they point at the same node as their dot.
  const overlay = document.getElementById('nb-cg-overlay');
  overlay?.addEventListener('mouseover', (event) => {
    const label = event.target.closest?.('.nb-cg-label');
    if (label && !_nbCg.drag?.moved) _nbCgSetHovered(label.dataset.id);
  });
  overlay?.addEventListener('mouseout', (event) => {
    if (_nbCg.drag?.moved || !event.target.closest?.('.nb-cg-label')) return;
    const next = event.relatedTarget;
    if (!next?.closest?.('.nb-cg-label, .nb-cg-node-wrap')) _nbCgSetHovered('');
  });
  svg.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const node = event.target.closest?.('.nb-cg-node-wrap');
    if (!node) return;
    event.preventDefault();
    _nbCgSelectNode(node.dataset.id);
  });
  // Dragging: the node follows the pointer, its neighbours are
  // pulled along, and on release everything springs back to its place.
  svg.addEventListener('mousedown', (event) => {
    if (event.button !== 0 || !_nbCg.layout || !_nbCgMotionEnabled()) return;
    const node = event.target.closest?.('.nb-cg-node-wrap');
    if (!node) return;
    event.preventDefault();
    _nbCg.drag = { id: node.dataset.id, moved: false, startX: event.clientX, startY: event.clientY };
  });
  window.addEventListener('mousemove', (event) => {
    const drag = _nbCg.drag;
    if (!drag) return;
    if (!drag.moved && Math.abs(event.clientX - drag.startX) + Math.abs(event.clientY - drag.startY) < 3) return;
    const p = _nbCg.pos.get(drag.id);
    if (!p) return;
    if (!drag.moved) {
      drag.moved = true;
      stage.classList.add('is-dragging');
      _nbCgApplyFocus();
      _nbCgUpdateTooltip();
    }
    const point = _nbCgStagePoint(event);
    p.x = point.x;
    p.y = point.y;
    p.vx = 0;
    p.vy = 0;
    _nbCgStartLoop();
  });
  window.addEventListener('mouseup', () => {
    const drag = _nbCg.drag;
    if (!drag) return;
    _nbCg.drag = null;
    stage.classList.remove('is-dragging');
    if (!drag.moved) return;
    // The click that follows a drag must not select.
    _nbCg.suppressClick = true;
    window.setTimeout(() => { _nbCg.suppressClick = false; }, 0);
    _nbCgApplyFocus();
    _nbCgStartLoop();
  });
}

function _nbCgNormalizeOpenAlexClientWork(raw, expansionKind) {
  const openalexId = String(raw?.id || '').replace('https://openalex.org/', '');
  const primaryLocation = raw?.primary_location || {};
  const source = primaryLocation?.source || {};
  return {
    id: openalexId,
    openalex_id: openalexId,
    title: String(raw?.display_name || ''),
    year: Number(raw?.publication_year) || null,
    doi: String(raw?.doi || '').replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, ''),
    authors: (raw?.authorships || []).slice(0, 5)
      .map(record => String(record?.author?.display_name || '').trim())
      .filter(Boolean),
    cited_by_count: Number(raw?.cited_by_count) || 0,
    abstract: '',
    journal: String(source?.display_name || ''),
    landing_page_url: String(primaryLocation?.landing_page_url || ''),
    expansion_kind: expansionKind,
  };
}

async function _nbCgExpandDirectlyFromOpenAlex(openalexId, limit = 18) {
  const cleanId = String(openalexId || '').replace('https://openalex.org/', '');
  const fullSelect = 'id,display_name,doi,publication_year,authorships,cited_by_count,referenced_works,primary_location';
  const lightSelect = 'id,display_name,doi,publication_year,authorships,cited_by_count,primary_location';
  const centerUrl = new URL(`https://api.openalex.org/works/${encodeURIComponent(cleanId)}`);
  centerUrl.searchParams.set('select', fullSelect);
  const centerResponse = await fetch(centerUrl);
  if (!centerResponse.ok) throw new Error(`OpenAlex ${centerResponse.status}`);
  const centerRaw = await centerResponse.json();
  const referenceIds = (centerRaw?.referenced_works || [])
    .map(id => String(id || '').replace('https://openalex.org/', ''))
    .filter(id => id && id !== cleanId)
    .slice(0, 72);

  const requests = [];
  if (referenceIds.length) {
    const priorUrl = new URL('https://api.openalex.org/works');
    priorUrl.searchParams.set('filter', `openalex_id:${referenceIds.join('|')}`);
    priorUrl.searchParams.set('per-page', String(referenceIds.length));
    priorUrl.searchParams.set('select', lightSelect);
    requests.push(fetch(priorUrl).then(response => response.ok ? response.json() : { results: [] }));
  } else {
    requests.push(Promise.resolve({ results: [] }));
  }
  const derivativeUrl = new URL('https://api.openalex.org/works');
  derivativeUrl.searchParams.set('filter', `cites:${cleanId}`);
  derivativeUrl.searchParams.set('per-page', String(limit));
  derivativeUrl.searchParams.set('sort', 'cited_by_count:desc');
  derivativeUrl.searchParams.set('select', lightSelect);
  requests.push(fetch(derivativeUrl).then(response => response.ok ? response.json() : { results: [] }));

  const [priorPayload, derivativePayload] = await Promise.all(requests);
  const priorWorks = (priorPayload?.results || [])
    .map(raw => _nbCgNormalizeOpenAlexClientWork(raw, 'prior'))
    .filter(work => work.id && work.id !== cleanId)
    .sort((a, b) => b.cited_by_count - a.cited_by_count)
    .slice(0, limit);
  const derivativeWorks = (derivativePayload?.results || [])
    .map(raw => _nbCgNormalizeOpenAlexClientWork(raw, 'derivative'))
    .filter(work => work.id && work.id !== cleanId)
    .slice(0, limit);
  return {
    ok: true,
    center: _nbCgNormalizeOpenAlexClientWork(centerRaw, 'center'),
    prior_works: priorWorks,
    derivative_works: derivativeWorks,
    edges: [
      ...priorWorks.map(work => ({ source: cleanId, target: work.id, kind: 'prior' })),
      ...derivativeWorks.map(work => ({ source: work.id, target: cleanId, kind: 'derivative' })),
    ],
  };
}
