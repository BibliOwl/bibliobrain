// Focused discovery and writing workspaces. No document changes without acceptance.
const discoveryRefinement = { selected: new Set(), library: null, libraryPromise: null, generation: 0, comparison: [], evidence: null };
const draftingChanges = { issues: [], expectedHtml: '', sourceRange: null, undo: null, rewriteBusy: false };
const workspaceText = (key, values = {}) => _ti(`workspace.${key}`, values);

function setDiscoverySearchCollapsed(collapsed) {
  const form = document.getElementById('discovery-search-form');
  if (form) form.open = !collapsed;
  const summary = document.getElementById('discovery-search-summary');
  if (summary) summary.textContent = document.getElementById('discovery-input')?.value.trim()
    || document.getElementById('discovery-notebook-value')?.textContent || '';
  syncDiscoverySearchActivity();
}

function syncDiscoverySearchActivity() {
  const stop = document.getElementById('discovery-compact-stop');
  if (stop) stop.hidden = !state.discoveryLoading;
}

function resetDiscoveryRefinement() {
  discoveryRefinement.generation++;
  discoveryRefinement.selected.clear();
  for (const id of ['discovery-refine', 'discovery-comparison-bar', 'discovery-filter-empty']) {
    const element = document.getElementById(id);
    if (element) element.hidden = true;
  }
  const form = document.getElementById('discovery-search-form');
  if (form && !state.discoveryLoading) form.open = true;
}

function discoveryTitleKey(value) {
  return String(value || '').normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');
}

async function loadDiscoveryLibraryTitles(force = false) {
  if (force) discoveryRefinement.library = null;
  if (discoveryRefinement.library) return discoveryRefinement.library;
  if (discoveryRefinement.libraryPromise) return discoveryRefinement.libraryPromise;
  discoveryRefinement.libraryPromise = (async () => {
    const titles = new Set();
    let offset = 0;
    while (true) {
      const response = await fetch(`/api/library?indexed_only=false&limit=1000&offset=${offset}`);
      if (!response.ok) throw new Error('library unavailable');
      const payload = await response.json();
      const rows = Array.isArray(payload.rows) ? payload.rows : [];
      rows.forEach(row => { const key = discoveryTitleKey(row[0]); if (key) titles.add(key); });
      if (!payload.pagination?.has_more) break;
      if (!rows.length) throw new Error('incomplete library');
      offset += rows.length;
    }
    discoveryRefinement.library = titles;
    return titles;
  })();
  try { return await discoveryRefinement.libraryPromise; }
  finally { discoveryRefinement.libraryPromise = null; }
}

function matchesDiscoveryFilters(item, filters, library) {
  if (filters.year && String(item.year || '') !== filters.year) return false;
  if (filters.oa && !item.open_access_pdf) return false;
  if (filters.library && library) {
    const present = library.has(discoveryTitleKey(item.title));
    if (filters.library === 'present' ? !present : present) return false;
  }
  return true;
}

function applyDiscoveryFilters() {
  const filters = {
    year: document.getElementById('discovery-filter-year').value,
    oa: document.getElementById('discovery-filter-oa').checked,
    library: document.getElementById('discovery-filter-library').value,
  };
  let visible = 0;
  document.querySelectorAll('[data-discovery-index]').forEach(card => {
    const item = state.discoveryResults[Number(card.dataset.discoveryIndex)];
    card.hidden = !item || !matchesDiscoveryFilters(item, filters, discoveryRefinement.library);
    if (!card.hidden) visible++;
  });
  document.getElementById('discovery-filter-count').textContent = workspaceText('filtered', { count: visible, total: state.discoveryResults.length });
  document.getElementById('discovery-filter-empty').hidden = visible > 0;
}

function discoveryPublicationYear(item) {
  const year = Number.parseInt(String(item?.year || '').trim(), 10);
  return Number.isFinite(year) ? year : null;
}

// Reorders the cards in place: data-discovery-index keeps pointing at the
// relevance-ordered state.discoveryResults, so download/compare still work.
function applyDiscoverySort() {
  const mode = document.getElementById('discovery-sort')?.value || 'relevance';
  const root = document.getElementById('discovery-results');
  if (!root) return;
  const cards = [...root.querySelectorAll('[data-discovery-index]')];
  const keyed = cards.map(card => {
    const index = Number(card.dataset.discoveryIndex);
    return { card, index, year: discoveryPublicationYear(state.discoveryResults[index]) };
  });
  keyed.sort((a, b) => {
    if (mode !== 'relevance' && a.year !== b.year) {
      if (a.year === null) return 1;
      if (b.year === null) return -1;
      return mode === 'newest' ? b.year - a.year : a.year - b.year;
    }
    return a.index - b.index;
  });
  keyed.forEach(({ card }) => root.appendChild(card));
}

async function prepareDiscoveryRefinement(results) {
  const generation = ++discoveryRefinement.generation;
  discoveryRefinement.selected.clear();
  syncDiscoveryComparisonSelection();
  setDiscoverySearchCollapsed(true);
  document.getElementById('discovery-refine').hidden = false;
  const year = document.getElementById('discovery-filter-year');
  const years = [...new Set(results.map(p => String(p.year || '')).filter(Boolean))].sort().reverse();
  year.innerHTML = `<option value="">${escapeHtml(workspaceText('allYears'))}</option>`
    + years.map(value => `<option value="${escapeAttr(value)}">${escapeHtml(value)}</option>`).join('');
  document.getElementById('discovery-filter-oa').checked = false;
  document.getElementById('discovery-sort').value = 'relevance';
  const library = document.getElementById('discovery-filter-library');
  library.value = '';
  library.disabled = true;
  document.getElementById('discovery-library-status').textContent = workspaceText('libraryLoading');
  applyDiscoveryFilters();
  try {
    await loadDiscoveryLibraryTitles(true);
    if (generation !== discoveryRefinement.generation) return;
    library.disabled = false;
    document.getElementById('discovery-library-status').textContent = workspaceText('libraryMatch');
    refreshDiscoveryLibraryMarks();
    applyDiscoveryFilters();
  } catch (_) {
    if (generation === discoveryRefinement.generation) document.getElementById('discovery-library-status').textContent = workspaceText('libraryUnavailable');
  }
}

function syncDiscoveryComparisonSelection() {
  const selected = discoveryRefinement.selected;
  document.getElementById('discovery-comparison-bar').hidden = !selected.size;
  document.getElementById('discovery-comparison-count').textContent = _ti('discovery.selectedCount', { count: selected.size });
  // Any number can be selected (to add them to a notebook); comparing takes 2 to 4.
  const compare = document.getElementById('discovery-compare');
  compare.disabled = selected.size < 2 || selected.size > 4;
  compare.title = selected.size > 4 ? workspaceText('compareMax') : '';
  document.querySelectorAll('[data-compare-index]').forEach(input => {
    input.checked = selected.has(Number(input.dataset.compareIndex));
    input.disabled = false;
  });
}

function renderDiscoveryComparison() {
  const papers = discoveryRefinement.comparison;
  const evidence = discoveryRefinement.evidence;
  const cell = (paper, key) => {
    const field = evidence?.find(row => row.id === paper.id)?.[key];
    if (!field) return `<span class="workspace-muted">${escapeHtml(workspaceText(evidence ? 'notStated' : 'notAnalyzed'))}</span>`;
    return `<p>${escapeHtml(field.summary)}</p><details><summary class="disclosure">${escapeHtml(workspaceText('evidence'))}</summary><blockquote data-i18n-skip>${escapeHtml(field.quote)}</blockquote></details>`;
  };
  document.getElementById('discovery-comparison-table').innerHTML = `<table style="min-width:${Math.max(620, 115 + papers.length * 240)}px"><caption class="visually-hidden">${escapeHtml(workspaceText('comparison'))}</caption><thead><tr><th scope="col">${escapeHtml(workspaceText('criterion'))}</th>${papers.map(p => `<th scope="col" data-i18n-skip title="${escapeAttr(p.title)}"><span class="comparison-title">${escapeHtml(p.title)}</span><small>${escapeHtml(p.year || '')}</small></th>`).join('')}</tr></thead><tbody>
    ${['method', 'findings', 'limitations'].map(key => `<tr><th scope="row">${escapeHtml(workspaceText(key))}</th>${papers.map(p => `<td>${cell(p, key)}</td>`).join('')}</tr>`).join('')}
    <tr><th scope="row">${escapeHtml(workspaceText('abstract'))}</th>${papers.map(p => `<td><details><summary class="disclosure">${escapeHtml(workspaceText('read'))}</summary><p data-i18n-skip>${escapeHtml(p.abstract || workspaceText('noAbstract'))}</p></details></td>`).join('')}</tr></tbody></table>`;
}

function openDiscoveryComparison() {
  discoveryRefinement.comparison = [...discoveryRefinement.selected].map(id => {
    const p = state.discoveryResults[id];
    return { id, title: String(p.title || '').slice(0, 2000), year: p.year,
      abstract: String(p.abstract || p.abstract_preview || '').slice(0, 12000) };
  });
  discoveryRefinement.evidence = null;
  document.getElementById('discovery-comparison-status').textContent = '';
  document.getElementById('discovery-analyze-comparison').disabled = false;
  renderDiscoveryComparison();
  document.getElementById('discovery-comparison-dialog').showModal();
}

async function analyzeDiscoveryComparison() {
  const model = optionalSelectedModel('discovery');
  if (!model) { showToast(workspaceText('chooseModel')); return; }
  const papers = discoveryRefinement.comparison;
  const button = document.getElementById('discovery-analyze-comparison');
  const status = document.getElementById('discovery-comparison-status');
  button.disabled = true;
  status.textContent = workspaceText('analyzing');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 180000);
  try {
    const response = await fetch('/api/article-discovery/compare', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
      body: JSON.stringify({ papers: papers.map(({id, title, abstract}) => ({id, title, abstract})), model, language: window.getLanguage?.() === 'en' ? 'en' : 'fr' }),
    });
    const payload = await response.json();
    if (!response.ok || !Array.isArray(payload.papers)) throw new Error('comparison failed');
    if (papers !== discoveryRefinement.comparison) return;
    discoveryRefinement.evidence = payload.papers;
    renderDiscoveryComparison();
    status.textContent = workspaceText('comparisonDone');
  } catch (_) { if (papers === discoveryRefinement.comparison) status.textContent = workspaceText('failed'); }
  finally { clearTimeout(timeout); if (papers === discoveryRefinement.comparison) button.disabled = false; }
}

function setDraftingFocus(focused) {
  document.getElementById('drafting-assistant').classList.toggle('is-focused', focused);
  const button = document.getElementById('drafting-focus-toggle');
  button.setAttribute('aria-pressed', String(focused));
  button.textContent = workspaceText(focused ? 'exitFocus' : 'focus');
  button.dataset.i18n = focused ? 'workspace.exitFocus' : 'workspace.focus';
}

let draftingSelectionToolbarFrame = null;

function positionDraftingSelectionToolbar(range) {
  const toolbar = document.getElementById('drafting-selection-toolbar');
  const editor = _draftingGetEditor();
  if (!toolbar || !editor || !range) return;
  toolbar.style.visibility = '';
  const rects = Array.from(range.getClientRects()).filter(rect => rect.width || rect.height);
  const selectionRect = rects.find(rect => rect.bottom >= 0 && rect.top <= innerHeight && rect.right >= 0 && rect.left <= innerWidth)
    || range.getBoundingClientRect();
  if ((!selectionRect.width && !selectionRect.height)
      || selectionRect.bottom < 0 || selectionRect.top > innerHeight
      || selectionRect.right < 0 || selectionRect.left > innerWidth) {
    toolbar.style.visibility = 'hidden';
    return;
  }
  const toolbarRect = toolbar.getBoundingClientRect();
  const editorRect = editor.getBoundingClientRect();
  const viewportPadding = 8;
  const selectionGap = 10;
  const editorLeft = Math.max(viewportPadding, editorRect.left + viewportPadding);
  const editorRight = Math.min(innerWidth - viewportPadding, editorRect.right - viewportPadding);
  const fitsEditor = toolbarRect.width <= editorRight - editorLeft;
  const minLeft = fitsEditor ? editorLeft : viewportPadding;
  const maxLeft = Math.max(minLeft, (fitsEditor ? editorRight : innerWidth - viewportPadding) - toolbarRect.width);
  const desiredLeft = selectionRect.left + (selectionRect.width - toolbarRect.width) / 2;
  const left = Math.min(maxLeft, Math.max(minLeft, desiredLeft));
  // Never float above the editor's top edge: the format toolbar lives there,
  // and a selection starting on the first line (e.g. Ctrl+A) would cover it.
  const minTop = Math.max(viewportPadding, editorRect.top + viewportPadding);
  const above = selectionRect.top - toolbarRect.height - selectionGap;
  const below = selectionRect.bottom + selectionGap;
  const top = above >= minTop
    ? above
    : Math.min(below, Math.max(minTop, innerHeight - toolbarRect.height - viewportPadding));
  toolbar.style.left = `${Math.round(left)}px`;
  toolbar.style.top = `${Math.round(top)}px`;
}

function scheduleDraftingSelectionToolbarPosition() {
  if (draftingSelectionToolbarFrame !== null) return;
  draftingSelectionToolbarFrame = requestAnimationFrame(() => {
    draftingSelectionToolbarFrame = null;
    const toolbar = document.getElementById('drafting-selection-toolbar');
    const range = state.drafting.selectionRange;
    if (toolbar && !toolbar.hidden && range) positionDraftingSelectionToolbar(range);
  });
}

function updateDraftingSelectionToolbar() {
  const range = state.drafting.selectionRange;
  const active = range && _draftingRangePointsIntoEditor(range) && !range.collapsed && range.toString().trim();
  const toolbar = document.getElementById('drafting-selection-toolbar');
  if (!toolbar) return;
  toolbar.hidden = !active;
  document.getElementById('drafting-selection-label').textContent = active
    ? workspaceText('passage', { count: range.toString().trim().split(/\s+/).length }) : '';
  if (active) positionDraftingSelectionToolbar(range);
  else {
    toolbar.style.removeProperty('left');
    toolbar.style.removeProperty('top');
    toolbar.style.removeProperty('visibility');
  }
  _draftingScheduleScopeUpdate();
}

function captureDraftingChangeSource() {
  const editor = _draftingGetEditor();
  const selected = state.drafting.selectionRange;
  const range = selected && _draftingRangePointsIntoEditor(selected) && !selected.collapsed ? selected.cloneRange() : document.createRange();
  if (range !== selected && (!selected || !_draftingRangePointsIntoEditor(selected) || selected.collapsed)) range.selectNodeContents(editor);
  draftingChanges.sourceRange = range;
  draftingChanges.expectedHtml = editor.innerHTML;
  draftingChanges.undo = null;
  draftingChanges.issues = [];
  draftingChanges.versionSaved = false;
}

function findDraftingChangeRange(original) {
  const editor = _draftingGetEditor();
  const scope = draftingChanges.sourceRange;
  if (!original || !_draftingRangePointsIntoEditor(scope) || editor.innerHTML !== draftingChanges.expectedHtml) return null;
  // Citation tokens and block boundaries must never be erased by a text replacement.
  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let text = '', node, previousBlock = null;
  while ((node = walker.nextNode())) {
    if (!scope.intersectsNode(node)) continue;
    const block = node.parentElement.closest('p,div,li,blockquote,h1,h2,h3') || editor;
    if (previousBlock && previousBlock !== block) text += '\u0000';
    previousBlock = block;
    if (node.parentElement.closest('.drafting-citation-token,[contenteditable="false"]')) { text += '\u0000'; continue; }
    const start = node === scope.startContainer ? scope.startOffset : 0;
    const end = node === scope.endContainer ? scope.endOffset : node.length;
    nodes.push({ node, offset: text.length, start, length: end - start });
    text += node.data.slice(start, end);
  }
  const index = text.indexOf(original);
  if (index < 0 || text.indexOf(original, index + 1) >= 0 || original.includes('\u0000')) return null;
  const first = nodes.find(n => index >= n.offset && index < n.offset + n.length);
  const last = nodes.find(n => index + original.length > n.offset && index + original.length <= n.offset + n.length);
  if (!first || !last) return null;
  const range = document.createRange();
  range.setStart(first.node, first.start + index - first.offset);
  range.setEnd(last.node, last.start + index + original.length - last.offset);
  return range;
}

function acceptDraftingChange(index) {
  const issue = draftingChanges.issues[index];
  if (!issue || issue.resolved || state.drafting.reviewBusy || draftingChanges.rewriteBusy) return;
  const range = findDraftingChangeRange(issue.original);
  if (!range) { showToast(workspaceText('staleChange'), 5000, 'info'); return; }
  const editor = _draftingGetEditor();
  const before = editor.innerHTML;
  // One version per review, taken before its first accepted change.
  if (!draftingChanges.versionSaved) {
    draftingChanges.versionSaved = true;
    _draftingSnapshotVersion('before_proofread');
  }
  range.deleteContents();
  range.insertNode(document.createTextNode(issue.suggestion));
  issue.resolved = 'accepted';
  _draftingSyncFromEditor();
  draftingChanges.expectedHtml = editor.innerHTML;
  draftingChanges.undo = { before, after: editor.innerHTML, index };
  renderDraftingChangeCards();
}

function undoDraftingChange() {
  const undo = draftingChanges.undo;
  const editor = _draftingGetEditor();
  if (!undo || editor.innerHTML !== undo.after) { showToast(workspaceText('staleChange')); return; }
  editor.innerHTML = undo.before;
  _draftingSyncFromEditor();
  // Ranges cannot survive restoring markup; remaining suggestions require a new analysis.
  draftingChanges.expectedHtml = '';
  draftingChanges.undo = null;
  draftingChanges.issues[undo.index].resolved = 'undone';
  renderDraftingChangeCards();
  showToast(workspaceText('undone'));
}

function renderDraftingChangeCards() {
  const root = document.getElementById('drafting-change-list');
  if (!root) return;
  root.innerHTML = draftingChanges.issues.map((issue, index) => `<article class="drafting-change-card${issue.unlisted ? ' is-unlisted' : ''}">
    ${issue.unlisted ? `<span class="drafting-change-flag">${escapeHtml(workspaceText('unlistedChange'))}</span>` : ''}
    <div class="drafting-change-pair"><div><span>${escapeHtml(workspaceText('before'))}</span><p data-i18n-skip>${escapeHtml(issue.original)}</p></div><div><span>${escapeHtml(workspaceText('after'))}</span><p data-i18n-skip>${escapeHtml(issue.suggestion)}</p></div></div>
    ${issue.explanation ? `<p class="workspace-muted" data-i18n-skip>${escapeHtml(issue.explanation)}</p>` : ''}
    <div class="drafting-change-actions">${issue.resolved ? `<span role="status">${escapeHtml(workspaceText(issue.resolved))}</span>` : `<button type="button" class="workspace-button is-primary" data-accept-change="${index}">${escapeHtml(workspaceText('accept'))}</button><button type="button" class="workspace-button" data-dismiss-change="${index}">${escapeHtml(workspaceText('dismiss'))}</button>`}</div></article>`).join('')
    + (draftingChanges.undo ? `<button type="button" class="workspace-button" id="drafting-undo-change">${escapeHtml(workspaceText('undo'))}</button>` : '');
  _draftingRefreshViewTabs();
}

async function runDraftingSelectionAction(action) {
  if (action === 'clear') {
    state.drafting.selectionRange = null;
    window.getSelection()?.removeAllRanges();
    updateDraftingSelectionToolbar();
    return;
  }
  if (action === 'word') { await copyDraftingSelectionForWord(state.drafting.selectionRange); return; }
  if (state.drafting.reviewBusy || state.drafting.searching || draftingChanges.rewriteBusy) return;
  const range = state.drafting.selectionRange;
  if (!range || range.collapsed || !_draftingRangePointsIntoEditor(range)) return;
  setDraftingFocus(false);
  if (action === 'cite') { setDraftingView('citations'); _draftingRestoreSelection(); await runDraftingAssistant(); return; }
  if (!['rephrase', 'shorten'].includes(action)) return;
  if (range.cloneContents().querySelector('.drafting-citation-token,[contenteditable="false"]')) {
    showToast(workspaceText('keepCitations'), 5000, 'info'); return;
  }
  const text = range.toString();
  if (text.length > 12000) { showToast(workspaceText('shorterSelection')); return; }
  const model = optionalSelectedModel('drafting');
  if (!model) { showToast(workspaceText('chooseModel')); return; }
  captureDraftingChangeSource();
  _draftingStartReviewView('proofread');
  draftingChanges.rewriteBusy = true;
  _draftingSetReviewBusy(true);
  const root = _draftingGetReviewPanel();
  root.innerHTML = `<p role="status">${escapeHtml(workspaceText('rewriting'))}</p>`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 180000);
  try {
    const response = await fetch('/api/drafting-rewrite', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, action, model }), signal: controller.signal });
    const payload = await response.json();
    if (!response.ok || !payload.text) throw new Error('rewrite failed');
    draftingChanges.issues = [{ original: text, suggestion: payload.text }];
    root.innerHTML = '<div id="drafting-change-list"></div>';
    renderDraftingChangeCards();
  } catch (_) { root.textContent = workspaceText('failed'); }
  finally { clearTimeout(timeout); draftingChanges.rewriteBusy = false; _draftingSetReviewBusy(false); }
}

function initFocusedWorkspaces() {
  const results = document.getElementById('discovery-results');
  if (!results || document.getElementById('discovery-refine')) return;
  results.insertAdjacentHTML('beforebegin', `<div class="discovery-refine" id="discovery-refine" hidden>
    <label><span data-i18n="workspace.sort">${escapeHtml(workspaceText('sort'))}</span><select id="discovery-sort">${['relevance', 'newest', 'oldest'].map(mode => `<option value="${mode}" data-i18n="workspace.sort${mode[0].toUpperCase()}${mode.slice(1)}">${escapeHtml(workspaceText(`sort${mode[0].toUpperCase()}${mode.slice(1)}`))}</option>`).join('')}</select></label>
    <label><span data-i18n="workspace.year">${escapeHtml(workspaceText('year'))}</span><select id="discovery-filter-year"><option value="">${escapeHtml(workspaceText('allYears'))}</option></select></label>
    <label class="workspace-check"><input type="checkbox" id="discovery-filter-oa"><span data-i18n="workspace.oa">${escapeHtml(workspaceText('oa'))}</span></label>
    <label><span data-i18n="workspace.library">${escapeHtml(workspaceText('library'))}</span><select id="discovery-filter-library"><option value="">${escapeHtml(workspaceText('all'))}</option><option value="present">${escapeHtml(workspaceText('present'))}</option><option value="absent">${escapeHtml(workspaceText('absent'))}</option></select></label>
    <button type="button" class="workspace-button" id="discovery-filter-reset">${escapeHtml(workspaceText('reset'))}</button><span id="discovery-filter-count" role="status"></span><small id="discovery-library-status"></small></div>
    <div class="discovery-comparison-bar" id="discovery-comparison-bar" hidden><span id="discovery-comparison-count"></span><button type="button" class="workspace-button is-primary" id="discovery-add-selection-notebook">${escapeHtml(_ti('discovery.addToNotebookMenu'))}</button><button type="button" class="workspace-button" id="discovery-compare" disabled>${escapeHtml(workspaceText('compare'))}</button><button type="button" class="workspace-button" id="discovery-clear-comparison">${escapeHtml(workspaceText('clear'))}</button></div>
    <p id="discovery-filter-empty" hidden role="status">${escapeHtml(workspaceText('noMatches'))}</p>`);
  document.getElementById('discovery-search-feedback').insertAdjacentHTML('afterbegin', `<button type="button" class="workspace-button" id="discovery-compact-stop" hidden>${escapeHtml(workspaceText('stopSearch'))}</button>`);
  document.body.insertAdjacentHTML('beforeend', `<dialog id="discovery-comparison-dialog" class="workspace-dialog" aria-labelledby="discovery-comparison-title"><header><div><h2 id="discovery-comparison-title">${escapeHtml(workspaceText('comparison'))}</h2><p>${escapeHtml(workspaceText('comparisonHint'))}</p></div><button type="button" class="workspace-button" id="discovery-close-comparison">${escapeHtml(workspaceText('close'))}</button></header><div class="workspace-dialog-actions"><button type="button" class="workspace-button is-primary" id="discovery-analyze-comparison">${escapeHtml(workspaceText('analyze'))}</button><span id="discovery-comparison-status" role="status"></span></div><div id="discovery-comparison-table" tabindex="0"></div></dialog>`);
  document.body.insertAdjacentHTML('beforeend', `<div class="drafting-selection-toolbar" id="drafting-selection-toolbar" role="toolbar" aria-label="${escapeAttr(workspaceText('selectionActions'))}" hidden><span id="drafting-selection-label"></span>${['cite', 'rephrase', 'shorten', 'word', 'clear'].map(action => `<button type="button" class="workspace-button" data-drafting-selection-action="${action}"${action === 'word' ? ` title="${escapeAttr(workspaceText('wordHint'))}"` : ''}>${escapeHtml(workspaceText(action === 'clear' ? 'wholeDraft' : action))}</button>`).join('')}</div>`);
  document.getElementById('discovery-refine').addEventListener('change', event => {
    if (event.target.id === 'discovery-sort') applyDiscoverySort();
    else applyDiscoveryFilters();
  });
  document.getElementById('discovery-filter-reset').addEventListener('click', () => {
    document.getElementById('discovery-sort').value = 'relevance';
    applyDiscoverySort();
    document.getElementById('discovery-filter-year').value = '';
    document.getElementById('discovery-filter-library').value = '';
    document.getElementById('discovery-filter-oa').checked = false;
    applyDiscoveryFilters();
  });
  results.addEventListener('change', event => {
    const input = event.target.closest('[data-compare-index]');
    if (!input) return;
    const id = Number(input.dataset.compareIndex);
    if (input.checked) discoveryRefinement.selected.add(id);
    else discoveryRefinement.selected.delete(id);
    syncDiscoveryComparisonSelection();
  });
  document.getElementById('discovery-clear-comparison').addEventListener('click', () => { discoveryRefinement.selected.clear(); syncDiscoveryComparisonSelection(); });
  document.getElementById('discovery-add-selection-notebook').addEventListener('click', () => { void addDiscoveryResultsToNotebook([...discoveryRefinement.selected]); });
  document.getElementById('discovery-compare').addEventListener('click', openDiscoveryComparison);
  document.getElementById('discovery-close-comparison').addEventListener('click', () => document.getElementById('discovery-comparison-dialog').close());
  document.getElementById('discovery-analyze-comparison').addEventListener('click', analyzeDiscoveryComparison);
  document.getElementById('discovery-compact-stop').addEventListener('click', () => { stopArticleDiscovery(); syncDiscoverySearchActivity(); });
  document.getElementById('drafting-focus-toggle').addEventListener('click', event => setDraftingFocus(event.currentTarget.getAttribute('aria-pressed') !== 'true'));
  const toolbar = document.getElementById('drafting-selection-toolbar');
  toolbar.addEventListener('pointerdown', event => { if (event.target.closest('button')) event.preventDefault(); });
  toolbar.addEventListener('click', event => { const button = event.target.closest('[data-drafting-selection-action]'); if (button) void runDraftingSelectionAction(button.dataset.draftingSelectionAction); });
  document.addEventListener('selectionchange', () => { _draftingStoreSelection(); updateDraftingSelectionToolbar(); });
  document.addEventListener('scroll', scheduleDraftingSelectionToolbarPosition, true);
  window.addEventListener('resize', scheduleDraftingSelectionToolbarPosition);
  document.getElementById('drafting-proofread-result').addEventListener('click', event => {
    const accept = event.target.closest('[data-accept-change]');
    const dismiss = event.target.closest('[data-dismiss-change]');
    if (accept) acceptDraftingChange(Number(accept.dataset.acceptChange));
    if (dismiss) { draftingChanges.issues[Number(dismiss.dataset.dismissChange)].resolved = 'dismissed'; renderDraftingChangeCards(); }
    if (event.target.closest('#drafting-undo-change')) undoDraftingChange();
  });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') setDraftingFocus(false); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initFocusedWorkspaces);
else initFocusedWorkspaces();
