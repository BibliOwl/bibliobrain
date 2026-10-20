function isExternalDiscoveryEnabled() {
  return Boolean(document.getElementById('allow-external-discovery')?.checked);
}

function setDiscoveryStatus(msg, loading = false, actionHtml = '') {
  const status = document.getElementById('discovery-status');
  if (!status) return;
  delete status.dataset.privacyNotice;
  const text = _tr(String(msg || ''));
  status.style.display = text ? '' : 'none';
  status.classList.toggle('is-loading', Boolean(loading));
  status.innerHTML = `${escapeHtml(text).replace(/\n/g, '<br>')}${actionHtml || ''}`;
}

function setDiscoveryResultsMeta(count = 0) {
  const normalizedCount = Math.max(0, Number(count) || 0);
  const countEl = document.getElementById('discovery-results-count');
  if (countEl) {
    countEl.textContent = _ti('discovery.paperCount', { count: normalizedCount });
    countEl.dataset.i18nCount = String(normalizedCount);
  }
  // No head over the welcome or the offline card.
  const head = document.getElementById('discovery-results-head');
  if (head) head.hidden = normalizedCount === 0;
}

function discoveryEmptyStateMarkup(title, body, options = {}) {
  const loading = Boolean(options.loading);
  if (!loading) {
    return `
    <div class="empty-state discovery-empty-state">
      <h3 class="empty-state-title">${escapeHtml(title)}</h3>
      <p class="empty-state-text">${escapeHtml(body)}</p>
      ${options.action === false ? '' : `<button type="button" class="btn btn-primary empty-state-action" data-discovery-edit-search>${escapeHtml(_ti('discovery.editSearch'))}</button>`}
    </div>
  `;
  }
  return `
    <div class="empty-state discovery-empty-state is-loading">
      <div class="discovery-empty-icon" aria-hidden="true"><span class="spinner"></span></div>
      <h3 class="empty-state-title">${escapeHtml(title)}</h3>
      <p class="empty-state-text">${escapeHtml(body)}</p>
    </div>
  `;
}

function focusDiscoveryQuery() {
  const input = document.getElementById('discovery-input');
  if (!input) return;
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

function renderDiscoveryEmptyState(title, body, options = {}) {
  if (typeof resetDiscoveryRefinement === 'function') resetDiscoveryRefinement();
  const root = document.getElementById('discovery-results');
  if (root) {
    root.innerHTML = discoveryEmptyStateMarkup(title, body, options);
    root.querySelector('[data-discovery-edit-search]')?.addEventListener('click', focusDiscoveryQuery);
  }
  setDiscoveryResultsMeta(Number(options.count || 0));
}

function openExternalDiscoverySettings() {
  openExternalApiSettings();
}

function stopDiscoveryProgressMessages() {
  if (state.discoveryProgressTimer) {
    clearInterval(state.discoveryProgressTimer);
    state.discoveryProgressTimer = null;
  }
}

function startDiscoveryProgressMessages(mode) {
  stopDiscoveryProgressMessages();
  const resultRoot = document.getElementById('discovery-results');
  state.discoveryLiveMessage = mode === 'notebook'
    ? _tr('Synthèse des articles et des notes du carnet…')
    : _tr('Analyse de votre demande de recherche…');
  const startedAt = Date.now();
  state.discoveryStartedAt = startedAt;
  const renderStep = () => {
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
    setDiscoveryStatus('', false);
    if (resultRoot) renderDiscoveryEmptyState(
      _tr('Recherche en cours'),
      `${state.discoveryLiveMessage} (${elapsed} s)`,
      { loading: true },
    );
  };
  renderStep();
  state.discoveryProgressTimer = setInterval(renderStep, 1000);
}

function _setDiscoveryRunButtonIdle() {
  const btn = document.getElementById('discovery-run-btn');
  if (!btn) return;
  btn.disabled = false;
  btn.classList.remove('is-stop');
  btn.innerHTML = `
    <span class="discovery-run-icon" aria-hidden="true">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
        <circle cx="11" cy="11" r="7"></circle>
        <line x1="20" y1="20" x2="16.5" y2="16.5"></line>
      </svg>
    </span>
    <span>${escapeHtml(_ti(_discoverySearchAgainId() ? 'discovery.searchAgain' : 'discovery.run'))}</span>
  `;
}

function _setDiscoveryRunButtonStop() {
  const btn = document.getElementById('discovery-run-btn');
  if (!btn) return;
  btn.disabled = false;
  btn.classList.add('is-stop');
  btn.innerHTML = `
    <span class="discovery-run-icon" aria-hidden="true"></span>
    <span>${escapeHtml(_tr('Arrêter la recherche'))}</span>
  `;
}

async function stopArticleDiscovery() {
  const ctl = state.discoveryAbortController;
  const model = state.discoveryActiveModel || optionalSelectedModel('discovery') || '';
  stopDiscoveryProgressMessages();
  if (ctl) ctl.abort();
  state.discoveryAbortController = null;
  state.discoveryLoading = false;
  _setDiscoveryRunButtonIdle();
  setDiscoveryStatus('Recherche interrompue, arret du modele local…');
  renderDiscoveryEmptyState(
    _tr('Recherche interrompue'),
    _tr('Vous pouvez modifier la requête puis relancer la recherche.'),
  );
  if (!model) {
    setDiscoveryStatus('Recherche interrompue.');
    return;
  }
  try {
    const r = await fetch('/api/article-discovery/abort', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ model }),
    });
    const d = await r.json();
    setDiscoveryStatus(d?.model_unloaded ? 'Recherche interrompue. Modele Ollama decharge.' : 'Recherche interrompue.');
  } catch(e) {
    setDiscoveryStatus('Recherche interrompue. Arret Ollama non confirme.');
  }
}

function _discoveryShowsResults() {
  return Boolean(document.querySelector('#discovery-results .discovery-card'));
}

// With online search off, the results area explains it and offers the switch;
// otherwise an empty results area shows the welcome with example searches.
function updateDiscoveryPrivacyUi() {
  const enabled = isExternalDiscoveryEnabled();
  const status = document.getElementById('discovery-status');
  if (status?.dataset.privacyNotice) {
    delete status.dataset.privacyNotice;
    setDiscoveryStatus('');
  }
  if (state.discoveryLoading || _discoveryShowsResults()) return;
  if (!enabled) renderDiscoveryOfflineCard();
  else if (document.querySelector('#discovery-results .discovery-offline-card') || !document.querySelector('#discovery-results > *')) {
    renderDiscoveryWelcome();
  }
}

function renderDiscoveryOfflineCard() {
  if (typeof resetDiscoveryRefinement === 'function') resetDiscoveryRefinement();
  const root = document.getElementById('discovery-results');
  if (!root) return;
  setDiscoveryResultsMeta(0);
  _setDiscoveryResultsNote('');
  root.innerHTML = `
    <div class="discovery-offline-card">
      <div class="discovery-offline-icon" aria-hidden="true">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
      </div>
      <strong>${escapeHtml(_ti('discovery.offlineTitle'))}</strong>
      <p>${escapeHtml(_ti('discovery.offlineBody'))}</p>
      <div class="discovery-offline-actions">
        <button type="button" class="btn btn-indigo" ${bindBbHandler('click', function() { enableExternalDiscovery(); })}>${escapeHtml(_ti('discovery.offlineAllow'))}</button>
        <button type="button" class="discovery-link" ${bindBbHandler('click', function() { openExternalDiscoverySettings(); })}>${escapeHtml(_ti('discovery.offlineLearnMore'))}</button>
      </div>
    </div>`;
}

// The same switch as Settings › Online: its change handler saves the setting.
function enableExternalDiscovery() {
  const toggle = document.getElementById('allow-external-discovery');
  if (!toggle) return;
  toggle.checked = true;
  toggle.dispatchEvent(new Event('change', { bubbles: true }));
  updateDiscoveryPrivacyUi();
  document.getElementById('discovery-input')?.focus();
}

const DISCOVERY_EXAMPLES = [
  { key: 'topic', mode: 'search' },
  { key: 'article', mode: 'search' },
  { key: 'notebook', mode: 'notebook' },
];

function renderDiscoveryWelcome() {
  if (typeof resetDiscoveryRefinement === 'function') resetDiscoveryRefinement();
  const root = document.getElementById('discovery-results');
  if (!root) return;
  setDiscoveryResultsMeta(0);
  _setDiscoveryResultsNote('');
  root.innerHTML = `
    <div class="discovery-welcome">
      <strong>${escapeHtml(_ti('discovery.welcomeTitle'))}</strong>
      <p>${escapeHtml(_ti('discovery.welcomeBody'))}</p>
      <div class="discovery-examples">
        ${DISCOVERY_EXAMPLES.map(({ key }) => `
          <button type="button" class="discovery-example" ${bindBbHandler('click', function() { useDiscoveryExample(key); })}>
            <span class="discovery-example-kind">${escapeHtml(_ti(`discovery.example.${key}Kind`))}</span>
            <span class="discovery-example-text">${escapeHtml(_ti(`discovery.example.${key}`))}</span>
          </button>`).join('')}
      </div>
    </div>`;
}

function useDiscoveryExample(key) {
  const example = DISCOVERY_EXAMPLES.find(item => item.key === key);
  if (!example) return;
  selectDiscoveryMode(example.mode);
  const input = document.getElementById('discovery-input');
  if (example.mode === 'notebook') {
    document.getElementById('discovery-notebook-trigger')?.focus();
    return;
  }
  if (input) {
    input.value = _ti(`discovery.example.${key}Prompt`);
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

function _setDiscoveryResultsNote(text) {
  const note = document.getElementById('discovery-results-note');
  if (note) note.textContent = text || '';
}

// The two starting points are a segmented radio group driving the hidden
// <select>, which the rest of the code reads.
function syncDiscoveryModePicker() {
  const mode = document.getElementById('discovery-mode')?.value === 'notebook' ? 'notebook' : 'search';
  document.querySelectorAll('#discovery-mode-switch [data-discovery-mode]').forEach((option) => {
    const selected = option.dataset.discoveryMode === mode;
    option.classList.toggle('is-selected', selected);
    option.setAttribute('aria-checked', selected ? 'true' : 'false');
    option.tabIndex = selected ? 0 : -1;
  });
  const input = document.getElementById('discovery-input');
  const placeholderKey = mode === 'notebook' ? 'discovery.queryPlaceholderNotebook' : 'discovery.queryPlaceholder';
  if (input && input.dataset.i18nPlaceholder !== placeholderKey) {
    input.dataset.i18nPlaceholder = placeholderKey;
    input.placeholder = _ti(placeholderKey);
  }
  const hint = document.getElementById('discovery-query-hint');
  const hintKey = mode === 'notebook' ? 'discovery.queryHintNotebook' : 'discovery.queryHint';
  if (hint && hint.dataset.i18n !== hintKey) {
    hint.dataset.i18n = hintKey;
    hint.textContent = _ti(hintKey);
  }
}

function selectDiscoveryMode(mode, { focus = false } = {}) {
  const select = document.getElementById('discovery-mode');
  if (!select || !['search', 'notebook'].includes(mode)) return;
  select.value = mode;
  onDiscoveryModeChange();
  if (focus) document.querySelector(`#discovery-mode-switch [data-discovery-mode="${mode}"]`)?.focus();
}

function onDiscoveryModeChange() {
  const mode = document.getElementById('discovery-mode')?.value || 'search';
  const wrap = document.getElementById('discovery-notebook-wrap');
  if (wrap) wrap.hidden = mode !== 'notebook';
  syncDiscoveryModePicker();
  if (mode === 'notebook') loadDiscoveryNotebookOptions();
  updateDiscoveryNotebookContext();
  syncDiscoveryRunLabel();
}

document.addEventListener('click', (event) => {
  const target = event?.target;
  const notebookPicker = document.getElementById('discovery-notebook-picker');
  if (!notebookPicker || !(target instanceof Node && notebookPicker.contains(target))) closeDiscoveryNotebookMenu();
});

document.addEventListener('keydown', (event) => {
  // Arrow keys move between the two starting points, as in any radio group.
  const option = event.target instanceof Element ? event.target.closest('#discovery-mode-switch [data-discovery-mode]') : null;
  if (option && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
    event.preventDefault();
    selectDiscoveryMode(option.dataset.discoveryMode === 'search' ? 'notebook' : 'search', { focus: true });
    return;
  }
  if (event.key !== 'Escape') return;
  const notebookMenu = document.getElementById('discovery-notebook-menu');
  if (notebookMenu && !notebookMenu.hidden) {
    event.preventDefault();
    closeDiscoveryNotebookMenu({ restoreFocus: true });
  }
});

function syncDiscoveryNotebookPicker() {
  const select = document.getElementById('discovery-notebook-select');
  const value = document.getElementById('discovery-notebook-value');
  const menu = document.getElementById('discovery-notebook-menu');
  const selectedId = String(select?.value || '');
  const selectedText = select?.options?.[select.selectedIndex]?.textContent?.trim() || _tr('Choisir un carnet');
  if (value) value.textContent = selectedText;
  menu?.querySelectorAll('[data-discovery-notebook]').forEach((option) => {
    const selected = option.dataset.discoveryNotebook === selectedId;
    option.classList.toggle('is-selected', selected);
    option.setAttribute('aria-checked', selected ? 'true' : 'false');
  });
}

function closeDiscoveryNotebookMenu({ restoreFocus = false } = {}) {
  const picker = document.getElementById('discovery-notebook-picker');
  const trigger = document.getElementById('discovery-notebook-trigger');
  const menu = document.getElementById('discovery-notebook-menu');
  if (menu) menu.hidden = true;
  picker?.classList.remove('is-open');
  if (trigger) {
    trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus) trigger.focus();
  }
}

function toggleDiscoveryNotebookMenu(event) {
  event?.stopPropagation?.();
  const picker = document.getElementById('discovery-notebook-picker');
  const trigger = document.getElementById('discovery-notebook-trigger');
  const menu = document.getElementById('discovery-notebook-menu');
  if (!picker || !trigger || !menu) return;
  const open = menu.hidden;
  menu.hidden = !open;
  picker.classList.toggle('is-open', open);
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) window.setTimeout(() => menu.querySelector('.is-selected, .discovery-mode-option')?.focus(), 0);
}

function selectDiscoveryNotebook(notebookId) {
  const select = document.getElementById('discovery-notebook-select');
  if (!select) return;
  select.value = String(notebookId || '');
  onDiscoveryNotebookChange();
  closeDiscoveryNotebookMenu({ restoreFocus: true });
}

function onDiscoveryNotebookChange() {
  const select = document.getElementById('discovery-notebook-select');
  const id = String(select?.value || '');
  const title = select?.options?.[select.selectedIndex]?.textContent?.trim() || '';
  state.discoveryNotebookContext = id ? { id, title } : null;
  syncDiscoveryNotebookPicker();
  updateDiscoveryNotebookContext();
}

function updateDiscoveryNotebookContext(profile = null) {
  const root = document.getElementById('discovery-notebook-context');
  if (!root) return;
  const mode = document.getElementById('discovery-mode')?.value || 'search';
  const context = profile || state.discoveryNotebookContext;
  const visible = mode === 'notebook' && Boolean(context?.id);
  root.hidden = !visible;
  if (!visible) return;
  const title = document.getElementById('discovery-notebook-context-title');
  const summary = document.getElementById('discovery-notebook-context-summary');
  if (title) title.textContent = `Recherche liée à « ${context.title || _tr('Carnet sans titre')} »`;
  if (summary) summary.textContent = context.summary
    || _tr('Les articles et les notes du carnet seront résumés pour guider la recherche.');
}

function clearDiscoveryNotebookContext() {
  const select = document.getElementById('discovery-notebook-select');
  if (select) select.value = '';
  state.discoveryNotebookContext = null;
  syncDiscoveryNotebookPicker();
  updateDiscoveryNotebookContext();
}

async function loadDiscoveryNotebookOptions() {
  const sel = document.getElementById('discovery-notebook-select');
  const menu = document.getElementById('discovery-notebook-menu');
  if (!sel || !menu) return;
  if (state.discoveryNotebooksLoaded) {
    syncDiscoveryNotebookPicker();
    return;
  }
  try {
    const r = await fetch('/api/notebooks');
    const d = await r.json();
    const rows = Array.isArray(d?.notebooks) ? d.notebooks : [];
    sel.innerHTML = '<option value="">Choisir un carnet</option>'
      + rows.map(nb => `<option value="${escapeAttr(nb.id)}">${escapeHtml(nb.title || _tr('Sans titre'))}</option>`).join('');
    menu.innerHTML = rows.length
      ? rows.map(nb => `<button type="button" class="discovery-mode-option" role="menuitemradio" aria-checked="false" data-discovery-notebook="${escapeAttr(nb.id)}" ${bindBbHandler('click', function() { selectDiscoveryNotebook(nb.id); })}><span>${escapeHtml(nb.title || _tr('Sans titre'))}</span></button>`).join('')
      : `<div class="discovery-picker-empty">${escapeHtml(_tr('Aucun carnet disponible.'))}</div>`;
    state.discoveryNotebooksLoaded = true;
    syncDiscoveryNotebookPicker();
  } catch (e) {
    console.error('loadDiscoveryNotebookOptions failed:', e);
    showToast(_tr('Impossible de charger les carnets pour la découverte.'), 3200, 'error');
  }
}

function renderDiscoveryDebug(debug) {
  const root = document.getElementById('discovery-debug');
  if (!root) return;
  if (!isDebugMetricsEnabled()) {
    root.style.display = 'none';
    root.innerHTML = '';
    return;
  }
  const events = Array.isArray(debug?.llm_events) ? debug.llm_events : [];
  if (!events.length) {
    root.style.display = 'none';
    root.innerHTML = '';
    return;
  }
  root.style.display = 'block';
  root.innerHTML = events.map((event, idx) => {
    const stage = String(event?.stage || `pass_${idx + 1}`);
    const error = String(event?.error || '').trim();
    const raw = String(event?.raw_output || '').trim();
    const parsed = event?.parsed ? JSON.stringify(event.parsed, null, 2) : '';
    const prompt = String(event?.prompt || '').trim();
    return `
      <details class="discovery-debug-item">
        <summary class="discovery-debug-summary"><span class="discovery-debug-badge">DEBUG</span><span>LLM ${idx + 1} | ${escapeHtml(stage)}${error ? ' | erreur' : ''}</span></summary>
        ${error ? `<pre class="discovery-debug-block">Erreur:\n${escapeHtml(error)}</pre>` : ''}
        ${raw ? `<pre class="discovery-debug-block">Retour brut:\n${escapeHtml(raw)}</pre>` : ''}
        ${parsed ? `<pre class="discovery-debug-block">JSON parse:\n${escapeHtml(parsed)}</pre>` : ''}
        ${prompt ? `<pre class="discovery-debug-block">Prompt:\n${escapeHtml(prompt)}</pre>` : ''}
      </details>
    `;
  }).join('');
}

const _DISCOVERY_SOURCE_STATUS_LABELS = {
  ok: 'a répondu',
  rate_limited: 'limitée',
  unavailable: 'indisponible',
  empty: 'aucun résultat',
  not_queried: 'non interrogée',
};

// One chip per external source, so an empty or thin result says whether the
// sources failed or simply had nothing matching.
function _discoverySourceReportHtml(meta) {
  const report = Array.isArray(meta?.source_report) ? meta.source_report : [];
  if (!report.length) return '';
  const chips = report.map((item) => {
    const status = String(item?.status || 'not_queried');
    const label = _DISCOVERY_SOURCE_STATUS_LABELS[status] || status;
    const detail = String(item?.detail || '');
    return `<span class="discovery-source-chip is-${escapeAttr(status)}" title="${escapeAttr(detail)}">`
      + `<strong>${escapeHtml(String(item?.label || item?.provider || ''))}</strong> · ${escapeHtml(_tr(label))}`
      + `${status === 'ok' ? ` (${escapeHtml(String(item?.count ?? 0))})` : ''}</span>`;
  }).join('');
  return `<div class="discovery-source-report" role="list" aria-label="${escapeAttr(_tr('État des sources'))}">${chips}</div>`;
}

function _formatDiscoveryApiMessage(payload) {
  const meta = payload?.meta || {};
  const providers = Array.isArray(meta?.providers_used)
    ? meta.providers_used.map((item) => String(item || '').trim()).filter(Boolean)
    : [];
  const providerUsedRaw = String(meta?.provider_used || '').trim();
  const providerMap = {
    semanticscholar: 'Semantic Scholar',
    openalex: 'OpenAlex',
    crossref: 'Crossref',
  };
  const providerUsed = providerMap[providerUsedRaw] || providerUsedRaw;
  if (providers.length > 1) return `APIs utilisées : ${providers.join(', ')}.`;
  if (providers.length === 1) return `API utilisée : ${providers[0]}.`;
  if (providerUsed) return `API utilisée : ${providerUsed}.`;
  return '';
}

function _hasDiscoveryRelevanceScore(item) {
  const value = item?.llm_relevance_score;
  return value !== null
    && value !== undefined
    && value !== ''
    && Number.isFinite(Number(value));
}

function _hasDiscoveryAbstract(item) {
  if (typeof item?.abstract_available === 'boolean') return item.abstract_available;
  return Boolean(String(item?.abstract || '').trim());
}

function _renderDiscoveryResults(payload) {
  const root = document.getElementById('discovery-results');
  if (!root) return;
  renderDiscoveryDebug(payload?.debug);

  const results = _discoveryMarkAgainstPrevious(Array.isArray(payload?.results) ? [...payload.results] : []);
  state.discoveryResults = results;
  state.discoveryQuestion = document.getElementById('discovery-mode')?.value === 'notebook'
    ? ''
    : (document.getElementById('discovery-input')?.value || '');
  const notebookProfile = payload?.meta?.notebook_profile;
  if (notebookProfile?.id) {
    state.discoveryNotebookContext = {
      id: String(notebookProfile.id),
      title: String(notebookProfile.title || ''),
      summary: String(notebookProfile.summary || ''),
    };
    updateDiscoveryNotebookContext(state.discoveryNotebookContext);
  }
  const notebookContextActive = (document.getElementById('discovery-mode')?.value === 'notebook')
    && Boolean(state.discoveryNotebookContext?.id);

  const warningText = Array.isArray(payload?.warnings)
    ? [...new Set(payload.warnings.filter(Boolean).map(String))].join('\n')
    : '';
  const apiMessage = _formatDiscoveryApiMessage(payload);
  const statusText = [apiMessage, warningText].filter(Boolean).join('\n');
  const sourcesHtml = _discoverySourceReportHtml(payload?.meta);
  if (!results.length) {
    const report = Array.isArray(payload?.meta?.source_report) ? payload.meta.source_report : [];
    const noSourceAnswered = report.length > 0 && !report.some((item) => item?.status === 'ok');
    const reason = String(payload?.meta?.empty_reason || '');
    renderDiscoveryEmptyState(
      noSourceAnswered ? _tr('Sources externes indisponibles') : _tr('Aucun article retenu'),
      reason || _tr('Essayez des termes plus précis, un auteur, un DOI ou un résumé plus détaillé.'),
    );
    setDiscoveryStatus(statusText, false, sourcesHtml);
    return;
  }

  setDiscoveryStatus(statusText, false, sourcesHtml);
  setDiscoveryResultsMeta(results.length);
  state.discoveryNotebookContextActive = notebookContextActive;
  root.innerHTML = results.map((item, idx) => _discoveryCardHtml(item, idx, notebookContextActive)).join('');
  refreshDiscoveryLibraryMarks();
  prepareDiscoveryRefinement(results);
}

function _discoverySignalHtml(label, help, className = '') {
  return `<span class="${className ? `${className} ` : ''}is-help" tabindex="0" data-tooltip="${escapeAttr(help)}">${escapeHtml(label)}</span>`;
}

// Missing metadata is left out rather than shown as "n.d." or "DOI unavailable".
function _discoveryMetaHtml(item) {
  const parts = [escapeHtml(item.authors_line || _tr('Auteur inconnu'))];
  if (item.year) parts.push(escapeHtml(String(item.year)));
  if (item.venue) parts.push(`<span class="discovery-venue">${escapeHtml(String(item.venue))}</span>`);
  if (item.publisher) parts.push(`<span class="discovery-publisher">${escapeHtml(String(item.publisher))}</span>`);
  const citations = Number(item.citation_count);
  if (Number.isFinite(citations) && citations > 0) parts.push(escapeHtml(_ti('discovery.citations', { count: formatCount(citations) })));
  if (item.open_access_pdf) parts.push(`<span class="discovery-oa">${escapeHtml(_ti('workspace.oa'))}</span>`);
  return parts.join(' · ');
}

function _discoveryInLibrary(item) {
  const library = typeof discoveryRefinement === 'object' ? discoveryRefinement.library : null;
  return Boolean(library && typeof discoveryTitleKey === 'function' && library.has(discoveryTitleKey(item?.title)));
}

function _discoveryPageUrl(item) {
  return item.publisher_url || item.external_url || item.semantic_scholar_url
    || (item.doi ? `https://doi.org/${encodeURI(String(item.doi).trim())}` : '');
}

// One action per result: the publisher's page, where the reader gets the
// paper the way the publisher allows. "In your library" replaces it once the
// title is found in the library.
function _discoveryPrimaryActionHtml(item) {
  if (_discoveryInLibrary(item)) {
    return `<span class="discovery-in-library">${escapeHtml(_ti('discovery.inLibrary'))}</span>`;
  }
  const pageUrl = _discoveryPageUrl(item);
  if (pageUrl) {
    return `<a class="btn btn-indigo discovery-page-btn" href="${escapeAttr(pageUrl)}" target="_blank" rel="noopener">${escapeHtml(_ti('discovery.viewAtPublisher'))}</a>`;
  }
  if (item.open_access_pdf) {
    return `<a class="btn btn-ghost discovery-page-btn" href="${escapeAttr(item.open_access_pdf)}" target="_blank" rel="noopener">${escapeHtml(_ti('library.openPdf'))}</a>`;
  }
  return `<span class="discovery-no-pdf">${escapeHtml(_ti('discovery.noLink'))}</span>`;
}

function _discoveryActionsHtml(item, idx) {
  const doi = String(item.doi || '').trim();
  const menu = [
    `<button type="button" role="menuitem" class="discovery-menu-item" ${bindBbHandler('click', function() { findSimilarDiscoveryArticles(idx); })}>${escapeHtml(_ti('discovery.findSimilar'))}</button>`,
    `<button type="button" role="menuitem" class="discovery-menu-item" ${bindBbHandler('click', function() { void addDiscoveryResultsToNotebook([idx]); })}>${escapeHtml(_ti('discovery.addToNotebookMenu'))}</button>`,
    item.open_access_pdf ? `<a role="menuitem" class="discovery-menu-item" href="${escapeAttr(item.open_access_pdf)}" target="_blank" rel="noopener">${escapeHtml(_ti('library.openPdf'))}</a>` : '',
    doi ? `<a role="menuitem" class="discovery-menu-item" href="${escapeAttr(`https://doi.org/${encodeURI(doi)}`)}" target="_blank" rel="noopener">DOI ${escapeHtml(doi)}</a>` : '',
    `<button type="button" role="menuitem" class="discovery-menu-item" ${bindBbHandler('click', function() { hideDiscoveryResult(idx); })}>${escapeHtml(_ti('discovery.hideResult'))}</button>`,
  ].filter(Boolean).join('');
  return `
    <span class="discovery-primary-action">${_discoveryPrimaryActionHtml(item)}</span>
    <details class="discovery-more">
      <summary class="btn-icon-more" aria-label="${escapeAttr(_ti('library.moreActions'))}" title="${escapeAttr(_ti('library.moreActions'))}"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg></summary>
      <div class="discovery-more-menu" role="menu">${menu}</div>
    </details>`;
}

function _discoveryScoreTooltip(item) {
  const parts = [];
  if (_hasDiscoveryRelevanceScore(item)) parts.push(_ti('discovery.relevanceBadge', { score: Math.round(Number(item.llm_relevance_score)) }));
  if (item.external_source_label) parts.push(item.external_source_label);
  if (Array.isArray(item.score_reasons) && item.score_reasons.length) parts.push(item.score_reasons.join(' | '));
  return parts.join(' · ');
}

function _discoveryCardHtml(item, idx, notebookContextActive) {
  const title = item.title || _tr('Sans titre');
  const why = String(item.llm_rationale || '').trim();
  const summary = String(item.short_summary || item.abstract_preview || '').trim();
  const passage = _discoveryMatchingPassage(item, state.discoveryQuestion || '');
  const tooltip = _discoveryScoreTooltip(item);
  const whyLabel = _ti(notebookContextActive ? 'discovery.whyNotebook' : 'discovery.whyRelevant');
  // After "Search again": new papers carry a tag, those already seen are dimmed.
  const badge = item._seen === false
    ? `<span class="discovery-badge is-new">${escapeHtml(_ti('discovery.newBadge'))}</span>`
    : (item._seen ? `<span class="discovery-badge">${escapeHtml(_ti('discovery.seenBadge'))}</span>` : '');
  const classes = ['discovery-card', _discoveryInLibrary(item) ? 'is-in-library' : '', item._seen ? 'is-seen' : ''].filter(Boolean).join(' ');
  return `
    <article class="${classes}" data-discovery-index="${idx}">
      <label class="discovery-select"><input type="checkbox" data-compare-index="${idx}" aria-label="${escapeAttr(_ti('discovery.selectResult', { title }))}"></label>
      <div class="discovery-card-main">
        <div class="discovery-title-line">${badge}<span class="discovery-title" data-i18n-skip${tooltip ? ` title="${escapeAttr(tooltip)}"` : ''}>${escapeHtml(title)}</span></div>
        <div class="discovery-meta" data-i18n-skip>${_discoveryMetaHtml(item)}</div>
        ${why ? `<div class="discovery-why"><span class="discovery-why-label">${escapeHtml(whyLabel)}</span> <span class="discovery-why-text" data-i18n-skip>${escapeHtml(why)}</span></div>` : ''}
        ${passage
          ? `<blockquote class="discovery-quote" data-i18n-skip title="${escapeAttr(_ti('discovery.matchingPassage'))}">« ${escapeHtml(passage)} »</blockquote>`
          : (summary && summary !== why ? `<div class="discovery-summary" data-i18n-skip>${escapeHtml(summary)}</div>` : '')}
      </div>
      <div class="discovery-actions">${_discoveryActionsHtml(item, idx)}</div>
    </article>
  `;
}

// Called once the library titles are loaded and after each add: swaps
// "Add to library" for "In your library" and updates the note above the cards.
function refreshDiscoveryLibraryMarks() {
  const results = Array.isArray(state.discoveryResults) ? state.discoveryResults : [];
  let inLibrary = 0;
  document.querySelectorAll('#discovery-results [data-discovery-index]').forEach((card) => {
    const index = Number(card.dataset.discoveryIndex);
    const item = results[index];
    if (!item) return;
    const present = _discoveryInLibrary(item);
    if (present) inLibrary += 1;
    card.classList.toggle('is-in-library', present);
    const slot = card.querySelector('.discovery-primary-action');
    if (slot) slot.innerHTML = _discoveryPrimaryActionHtml(item);
  });
  const parts = [state.discoveryNewCount === null || state.discoveryNewCount === undefined
    ? _ti('discovery.sortedByRelevance')
    : (state.discoveryNewCount ? _ti('discovery.newSinceLast', { count: state.discoveryNewCount }) : _ti('discovery.noNewSinceLast'))];
  if (inLibrary) parts.push(_ti('discovery.alreadyInLibrary', { count: inLibrary }));
  _setDiscoveryResultsNote(results.length ? parts.join(' · ') : '');
}

document.addEventListener('click', (event) => {
  document.querySelectorAll('#discovery-results .discovery-more[open]').forEach((menu) => {
    const item = event.target.closest?.('.discovery-menu-item');
    if (!event.composedPath().includes(menu) || (item && menu.contains(item) && !item.classList.contains('discovery-compare-check'))) {
      menu.removeAttribute('open');
    }
  });
});

async function runArticleDiscovery() {
  if (state.discoveryLoading) {
    stopArticleDiscovery();
    return;
  }

  const enabled = isExternalDiscoveryEnabled();
  if (!enabled) {
    showToast(_tr("Activez d'abord l'option de confidentialité API externe."));
    updateDiscoveryPrivacyUi();
    return;
  }

  const mode = document.getElementById('discovery-mode')?.value || 'search';
  const inputText = (document.getElementById('discovery-input')?.value || '').trim();
  const notebookId = document.getElementById('discovery-notebook-select')?.value || '';
  const model = optionalSelectedModel('discovery');
  const maxResultsRaw = parseInt(document.getElementById('discovery-max-results')?.value || '8', 10);
  const maxResults = Number.isFinite(maxResultsRaw) ? Math.max(1, Math.min(50, maxResultsRaw)) : 8;
  const maxResultsEl = document.getElementById('discovery-max-results');
  if (maxResultsEl) maxResultsEl.value = String(maxResults);
  const unpaywallEmail = (document.getElementById('unpaywall-email')?.value || '').trim();

  if (mode === 'notebook' && !notebookId) {
    showToast(_tr('Choisissez un carnet pour lancer cette recherche.'));
    return;
  }
  if (mode === 'search' && !inputText) {
    showToast(_tr('Ajoutez du texte pour guider la recherche.'));
    return;
  }
  if (!model) {
    showToast(_tr('Sélectionnez un modèle valide pour la génération locale.'));
    return;
  }

  // "Search again" on the opened search: updated in place, compared with it.
  const againId = _discoverySearchAgainId();
  state.discoveryCompareTo = againId ? _discoveryResultKeys(state.discoveryResults) : null;
  const controller = new AbortController();
  state.discoveryAbortController = controller;
  state.discoveryActiveModel = model;
  state.discoveryLoading = true;
  setDiscoverySearchCollapsed(true);
  _setDiscoveryRunButtonStop();
  renderDiscoveryDebug(null);
  startDiscoveryProgressMessages(mode);

  try {
    await loadDiscoveryNotebookOptions();
    const r = await fetch('/api/article-discovery/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        mode,
        input_text: inputText,
        model,
        notebook_id: notebookId || null,
        max_results: maxResults,
        include_unpaywall: true,
        unpaywall_email: unpaywallEmail || null,
        query_planner: Boolean(state.appSettings?.discovery_query_planner),
        privacy_ack: true,
        debug: isDebugMetricsEnabled(),
        history_id: againId || null,
      }),
      signal: controller.signal,
    });
    let d = null;
    if (!r.ok || !String(r.headers.get('content-type') || '').includes('text/event-stream')) {
      d = await r.json();
    } else {
      // First results appear before the LLM validation; the final payload
      // then replaces them with the validated, summarised ranking.
      let preliminaryShown = false;
      let liveTimer = null;
      const showLiveStatus = (message, meta) => {
        const elapsed = Math.round((Date.now() - (state.discoveryStartedAt || Date.now())) / 1000);
        setDiscoveryStatus(`${message} (${elapsed} s)`, true, _discoverySourceReportHtml(meta));
      };
      try {
        for await (const event of readSSE(r)) {
          if (controller.signal.aborted) break;
          if (event?.type === 'progress') {
            state.discoveryLiveMessage = _tr(String(event.msg || ''));
            if (preliminaryShown) showLiveStatus(state.discoveryLiveMessage, state.discoveryPreliminaryMeta);
          } else if (event?.type === 'preliminary') {
            stopDiscoveryProgressMessages();
            preliminaryShown = true;
            state.discoveryPreliminaryMeta = event.meta || {};
            _renderDiscoveryResults({ ok: true, ...event, preliminary: true });
            state.discoveryLiveMessage = _tr('Premiers résultats affichés. Le modèle vérifie leur pertinence…');
            showLiveStatus(state.discoveryLiveMessage, event.meta);
            liveTimer = setInterval(() => showLiveStatus(state.discoveryLiveMessage, state.discoveryPreliminaryMeta), 1000);
          } else if (event?.type === 'done') {
            d = event.payload || { ok: false, msg: 'article_discovery_failed' };
          }
        }
      } finally {
        if (liveTimer) clearInterval(liveTimer);
        state.discoveryPreliminaryMeta = null;
      }
      if (!d) throw new TypeError('stream ended without result');
    }
    if (!d?.ok) {
      stopDiscoveryProgressMessages();
      state.discoveryResults = [];
      setDiscoveryStatus(d?.msg || 'Erreur article discovery.', false, _discoverySourceReportHtml(d?.meta));
      renderDiscoveryDebug(d?.debug);
      renderDiscoveryEmptyState(
        _tr('Recherche impossible'),
        d?.msg || _tr('La recherche n’a pas pu être terminée. Vérifiez les paramètres puis réessayez.'),
      );
      return;
    }
    stopDiscoveryProgressMessages();
    state.activeDiscoveryHistoryId = String(d?.history_id || '');
    if (d?.meta?.notebook_profile) updateDiscoveryNotebookContext(d.meta.notebook_profile);
    await loadDiscoveryHistory();
    const saved = (state.discoveryHistory || []).find((search) => search.id === state.activeDiscoveryHistoryId);
    _renderDiscoveryResults(d);
    setDiscoveryOpenedSearch({
      id: state.activeDiscoveryHistoryId,
      title: saved?.title || '',
      input_text: inputText,
      mode,
      notebook_id: notebookId,
      updated: saved?.updated || new Date().toISOString(),
    });
  } catch(e) {
    stopDiscoveryProgressMessages();
    if (controller.signal.aborted) {
      setDiscoveryStatus('Recherche interrompue.');
      renderDiscoveryEmptyState(
        _tr('Recherche interrompue'),
        _tr('Vous pouvez modifier la requête puis relancer la recherche.'),
      );
    } else {
      state.discoveryResults = [];
      const connectionLost = e instanceof TypeError;
      setDiscoveryStatus(connectionLost
        ? _tr('Connexion au serveur Bibliobrain perdue pendant la recherche.')
        : 'Erreur article discovery: ' + (e?.message || 'inconnue'));
      renderDiscoveryEmptyState(
        connectionLost ? _tr('Connexion interrompue') : _tr('Erreur de chargement'),
        connectionLost
          ? _tr('Le serveur Bibliobrain a redémarré ou ne répond plus. Relancez la recherche.')
          : (e?.message || _tr('Une erreur inattendue empêche l’affichage des résultats.')),
      );
    }
  } finally {
    state.discoveryCompareTo = null;
    stopDiscoveryProgressMessages();
    state.discoveryAbortController = null;
    state.discoveryActiveModel = '';
    state.discoveryLoading = false;
    syncDiscoverySearchActivity();
    const status = document.getElementById('discovery-status');
    if (status) status.classList.remove('is-loading');
    _setDiscoveryRunButtonIdle();
  }
}

// ---------------------------------------------
//  SEARCH AGAIN, SELECTION, MATCHING PASSAGE, FIND SIMILAR
// ---------------------------------------------
// A result is the same paper across two runs when its DOI, or else its
// normalised title, matches.
function _discoveryResultKey(item) {
  const doi = String(item?.doi || '').trim().toLowerCase();
  if (doi) return `doi:${doi}`;
  const title = typeof discoveryTitleKey === 'function'
    ? discoveryTitleKey(item?.title)
    : String(item?.title || '').toLowerCase();
  return `title:${title}`;
}

function _discoveryResultKeys(results) {
  return new Set((Array.isArray(results) ? results : []).map(_discoveryResultKey));
}

// The saved search the reader opened is run again, in place, when the
// question (or the notebook) has not changed; any edit makes a new search.
function _discoverySearchAgainId() {
  const opened = state.discoveryOpenedSearch;
  if (!opened?.id) return '';
  const mode = document.getElementById('discovery-mode')?.value || 'search';
  const openedMode = opened.mode === 'recommendations' ? 'notebook' : (opened.mode || 'search');
  if (openedMode !== mode) return '';
  if (mode === 'notebook') {
    const notebookId = document.getElementById('discovery-notebook-select')?.value || '';
    return String(opened.notebookId || '') === String(notebookId) ? opened.id : '';
  }
  const inputText = (document.getElementById('discovery-input')?.value || '').trim();
  return String(opened.inputText || '').trim() === inputText ? opened.id : '';
}

function syncDiscoveryRunLabel() {
  if (!state.discoveryLoading) _setDiscoveryRunButtonIdle();
}

function setDiscoveryOpenedSearch(search) {
  state.discoveryOpenedSearch = search?.id
    ? {
      id: String(search.id),
      title: String(search.title || ''),
      inputText: String(search.input_text ?? search.inputText ?? ''),
      mode: String(search.mode || 'search'),
      notebookId: String(search.notebook_id ?? search.notebookId ?? ''),
      updated: String(search.updated || search.created || ''),
    }
    : null;
  renderDiscoverySearchMeta();
  syncDiscoveryRunLabel();
}

function _discoveryDayDiff(date) {
  const start = (value) => new Date(value.getFullYear(), value.getMonth(), value.getDate()).getTime();
  return Math.round((start(new Date()) - start(date)) / 86400000);
}

function _discoveryWhenLabel(iso) {
  const date = new Date(iso);
  if (Number.isNaN(+date)) return '';
  const locale = window.getLanguage?.() === 'en' ? 'en-GB' : 'fr-FR';
  const time = date.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
  const days = _discoveryDayDiff(date);
  if (days === 0) return `${_ti('discovery.today')}, ${time}`;
  if (days === 1) return `${_ti('discovery.yesterday')}, ${time}`;
  return date.toLocaleDateString(locale, { day: 'numeric', month: 'short', year: 'numeric' });
}

// "Today / This week / Older" for the history list.
function discoveryHistoryGroup(iso) {
  const date = new Date(iso);
  if (Number.isNaN(+date)) return 'older';
  const days = _discoveryDayDiff(date);
  if (days === 0) return 'today';
  if (days < 7) return 'week';
  return 'older';
}

function renderDiscoverySearchMeta() {
  const meta = document.getElementById('discovery-search-meta');
  if (!meta) return;
  const opened = state.discoveryOpenedSearch;
  meta.hidden = !opened?.id;
  const when = document.getElementById('discovery-search-when');
  if (when) {
    when.textContent = opened?.id ? _discoveryWhenLabel(opened.updated) : '';
    when.title = opened?.title || '';
  }
}

async function renameOpenedDiscoverySearch() {
  const opened = state.discoveryOpenedSearch;
  if (!opened?.id) return;
  const title = window.prompt(_ti('discovery.renamePrompt'), opened.title || '');
  if (title === null || !title.trim()) return;
  try {
    const response = await fetch(`/api/discovery-history/${encodeURIComponent(opened.id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: title.trim().slice(0, 200) }),
    });
    const payload = await response.json();
    if (!response.ok || !payload?.ok) throw new Error('rename failed');
    opened.title = title.trim();
    await loadDiscoveryHistory();
    renderDiscoverySearchMeta();
  } catch (_) {
    showToast(_ti('discovery.renameFailed'), 3600, 'error');
  }
}

function deleteOpenedDiscoverySearch() {
  const opened = state.discoveryOpenedSearch;
  if (!opened?.id) return;
  deleteDiscoveryHistory(opened.id);
  startNewDiscoverySearch();
}

// Results of a run against the saved one: new papers first, seen ones after.
function _discoveryMarkAgainstPrevious(results) {
  const previous = state.discoveryCompareTo;
  if (!(previous instanceof Set) || !results.length) {
    results.forEach((item) => { delete item._seen; });
    state.discoveryNewCount = null;
    return results;
  }
  results.forEach((item) => { item._seen = previous.has(_discoveryResultKey(item)); });
  const ordered = [...results.filter((item) => !item._seen), ...results.filter((item) => item._seen)];
  state.discoveryNewCount = ordered.filter((item) => !item._seen).length;
  return ordered;
}

// The sentence of the abstract that shares the most words with the question.
// Acronyms count (AC, HTS); short function words do not; "magnet" matches
// "magnets" (same first five letters).
const DISCOVERY_QUOTE_STOPWORDS = new Set([
  'the', 'and', 'for', 'with', 'from', 'into', 'are', 'was', 'were', 'this', 'that', 'these', 'those', 'its', 'their',
  'des', 'les', 'une', 'dans', 'sur', 'pour', 'par', 'aux', 'que', 'qui', 'est', 'sont', 'avec', 'ces', 'cette', 'leur',
  'of', 'in', 'on', 'to', 'an', 'by', 'or', 'as', 'at', 'is', 'de', 'la', 'le', 'et', 'en', 'du', 'au', 'un', 'ou',
]);

function _discoveryQuoteTokens(text) {
  const normalised = String(text || '').toLowerCase().normalize('NFD').replace(/\p{M}/gu, '');
  return (normalised.match(/[\p{L}\p{N}]{2,}/gu) || [])
    .filter((token) => !DISCOVERY_QUOTE_STOPWORDS.has(token))
    .map((token) => (token.length > 5 ? token.slice(0, 5) : token));
}

function _discoveryMatchingPassage(item, question) {
  const words = new Set(_discoveryQuoteTokens(question));
  const abstract = String(item?.abstract || item?.abstract_preview || '').trim();
  if (!words.size || !abstract) return '';
  let best = '';
  let bestHits = 0;
  (abstract.match(/[^.!?]+[.!?]*/g) || []).forEach((sentence) => {
    const hits = new Set(_discoveryQuoteTokens(sentence).filter((token) => words.has(token))).size;
    if (hits > bestHits) {
      bestHits = hits;
      best = sentence.trim();
    }
  });
  if (bestHits < Math.min(2, words.size)) return '';
  return best.length > 320 ? `${best.slice(0, 317).trimEnd()}…` : best;
}

function findSimilarDiscoveryArticles(index) {
  const item = (state.discoveryResults || [])[index];
  if (!item) return;
  const abstract = String(item.abstract || item.abstract_preview || '').trim();
  const text = `${_ti('discovery.similarPrefix')}${item.title || ''}.${abstract ? ` ${abstract.slice(0, 600)}` : ''}`;
  startNewDiscoverySearch();
  selectDiscoveryMode('search');
  const input = document.getElementById('discovery-input');
  if (input) input.value = text;
  syncDiscoveryRunLabel();
  void runArticleDiscovery();
}

function hideDiscoveryResult(index) {
  const card = document.querySelector(`#discovery-results [data-discovery-index="${index}"]`);
  if (card) card.hidden = true;
  const item = (state.discoveryResults || [])[index];
  if (item) item._hidden = true;
}

// Papers found online go into a notebook as text sources: title, authors,
// links and abstract (never the full text, which is not downloaded).
function _discoveryNotebookSourceText(item) {
  const lines = [String(item.title || _tr('Sans titre'))];
  const meta = [item.authors_line, item.year, item.venue].filter(Boolean).join(' · ');
  if (meta) lines.push(meta);
  const doi = String(item.doi || '').trim();
  if (doi) lines.push(`DOI : https://doi.org/${doi}`);
  const page = _discoveryPageUrl(item);
  if (page && !(doi && page.includes(doi))) lines.push(page);
  const abstract = String(item.abstract || item.abstract_preview || '').trim();
  if (abstract) lines.push('', abstract);
  lines.push('', _ti('discovery.sourceAbstractOnly'));
  return lines.join('\n');
}

function _discoveryNotebookSourceTitle(item) {
  const lead = String(item.authors_line || '').split(/[;,]/)[0].trim();
  const year = item.year ? ` (${item.year})` : '';
  const title = String(item.title || _tr('Sans titre'));
  return lead ? `${lead}${year} — ${title}`.slice(0, 300) : title.slice(0, 300);
}

function _discoveryNotebookDialog() {
  let dialog = document.getElementById('discovery-notebook-dialog');
  if (dialog) return dialog;
  document.body.insertAdjacentHTML('beforeend', `
    <dialog id="discovery-notebook-dialog" class="discovery-notebook-dialog" aria-labelledby="discovery-notebook-dialog-title">
      <h3 id="discovery-notebook-dialog-title"></h3>
      <div id="discovery-notebook-dialog-list" class="discovery-notebook-dialog-list"></div>
      <div class="discovery-notebook-dialog-actions"><button type="button" class="btn btn-ghost" data-discovery-dialog-cancel></button></div>
    </dialog>`);
  dialog = document.getElementById('discovery-notebook-dialog');
  dialog.addEventListener('click', (event) => {
    if (event.target.closest('[data-discovery-dialog-cancel]') || event.target === dialog) dialog.close();
  });
  return dialog;
}

async function addDiscoveryResultsToNotebook(indexes) {
  const items = (Array.isArray(indexes) ? indexes : [])
    .map((index) => (state.discoveryResults || [])[index])
    .filter(Boolean);
  if (!items.length) return;
  await openNotebookPickerForItems(items);
}

// Also used by the citation graph. onAdded(notebook, count) runs once the
// papers are in the chosen notebook.
async function openNotebookPickerForItems(items, onAdded = null) {
  if (!items.length) return;
  const dialog = _discoveryNotebookDialog();
  const list = dialog.querySelector('#discovery-notebook-dialog-list');
  dialog.querySelector('#discovery-notebook-dialog-title').textContent = _ti('discovery.pickNotebook', { count: items.length });
  dialog.querySelector('[data-discovery-dialog-cancel]').textContent = _ti('notebooks.cancel');
  list.innerHTML = `<p class="discovery-notebook-dialog-empty">${escapeHtml(_ti('drafting.notebookLoading'))}</p>`;
  if (!dialog.open) dialog.showModal();
  let notebooks = [];
  try {
    const response = await fetch('/api/notebooks', { cache: 'no-store' });
    const payload = await response.json();
    notebooks = Array.isArray(payload?.notebooks) ? payload.notebooks : [];
  } catch (_) { notebooks = []; }
  if (!notebooks.length) {
    list.innerHTML = `<p class="discovery-notebook-dialog-empty">${escapeHtml(_ti('discovery.noNotebook'))}</p>`;
    return;
  }
  list.innerHTML = notebooks.map((notebook) => `
    <button type="button" class="discovery-notebook-choice" ${bindBbHandler('click', function() {
      void _addDiscoveryItemsToNotebook(items, notebook).then((count) => {
        if (count && onAdded) onAdded(notebook, count);
      });
    })}>
      <strong>${escapeHtml(notebook.title || _tr('Sans titre'))}</strong>
      <span>${escapeHtml(_ti('notebooks.sourceCount', { count: Number(notebook.source_count) || 0 }))}</span>
    </button>`).join('');
}

async function _addDiscoveryItemsToNotebook(items, notebook) {
  const dialog = document.getElementById('discovery-notebook-dialog');
  dialog?.close();
  let added = 0;
  for (const item of items) {
    try {
      const response = await fetch(`/api/notebooks/${encodeURIComponent(notebook.id)}/sources`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: _discoveryNotebookSourceTitle(item), content: _discoveryNotebookSourceText(item) }),
      });
      if (response.ok) added += 1;
    } catch (_) { /* counted as not added */ }
  }
  if (added) {
    showToast(_ti('discovery.addedToNotebook', { count: added, title: notebook.title || '' }), 3600, 'success');
    if (state.drafting) state.drafting.notebookOptionsLoaded = false;
  } else {
    showToast(_ti('discovery.addToNotebookFailed'), 4000, 'error');
  }
  return added;
}

document.addEventListener('click', (event) => {
  const searchAction = event.target.closest?.('[data-discovery-search-action]');
  if (searchAction) {
    searchAction.closest('details')?.removeAttribute('open');
    if (searchAction.dataset.discoverySearchAction === 'rename') void renameOpenedDiscoverySearch();
    else if (searchAction.dataset.discoverySearchAction === 'delete') deleteOpenedDiscoverySearch();
  }
  const searchMore = document.getElementById('discovery-search-more');
  if (searchMore?.open && !event.composedPath().includes(searchMore)) searchMore.removeAttribute('open');
});
document.getElementById('discovery-input')?.addEventListener('input', syncDiscoveryRunLabel);

const RESEARCH_TREND_COLORS = ['#818cf8', '#22d3ee', '#f59e0b'];

const DISCOVERY_WORKSPACES = ['articles', 'trends', 'origins'];

function switchDiscoveryWorkspace(view = 'articles', options = {}) {
  const normalized = DISCOVERY_WORKSPACES.includes(view) ? view : 'articles';
  document.querySelectorAll('[data-discovery-workspace]').forEach((button) => {
    const active = button.dataset.discoveryWorkspace === normalized;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
  });
  DISCOVERY_WORKSPACES.forEach((name) => {
    const panel = document.getElementById(`discovery-${name}-view`);
    if (panel) panel.hidden = normalized !== name;
  });

  const titleKey = { trends: 'trends.title', origins: 'origins.title' }[normalized] || 'discovery.title';
  const title = document.getElementById('discovery-workspace-title');
  if (title) {
    title.dataset.i18n = titleKey;
    title.textContent = _ti(titleKey);
  }
  if (normalized === 'trends' && options.focus !== false) {
    window.setTimeout(() => document.getElementById('research-trend-query-0')?.focus(), 40);
  }
  if (normalized === 'origins' && options.focus !== false) {
    window.setTimeout(() => document.getElementById('origins-query')?.focus(), 40);
  }
}

function _researchTrendVisibleQueries() {
  return [...document.querySelectorAll('.research-trend-topic-row:not([hidden]) input')]
    .map(input => String(input.value || '').trim())
    .filter(Boolean);
}

function _setResearchTrendTopicQueries(queries) {
  const normalized = (Array.isArray(queries) ? queries : []).slice(0, 3);
  document.querySelectorAll('.research-trend-topic-row').forEach((row, index) => {
    const visible = index === 0 || index < normalized.length;
    row.hidden = !visible;
    const input = row.querySelector('input');
    if (input) input.value = normalized[index] || '';
  });
  const addButton = document.getElementById('research-trends-add-topic');
  if (addButton) addButton.disabled = normalized.length >= 3;
}

function addResearchTrendTopic() {
  const queries = Array.from(document.querySelectorAll('.research-trend-topic-row'))
    .filter((row) => !row.hidden)
    .map((row) => String(row.querySelector('.research-trend-query')?.value || '').trim());
  if (queries.length >= 3) return;
  queries.push('');
  _setResearchTrendTopicQueries(queries);
  document.getElementById(`research-trend-query-${queries.length - 1}`)?.focus();
}

function removeResearchTrendTopic(index) {
  const rows = [...document.querySelectorAll('.research-trend-topic-row:not([hidden])')];
  const queries = rows
    .filter(row => Number(row.dataset.trendTopicRow) !== Number(index))
    .map(row => String(row.querySelector('input')?.value || '').trim());
  _setResearchTrendTopicQueries(queries.length ? queries : ['']);
}

function _researchTrendTopicsFromForm() {
  return _researchTrendVisibleQueries().map((query) => ({
    query,
    label: query.length > 72 ? `${query.slice(0, 69)}…` : query,
  }));
}

function _researchTrendSetStatus(message = '', kind = '') {
  const status = document.getElementById('research-trends-status');
  if (!status) return;
  status.textContent = String(message || '');
  status.classList.toggle('is-loading', kind === 'loading');
  status.classList.toggle('is-error', kind === 'error');
}

function _researchTrendErrorMessage(payload) {
  const errorKeys = {
    external_api_permission_required: 'trends.externalRequired',
    openalex_api_key_required: 'trends.apiKeyRequired',
    invalid_research_trends_request: 'trends.invalidRequest',
    research_trends_unavailable: 'trends.loadError',
  };
  const key = errorKeys[String(payload?.error || '')];
  return key ? _ti(key) : _ti('trends.loadError');
}

function _researchTrendFormatNumber(value, metric = 'count', compact = false) {
  const number = Number(value || 0);
  const options = compact
    ? { notation: 'compact', maximumFractionDigits: 1 }
    : metric === 'count'
      ? { maximumFractionDigits: 0 }
      : { maximumFractionDigits: metric === 'index' ? 1 : 2 };
  return new Intl.NumberFormat(undefined, options).format(number);
}

function _researchTrendMetricLabel(metric) {
  if (metric === 'share_per_million') return _ti('trends.metricShare');
  if (metric === 'index') return _ti('trends.metricIndex');
  return _ti('trends.metricCount');
}

function _researchTrendPercent(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  const normalized = Number(value);
  return `${normalized > 0 ? '+' : ''}${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(normalized)} %`;
}

function _renderResearchTrendSummary(payload) {
  const root = document.getElementById('research-trends-summary');
  if (!root) return;
  const series = Array.isArray(payload?.series) ? payload.series : [];
  root.hidden = !series.length;
  root.innerHTML = series.map((item, index) => {
    const stats = item?.stats || {};
    const peak = stats.peak_year
      ? `${_ti('trends.peak')} ${escapeHtml(String(stats.peak_year))} · ${_researchTrendFormatNumber(stats.peak_count)}`
      : `${_ti('trends.peak')} —`;
    return `
      <article class="research-trend-summary-card" style="border-top-color:${RESEARCH_TREND_COLORS[index]}">
        <strong title="${escapeAttr(item.query || item.label || '')}">${escapeHtml(item.label || item.query || '')}</strong>
        <span class="research-trend-total">${_researchTrendFormatNumber(stats.total)}</span>
        <small>${escapeHtml(_ti('trends.total'))}</small>
        <small>${peak} · ${escapeHtml(_ti('trends.yoy'))} ${_researchTrendPercent(stats.year_over_year_pct)} · ${_researchTrendPercent(stats.five_year_growth_pct)} ${escapeHtml(_ti('trends.fiveYears'))}</small>
      </article>
    `;
  }).join('');
}

function _renderResearchTrendChart(payload, metric = 'count') {
  const svg = document.getElementById('research-trends-chart');
  const empty = document.getElementById('research-trends-empty');
  if (!svg) return;
  const series = Array.isArray(payload?.series) ? payload.series : [];
  const years = series[0]?.points?.map(point => Number(point.year)) || [];
  if (!series.length || !years.length) {
    svg.setAttribute('hidden', '');
    if (empty) empty.hidden = false;
    return;
  }

  if (empty) empty.hidden = true;
  svg.removeAttribute('hidden');
  const width = 960;
  const height = 380;
  const margin = { left: 78, right: 24, top: 24, bottom: 50 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const values = series.flatMap(item => (item.points || []).map(point => Number(point[metric] || 0)));
  const maximum = Math.max(1, ...values);
  const xFor = index => margin.left + (years.length <= 1 ? plotWidth / 2 : (index / (years.length - 1)) * plotWidth);
  const yFor = value => margin.top + plotHeight - (Number(value || 0) / maximum) * plotHeight;
  const tickCount = 5;
  const grid = [];
  for (let index = 0; index <= tickCount; index += 1) {
    const value = (maximum / tickCount) * index;
    const y = yFor(value);
    grid.push(`<line class="research-trend-grid" x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}"></line>`);
    grid.push(`<text class="research-trend-axis-label" x="${margin.left - 10}" y="${y + 4}" text-anchor="end">${escapeHtml(_researchTrendFormatNumber(value, metric, true))}</text>`);
  }
  const yearStep = Math.max(1, Math.ceil(years.length / 9));
  years.forEach((year, index) => {
    if (index % yearStep !== 0 && index !== years.length - 1) return;
    grid.push(`<text class="research-trend-axis-label" x="${xFor(index)}" y="${height - 18}" text-anchor="middle">${year}</text>`);
  });

  const lines = series.map((item, seriesIndex) => {
    const color = RESEARCH_TREND_COLORS[seriesIndex] || RESEARCH_TREND_COLORS[0];
    const points = Array.isArray(item.points) ? item.points : [];
    const coordinates = points.map((point, index) => ({
      x: xFor(index),
      y: yFor(point[metric]),
      point,
    }));
    const lastIncomplete = Boolean(coordinates.at(-1)?.point?.incomplete);
    const solid = lastIncomplete ? coordinates.slice(0, -1) : coordinates;
    const dashed = lastIncomplete && coordinates.length >= 2 ? coordinates.slice(-2) : [];
    const solidLine = solid.length >= 2
      ? `<polyline class="research-trend-line" stroke="${color}" points="${solid.map(point => `${point.x},${point.y}`).join(' ')}"></polyline>`
      : '';
    const dashedLine = dashed.length >= 2
      ? `<polyline class="research-trend-line is-incomplete" stroke="${color}" points="${dashed.map(point => `${point.x},${point.y}`).join(' ')}"></polyline>`
      : '';
    const circles = coordinates.map(({ x, y, point }) => {
      const label = `${item.label}, ${point.year}: ${_researchTrendFormatNumber(point[metric], metric)} ${_researchTrendMetricLabel(metric)}`;
      return `<circle class="research-trend-point" cx="${x}" cy="${y}" r="${point.incomplete ? 4.5 : 3.4}" fill="${color}" aria-label="${escapeAttr(label)}"><title>${escapeHtml(label)}</title></circle>`;
    }).join('');
    return `${solidLine}${dashedLine}${circles}`;
  }).join('');

  const chartTitle = _ti('trends.chartTitle');
  const chartDescription = `${_ti('trends.chartDesc')} ${_researchTrendMetricLabel(metric)}.`;
  svg.innerHTML = `
    <title id="research-trends-chart-title">${escapeHtml(chartTitle)}</title>
    <desc id="research-trends-chart-desc">${escapeHtml(chartDescription)}</desc>
    <g>${grid.join('')}</g>
    <g>${lines}</g>
  `;
}

function _renderResearchTrendLegend(payload) {
  const root = document.getElementById('research-trends-legend');
  if (!root) return;
  const series = Array.isArray(payload?.series) ? payload.series : [];
  root.hidden = !series.length;
  root.innerHTML = series.map((item, index) => `
    <span title="${escapeAttr(item.query || '')}"><i style="background:${RESEARCH_TREND_COLORS[index]}"></i>${escapeHtml(item.label || item.query || '')}</span>
  `).join('');
}

function _renderResearchTrendTable(payload, metric = 'count') {
  const root = document.getElementById('research-trends-table-wrap');
  if (!root) return;
  const series = Array.isArray(payload?.series) ? payload.series : [];
  const points = series[0]?.points || [];
  root.hidden = !series.length || !points.length;
  if (root.hidden) {
    root.innerHTML = '';
    return;
  }
  root.innerHTML = `
    <table class="research-trends-table">
      <caption class="visually-hidden">${escapeHtml(_ti('trends.chartTitle'))} — ${escapeHtml(_researchTrendMetricLabel(metric))}</caption>
      <thead><tr><th scope="col">${escapeHtml(_ti('trends.year'))}</th>${series.map(item => `<th scope="col">${escapeHtml(item.label || item.query || '')}</th>`).join('')}</tr></thead>
      <tbody>${points.map((point, pointIndex) => `
        <tr><th scope="row">${point.year}${point.incomplete ? ' *' : ''}</th>${series.map(item => `<td>${escapeHtml(_researchTrendFormatNumber(item.points?.[pointIndex]?.[metric], metric))}</td>`).join('')}</tr>
      `).join('')}</tbody>
    </table>
  `;
}

function renderResearchTrends(payload = state.researchTrends.data) {
  if (!payload?.ok) return;
  const metric = state.researchTrends.metric || 'count';
  _renderResearchTrendSummary(payload);
  _renderResearchTrendChart(payload, metric);
  _renderResearchTrendLegend(payload);
  _renderResearchTrendTable(payload, metric);
  const note = document.getElementById('research-trends-note');
  if (note) {
    note.hidden = false;
    note.textContent = `${_ti('trends.methodology')}${payload?.meta?.current_year_incomplete ? ` ${_ti('trends.currentIncomplete')}` : ''}`;
  }
  const exportButton = document.getElementById('research-trends-export');
  if (exportButton) exportButton.disabled = false;
}

function setResearchTrendMetric(metric) {
  const normalized = ['count', 'share_per_million', 'index'].includes(metric) ? metric : 'count';
  state.researchTrends.metric = normalized;
  document.querySelectorAll('[data-trend-metric]').forEach((button) => {
    const active = button.dataset.trendMetric === normalized;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  renderResearchTrends();
}

async function runResearchTrends(event) {
  event?.preventDefault?.();
  if (state.researchTrends.loading) return;
  if (!isExternalDiscoveryEnabled()) {
    _researchTrendSetStatus(_ti('trends.externalRequired'), 'error');
    updateDiscoveryPrivacyUi();
    return;
  }
  const topics = _researchTrendTopicsFromForm();
  if (!topics.length) {
    _researchTrendSetStatus(_ti('trends.queryRequired'), 'error');
    document.getElementById('research-trend-query-0')?.focus();
    return;
  }
  const workTypes = [...document.querySelectorAll('input[name="research-trend-type"]:checked')]
    .map(input => input.value);
  if (!workTypes.length) {
    _researchTrendSetStatus(_ti('trends.typeRequired'), 'error');
    return;
  }
  const currentYear = new Date().getFullYear();
  const fromYear = Number.parseInt(document.getElementById('research-trends-from-year')?.value || '', 10);
  const toYear = Number.parseInt(document.getElementById('research-trends-to-year')?.value || '', 10);
  if (!Number.isFinite(fromYear) || !Number.isFinite(toYear) || fromYear < 1800 || fromYear > toYear || toYear > currentYear || toYear - fromYear >= 200) {
    _researchTrendSetStatus(_ti('trends.invalidYears'), 'error');
    return;
  }

  const controller = new AbortController();
  state.researchTrends.abortController = controller;
  state.researchTrends.loading = true;
  const runButton = document.getElementById('research-trends-run');
  if (runButton) runButton.disabled = true;
  _researchTrendSetStatus(_ti('trends.loading'), 'loading');
  try {
    const response = await fetch('/api/research-trends', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        topics,
        from_year: fromYear,
        to_year: toYear,
        work_types: workTypes,
        open_access_only: Boolean(document.getElementById('research-trends-oa-only')?.checked),
        privacy_ack: true,
      }),
      signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok || !payload?.ok) {
      throw new Error(_researchTrendErrorMessage(payload));
    }
    state.researchTrends.data = payload;
    renderResearchTrends(payload);
    _researchTrendSetStatus(payload?.meta?.cached ? _ti('trends.cached') : _ti('trends.ready'));
  } catch (error) {
    if (!controller.signal.aborted) {
      _researchTrendSetStatus(error?.message || _ti('trends.loadError'), 'error');
    }
  } finally {
    state.researchTrends.loading = false;
    state.researchTrends.abortController = null;
    if (runButton) runButton.disabled = false;
  }
}

function exportResearchTrendsCsv() {
  const payload = state.researchTrends.data;
  const series = Array.isArray(payload?.series) ? payload.series : [];
  const points = series[0]?.points || [];
  if (!series.length || !points.length) return;
  const csvCell = (value) => `"${String(value ?? '').replaceAll('"', '""')}"`;
  const header = [_ti('trends.year')];
  series.forEach((item) => {
    header.push(`${item.label} — count`, `${item.label} — share_per_million`, `${item.label} — index`);
  });
  const rows = [header.map(csvCell).join(',')];
  points.forEach((point, pointIndex) => {
    const row = [point.year];
    series.forEach((item) => {
      const value = item.points?.[pointIndex] || {};
      row.push(value.count ?? 0, value.share_per_million ?? 0, value.index ?? 0);
    });
    rows.push(row.map(csvCell).join(','));
  });
  const blob = new Blob([`\ufeff${rows.join('\r\n')}`], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `bibliobrain-trends-${payload?.meta?.from_year || 'data'}-${payload?.meta?.to_year || 'data'}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  showToast(_ti('trends.exported'), 2200, 'success');
}

function initResearchTrendsUi() {
  const currentYear = new Date().getFullYear();
  const toYear = document.getElementById('research-trends-to-year');
  const fromYear = document.getElementById('research-trends-from-year');
  if (toYear) {
    toYear.max = String(currentYear);
    if (!toYear.value) toYear.value = String(currentYear);
  }
  if (fromYear) fromYear.max = String(currentYear);
  document.querySelectorAll('[data-discovery-workspace]').forEach((button) => {
    button.addEventListener('click', () => switchDiscoveryWorkspace(button.dataset.discoveryWorkspace));
    button.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const current = DISCOVERY_WORKSPACES.indexOf(button.dataset.discoveryWorkspace);
      const step = event.key === 'ArrowRight' ? 1 : -1;
      const next = DISCOVERY_WORKSPACES[(current + step + DISCOVERY_WORKSPACES.length) % DISCOVERY_WORKSPACES.length];
      switchDiscoveryWorkspace(next);
      document.getElementById(`discovery-workspace-${next}`)?.focus();
    });
  });
  document.getElementById('research-trends-form')?.addEventListener('submit', runResearchTrends);
  document.getElementById('research-trends-add-topic')?.addEventListener('click', addResearchTrendTopic);
  document.querySelectorAll('[data-remove-trend-topic]').forEach((button) => {
    button.addEventListener('click', () => removeResearchTrendTopic(button.dataset.removeTrendTopic));
  });
  document.querySelectorAll('[data-trend-metric]').forEach((button) => {
    button.addEventListener('click', () => setResearchTrendMetric(button.dataset.trendMetric));
  });
  document.getElementById('research-trends-export')?.addEventListener('click', exportResearchTrendsCsv);
  window.addEventListener('bibliobrain:languagechange', () => {
    const active = document.querySelector('[data-discovery-workspace].is-active')?.dataset.discoveryWorkspace || 'articles';
    switchDiscoveryWorkspace(active, { focus: false });
    renderResearchTrends();
  });
}

initResearchTrendsUi();

// ---- Origines: where an idea comes from ----
// Two independent answers: the citation chain through the user's own papers
// (local, the same walk as /origine in the chat) and the roots of the topic,
// the older works its OpenAlex literature keeps citing (web access only).

const ORIGINS_LABELLED_ROOTS = 3;
const ORIGINS_OLDEST_SHARE = 0.1;

function _originsState() {
  if (!state.origins) state.origins = { runId: 0, roots: null, library: null };
  return state.origins;
}

function _originsSetStatus(id, message = '', kind = '') {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = `origins-status${kind ? ` is-${kind}` : ''}`;
  el.innerHTML = kind === 'loading' && message
    ? `<span class="spinner"></span> ${escapeHtml(message)}`
    : escapeHtml(message);
}

function _originsAuthors(work) {
  const authors = Array.isArray(work?.authors) ? work.authors : [];
  if (!authors.length) return '';
  const surname = (name) => String(name || '').trim().split(/\s+/).pop();
  return authors.length > 2 ? `${surname(authors[0])} et al.` : authors.map(surname).join(' & ');
}

function _originsWorkLabel(work) {
  const authors = _originsAuthors(work);
  return `${authors || _ti('origins.unknownAuthor')} (${work?.year || 'n.d.'})`;
}

function _originsPercent(share) {
  return `${Math.round(Number(share || 0) * 100)} %`;
}

// Oldest root cited by at least a tenth of the topic's papers: older ones
// cited by a handful are noise, not foundations.
function _originsOldestRoot(roots) {
  const strong = roots.filter((root) => Number(root.citing_share) >= ORIGINS_OLDEST_SHARE && root.year);
  return strong.sort((a, b) => (a.year - b.year) || (b.citing_count - a.citing_count))[0] || null;
}

function _originsRootsChart(roots) {
  const width = 960;
  const height = 300;
  const margin = { left: 56, right: 24, top: 22, bottom: 40 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const years = roots.map((root) => Number(root.year));
  const minYear = Math.min(...years) - 2;
  const maxYear = Math.max(...years) + 2;
  // Axis up to the next round ten percent, ticks every 10 % (20 % above 50 %).
  const maxShare = Math.max(0.1, Math.ceil(Math.max(...roots.map((root) => Number(root.citing_share))) * 10) / 10);
  const shareStep = maxShare > 0.5 ? 0.2 : 0.1;
  const xFor = (year) => margin.left + ((year - minYear) / Math.max(1, maxYear - minYear)) * plotWidth;
  const yFor = (share) => margin.top + plotHeight - (share / maxShare) * plotHeight;
  const grid = [];
  for (let share = 0; share <= maxShare + 1e-9; share += shareStep) {
    const y = yFor(share);
    grid.push(`<line class="research-trend-grid" x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}"></line>`);
    grid.push(`<text class="research-trend-axis-label" x="${margin.left - 8}" y="${y + 4}" text-anchor="end">${escapeHtml(_originsPercent(share))}</text>`);
  }
  const span = maxYear - minYear;
  const tickStep = span > 60 ? 20 : span > 25 ? 10 : 5;
  for (let year = Math.ceil(minYear / tickStep) * tickStep; year <= maxYear; year += tickStep) {
    grid.push(`<text class="research-trend-axis-label" x="${xFor(year)}" y="${height - 14}" text-anchor="middle">${year}</text>`);
  }
  const labelled = new Set([...roots].sort((a, b) => b.citing_count - a.citing_count)
    .slice(0, ORIGINS_LABELLED_ROOTS).map((root) => root.openalex_id));
  // Direct labels for the most cited roots, each placed where it overlaps
  // neither another label nor a dot (two papers by Bean, 1962 and 1964, sit
  // side by side on the critical state).
  const dots = roots.map((root) => ({ x: xFor(Number(root.year)), y: yFor(Number(root.citing_share)) }));
  const placed = [];
  const overlaps = (box) => placed.some((other) => box.x1 < other.x2 && box.x2 > other.x1 && box.y1 < other.y2 && box.y2 > other.y1)
    || dots.some((dot) => dot.x > box.x1 - 6 && dot.x < box.x2 + 6 && dot.y > box.y1 - 6 && dot.y < box.y2 + 6);
  const placeLabel = (x, y, text) => {
    const textWidth = text.length * 6.6;
    const options = [
      { anchor: 'start', tx: x + 10, ty: y - 8 },
      { anchor: 'start', tx: x + 10, ty: y + 18 },
      { anchor: 'end', tx: x - 10, ty: y - 8 },
      { anchor: 'end', tx: x - 10, ty: y + 18 },
    ];
    for (const option of options) {
      const x1 = option.anchor === 'start' ? option.tx : option.tx - textWidth;
      const box = { x1, x2: x1 + textWidth, y1: option.ty - 12, y2: option.ty + 3 };
      if (box.x1 < margin.left || box.x2 > width - margin.right || box.y1 < 0) continue;
      if (!overlaps(box)) {
        placed.push(box);
        return option;
      }
    }
    return null;
  };
  const marks = roots.map((root, index) => {
    const { x, y } = dots[index];
    const inLibrary = Boolean(root.library);
    const tip = `${_originsWorkLabel(root)} — ${root.title || ''} · ${_ti('origins.citedByShare', { share: _originsPercent(root.citing_share) })}`;
    const text = _originsWorkLabel(root);
    const spot = labelled.has(root.openalex_id) ? placeLabel(x, y, text) : null;
    const label = spot
      ? `<text class="origins-dot-label" x="${spot.tx}" y="${spot.ty}" text-anchor="${spot.anchor}">${escapeHtml(text)}</text>`
      : '';
    return `
      <line class="origins-stem" x1="${x}" y1="${yFor(0)}" x2="${x}" y2="${y + 5}"></line>
      <g class="origins-dot-hit" data-origins-root="${index}" tabindex="0" role="img" aria-label="${escapeAttr(tip)}">
        <circle class="origins-dot-target" cx="${x}" cy="${y}" r="12"></circle>
        <circle class="origins-dot${inLibrary ? ' is-library' : ''}" cx="${x}" cy="${y}" r="5"></circle>
        <title>${escapeHtml(tip)}</title>
      </g>
      ${label}`;
  }).join('');
  return `
    <svg class="origins-chart" viewBox="0 0 ${width} ${height}" role="img" aria-labelledby="origins-chart-title">
      <title id="origins-chart-title">${escapeHtml(_ti('origins.chartTitle'))}</title>
      <g>${grid.join('')}</g>
      <g>${marks}</g>
    </svg>
    <div class="origins-legend">
      <span><i class="origins-legend-dot is-library"></i>${escapeHtml(_ti('origins.legendLibrary'))}</span>
      <span><i class="origins-legend-dot"></i>${escapeHtml(_ti('origins.legendOutside'))}</span>
      <span class="origins-legend-axis">${escapeHtml(_ti('origins.axisNote'))}</span>
    </div>`;
}

function _originsRootActions(root) {
  const library = root.library;
  if (library?.rel_path) {
    return `
      <button type="button" class="drafting-mini-btn" data-origins-action="read" data-path="${escapeAttr(library.rel_path)}" data-title="${escapeAttr(root.title || '')}">${escapeHtml(_ti('origins.read'))}</button>
      <button type="button" class="drafting-mini-btn" data-origins-action="library" data-path="${escapeAttr(library.rel_path)}">${escapeHtml(_ti('origins.showInLibrary'))}</button>`;
  }
  return root.url
    ? `<a class="drafting-mini-btn" href="${escapeAttr(root.url)}" target="_blank" rel="noopener">${escapeHtml(_ti('discovery.viewAtPublisher'))}</a>`
    : '';
}

function renderOriginsRoots(payload) {
  const body = document.getElementById('origins-roots-body');
  if (!body) return;
  const roots = Array.isArray(payload?.roots) ? payload.roots : [];
  const meta = payload?.meta || {};
  if (!roots.length) {
    body.innerHTML = '';
    _originsSetStatus('origins-roots-status', _ti('origins.noRoots'));
    return;
  }
  _originsSetStatus('origins-roots-status', '');
  const oldest = _originsOldestRoot(roots);
  const summary = _ti('origins.rootsSummary', {
    count: meta.corpus_size || 0,
    first: meta.corpus_first_year || '?',
    median: meta.corpus_median_year || '?',
  });
  const ordered = [...roots].sort((a, b) => (a.year - b.year) || (b.citing_count - a.citing_count));
  body.innerHTML = `
    <p class="origins-summary">${escapeHtml(summary)}</p>
    ${oldest ? `<p class="origins-oldest">${escapeHtml(_ti('origins.oldestRoot'))} : <strong>${escapeHtml(_originsWorkLabel(oldest))}</strong> — ${escapeHtml(oldest.title || '')} · ${escapeHtml(_ti('origins.citedByShare', { share: _originsPercent(oldest.citing_share) }))}</p>` : ''}
    ${_originsRootsChart(roots)}
    <ol class="origins-root-list">
      ${ordered.map((root) => `
        <li class="origins-root">
          <span class="origins-root-year">${escapeHtml(String(root.year || ''))}</span>
          <span class="origins-root-main">
            <span class="origins-root-title">${escapeHtml(root.title || _ti('origins.untitled'))}</span>
            <span class="origins-root-meta">${escapeHtml([_originsAuthors(root), root.journal].filter(Boolean).join(' · '))}</span>
            <span class="origins-root-meta">${escapeHtml(_ti('origins.citedByShare', { share: _originsPercent(root.citing_share) }))}${root.library ? ` · <span class="origins-badge">${escapeHtml(_ti('origins.inLibrary'))}</span>` : ''}</span>
            <span class="origins-root-actions">${_originsRootActions(root)}</span>
          </span>
        </li>`).join('')}
    </ol>
    <p class="origins-note">${escapeHtml(_ti('origins.rootsNote'))}</p>`;
}

function _originsVerdictText(step) {
  if (step.error) return _ti('origins.stepUnreadable', { message: step.error });
  if (step.verdict === 'attributes_elsewhere') return _ti('origins.stepCredits');
  if (step.verdict === 'no_attribution') {
    return step.self_claim ? _ti('origins.stepSelfClaim') : _ti('origins.stepNoOther');
  }
  if (step.verdict === 'unchecked') return _ti('origins.stepUnchecked');
  return _ti('origins.stepUndetermined');
}

function _originsReferenceHtml(reference) {
  const library = reference.library;
  const text = String(reference.text || '').replace(/^\s*\[?\d{1,3}\]?\.?\s*/, '');
  const where = library?.rel_path
    ? `<button type="button" class="drafting-mini-btn" data-origins-action="read" data-path="${escapeAttr(library.rel_path)}" data-title="${escapeAttr(library.title || library.label || '')}">${escapeHtml(_ti('origins.read'))}</button>`
    : `<span class="drafting-origin-badge">${escapeHtml(_ti('origins.outside'))}</span>`;
  return `<li><span class="origins-ref-text">${escapeHtml(library?.label || text)}</span> ${where}</li>`;
}

function _originsStepHtml(step, steps) {
  const children = steps.filter((other) => other.parent === step.index);
  const passage = step.passage?.text
    ? `<details class="drafting-origin-passage"><summary class="disclosure">${escapeHtml(_ti('origins.passage'))}${step.passage.page ? ` · ${escapeHtml(String(step.passage.page))}` : ''}</summary><blockquote class="drafting-item-evidence">« ${escapeHtml(step.passage.text)} »</blockquote></details>`
    : '';
  return `
    <li class="origins-step">
      <div class="origins-step-head">
        <button type="button" class="origins-step-title" data-origins-action="read" data-path="${escapeAttr(step.rel_path)}" data-title="${escapeAttr(step.record?.title || step.label)}">${escapeHtml(step.label)}</button>
        <span class="origins-step-verdict">${escapeHtml(_originsVerdictText(step))}</span>
      </div>
      ${step.credited?.length ? `<ul class="origins-ref-list">${step.credited.map(_originsReferenceHtml).join('')}</ul>` : ''}
      ${passage}
      ${children.length ? `<ol class="origins-chain is-nested">${children.map((child) => _originsStepHtml(child, steps)).join('')}</ol>` : ''}
    </li>`;
}

function renderOriginsLibrary(payload) {
  const body = document.getElementById('origins-library-body');
  if (!body) return;
  const steps = Array.isArray(payload?.steps) ? payload.steps : [];
  if (!steps.length) {
    body.innerHTML = '';
    _originsSetStatus('origins-library-status', _ti('origins.noLibrary'));
    return;
  }
  _originsSetStatus('origins-library-status', '');
  const earliest = payload.earliest;
  const earliestText = earliest
    ? (earliest.library?.label || String(earliest.text || '').replace(/^\s*\[?\d{1,3}\]?\.?\s*/, ''))
    : '';
  const weak = steps.some((step) => step.model_reliability === 'low');
  body.innerHTML = `
    ${earliest ? `<p class="origins-oldest">${escapeHtml(_ti('origins.earliestCredited'))} : <strong>${escapeHtml(earliestText)}</strong>${earliest.library ? '' : ` · ${escapeHtml(_ti('origins.outside'))}`}</p>` : ''}
    <ol class="origins-chain">${steps.filter((step) => step.parent == null).map((step) => _originsStepHtml(step, steps)).join('')}</ol>
    <p class="origins-note">${escapeHtml(_ti('origins.libraryNote'))}${weak ? ` ${escapeHtml(_ti('origins.weakModel'))}` : ''}</p>`;
}

async function _originsFetch(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    cache: 'no-store',
    body: JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || payload?.ok === false) throw new Error(payload?.msg || _ti('origins.loadError'));
  return payload;
}

async function runOrigins(event) {
  event?.preventDefault?.();
  const origins = _originsState();
  const idea = String(document.getElementById('origins-query')?.value || '').trim();
  if (idea.length < 3) {
    document.getElementById('origins-query')?.focus();
    return;
  }
  const runId = ++origins.runId;
  document.getElementById('origins-empty')?.setAttribute('hidden', '');
  ['origins-roots', 'origins-library'].forEach((id) => { document.getElementById(id).hidden = false; });
  document.getElementById('origins-roots-body').innerHTML = '';
  document.getElementById('origins-library-body').innerHTML = '';

  const library = (async () => {
    _originsSetStatus('origins-library-status', _ti('origins.libraryLoading'), 'loading');
    try {
      const payload = await _originsFetch('/api/discovery/origins/library', {
        idea,
        model: optionalSelectedModel('discovery') || null,
      });
      if (runId === origins.runId) { origins.library = payload; renderOriginsLibrary(payload); }
    } catch (error) {
      if (runId === origins.runId) _originsSetStatus('origins-library-status', error.message, 'error');
    }
  })();

  const roots = (async () => {
    if (!isExternalDiscoveryEnabled()) {
      document.getElementById('origins-roots-status').innerHTML = `${escapeHtml(_ti('origins.webRequired'))} <button type="button" class="drafting-mini-btn" data-origins-action="settings">${escapeHtml(_ti('origins.openSettings'))}</button>`;
      return;
    }
    _originsSetStatus('origins-roots-status', _ti('origins.rootsLoading'), 'loading');
    try {
      const payload = await _originsFetch('/api/discovery/origins/roots', { topic: idea, privacy_ack: true });
      if (runId === origins.runId) { origins.roots = payload; renderOriginsRoots(payload); }
    } catch (error) {
      if (runId === origins.runId) _originsSetStatus('origins-roots-status', error.message, 'error');
    }
  })();
  await Promise.all([library, roots]);
}

function _onOriginsAction(event) {
  const button = event.target instanceof Element ? event.target.closest('[data-origins-action]') : null;
  if (!button) return;
  const action = button.dataset.originsAction;
  const path = String(button.dataset.path || '');
  if (action === 'settings') { openExternalDiscoverySettings(); return; }
  if (!path) return;
  if (action === 'library') goToSource(path);
  else openArticleViewer(path, button.dataset.title || path, button, null);
}

function initOriginsUi() {
  document.getElementById('origins-form')?.addEventListener('submit', runOrigins);
  document.getElementById('discovery-origins-view')?.addEventListener('click', _onOriginsAction);
  window.addEventListener('bibliobrain:languagechange', () => {
    const origins = _originsState();
    if (origins.roots) renderOriginsRoots(origins.roots);
    if (origins.library) renderOriginsLibrary(origins.library);
  });
}

initOriginsUi();

async function generateAnalysis() {
  const relPath      = _currentSelectedArticlePath();
  const analysisType = document.getElementById('analysis-type-select').value;
  const model        = requireSelectedModel();
  const result       = document.getElementById('analysis-result');
  const btn          = document.getElementById('analysis-btn');
  if (!relPath) { showToast(_tr('Sélectionnez un article.')); return; }
  if (!model) return;
  _syncArticleSelectValue(relPath, document.getElementById('library-detail-title')?.textContent?.trim() || relPath);
  const bucket = _ensureArticleAnalysisState(relPath);
  bucket.lastView = 'analysis';
  _rememberArticleAnalysisView(relPath, 'analysis');
  bucket.lastType = analysisType;
  const requestKey = `generate::${relPath}::${analysisType}::${Date.now()}`;
  state.analysisViewRequestKey = requestKey;
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
  result.innerHTML = '<span style="color:var(--muted);font-style:italic"><span class="spinner"></span> ' + _tr('Génération en cours…') + '</span>';
  _updateAnalysisNotebookButton();
  try {
    await syncOllamaBaseUrl({ silent: true });
    const response = await fetch('/api/summary', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        rel_path: relPath,
        analysis_type: analysisType,
        model,
        thinking: isThinkingEnabled(),
        force_thinking_small_models: isForceThinkingSmallModelsEnabled(),
      }),
    });
    for await (const event of readSSE(response)) {
      if (event.type === 'progress') {
        if (state.analysisViewRequestKey === requestKey) {
          result.innerHTML = `<span style="color:var(--muted);font-style:italic"><span class="spinner"></span> ${escapeHtml(event.desc)}</span>`;
        }
      } else if (event.type === 'done') {
        bucket.byType[analysisType] = event.text || '';
        bucket.fetched[analysisType] = true;
        const stillSelected = _currentSelectedArticlePath() === relPath
          && (document.getElementById('analysis-type-select')?.value || '') === analysisType;
        if (state.analysisViewRequestKey === requestKey && stillSelected) {
          _renderStoredAnalysis(event.text || '');
        }
        await loadItemCitations(relPath);
        await loadLibrary();
      } else if (event.type === 'error') {
        throw new Error(event.msg || _tr("La génération de l'analyse a échoué."));
      }
    }
  } catch(e) {
    if (state.analysisViewRequestKey === requestKey) {
      result.innerHTML = `<span style="color:var(--red)">Erreur : ${escapeHtml(e.message)}</span>`;
    }
  }
  btn.disabled = false; btn.innerHTML = _tr("Générer l'analyse");
  _updateAnalysisNotebookButton();
}
