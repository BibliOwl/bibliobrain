// ---------------------------------------------
//  NOTEBOOKS
// ---------------------------------------------
const _nb = {
  id: null,
  data: null,
  noteTimer: null,
  selectedPdfPath: '',
  streaming: false,
  chatAbortController: null,
  activeStreamRequestId: '',
  activeStreamModel: '',
  refreshingCollection: false,
};

async function loadNotebooks() {
  const cards = document.getElementById('nb-cards');
  if (cards && !cards.children.length) {
    const skeletonCard = `
      <div class="skeleton-card" aria-hidden="true">
        <div class="skeleton" style="width:22px;height:22px;border-radius:6px"></div>
        <div class="skeleton skeleton-line" style="width:70%"></div>
        <div class="skeleton skeleton-line" style="width:45%;height:9px"></div>
      </div>`;
    cards.innerHTML = skeletonCard.repeat(3);
  }
  try {
    const d = await fetchAllPaginated('/api/notebooks', ['notebooks'], { limit: 500 });
    _renderNbCards(d.notebooks);
    _draftingPopulateNotebookOptions(d.notebooks);
    state.discoveryNotebooksLoaded = false;
  } catch(e) {
    if (cards) cards.innerHTML = '';
    showToast(_tr('Erreur chargement carnets'));
  }
}

function _renderNbCards(list) {
  state._nbCardsCache = Array.isArray(list) ? list : [];
  const el = document.getElementById('nb-cards');
  if (!list.length) {
    el.innerHTML = `
      <div class="empty-state nb-empty-state">
        <h3 class="empty-state-title">${escapeHtml(_ti('notebooks.emptyTitle'))}</h3>
        <p class="empty-state-text">${escapeHtml(_ti('notebooks.emptyText'))}</p>
        <button type="button" class="btn btn-primary empty-state-action" ${bindBbHandler('click', function() { showNbCreateForm(); })}>${escapeHtml(_ti('notebooks.newNotebook'))}</button>
      </div>`;
    return;
  }
  el.innerHTML = list.map(nb => `
    <div class="nb-card" ${bindBbHandler('click', function() { openNotebook(nb.id); })}>
      <div class="nb-card-icon">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>
      </div>
      <div class="nb-card-title">${escapeHtml(nb.title)}</div>
      <div class="nb-card-meta">
        <span>${nb.source_count} source${nb.source_count !== 1 ? 's' : ''}</span>
        <span>·</span>
        <span>${nb.message_count} msg</span>
      </div>
      <div class="nb-card-date">${new Date(nb.created).toLocaleDateString('fr-FR')}</div>
      <button class="nb-card-delete" ${bindBbHandler('click', function(event) { event.stopPropagation(); deleteNotebook(nb.id); })} title="Supprimer" aria-label="Supprimer">&times;</button>
    </div>`).join('');
}

function showNbCreateForm() {
  const dialog = document.getElementById('nb-create-form');
  // The dialog sits in the notebook list, which is hidden while a notebook
  // is open: opened there it is modal but invisible, and the whole page
  // stops responding. It lives in <body> instead.
  if (dialog && dialog.parentElement !== document.body) document.body.appendChild(dialog);
  if (dialog && !dialog.open) dialog.showModal();
  onNbCreateModeChange();
  setTimeout(() => document.getElementById('nb-new-title').focus(), 50);
}
function hideNbCreateForm() {
  const dialog = document.getElementById('nb-create-form');
  if (dialog?.open) dialog.close();
  document.getElementById('nb-new-title').value = '';
  document.getElementById('nb-create-mode').value = 'blank';
  document.getElementById('nb-zotero-collection').value = '';
  setNbCreateBusy(false);
  updateNbImportProgress(null);
  onNbCreateModeChange();
}
function selectNbCreateMode(mode) {
  const select = document.getElementById('nb-create-mode');
  if (!select) return;
  select.value = mode;
  onNbCreateModeChange();
}

function onNbCreateModeChange() {
  const select = document.getElementById('nb-create-mode');
  const mode = select?.value || 'blank';
  // The two cards mirror the hidden <select>, whose Zotero option exists
  // only when Zotero is in use (startup.js).
  const zoteroOption = select?.querySelector('option[value="zotero_collection"]');
  document.querySelectorAll('[data-nb-create-mode]').forEach((card) => {
    const selected = card.dataset.nbCreateMode === mode;
    card.classList.toggle('is-selected', selected);
    card.setAttribute('aria-checked', selected ? 'true' : 'false');
    if (card.dataset.nbCreateMode === 'zotero_collection') card.hidden = !zoteroOption || zoteroOption.hidden;
  });
  const collectionInput = document.getElementById('nb-zotero-collection');
  if (collectionInput) {
    const showCollection = isZoteroInUse() && mode === 'zotero_collection';
    collectionInput.style.display = showCollection ? '' : 'none';
    if (showCollection) setTimeout(() => collectionInput.focus(), 50);
  }
}

function setNbCreateBusy(busy) {
  ['nb-create-mode', 'nb-new-title', 'nb-zotero-collection', 'nb-create-submit', 'nb-create-cancel']
    .forEach(id => {
      const element = document.getElementById(id);
      if (element) element.disabled = Boolean(busy);
    });
  const submit = document.getElementById('nb-create-submit');
  if (submit) submit.textContent = busy ? _tr('Import en cours…') : _tr('Créer');
}

function updateNbImportProgress(event) {
  const container = document.getElementById('nb-create-progress');
  const bar = document.getElementById('nb-create-progress-bar');
  const text = document.getElementById('nb-create-progress-text');
  if (!container || !bar || !text) return;
  if (!event) {
    container.hidden = true;
    bar.style.width = '0%';
    text.textContent = '';
    return;
  }
  const total = Math.max(0, Number(event.total) || 0);
  const processed = Math.max(0, Number(event.processed) || 0);
  const percent = total ? Math.min(100, Math.round(processed * 100 / total)) : 0;
  const current = String(event.current || '').trim();
  container.hidden = false;
  bar.style.width = `${percent}%`;
  text.textContent = total
    ? `${processed}/${total} articles (${percent} %)${current ? ` — ${current}` : ''}`
    : _tr('Préparation de la collection Zotero…');
}

async function createNotebook() {
  const title = document.getElementById('nb-new-title').value.trim() || 'Nouveau carnet';
  const mode = document.getElementById('nb-create-mode')?.value || 'blank';
  let d = {};
  try {
    setNbCreateBusy(true);
    if (mode === 'zotero_collection') {
      const collection = document.getElementById('nb-zotero-collection').value.trim();
      if (!collection) {
        showToast(_tr('Collection Zotero requise'));
        return;
      }
      updateNbImportProgress({total: 0, processed: 0});
      const response = await fetch('/api/notebooks/import-zotero-collection', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
        body: JSON.stringify({
          title: title === 'Nouveau carnet' ? '' : title,
          collection,
          include_descendants: true,
        }),
      });
      if (!response.ok) throw new Error(await responseErrorMessage(response, 'Import Zotero impossible'));
      for await (const event of readSSE(response)) {
        if (event.type === 'preparing' || event.type === 'start' || event.type === 'progress') {
          updateNbImportProgress(event);
        } else if (event.type === 'error') {
          throw new Error(event.detail || _tr('Erreur création carnet'));
        } else if (event.type === 'done') {
          d = event;
          updateNbImportProgress({...event, processed: event.matched, total: event.matched});
        }
      }
      if (!d.notebook_id) throw new Error(_tr("L'import Zotero s'est terminé sans résultat"));
    } else {
      const response = await fetch('/api/notebooks', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title}),
      });
      const raw = await response.text();
      try { d = raw ? JSON.parse(raw) : {}; } catch (e) { d = { detail: raw }; }
      if (!response.ok || d.error || d.detail) {
        throw new Error(d.detail || d.error || _tr(`Erreur creation carnet (${response.status})`));
      }
    }
    hideNbCreateForm();
    window.dispatchEvent(new CustomEvent('bibliobrain:milestone', { detail: { name: 'notebook-created' } }));
    await openNotebook(d.id || d.notebook_id);
    await _draftingLoadNotebookOptions(true);
    if (mode === 'zotero_collection') {
      showToast(_tr(`${d.added || 0} article(s) ajoute(s) au carnet`), 3200, 'success');
    }
    await refreshKgNotebookTargets();
    _updateKnowledgeFocusActions();
  } catch(e) {
    showToast(e?.message || _tr('Erreur création carnet'));
  } finally {
    setNbCreateBusy(false);
  }
}

async function deleteNotebook(id) {
  const cache = Array.isArray(state._nbCardsCache) ? state._nbCardsCache : [];
  const idx = cache.findIndex((nb) => nb.id === id);
  if (idx === -1) return;
  const removed = cache[idx];
  cache.splice(idx, 1);
  _renderNbCards([...cache]);
  _draftingPopulateNotebookOptions(cache);
  deleteWithUndo('notebook:' + id, {
    message: 'Carnet supprimé.',
    restore: () => {
      const list = Array.isArray(state._nbCardsCache) ? state._nbCardsCache : [];
      list.splice(Math.min(idx, list.length), 0, removed);
      _renderNbCards([...list]);
      _draftingPopulateNotebookOptions(list);
    },
    commit: async () => {
      try {
        await fetch('/api/notebooks/' + id, { method: 'DELETE', keepalive: true });
        await loadNotebooks();
        await refreshKgNotebookTargets();
        _updateKnowledgeFocusActions();
      } catch (e) {
        showToast(_tr('Suppression impossible.'));
        loadNotebooks();
      }
    },
  });
}

async function openNotebook(id) {
  flushPendingUndo('nb-message');
  try {
    const r = await fetch('/api/notebooks/' + id);
    const d = await r.json();
    _nb.id = id;
    _nb.data = d;
    _nb.refreshingCollection = false;
    document.getElementById('nb-home').style.display = 'none';
    document.getElementById('nb-workspace').classList.add('open');
    document.getElementById('nb-title-input').value = d.title;
    document.getElementById('nb-notes-input').value = d.notes || '';
    _updateNbCollectionRefreshAction();
    _renderNbSources();
    _renderNbMessages();
    _updateNbContext();
    window.requestAnimationFrame(refreshLayoutResizers);
  } catch(e) { showToast(_tr('Erreur ouverture carnet')); }
}

async function discoverRelatedFromNotebook() {
  if (!_nb.id || !_nb.data) return;
  const sources = Array.isArray(_nb.data.sources) ? _nb.data.sources : [];
  const notesInput = document.getElementById('nb-notes-input');
  const notes = String(notesInput?.value || '').trim();
  if (!sources.length && !notes) {
    showToast(_tr("Ajoutez au moins une source ou quelques notes avant d'explorer ce carnet."), 3200, 'info');
    return;
  }

  clearTimeout(_nb.noteTimer);
  if (notesInput) {
    try {
      await fetch(`/api/notebooks/${encodeURIComponent(_nb.id)}/notes`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({notes: notesInput.value || ''}),
      });
      _nb.data.notes = notesInput.value || '';
    } catch (e) {
      showToast(_tr('Les dernières notes ne pourront peut-être pas guider la recherche.'), 2800, 'info');
    }
  }

  switchTab('discovery');
  switchDiscoveryWorkspace('articles', { focus: false });
  const mode = document.getElementById('discovery-mode');
  if (mode) mode.value = 'notebook';
  onDiscoveryModeChange();
  await loadDiscoveryNotebookOptions();
  const notebookSelect = document.getElementById('discovery-notebook-select');
  if (notebookSelect) notebookSelect.value = _nb.id;
  state.discoveryNotebookContext = {
    id: _nb.id,
    title: String(_nb.data.title || document.getElementById('nb-title-input')?.value || _tr('Carnet sans titre')),
  };
  onDiscoveryNotebookChange();
  // The notebook placeholder is set by onDiscoveryModeChange().
  const input = document.getElementById('discovery-input');
  if (input) input.value = '';
  renderDiscoveryEmptyState(
    _tr('Prêt à explorer ce carnet'),
    _tr('Les notes et les articles du carnet seront synthétisés, puis utilisés pour trouver des travaux nouveaux et liés.'),
    { action: false },
  );
  document.getElementById('discovery-run-btn')?.focus();
}

function closeNotebook() {
  if (_nb.streaming) stopCurrentNotebookChat(true);
  _nb.id = null; _nb.data = null;
  document.getElementById('nb-workspace').classList.remove('open');
  document.getElementById('nb-home').style.display = 'flex';
  window.requestAnimationFrame(refreshLayoutResizers);
  loadNotebooks();
}

function _nbSourceCollection() {
  const collection = _nb.data?.source_collection;
  if (!collection || collection.provider !== 'zotero' || !String(collection.name || '').trim()) {
    return null;
  }
  return collection;
}

function _updateNbCollectionRefreshAction() {
  const button = document.getElementById('nb-refresh-collection-btn');
  if (!button) return;
  const collection = _nbSourceCollection();
  button.hidden = !collection;
  button.disabled = Boolean(_nb.refreshingCollection) || !isZoteroInUse();
  button.title = collection
    ? (isZoteroInUse()
      ? _tr(`Ajouter les nouveaux articles de « ${collection.name} »`)
      : _tr('Activez Zotero dans les réglages pour mettre à jour cette collection.'))
    : '';
}

async function refreshNbCollection() {
  const collection = _nbSourceCollection();
  if (!collection || !_nb.id || _nb.refreshingCollection) return;
  const button = document.getElementById('nb-refresh-collection-btn');
  const label = button?.querySelector('span');
  const idleLabel = label?.textContent || _tr('Mettre à jour');
  _nb.refreshingCollection = true;
  _updateNbCollectionRefreshAction();
  if (label) label.textContent = _tr('Mise à jour…');
  try {
    const response = await fetch('/api/notebooks/import-zotero-collection', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
      body: JSON.stringify({
        notebook_id: _nb.id,
        collection: collection.name,
        include_descendants: collection.include_descendants !== false,
      }),
    });
    if (!response.ok) throw new Error(await responseErrorMessage(response, 'Mise à jour impossible'));
    let result = null;
    for await (const event of readSSE(response)) {
      if (event.type === 'error') {
        throw new Error(event.detail || _tr('Mise à jour impossible'));
      }
      if (event.type === 'done') result = event;
    }
    const notebookResponse = await fetch(`/api/notebooks/${encodeURIComponent(_nb.id)}`);
    if (!notebookResponse.ok) {
      throw new Error(await responseErrorMessage(notebookResponse, 'Rechargement du carnet impossible'));
    }
    _nb.data = await notebookResponse.json();
    _renderNbSources();
    _updateNbContext();
    const added = Number(result?.added || 0);
    showToast(
      added
        ? _tr(`${added} nouvel article ajouté à partir de la collection.`)
        : _tr('Les sources sont déjà à jour.'),
      3200,
      'success',
    );
  } catch (e) {
    showToast(e?.message || _tr('Erreur de mise à jour de la collection'), 3600, 'error');
  } finally {
    _nb.refreshingCollection = false;
    if (label) label.textContent = idleLabel;
    _updateNbCollectionRefreshAction();
  }
}

async function saveNbTitle() {
  if (!_nb.id) return;
  const title = document.getElementById('nb-title-input').value.trim();
  if (!title || !_nb.data || title === _nb.data.title) return;
  _nb.data.title = title;
  await fetch(`/api/notebooks/${_nb.id}/title`, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title}),
  });
  state.drafting.notebookOptionsLoaded = false;
}

// Sources
const _nbArticleViewer = {
  sourcePath: '',
  returnFocusEl: null,
  closeTimer: null,
};

function _nbArticleViewerKeyHandler(event) {
  const modal = document.getElementById('nb-article-viewer');
  if (!modal?.classList.contains('open')) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeNbArticleViewer();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = Array.from(modal.querySelectorAll(
    'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
  )).filter(el => el.offsetParent !== null);
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

function openNbArticleViewer(event) {
  event?.preventDefault();
  event?.stopPropagation();
  const button = event?.currentTarget;
  const path = String(button?.dataset?.sourcePath || '').trim();
  const title = String(button?.dataset?.sourceTitle || path || _tr('Article')).trim();
  openArticleViewer(path, title, button);
}

function openSelectedLibraryArticleViewer(event) {
  event?.preventDefault();
  const path = String(_currentSelectedArticlePath() || '').trim();
  if (!path) {
    showToast(_tr("Sélectionnez un article d'abord."), 2800);
    return;
  }
  const row = state.libRows[state.selectedRow] || [];
  const title = String(row[0] || document.getElementById('library-detail-title')?.textContent || path).trim();
  openArticleViewer(path, title, event?.currentTarget || document.activeElement);
}

function openArticleViewer(path, title, returnFocusEl = null, page = null) {
  if (!path) return;

  if (!/\.pdf$/i.test(path)) {
    window.open('/api/open-pdf?path=' + encodeURIComponent(path), '_blank', 'noopener');
    return;
  }

  if (_nbArticleViewer.closeTimer) {
    window.clearTimeout(_nbArticleViewer.closeTimer);
    _nbArticleViewer.closeTimer = null;
  }
  _nbArticleViewer.sourcePath = path;
  _nbArticleViewer.returnFocusEl = returnFocusEl || document.activeElement;
  const titleEl = document.getElementById('nb-article-viewer-title');
  const pathEl = document.getElementById('nb-article-viewer-path');
  const frame = document.getElementById('nb-article-viewer-frame');
  if (titleEl) titleEl.textContent = title;
  if (pathEl) pathEl.textContent = path;
  if (frame) {
    frame.title = `${_tr("Lecteur PDF de l'article")} - ${title}`;
    frame.src = '/api/open-pdf?path=' + encodeURIComponent(path) + (page ? `#page=${page}` : '');
  }

  const overlay = document.getElementById('nb-article-viewer-overlay');
  const modal = document.getElementById('nb-article-viewer');
  overlay?.removeAttribute('hidden');
  modal?.removeAttribute('hidden');
  window.requestAnimationFrame(() => {
    overlay?.classList.add('open');
    modal?.classList.add('open');
  });
  document.body.classList.add('nb-article-viewer-open');
  document.addEventListener('keydown', _nbArticleViewerKeyHandler);
  document.getElementById('nb-article-viewer-close')?.focus();
}

function closeNbArticleViewer() {
  const modal = document.getElementById('nb-article-viewer');
  if (!modal?.classList.contains('open') && modal?.hasAttribute('hidden')) return;
  document.removeEventListener('keydown', _nbArticleViewerKeyHandler);
  document.getElementById('nb-article-viewer-overlay')?.classList.remove('open');
  modal?.classList.remove('open');
  document.body.classList.remove('nb-article-viewer-open');
  _nbArticleViewer.closeTimer = window.setTimeout(() => {
    document.getElementById('nb-article-viewer-overlay')?.setAttribute('hidden', '');
    modal?.setAttribute('hidden', '');
    const frame = document.getElementById('nb-article-viewer-frame');
    if (frame) frame.src = 'about:blank';
    _nbArticleViewer.closeTimer = null;
  }, 160);
  const returnFocus = _nbArticleViewer.returnFocusEl;
  _nbArticleViewer.sourcePath = '';
  _nbArticleViewer.returnFocusEl = null;
  if (returnFocus && typeof returnFocus.focus === 'function') returnFocus.focus();
}

function openNbViewerSourceInLibrary() {
  const path = _nbArticleViewer.sourcePath;
  if (!path) return;
  closeNbArticleViewer();
  goToSource(path);
}

function openNbSourceInLibrary(event) {
  event?.preventDefault();
  event?.stopPropagation();
  const path = String(event?.currentTarget?.dataset?.sourcePath || '').trim();
  if (!path) return;
  goToSource(path);
}

function _nbSourcePaperUrl(source) {
  const text = String(source?.content_preview ?? source?.content ?? '');
  const match = text.match(/https?:\/\/[^\s<>"')]+/);
  return match ? match[0].replace(/[.,;]+$/, '') : '';
}

// What the chat reads of each source: the full text of a library PDF, or only
// the text of a source typed or pasted by hand.
function _nbSourceStatusHtml(source) {
  const sourcePath = String(source?.source_path || '').trim();
  return sourcePath
    ? `<div class="nb-source-status is-pdf">${escapeHtml(_ti('notebooks.statusPdf'))}</div>`
    : `<div class="nb-source-status is-text">${escapeHtml(_ti('notebooks.statusTextOnly'))}</div>`;
}

function _nbSourceActionsHtml(source) {
  const sourcePath = String(source?.source_path || '').trim();
  const buttons = [];
  if (sourcePath) {
    buttons.push(`<button type="button" class="nb-source-item-open is-primary" data-source-path="${escapeAttr(sourcePath)}" data-source-title="${escapeAttr(source.title || sourcePath)}" ${bindBbHandler('click', function(event) { openNbArticleViewer(event); })}>${escapeHtml(_tr('Lire ici'))}</button>`);
    buttons.push(`<button type="button" class="nb-source-item-open" data-source-path="${escapeAttr(sourcePath)}" ${bindBbHandler('click', function(event) { openNbSourceInLibrary(event); })}>${escapeHtml(_tr('Bibliothèque'))}</button>`);
  }
  const paperUrl = sourcePath ? '' : _nbSourcePaperUrl(source);
  if (paperUrl) {
    buttons.push(`<a class="nb-source-item-open" href="${escapeAttr(paperUrl)}" target="_blank" rel="noopener">${escapeHtml(_ti('notebooks.viewPaper'))}</a>`);
  }
  return buttons.length ? `<div class="nb-source-item-actions">${buttons.join('')}</div>` : '';
}

function _renderNbSources() {
  const sources = _nb.data.sources || [];
  const created = new Date(_nb.data.created).toLocaleDateString('fr-FR', {day:'numeric',month:'long',year:'numeric'});
  document.getElementById('nb-ws-subtitle').textContent = `Créé le ${created}`;
  const sourceCount = document.getElementById('nb-ws-source-count');
  if (sourceCount) sourceCount.textContent = _ti('notebooks.sourceCount', { count: sources.length });
  const input = document.getElementById('nb-msg-input');
  if (input) input.placeholder = _ti('notebooks.askPlaceholder', { count: sources.length });
  document.getElementById('nb-src-count').textContent = sources.length;
  const list = document.getElementById('nb-sources-list');
  if (!sources.length) {
    list.innerHTML = `
      <div class="nb-col-empty">
        <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        <div class="nb-col-empty-title">Aucune source pour le moment</div>
        <div class="nb-col-empty-desc">Ajoutez votre première source pour commencer à bâtir votre base de connaissances.</div>
      </div>`;
    return;
  }
  list.innerHTML = sources.map((s, index) => {
    const sourcePreview = String(s?.content_preview ?? s?.content ?? '');
    const snippetText = sourcePreview.length > 120 ? sourcePreview.slice(0, 120) : sourcePreview;
    const isTruncated = Boolean(s?.content_truncated) || sourcePreview.length > snippetText.length;
    const refLabel = sourceRefLabel(s, index + 1);
    const sourceActions = _nbSourceActionsHtml(s);
    return `
    <div class="nb-source-item" data-source-label="${escapeAttr(refLabel)}">
      <div class="nb-source-item-head">
        <span class="nb-source-ref-badge">${escapeHtml(refLabel)}</span>
        <div class="nb-source-item-title">${escapeHtml(s.title)}</div>
      </div>
      ${_nbSourceStatusHtml(s)}
      <div class="nb-source-item-snippet">${escapeHtml(snippetText)}${isTruncated ? '...' : ''}</div>
      ${sourceActions}
      <button class="nb-source-item-del" ${bindBbHandler('click', function() { removeNbSource(s.id); })} title="Supprimer" aria-label="Supprimer">&times;</button>
    </div>`;
  }).join('');
  _updateNbContext();
}

function showNbSourceForm() {
  const column = document.getElementById('nb-col-sources');
  if (column?.classList.contains('collapsed')) toggleNbCol('sources');
  const form = document.getElementById('nb-add-source-form');
  const body = column?.querySelector('.nb-col-body');
  if (!form || !body) return;
  form.style.display = 'flex';
  window.requestAnimationFrame(() => {
    body.scrollTo({top: body.scrollHeight, behavior: 'smooth'});
    window.setTimeout(() => {
      if (form.style.display !== 'none') form.querySelector('#nb-source-type')?.focus();
    }, 180);
  });
}
function hideNbSourceForm() {
  document.getElementById('nb-add-source-form').style.display = 'none';
  document.getElementById('nb-source-type').value = 'text';
  document.getElementById('nb-source-pdf-row').style.display = 'none';
  document.getElementById('nb-source-title').value = '';
  document.getElementById('nb-source-content').value = '';
  document.getElementById('nb-pdf-search').value = '';
  document.getElementById('nb-pdf-results').innerHTML = '';
  document.getElementById('nb-pdf-results').classList.remove('open');
  _nb.selectedPdfPath = '';
}
function onNbSourceTypeChange() {
  const isPdf = document.getElementById('nb-source-type').value === 'pdf';
  document.getElementById('nb-source-pdf-row').style.display = isPdf ? 'flex' : 'none';
  if (!isPdf) {
    document.getElementById('nb-source-title').value = '';
    document.getElementById('nb-source-content').value = '';
    _nb.selectedPdfPath = '';
  }
}
function filterNbPdfs(query) {
  const resultsEl = document.getElementById('nb-pdf-results');
  const rows  = state.libRows  || [];
  const paths = state.libPaths || [];
  const q = query.trim().toLowerCase();
  const filtered = paths.map((p, i) => ({
    path: p,
    title:  rows[i] ? rows[i][0] : p,
    author: rows[i] ? rows[i][1] : '',
  })).filter(item =>
    !q ||
    item.title.toLowerCase().includes(q) ||
    item.author.toLowerCase().includes(q)
  ).slice(0, 30);

  if (!filtered.length) {
    resultsEl.innerHTML = '<div class="pdf-result-empty">' + _tr('Aucun résultat') + '</div>';
  } else {
    resultsEl.innerHTML = filtered.map(item => `
      <div class="pdf-result-item" data-path="${escapeAttr(item.path)}" data-title="${escapeAttr(item.title)}">
        <div class="pdf-result-title">${escapeHtml(item.title)}</div>
        ${item.author && item.author !== '-' ? `<div class="pdf-result-author">${escapeHtml(item.author)}</div>` : ''}
      </div>`).join('');
    resultsEl.querySelectorAll('.pdf-result-item').forEach(el => {
      el.addEventListener('mousedown', e => {
        e.preventDefault(); // empêche le blur du champ de recherche
        selectNbPdf(el.dataset.path, el.dataset.title);
      });
    });
  }
  resultsEl.classList.add('open');
}
async function selectNbPdf(path, title) {
  _nb.selectedPdfPath = path || '';
  document.getElementById('nb-pdf-search').value = title;
  document.getElementById('nb-pdf-results').classList.remove('open');
  document.getElementById('nb-source-title').value = title;
  const contentEl = document.getElementById('nb-source-content');
  contentEl.value = 'Extraction en cours…';
  try {
    const r = await fetch('/api/extract-pdf-text?path=' + encodeURIComponent(path));
    const d = await r.json();
    contentEl.value = d.error ? ('Erreur : ' + d.error) : d.text;
  } catch(e) { contentEl.value = 'Erreur extraction du document'; }
}
// Close PDF results when clicking outside
document.addEventListener('click', e => {
  if (!e.target.closest('.pdf-search-wrap')) {
    const el = document.getElementById('nb-pdf-results');
    if (el) el.classList.remove('open');
  }
});
async function submitNbSource() {
  const title   = document.getElementById('nb-source-title').value.trim();
  const content = document.getElementById('nb-source-content').value.trim();
  const sourceType = document.getElementById('nb-source-type')?.value || 'text';
  const source_path = sourceType === 'pdf' ? (_nb.selectedPdfPath || '') : '';
  if (!title)   { showToast(_tr('Titre requis')); return; }
  if (!content) { showToast(_tr('Contenu requis')); return; }
  try {
    const r = await fetch(`/api/notebooks/${_nb.id}/sources`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title, content, source_path}),
    });
    const d = await r.json();
    _nb.data.sources.push(d.source);
    _renderNbSources();
    hideNbSourceForm();
  } catch(e) { showToast(_tr('Erreur ajout source')); }
}
async function removeNbSource(srcId) {
  try {
    const r = await fetch(`/api/notebooks/${_nb.id}/sources/${srcId}`, { method: 'DELETE' });
    if (!r.ok) {
      showToast(_tr(`Suppression impossible (${r.status}).`), 3200, 'error');
      return;
    }
    _nb.data.sources = _nb.data.sources.filter(s => s.id !== srcId);
    _renderNbSources();
  } catch (e) {
    showToast(_tr(`Erreur réseau: ${e?.message || 'suppression impossible'}.`), 3200, 'error');
  }
}

// N
function onNbNotesInput() {
  clearTimeout(_nb.noteTimer);
  _nb.noteTimer = setTimeout(async () => {
    if (!_nb.id) return;
    const notes = document.getElementById('nb-notes-input').value;
    await fetch(`/api/notebooks/${_nb.id}/notes`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({notes}),
    });
  }, 800);
}

// C
function _updateNbContext() {
  const el = document.getElementById('nb-input-context');
  if (!el) return;
  const n     = (_nb.data && _nb.data.sources) ? _nb.data.sources.length : 0;
  const srcTxt = n === 0 ? 'Aucune source' : `${n} source${n > 1 ? 's' : ''}`;
  el.innerHTML =
    `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="opacity:0.5;flex-shrink:0"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
    ${escapeHtml(srcTxt)}`;
  // The model shows on the chip beside the send button.
}

// C
function _nbSendIconHtml() {
  return `
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <line x1="22" y1="2" x2="11" y2="13"></line>
      <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
    </svg>
  `;
}

function _nbStopIconHtml() {
  return `
    <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="2"></rect>
    </svg>
  `;
}

function _setNbSendButtonIdle() {
  const btn = document.getElementById('nb-send-btn');
  if (!btn) return;
  btn.disabled = false;
  btn.classList.remove('is-stop');
  btn.innerHTML = _nbSendIconHtml();
  btn.title = _tr('Envoyer');
}

function _setNbSendButtonStop() {
  const btn = document.getElementById('nb-send-btn');
  if (!btn) return;
  btn.disabled = false;
  btn.classList.add('is-stop');
  btn.innerHTML = _nbStopIconHtml();
  btn.title = _tr('Stop');
}

function _nbMessageRequestsNotesUpdate(message) {
  const text = String(message || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
  if (!text) return false;
  const mentionsNotes = /\bnotes?\b/.test(text)
    || text.includes('dans les notes')
    || text.includes('aux notes')
    || text.includes('notebook notes');
  if (!mentionsNotes) return false;
  return /\b(ajoute|ajoutes|ajouter|ajoutez|rajoute|rajoutes|rajouter|rajoutez|mets|mettre|inscris|inscrire|enregistre|enregistrer|sauvegarde|sauvegarder|garde|garder|note|noter|add|save|append|write|keep)\b/.test(text);
}

function _formatNotebookChatNoteBlock(userMessage, assistantAnswer) {
  let answer = stripThinkingMarkup(String(assistantAnswer || '')).trim();
  if (!answer) return '';
  const noteHeading = /(^|\n)\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:notes?\s+(?:pour|du|de)\s+le\s+carnet|note\s+(?:pour|du|de)\s+le\s+carnet|notes?\s*:|note\s*:|a\s+ajouter\s+(?:aux|dans\s+les)\s+notes?)\s*:?\s*(?:\*\*)?\s*(?:\n|$)/gi;
  const matches = Array.from(answer.matchAll(noteHeading));
  if (matches.length) {
    const last = matches[matches.length - 1];
    answer = answer.slice((last.index || 0) + String(last[0] || '').length).trim();
  }
  answer = answer
    .replace(/^\s*[-–—]*\s*notes?\s+du\s+carnet\s*\(ajout\)\s*:?\s*/i, '')
    .replace(/^\s*[-–—]*\s*note\s+pour\s+le\s+carnet\s*:?\s*/i, '')
    .replace(/^\s*\*?remarque\*?\s*:\s*/i, '')
    .trim();
  return answer;
}

async function appendNotebookAnswerToNotesFromClient(userMessage, assistantAnswer, silent = false) {
  if (!_nb.id) return false;
  const noteBlock = _formatNotebookChatNoteBlock(userMessage, assistantAnswer);
  if (!noteBlock) return false;
  try {
    const response = await fetch('/api/notebooks/' + encodeURIComponent(_nb.id), { cache: 'no-store' });
    if (!response.ok) return false;
    const data = await response.json();
    const currentNotes = String(data?.notes || '').trim();
    const answer = _formatNotebookChatNoteBlock(userMessage, assistantAnswer);
    const alreadySaved = answer && currentNotes.includes(answer);
    const nextNotes = alreadySaved
      ? currentNotes
      : (currentNotes ? `${currentNotes}\n\n---\n\n${noteBlock}` : noteBlock);
    if (!alreadySaved) {
      const updateResponse = await fetch(`/api/notebooks/${_nb.id}/notes`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({notes: nextNotes}),
      });
      if (!updateResponse.ok) return false;
    }
    if (_nb.data) _nb.data.notes = nextNotes;
    clearTimeout(_nb.noteTimer);
    const notesInput = document.getElementById('nb-notes-input');
    if (notesInput) notesInput.value = nextNotes;
    if (!silent) showToast(_tr('Notes du carnet mises a jour.'), 2200, 'success');
    return true;
  } catch (e) {
    return false;
  }
}

function _upsertNbDeleteButton(bubble, messageIndex) {
  if (!bubble) return;
  const idx = Number(messageIndex);
  if (!Number.isInteger(idx) || idx < 0) return;
  bubble.dataset.nbMsgIndex = String(idx);
  let button = bubble.querySelector('.nb-bubble-delete-btn');
  if (!button) {
    button = document.createElement('button');
    button.type = 'button';
    button.className = 'nb-bubble-delete-btn';
    button.textContent = _tr('Supprimer');
    button.setAttribute('aria-label', _tr('Supprimer le message'));
    button.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const currentIndex = Number(bubble.dataset.nbMsgIndex);
      if (!Number.isInteger(currentIndex) || currentIndex < 0) return;
      await deleteNbMessage(currentIndex);
    });
    bubble.appendChild(button);
  }
}

async function stopCurrentNotebookChat(silent = false) {
  if (!_nb.streaming) return;
  const requestId = String(_nb.activeStreamRequestId || '').trim();
  const model = String(_nb.activeStreamModel || optionalSelectedModel('notebook') || '').trim();
  const abortPromise = requestChatStreamAbort(requestId, model, 'notebook_stop');
  const ctl = _nb.chatAbortController;
  if (abortPromise) {
    await Promise.race([
      abortPromise,
      new Promise((resolve) => window.setTimeout(() => resolve(null), 1200)),
    ]);
  }
  if (ctl) {
    try { ctl.abort(); } catch {}
  }
  _nb.chatAbortController = null;
  _nb.activeStreamRequestId = '';
  _nb.activeStreamModel = '';
  _nb.streaming = false;
  _setNbSendButtonIdle();
  if (!silent) showToast(_tr('Generation interrompue.'));
}

const NB_CHAT_SUGGESTIONS = [
  'notebooks.suggestion.disagree',
  'notebooks.suggestion.mainIdeas',
  'notebooks.suggestion.gaps',
];

function useNbChatSuggestion(key) {
  const input = document.getElementById('nb-msg-input');
  if (!input) return;
  input.value = _ti(key);
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function _renderNbMessages() {
  const msgs = document.getElementById('nb-messages');
  const messages = _nb.data.messages || [];
  if (!messages.length) {
    const tr = (key, fallback) => (typeof window.t === 'function' ? window.t(key) : fallback);
    msgs.innerHTML = `
      <div id="nb-empty-chat">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
        <strong data-i18n="notebooks.startConversation">${escapeHtml(tr('notebooks.startConversation', 'Commencer une conversation'))}</strong>
        <p data-i18n="notebooks.askQuestions">${escapeHtml(tr('notebooks.askQuestions', 'Posez des questions pour mieux comprendre le contenu de vos sources.'))}</p>
        <div class="nb-suggestions">
          ${NB_CHAT_SUGGESTIONS.map((key) => `<button type="button" class="nb-suggestion" ${bindBbHandler('click', function() { useNbChatSuggestion(key); })}>${escapeHtml(_ti(key))}</button>`).join('')}
        </div>
      </div>`;
    return;
  }
  msgs.innerHTML = messages.map((m, idx) => {
    if (m.role === 'user') {
      const content = escapeHtml(m.content).replace(/\n/g, '<br>');
      return `<div class="nb-msg-wrap user"><div class="nb-bubble user" data-i18n-skip data-nb-msg-index="${idx}" data-nb-msg-role="user">${content}</div></div>`;
    }
    return `<div class="nb-msg-wrap assistant"><div class="nb-bubble assistant" data-nb-msg-index="${idx}" data-nb-msg-role="assistant"></div></div>`;
  }).join('');
  messages.forEach((m, idx) => {
    const el = msgs.querySelector(`.nb-bubble[data-nb-msg-index="${idx}"]`);
    if (!el) return;
    if (m.role === 'user') {
      el.dataset.copyText = String(m.content || '');
      _upsertChatCopyButton(el);
      _upsertNbDeleteButton(el, idx);
      return;
    }
    if (m.role !== 'assistant') return;
    renderRichAssistantOutput(el, m.content || '', {
      normalizeCitations: true,
      sources: _nb.data.sources || [],
      hideSourcesSection: true,
    });
    _upsertNbDeleteButton(el, idx);
  });
  msgs.scrollTop = msgs.scrollHeight;
}

async function sendNbMessage() {
  if (_nb.streaming) {
    await stopCurrentNotebookChat();
    return;
  }
  // Valide une éventuelle suppression de message en attente : son index
  // deviendrait faux après l'ajout d'un nouveau message.
  flushPendingUndo('nb-message');
  const input = document.getElementById('nb-msg-input');
  const msg = input.value.trim();
  const model = requireSelectedModel('notebook');
  if (!msg || !_nb.id) return;
  if (!model) return;
  input.value = ''; input.style.height = 'auto';

  const msgs = document.getElementById('nb-messages');
  const empty = document.getElementById('nb-empty-chat');
  if (empty) empty.remove();

  // User bubble
  const uwrap = document.createElement('div'); uwrap.className = 'nb-msg-wrap user';
  const ub = document.createElement('div'); ub.className = 'nb-bubble user';
  ub.setAttribute('data-i18n-skip', '');
  ub.innerHTML = escapeHtml(msg).replace(/\n/g, '<br>');
  ub.dataset.copyText = String(msg || '');
  _upsertChatCopyButton(ub);
  uwrap.appendChild(ub); msgs.appendChild(uwrap);

  // Bot bubble
  const bwrap = document.createElement('div'); bwrap.className = 'nb-msg-wrap assistant';
  const bb = document.createElement('div'); bb.className = 'nb-bubble status';
  bb.innerHTML = '<span class="spinner"></span><span class="status-wait-text">' + _tr('Recherche…') + '</span>';
  bwrap.appendChild(bb); msgs.appendChild(bwrap);
  msgs.scrollTop = msgs.scrollHeight;

  const controller = new AbortController();
  _nb.streaming = true;
  _nb.chatAbortController = controller;
  _nb.activeStreamRequestId = '';
  _nb.activeStreamModel = model;
  _setNbSendButtonStop();
  let fullText = '';
  let lastStreamRenderAt = 0;
  let scrollQueued = false;
  let hadError = false;
  let interrupted = false;
  let assistantText = '';
  let notesUpdated = false;
  const shouldRefreshNotesAfterStream = _nbMessageRequestsNotesUpdate(msg);
  const debugTracker = isDebugMetricsEnabled() ? createDebugMetricsTracker({ model }) : null;
  const scheduleScroll = () => {
    if (scrollQueued) return;
    scrollQueued = true;
    window.requestAnimationFrame(() => {
      scrollQueued = false;
      msgs.scrollTop = msgs.scrollHeight;
    });
  };

  try {
    await syncOllamaBaseUrl({ silent: true });
    const response = await fetch(`/api/notebooks/${_nb.id}/chat`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        message: msg,
        model,
        append_to_notes: shouldRefreshNotesAfterStream,
        thinking: isThinkingEnabled(),
        force_thinking_small_models: isForceThinkingSmallModelsEnabled(),
      }),
      signal: controller.signal,
    });
    for await (const event of readSSE(response)) {
      if (event.type === 'stream_meta') {
        _nb.activeStreamRequestId = String(event.request_id || '');
      } else if (event.type === 'status') {
        bb.style.whiteSpace = '';
        bb.className = 'nb-bubble status';
        bb.innerHTML = `<span class="spinner"></span><span class="status-wait-text">${escapeHtml(event.msg || _tr('Analyse en cours…'))}</span>`;
        scheduleScroll();
      } else if (event.type === 'token') {
        const piece = event.text || '';
        if (!piece) continue;
        fullText += piece;
        assistantText = fullText;
        debugTracker?.update(piece);
        bb.className = 'nb-bubble assistant';
        // Throttle : le rendu final repasse toujours derrière (fin de stream).
        const nowTs = performance.now();
        if (nowTs - lastStreamRenderAt >= streamRenderIntervalMs()) {
          lastStreamRenderAt = nowTs;
          renderRichAssistantOutput(bb, fullText, {
            streaming: true,
            appendCursor: true,
            normalizeCitations: false,
            sources: _nb.data.sources || [],
            hideSourcesSection: true,
            emptyText: 'Réflexion en cours…',
            debugMetrics: debugTracker?.snapshot(),
          });
          scheduleScroll();
        }
      } else if (event.type === 'error') {
        hadError = true;
        assistantText = String(event.msg || 'Erreur');
        bb.style.whiteSpace = '';
        bb.className = 'nb-bubble assistant';
        bb.dataset.copyText = assistantText;
        bb.innerHTML = `<span style="color:var(--red)">&#9888; ${escapeHtml(event.msg)}</span>`;
        _upsertChatCopyButton(bb);
      } else if (event.type === 'notes_updated') {
        const notes = String(event.notes || '');
        if (_nb.data) _nb.data.notes = notes;
        notesUpdated = true;
        clearTimeout(_nb.noteTimer);
        const notesInput = document.getElementById('nb-notes-input');
        if (notesInput) notesInput.value = notes;
        showToast(_tr(event.msg || 'Notes du carnet mises a jour.'), 2400, 'success');
      }
    }
  } catch(e) {
    bb.style.whiteSpace = '';
    if (controller.signal.aborted) {
      interrupted = true;
      assistantText = fullText
        ? `${fullText}\n\n_Réponse interrompue._`
        : 'Generation interrompue.';
      if (fullText) {
        bb.className = 'nb-bubble assistant';
        renderRichAssistantOutput(bb, assistantText, {
          normalizeCitations: true,
          sources: _nb.data.sources || [],
          hideSourcesSection: true,
          debugMetrics: debugTracker?.snapshot(),
        });
      } else {
        bb.className = 'nb-bubble assistant';
        bb.dataset.copyText = assistantText;
        bb.innerHTML = '<span style="color:var(--muted)">' + _tr('Generation interrompue.') + '</span>';
        _upsertChatCopyButton(bb);
      }
    } else {
      hadError = true;
      assistantText = `Erreur : ${e.message}`;
      bb.className = 'nb-bubble assistant';
      bb.dataset.copyText = assistantText;
      bb.innerHTML = `<span style="color:var(--red)">Erreur : ${escapeHtml(e.message)}</span>`;
      _upsertChatCopyButton(bb);
    }
  } finally {
    _nb.chatAbortController = null;
    _nb.activeStreamRequestId = '';
    _nb.activeStreamModel = '';
    _nb.streaming = false;
    _setNbSendButtonIdle();
  }
  if (!hadError && !interrupted) {
    assistantText = fullText || '...';
    bb.className = 'nb-bubble assistant';
    renderRichAssistantOutput(bb, assistantText, {
      normalizeCitations: true,
      sources: _nb.data.sources || [],
      hideSourcesSection: true,
      debugMetrics: debugTracker?.snapshot(),
    });
  }
  if (!_nb.data.messages) _nb.data.messages = [];
  const userIndex = _nb.data.messages.length;
  _nb.data.messages.push({role: 'user', content: msg});
  ub.dataset.nbMsgIndex = String(userIndex);
  ub.dataset.nbMsgRole = 'user';
  _upsertNbDeleteButton(ub, userIndex);
  if (assistantText) {
    const assistantIndex = _nb.data.messages.length;
    _nb.data.messages.push({role: 'assistant', content: assistantText});
    bb.dataset.nbMsgIndex = String(assistantIndex);
    bb.dataset.nbMsgRole = 'assistant';
    _upsertNbDeleteButton(bb, assistantIndex);
  }
  if (shouldRefreshNotesAfterStream && !notesUpdated && !hadError && !interrupted) {
    await appendNotebookAnswerToNotesFromClient(msg, assistantText, false);
  }
}

async function runNbThematicSynthesis() {
  if (_nb.streaming || !_nb.id) return;
  const input = document.getElementById('nb-msg-input');
  if (!input) return;
  input.value = _ti('notebooks.thematicSynthesisPrompt');
  await sendNbMessage();
}

async function deleteNbMessage(messageIndex) {
  if (!_nb.id) return;
  if (_nb.streaming) {
    showToast(_tr('Attendez la fin de la generation avant de supprimer un message.'), 3200, 'info');
    return;
  }
  const idx = Number(messageIndex);
  if (!Number.isInteger(idx) || idx < 0) return;
  if (!Array.isArray(_nb.data?.messages) || idx >= _nb.data.messages.length) return;
  const nbId = _nb.id;
  const removed = _nb.data.messages[idx];
  _nb.data.messages.splice(idx, 1);
  _renderNbMessages();
  deleteWithUndo('nb-message', {
    message: 'Message supprimé.',
    restore: () => {
      if (_nb.id === nbId && Array.isArray(_nb.data?.messages)) {
        _nb.data.messages.splice(Math.min(idx, _nb.data.messages.length), 0, removed);
        _renderNbMessages();
      }
    },
    commit: async () => {
      try {
        const r = await fetch(`/api/notebooks/${nbId}/messages/${idx}`, { method: 'DELETE', keepalive: true });
        const d = await r.json();
        if (!d?.ok) showToast(_tr('Suppression impossible.'), 3000, 'error');
      } catch (e) {
        showToast(_tr('Erreur suppression message.'), 3000, 'error');
      }
    },
  });
}

async function clearNbChat() {
  if (!_nb.id) return;
  if (_nb.streaming) await stopCurrentNotebookChat(true);
  flushPendingUndo('nb-message');
  if (!confirm(_tr('Effacer toute la conversation de ce carnet ?'))) return;
  await fetch(`/api/notebooks/${_nb.id}/messages`, {method: 'DELETE'});
  _nb.data.messages = [];
  _renderNbMessages();
  _setNbSendButtonIdle();
}

function toggleNbCol(col) {
  document.getElementById('nb-col-' + col).classList.toggle('collapsed');
  window.requestAnimationFrame(refreshLayoutResizers);
}

// The creation dialog: the mode cards, and Escape resets the form.
document.addEventListener('click', (event) => {
  const card = event.target.closest?.('[data-nb-create-mode]');
  if (card) selectNbCreateMode(card.dataset.nbCreateMode);
  const more = document.getElementById('nb-ws-more');
  if (more?.open && (!event.composedPath().includes(more) || event.target.closest?.('.nb-menu-item'))) more.removeAttribute('open');
});
document.getElementById('nb-create-form')?.addEventListener('cancel', (event) => {
  event.preventDefault();
  hideNbCreateForm();
});
