//  SETTINGS PANEL
// ---------------------------------------------
// Everyday areas ask plain questions; technical controls live in Expert.
// The former tab names stay valid for callers and saved preferences.
const SETTINGS_TAB_ALIASES = Object.freeze({
  preparation: 'overview',
  general: 'look',
  library: 'documents',
  models: 'assistant',
  research: 'online',
  prompts: 'expert-prompts',
  advanced: 'expert-diagnostics',
});
const _settingsLastTab = { everyday: 'overview', expert: 'expert-generation' };

function _resolveSettingsTab(tabName) {
  const requested = String(tabName || 'overview').trim().toLowerCase();
  const name = SETTINGS_TAB_ALIASES[requested] || requested;
  return document.getElementById(`settings-pane-${name}`) ? name : 'overview';
}

function toggleSettings() {
  const panel   = document.getElementById('settings-panel');
  const overlay = document.getElementById('settings-overlay');
  const btn     = document.getElementById('settings-btn');
  if (panel.classList.contains('open')) {
    closeSettings();
  } else {
    // The settings button lives inside the mobile drawer. Close that drawer
    // before showing the modal so it cannot cover the settings panel.
    if (typeof isMobileNavViewport === 'function' && isMobileNavViewport()) {
      setMobileNavOpen(false);
    }
    state.settingsReturnFocus = document.activeElement;
    panel.style.display = 'flex';
    panel.offsetHeight;
    panel.classList.add('open');
    overlay.classList.add('open');
    btn.classList.add('open');
    const search = document.getElementById('settings-search');
    if (search) search.value = '';
    filterSettings('');
    const savedTab = localStorage.getItem('iassistant_settings_tab');
    switchSettingsTab(hasCompletedFirstRagInit() && savedTab ? savedTab : 'overview');
    refreshSettingsOverview();
    refreshSettingsDefaults();
    if (typeof loadApiSecrets === 'function') loadApiSecrets();
    window.setTimeout(() => {
      document.querySelector('#settings-tabs .settings-tab.active')?.focus();
    }, 40);
  }
}
function switchSettingsTab(tabName, btn = null) {
  const target = _resolveSettingsTab(tabName);
  const activePane = document.getElementById(`settings-pane-${target}`);
  const mode = activePane?.dataset.settingsMode === 'expert' ? 'expert' : 'everyday';
  _settingsLastTab[mode] = target;
  localStorage.setItem('iassistant_settings_tab', target);
  document.getElementById('settings-panel')?.setAttribute('data-settings-mode', mode);
  document.querySelectorAll('#settings-tabs .settings-tab').forEach((el) => {
    const active = String(el.dataset.settingsTab || '').toLowerCase() === target;
    el.classList.toggle('active', active);
    el.setAttribute('aria-selected', active ? 'true' : 'false');
    el.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('#settings-body .settings-pane').forEach((pane) => {
    const active = pane === activePane;
    pane.classList.toggle('active', active);
    pane.setAttribute('role', 'tabpanel');
    pane.hidden = !active;
  });
  if (activePane) activePane.scrollTop = 0;
  const search = document.getElementById('settings-search');
  if (search?.value) filterSettings(search.value, { autoNavigate: false });
  if (
    target === 'expert-indexing'
    && typeof loadRemoteEmbeddingModels === 'function'
    && isRemoteMode(activeProviderMode())
  ) {
    loadRemoteEmbeddingModels({ silent: true });
  }
  if (btn) btn.blur();
  const activeTab = document.querySelector('#settings-tabs .settings-tab.active');
  if (window.innerWidth <= 820) activeTab?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

// Expert is a separate door in the header; each mode remembers its last page.
function toggleSettingsMode() {
  const expert = document.getElementById('settings-panel')?.dataset.settingsMode === 'expert';
  switchSettingsTab(expert ? _settingsLastTab.everyday : _settingsLastTab.expert);
  document.querySelector('#settings-tabs .settings-tab.active')?.focus();
}

function openSettingsAt(tabName, focusId = '') {
  if (!document.getElementById('settings-panel')?.classList.contains('open')) toggleSettings();
  switchSettingsTab(tabName);
  if (!focusId) return;
  window.setTimeout(() => {
    const target = document.getElementById(focusId);
    target?.scrollIntoView?.({ block: 'center', behavior: preferredScrollBehavior() });
    target?.focus();
  }, 80);
}

function openExternalApiSettings() {
  const panel = document.getElementById('settings-panel');
  if (!panel?.classList.contains('open')) {
    toggleSettings();
  }
  switchSettingsTab('online');
  window.setTimeout(() => {
    const target = document.getElementById('allow-external-discovery');
    target?.scrollIntoView?.({ block: 'center', behavior: preferredScrollBehavior() });
    target?.focus();
  }, 80);
}

function _normalizeSettingsSearchText(value) {
  return String(value || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLocaleLowerCase()
    .replace(/\s+/g, ' ')
    .trim();
}

function filterSettings(value, options = {}) {
  const query = _normalizeSettingsSearchText(value);
  const autoNavigate = options.autoNavigate !== false;
  // A search looks through both modes, so both navigations stay visible.
  document.getElementById('settings-panel')?.classList.toggle('settings-searching', Boolean(query));
  const panes = Array.from(document.querySelectorAll('#settings-body .settings-pane'));
  const tabs = Array.from(document.querySelectorAll('#settings-tabs .settings-tab'));
  const counts = new Map();
  let totalMatches = 0;
  let firstMatchingTab = '';

  panes.forEach((pane) => {
    let paneMatches = 0;
    const sections = Array.from(pane.querySelectorAll(':scope > .settings-section'));
    sections.forEach((section) => {
      if (section.hidden) return;
      const rows = Array.from(section.querySelectorAll('.settings-row'));
      const sectionHeader = [
        section.querySelector('.settings-section-label')?.textContent,
        section.querySelector('.settings-section-note')?.textContent,
      ].join(' ');
      const sectionMatches = Boolean(query)
        && _normalizeSettingsSearchText(sectionHeader).includes(query);
      let visibleMatches = 0;

      rows.forEach((row) => {
        const hiddenParent = row.closest('[hidden]');
        const available = (!hiddenParent || hiddenParent === pane) && row.style.display !== 'none';
        const rowMatches = !query
          || sectionMatches
          || _normalizeSettingsSearchText(row.textContent).includes(query);
        row.classList.toggle('settings-search-hidden', Boolean(query) && !rowMatches);
        if (query && available && rowMatches) {
          visibleMatches += 1;
          let details = row.closest('details');
          while (details && section.contains(details)) {
            if (!details.hasAttribute('data-search-was-open')) details.dataset.searchWasOpen = String(details.open);
            details.open = true;
            details = details.parentElement.closest('details');
          }
        }
      });

      if (query && sectionMatches && visibleMatches === 0) visibleMatches = 1;
      section.classList.toggle('settings-search-hidden', Boolean(query) && visibleMatches === 0);
      paneMatches += visibleMatches;
    });

    const tabName = pane.id.replace('settings-pane-', '').toLowerCase();
    counts.set(tabName, paneMatches);
    if (paneMatches > 0 && !firstMatchingTab) firstMatchingTab = tabName;
    totalMatches += paneMatches;
  });

  if (!query) {
    document.querySelectorAll('[data-search-was-open]').forEach((details) => {
      details.open = details.dataset.searchWasOpen === 'true';
      delete details.dataset.searchWasOpen;
    });
  }

  tabs.forEach((tab) => {
    const count = counts.get(String(tab.dataset.settingsTab || '').toLowerCase()) || 0;
    tab.classList.toggle('settings-search-hidden', Boolean(query) && totalMatches > 0 && count === 0);
    if (query && count > 0) tab.dataset.settingsMatchCount = String(count);
    else delete tab.dataset.settingsMatchCount;
  });

  if (query && totalMatches > 0 && autoNavigate) {
    const activeTab = document.querySelector('#settings-tabs .settings-tab.active');
    const activeCount = counts.get(String(activeTab?.dataset.settingsTab || '').toLowerCase()) || 0;
    if (activeCount === 0 && firstMatchingTab) {
      switchSettingsTab(firstMatchingTab);
    }
  }

  const status = document.getElementById('settings-search-status');
  if (status) {
    status.textContent = query
      ? (totalMatches > 0
        ? _ti('settings.searchResults', { count: totalMatches })
        : _ti('settings.searchNoResults'))
      : '';
  }
}

document.addEventListener('keydown', (event) => {
  const tab = event.target?.closest?.('#settings-tabs .settings-tab');
  if (!tab || !['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  const tabs = Array.from(document.querySelectorAll('#settings-tabs .settings-tab:not(.settings-search-hidden)'))
    .filter(node => node.getClientRects().length);
  if (!tabs.length) return;
  event.preventDefault();
  const currentIndex = Math.max(0, tabs.indexOf(tab));
  let nextIndex = currentIndex;
  if (event.key === 'Home') nextIndex = 0;
  else if (event.key === 'End') nextIndex = tabs.length - 1;
  else if (event.key === 'ArrowDown' || event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % tabs.length;
  else nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
  const next = tabs[nextIndex];
  switchSettingsTab(next.dataset.settingsTab, next);
  next.focus();
});

function closeSettings() {
  const panel   = document.getElementById('settings-panel');
  const overlay = document.getElementById('settings-overlay');
  const btn     = document.getElementById('settings-btn');
  panel.classList.remove('open');
  overlay.classList.remove('open');
  btn.classList.remove('open');
  setTimeout(() => { if (!panel.classList.contains('open')) panel.style.display = 'none'; }, 220);
  const restore = state.settingsReturnFocus;
  if (restore && typeof restore.focus === 'function') {
    window.setTimeout(() => restore.focus(), 0);
  }
  state.settingsReturnFocus = null;
}

// Status belongs to the edited group, and is only marked saved after the server replies.
let _settingsSaveSerial = 0;
let _settingsWriteQueue = Promise.resolve();
function settingsWrite(url, payload, failure) {
  const task = _settingsWriteQueue.catch(() => null).then(() => postJsonOrThrow(url, payload, failure));
  _settingsWriteQueue = task;
  return task;
}
function beginSettingsSave(options = {}) {
  const source = options.source || document.activeElement;
  const section = source?.closest?.('#settings-panel .settings-section');
  if (!section) return null;
  let status = section.querySelector(':scope > .settings-save-feedback');
  if (!status) {
    status = document.createElement('div');
    status.className = 'settings-save-feedback';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    section.append(status);
  }
  const serial = String(++_settingsSaveSerial);
  status.dataset.serial = serial;
  status.dataset.state = 'saving';
  status.textContent = _ti('settings.saving');
  return { status, serial };
}
function finishSettingsSave(ticket, ok, retry) {
  refreshSettingsDefaults();
  if (!ticket || ticket.status.dataset.serial !== ticket.serial) return;
  const { status } = ticket;
  status.dataset.state = ok ? 'saved' : 'error';
  status.textContent = _ti(ok ? 'settings.saved' : 'settings.saveFailed') + ' ';
  if (!ok && retry) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'settings-link-button';
    button.textContent = _ti('settings.retry');
    button.addEventListener('click', retry);
    status.append(button);
  }
}
function setSettingsDraft(name, dirty) {
  const group = document.querySelector(`[data-settings-draft="${name}"]`);
  if (!group) return;
  group.dataset.dirty = String(dirty);
  if (name === 'prompt') {
    const type = document.getElementById('analysis-prompt-type-select');
    if (type) type.disabled = dirty;
  }
  let status = group.querySelector(':scope > .settings-draft-status');
  if (!status) {
    status = document.createElement('div');
    status.className = 'settings-draft-status';
    status.setAttribute('role', 'status');
    group.append(status);
  }
  status.textContent = dirty ? _ti('settings.unsaved') : '';
  status.hidden = !dirty;
}
function settingsDraftIsDirty(name) {
  return document.querySelector(`[data-settings-draft="${name}"]`)?.dataset.dirty === 'true';
}
document.addEventListener('input', (event) => {
  const group = event.target.closest?.('[data-settings-draft]');
  if (group && (group.dataset.settingsDraft !== 'prompt' || event.target.id === 'analysis-prompt-editor')) {
    setSettingsDraft(group.dataset.settingsDraft, true);
  }
  refreshSettingsDefaults();
});
window.addEventListener('beforeunload', (event) => {
  if (document.querySelector('[data-settings-draft][data-dirty="true"]')) {
    event.preventDefault();
    event.returnValue = '';
  }
});

function refreshSettingsDefaults() {
  if (typeof LLM_GENERATION_RECOMMENDED_DEFAULTS === 'undefined') return;
  const value = (id) => document.getElementById(id)?.value;
  const checked = (id) => document.getElementById(id)?.checked;
  const llm = LLM_GENERATION_RECOMMENDED_DEFAULTS;
  const research = RESEARCH_RECOMMENDED_DEFAULTS;
  const generationDefault = Number(value('llm-temperature')) === llm.temperature
    && Number(value('llm-top-p')) === llm.topP
    && (!isOllamaMode(activeProviderMode()) || (
      Number(value('llm-repeat-penalty')) === llm.repeatPenalty && Number(value('ollama-context-length')) === 0
    ));
  const retrievalDefault = Number(value('threshold')) === research.threshold
    && Number(value('max-chunks')) === research.maxChunks
    && Number(value('chunk-size')) === research.chunkSize
    && checked('reranker-enabled') === research.rerankerEnabled
    && value('reranker-profile') === research.rerankerProfile
    && checked('reranker-translate-french') === research.rerankerTranslateFrench;
  [['settings-generation-summary', generationDefault], ['settings-retrieval-summary', retrievalDefault]].forEach(([id, defaults]) => {
    const node = document.getElementById(id);
    if (node) {
      node.textContent = _ti(defaults ? 'settings.defaultsActive' : 'settings.customActive');
      node.dataset.recommended = String(defaults);
    }
  });
  renderSettingsExpertDefaults();
}

// ---------------------------------------------
//  AREA STATUS (Overview, sidebar dots, pills)
// ---------------------------------------------
const SETTINGS_AREAS = ['documents', 'assistant', 'online', 'look'];
const SETTINGS_ACTION_LABELS = Object.freeze({
  setup: 'settings.overview.setUp',
  prepare: 'settings.overview.prepare',
  fix: 'settings.overview.fix',
  check: 'ocr.quickfix.check',
  connect: 'settings.overview.fix',
});

function _settingsCount(value) {
  const locale = window.getLanguage?.() === 'en' ? 'en-GB' : 'fr-FR';
  return Number(value || 0).toLocaleString(locale);
}

function _settingsDocumentsArea() {
  const snapshot = state.startupStatus || {};
  const count = _settingsCount(snapshot.library_count);
  const busy = Boolean(state.ragInitBusy || state.startupAutoInitRunning || state.startupGuideRagRunning);
  const ocr = typeof window.ocrQuickFixSummary === 'function' ? window.ocrQuickFixSummary() : null;
  if (!snapshot.library_configured) {
    return { state: 'needs', text: _ti('settings.overview.docsMissing'), action: 'setup' };
  }
  if (busy) return { state: 'working', text: _ti('settings.overview.docsPreparing', { count }), pill: 'settings.pill.preparing' };
  if (!(snapshot.rag?.ready || state.ragReady)) {
    // Say why: after a change of AI provider the index is usually kept and
    // only needs reloading, or must be redone for a new indexing model.
    const indexStatus = String(snapshot.vector_db?.status || '');
    const key = indexStatus === 'embedding_mismatch'
      ? 'settings.overview.docsModelChanged'
      : (indexStatus === 'present' ? 'settings.overview.docsNotLoaded' : 'settings.overview.docsNotPrepared');
    return { state: 'needs', text: _ti(key, { count }), action: 'prepare' };
  }
  if (ocr?.view === 'working') {
    const percent = Number.isFinite(ocr.percent) ? ocr.percent : null;
    return {
      state: 'working',
      text: _ti('settings.overview.docsReading', { count }),
      badge: percent === null ? '' : `${percent}%`,
      pill: 'settings.pill.readingScans',
    };
  }
  if (ocr && ['offer', 'confirm', 'blocked'].includes(ocr.view)) {
    return {
      state: 'needs',
      text: _ti('settings.overview.docsScans', { count, documents: _settingsCount(ocr.documents) }),
      action: 'fix',
    };
  }
  if (ocr?.view === 'check' || ocr?.view === 'checking') {
    return {
      state: 'ok',
      text: _ti('settings.overview.docsMaybeScans', { count }),
      action: ocr.view === 'check' ? 'check' : '',
    };
  }
  return { state: 'ok', text: _ti('settings.overview.docsFound', { count }) };
}

function _settingsAssistantArea() {
  const mode = normalizeProviderMode(activeProviderMode());
  const location = activeProviderLocation();
  const where = _ti({
    local: 'settings.overview.aiLocal',
    network: 'settings.overview.aiNetwork',
    remote: 'settings.overview.aiOnline',
  }[location]);
  const software = location === 'remote' ? '' : providerDisplayName(mode);
  // The sidebar's "Loading…" placeholder has no value attribute: not a model name.
  const loaded = Boolean(document.getElementById('model-select')?.selectedOptions[0]?.hasAttribute('value'));
  const select = document.getElementById('settings-model-select');
  const model = loaded && select && !select.disabled ? String(select.value || '').trim() : '';
  const summary = [where, software, model].filter(Boolean).join(' · ');
  const connection = state.settingsChatConnection;
  if (connection?.mode === activeProviderMode() && connection.ok) return { state: 'ok', text: summary };
  if (connection?.mode === activeProviderMode()) {
    return { state: 'needs', text: _ti('settings.overview.aiDown', { where }), action: 'connect' };
  }
  return { state: 'neutral', text: summary };
}

function settingsAreaStates() {
  const allowed = Boolean(document.getElementById('allow-external-discovery')?.checked);
  let themePreference = 'dark';
  try { themePreference = localStorage.getItem('iassistant_theme') || 'dark'; } catch { /* default theme */ }
  const theme = document.querySelector(`#settings-panel [data-theme-choice="${themePreference}"]`);
  const language = window.getLanguage?.() === 'en' ? 'English' : 'Français';
  return {
    documents: _settingsDocumentsArea(),
    assistant: _settingsAssistantArea(),
    online: allowed
      ? { state: 'ok', text: _ti('settings.overview.onlineOn') }
      : { state: 'neutral', text: _ti('settings.overview.onlineOff') },
    look: { state: 'neutral', text: [language, theme?.textContent.trim()].filter(Boolean).join(' · ') },
  };
}

function _setSettingsPill(id, state, key) {
  const pill = document.getElementById(id);
  if (!pill) return;
  pill.dataset.state = state;
  pill.textContent = key ? _ti(key) : '';
  pill.hidden = !key;
}

// "1 thing to fix" says what it is and offers the fix right below the heading;
// the pill itself leads there.
function _refreshSettingsDocumentsNeeds(area) {
  const card = document.getElementById('settings-documents-needs');
  const pill = document.getElementById('settings-documents-pill');
  const needs = area?.state === 'needs';
  if (card) {
    card.hidden = !needs;
    const text = document.getElementById('settings-documents-needs-text');
    if (text) text.textContent = needs ? area.text : '';
    const action = document.getElementById('settings-documents-needs-action');
    if (action) {
      action.hidden = !(needs && area.action);
      action.dataset.action = area?.action || '';
      if (needs && area.action) action.textContent = _ti(SETTINGS_ACTION_LABELS[area.action]);
    }
  }
  if (pill) {
    pill.classList.toggle('is-actionable', needs);
    pill.setAttribute('role', needs ? 'button' : 'status');
    if (needs) pill.tabIndex = 0;
    else pill.removeAttribute('tabindex');
  }
}

function _openSettingsDocumentsNeeds() {
  const card = document.getElementById('settings-documents-needs');
  if (!card || card.hidden) return;
  card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  const action = document.getElementById('settings-documents-needs-action');
  (action && !action.hidden ? action : card).focus?.();
  card.classList.remove('is-flash');
  void card.offsetWidth;
  card.classList.add('is-flash');
}

document.addEventListener('click', (event) => {
  if (event.target.closest?.('#settings-documents-pill.is-actionable')) _openSettingsDocumentsNeeds();
});
document.addEventListener('keydown', (event) => {
  if (!['Enter', ' '].includes(event.key)) return;
  if (!event.target.closest?.('#settings-documents-pill.is-actionable')) return;
  event.preventDefault();
  _openSettingsDocumentsNeeds();
});

function refreshSettingsOverview() {
  // startup.js may call this before settings.js (loaded last) defines the provider helpers.
  if (!document.getElementById('settings-overview') || typeof activeProviderMode !== 'function') return;
  const areas = settingsAreaStates();
  const needs = SETTINGS_AREAS.filter(name => areas[name].state === 'needs').length;
  const ready = SETTINGS_AREAS.length - needs;
  const readyLabel = document.getElementById('settings-overview-ready');
  if (readyLabel) readyLabel.textContent = _ti('settings.overview.ready', { ready, total: SETTINGS_AREAS.length });
  const meter = document.getElementById('settings-overview-meter');
  if (meter) meter.style.width = `${Math.round((100 * ready) / SETTINGS_AREAS.length)}%`;

  SETTINGS_AREAS.forEach((name) => {
    const area = areas[name];
    const row = document.querySelector(`[data-overview-area="${name}"]`);
    if (row) row.dataset.state = area.state;
    const text = document.getElementById(`settings-overview-${name}`);
    if (text) text.textContent = area.text;
    const action = document.getElementById(`settings-overview-${name}-action`);
    if (action) {
      action.hidden = !area.action;
      action.dataset.action = area.action || '';
      if (area.action) action.textContent = _ti(SETTINGS_ACTION_LABELS[area.action]);
    }
    const dot = document.querySelector(`[data-area-dot="${name}"]`);
    if (dot) dot.dataset.state = area.state;
    const badge = document.querySelector(`[data-area-badge="${name}"]`);
    if (badge) {
      badge.dataset.state = area.state;
      badge.textContent = area.state === 'needs' ? _ti('settings.nav.toFix') : (area.badge || '');
    }
  });
  const overviewDot = document.querySelector('[data-area-dot="overview"]');
  if (overviewDot) overviewDot.dataset.state = needs ? 'needs' : 'ok';

  const documents = areas.documents;
  _setSettingsPill('settings-documents-pill', documents.state, {
    needs: 'settings.pill.toFix', working: documents.pill, ok: 'settings.pill.allGood',
  }[documents.state]);
  _refreshSettingsDocumentsNeeds(documents);
  _setSettingsPill('settings-assistant-pill', areas.assistant.state, {
    ok: 'settings.pill.working', needs: 'settings.pill.notConnected', neutral: 'settings.pill.notChecked',
  }[areas.assistant.state]);
  _setSettingsPill('settings-online-pill', areas.online.state, areas.online.state === 'ok' ? 'settings.pill.allowed' : 'settings.pill.off');

  const snapshot = state.startupStatus || {};
  const folderDesc = document.getElementById('settings-docs-folder-desc');
  if (folderDesc) {
    folderDesc.textContent = snapshot.library_configured && snapshot.library_count
      ? _ti('settings.docs.folderFound', { count: _settingsCount(snapshot.library_count) })
      : _ti('settings.docs.folderDesc');
  }
  _refreshSettingsMetadataCard();
  _refreshSettingsOnlineSources();
  _refreshSettingsProviderChoices();
}
window.refreshSettingsOverview = refreshSettingsOverview;

function _refreshSettingsMetadataCard() {
  const source = state.metadataSource || 'none';
  const pill = document.getElementById('settings-zotero-state');
  if (pill) {
    pill.hidden = source === 'none';
    pill.textContent = _ti(source === 'bibtex' ? 'settings.docs.bibtexConnected' : 'settings.docs.connected');
  }
  const disconnect = document.getElementById('metadata-disconnect-btn');
  if (disconnect) disconnect.hidden = source === 'none';
  // Offer Zotero's storage folder only when it differs from the current one.
  const folderKey = value => String(value || '').trim().replace(/[\\/]+$/, '').replaceAll('\\', '/').toLowerCase();
  const useZotero = document.getElementById('btn-use-zotero');
  if (useZotero) {
    useZotero.hidden = !state.zoteroStorage
      || folderKey(state.zoteroStorage) === folderKey(document.getElementById('data-path')?.value);
  }
  const detect = document.getElementById('zotero-detect-btn');
  if (detect) detect.hidden = source !== 'none';
  // Once a source is connected the header button is gone: a path typed by
  // hand must still have a way back to the detected one.
  const backToDefault = document.getElementById('zotero-default-btn');
  if (backToDefault) backToDefault.hidden = source === 'none';
}

function _refreshSettingsOnlineSources() {
  const allowed = Boolean(document.getElementById('allow-external-discovery')?.checked);
  document.querySelectorAll('[data-online-source]').forEach((node) => {
    node.dataset.state = allowed ? 'ok' : 'neutral';
    node.textContent = _ti(allowed ? 'settings.online.used' : 'settings.online.off');
  });
  const keyStatus = document.querySelector('[data-api-secret-status="semantic_scholar_api_key"]');
  const keyConfigured = Boolean(keyStatus?.classList.contains('configured') || keyStatus?.classList.contains('environment'));
  const semanticState = document.querySelector('[data-online-source="semantic_scholar"]');
  if (semanticState) semanticState.hidden = !keyConfigured;
  const keyButton = document.getElementById('settings-semantic-key-btn');
  if (keyButton) keyButton.textContent = _ti(keyConfigured ? 'settings.online.changeKey' : 'settings.online.addKey');
}

function _refreshSettingsProviderChoices() {
  if (typeof settingsLocationChoice !== 'function') return;
  const location = settingsLocationChoice();
  const software = normalizeProviderMode(settingsProviderChoice());
  const pending = Boolean(document.getElementById('provider-mode-select')?.disabled);
  document.querySelectorAll('[data-provider-location]').forEach((button) => {
    const selected = button.dataset.providerLocation === location;
    button.setAttribute('aria-checked', selected ? 'true' : 'false');
    button.tabIndex = selected ? 0 : -1;
    button.disabled = pending;
  });
  // Ollama or LM Studio only matters for a model server you run yourself.
  const softwareRow = document.getElementById('settings-local-software');
  if (softwareRow) softwareRow.hidden = location === 'remote';
  document.querySelectorAll('[data-provider-software]').forEach((button) => {
    const selected = button.dataset.providerSoftware === software;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-pressed', selected ? 'true' : 'false');
    button.disabled = pending;
  });
}

function chooseSettingsProviderLocation(location) {
  if (location === settingsLocationChoice()) return;
  void chooseProviderLocation(location);
  _refreshSettingsProviderChoices();
}

function chooseSettingsProviderSoftware(software) {
  if (software === normalizeProviderMode(settingsProviderChoice())) return;
  void chooseLocalSoftware(software);
  _refreshSettingsProviderChoices();
}

function runSettingsOverviewAction(area, action) {
  if (action === 'setup' || action === 'prepare') openStartupAssistant();
  else if (action === 'fix') window.openOcrQuickFix?.();
  else if (action === 'check') {
    switchSettingsTab('documents');
    window.startOcrInventory?.();
  } else switchSettingsTab(area);
}

// ---------------------------------------------
//  PDF FOLDER AND METADATA SOURCE
// ---------------------------------------------
async function applySettingsPdfFolder() {
  const result = await setDataPath();
  const apply = document.getElementById('data-path-apply-btn');
  if (apply && result?.ok) apply.hidden = true;
  refreshSettingsOverview();
  return result;
}

async function selectSettingsPdfDirectory() {
  const button = document.getElementById('data-path-browse-btn');
  const input = document.getElementById('data-path');
  if (button) button.disabled = true;
  try {
    const response = await fetch('/api/select-data-directory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: normalizeUserPathInput(input?.value) }),
    });
    const data = await response.json();
    if (!response.ok || data?.ok === false) {
      showToast(_ti(data?.error === 'directory_picker_busy' ? 'startup.pdfFolderPickerBusy' : 'startup.pdfFolderPickerError'), 5200, 'error');
      return;
    }
    if (data?.cancelled || !data?.path) return;
    if (input) input.value = data.path;
    await applySettingsPdfFolder();
  } catch {
    showToast(_ti('startup.pdfFolderPickerError'), 5200, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

async function disconnectMetadataSource() {
  const button = document.getElementById('metadata-disconnect-btn');
  if (button) button.disabled = true;
  try {
    const path = document.getElementById('zotero-path');
    if (path) path.value = '';
    setMetadataSource('none');
    _applyZoteroStorage({});
    if (!(await syncAppSettings({ silent: true, source: button }))) return;
    if (typeof loadLibrary === 'function') await loadLibrary();
    if (typeof loadZoteroFacets === 'function') await loadZoteroFacets();
    showZoteroStatus(_ti('settings.docs.disconnected'));
  } finally {
    if (button) button.disabled = false;
    refreshSettingsOverview();
  }
}

// ---------------------------------------------
//  EXPERT DEFAULTS (one Reset per setting)
// ---------------------------------------------
function settingsExpertDefaults() {
  if (typeof LLM_GENERATION_RECOMMENDED_DEFAULTS === 'undefined') return {};
  const llm = LLM_GENERATION_RECOMMENDED_DEFAULTS;
  const research = RESEARCH_RECOMMENDED_DEFAULTS;
  return {
    'ollama-context-length': '0',
    'llm-temperature': llm.temperature,
    'llm-repeat-penalty': llm.repeatPenalty,
    'llm-top-p': llm.topP,
    threshold: research.threshold,
    'max-chunks': research.maxChunks,
    'reranker-enabled': research.rerankerEnabled,
    'reranker-profile': research.rerankerProfile,
    'reranker-translate-french': research.rerankerTranslateFrench,
    'chunk-size': research.chunkSize,
    'discovery-candidate-multiplier': 4,
    'discovery-validation-multiplier': 3,
  };
}

function _settingsDefaultLabel(input, value) {
  if (input.type === 'checkbox') return _ti(value ? 'settings.expert.on' : 'settings.expert.off');
  if (input.tagName === 'SELECT') {
    return Array.from(input.options).find(option => option.value === String(value))?.textContent.trim() || String(value);
  }
  const decimals = String(input.step || '').split('.')[1]?.length || 0;
  return Number(value).toFixed(decimals);
}

function _settingsIsDefault(input, value) {
  if (input.type === 'checkbox') return input.checked === Boolean(value);
  if (input.tagName === 'SELECT') return input.value === String(value);
  return Number(input.value) === Number(value);
}

function _settingsDefaultRows() {
  const rows = new Map();
  Object.entries(settingsExpertDefaults()).forEach(([id, value]) => {
    const input = document.getElementById(id);
    const row = input?.closest('.settings-row');
    if (!row) return;
    if (!rows.has(row)) rows.set(row, []);
    rows.get(row).push([input, value]);
  });
  return rows;
}

function renderSettingsExpertDefaults() {
  _settingsDefaultRows().forEach((controls, row) => {
    // Beside the description, not inside it: i18n rewrites translated text.
    let hint = row.querySelector('.settings-default-hint');
    let reset = row.querySelector('.settings-reset-one');
    if (!hint) {
      hint = document.createElement('span');
      hint.className = 'settings-row-desc settings-default-hint';
      row.querySelector('.settings-row-info')?.append(hint);
    }
    if (!reset) {
      reset = document.createElement('button');
      reset.type = 'button';
      reset.className = 'btn btn-ghost btn-small settings-reset-one';
      reset.addEventListener('click', () => resetSettingsRow(row));
      row.querySelector('.settings-control')?.append(reset);
    }
    if (!hint || !reset) return;
    hint.textContent = _ti('settings.expert.defaultValue', {
      value: controls.map(([input, value]) => _settingsDefaultLabel(input, value)).join(' · '),
    });
    reset.textContent = _ti('settings.expert.reset');
    reset.disabled = controls.every(([input, value]) => _settingsIsDefault(input, value));
  });
}

function resetSettingsRow(row) {
  (_settingsDefaultRows().get(row) || []).forEach(([input, value]) => {
    if (_settingsIsDefault(input, value)) return;
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else input.value = String(value);
    // input refreshes the value pill, change saves through the existing handlers.
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  renderSettingsExpertDefaults();
}

async function resetAllExpertSettings() {
  if (!window.confirm(_ti('settings.expert.resetAllConfirm'))) return;
  _settingsDefaultRows().forEach((controls, row) => {
    if (controls.some(([input]) => ['llm-temperature', 'llm-repeat-penalty', 'llm-top-p', 'threshold', 'max-chunks', 'chunk-size', 'reranker-enabled'].includes(input.id))) return;
    resetSettingsRow(row);
  });
  await resetLlmGenerationSettings();
  await resetResearchSettings();
  renderSettingsExpertDefaults();
}

document.addEventListener('click', (event) => {
  const target = event.target.closest?.('#settings-panel [data-overview-open], #settings-panel [data-expert-open], #settings-panel [data-provider-location], #settings-panel [data-provider-software], #settings-panel .settings-overview-action, #settings-expert-reset-all');
  if (!target || target.disabled) return;
  if (target.id === 'settings-expert-reset-all') void resetAllExpertSettings();
  else if (target.dataset.providerLocation) chooseSettingsProviderLocation(target.dataset.providerLocation);
  else if (target.dataset.providerSoftware) chooseSettingsProviderSoftware(target.dataset.providerSoftware);
  else if (target.dataset.expertOpen) openSettingsAt(target.dataset.expertOpen, target.dataset.expertFocus || '');
  else if (target.dataset.overviewOpen) switchSettingsTab(target.dataset.overviewOpen);
  else runSettingsOverviewAction(target.closest('[data-overview-area]')?.dataset.overviewArea, target.dataset.action);
});

// Arrow keys move between the AI location cards, like a radio group.
document.addEventListener('keydown', (event) => {
  const card = event.target?.closest?.('#settings-panel [data-provider-location]');
  if (!card || !['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  const cards = Array.from(document.querySelectorAll('#settings-panel [data-provider-location]'));
  const step = ['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : -1;
  const next = cards[(cards.indexOf(card) + step + cards.length) % cards.length];
  event.preventDefault();
  next.focus();
  chooseSettingsProviderLocation(next.dataset.providerLocation);
});

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('remote-connection-save')?.addEventListener('click', async () => {
    const saved = await saveApiSecrets('remote');
    if (saved) await setProviderMode('remote');
  });
  // A typed folder needs an explicit save; "Change…" applies the picked one.
  const dataPath = document.getElementById('data-path');
  dataPath?.addEventListener('input', () => {
    const apply = document.getElementById('data-path-apply-btn');
    if (apply) apply.hidden = false;
    _refreshSettingsMetadataCard();
  });
  dataPath?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      void applySettingsPdfFolder();
    }
  });
  ['allow-external-discovery', 'settings-model-select'].forEach((id) => {
    document.getElementById(id)?.addEventListener('change', refreshSettingsOverview);
  });
  refreshSettingsDefaults();
});
window.addEventListener('bibliobrain:languagechange', () => {
  refreshSettingsDefaults();
  refreshSettingsOverview();
  refreshProviderUrlUi();
  document.querySelectorAll('[data-settings-draft]').forEach((group) => setSettingsDraft(group.dataset.settingsDraft, group.dataset.dirty === 'true'));
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && document.getElementById('settings-panel')?.classList.contains('open')) {
    event.preventDefault();
    closeSettings();
  }
  if (event.key === 'Tab') {
    const panel = document.getElementById('settings-panel');
    if (!panel?.classList.contains('open') || !panel.contains(document.activeElement)) return;
    const focusable = Array.from(panel.querySelectorAll('button, input, select, textarea, summary, a[href], [tabindex]'))
      .filter(node => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length && !node.closest('[hidden]'));
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }
});
// Theme and language buttons change the Look summary shown in the Overview.
document.addEventListener('click', (event) => {
  if (event.target.closest?.('#settings-panel [data-theme-choice], #settings-panel [data-language-choice]')) {
    window.setTimeout(refreshSettingsOverview, 0);
  }
});
window.addEventListener('resize', () => {
  if (window.innerWidth <= 820 && document.getElementById('settings-panel')?.classList.contains('open')) {
    document.querySelector('#settings-tabs .settings-tab.active')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
});

async function _resetRagStatusUI() {
  // Show intermediate state while checking server
  ragStatusSet('starting', 'sidebar.starting', 'startup.statusChecking');
  updateFirstInitButtonVisibility();

  // Verify actual server state instead of assuming "non initialisé"
  try {
    const status = await fetchStartupStatus().catch(() => null);
    if (status?.rag?.ready) {
      ragStatusReady('ui.ready', status.rag.message || _ti('startup.statusReady'));
    } else {
      ragStatusReset();
    }
  } catch {
    ragStatusReset();
  }
}

async function resetHistory() {
  if (!confirm(_tr("Supprimer toutes les conversations, leurs exports associés, carnets, imports discovery et l’historique de la Rédaction ?\nLe brouillon en cours et les bases vectorielles restent intacts."))) return;

  const btn = document.getElementById("reset-history-btn");
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>'; }

  try {
    const d = await postJsonOrThrow("/api/reset-history", { confirm: true }, "Échec");
    if (!d?.ok) throw new Error(d?.msg || "Échec");

    state.discoveryResults = [];
    state.discoveryNotebooksLoaded = false;
    state.knowledgeGraph = null;
    state.knowledgeGraphDirty = true;
    state.knowledgeGraphVisiblePaths = [];
    state.knowledgeGraphFocusNodeId = "";

    startNewChat();
    _renderNbCards([]);
    closeNotebook();
    await loadChatHistory();
    await loadNotebooks();
    await loadLibrary();
    await loadZoteroFacets();
    if (state.drafting?.activeView === 'history') loadDraftingHistory();
    scheduleKnowledgeGraphReload(80);
    showToast(d.msg || "Historique effacé.", 4800, 'success');
  } catch (e) {
    showToast("Erreur : " + (e?.message || "inconnue"), 5200, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = _tr("Effacer l'historique"); }
  }
}

async function clearChatExports() {
  if (!confirm(_tr("Supprimer tous les exports Markdown et LaTeX générés par le chat ?\nLes conversations resteront intactes."))) return;

  const btn = document.getElementById("clear-chat-exports-btn");
  const old = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>'; }

  try {
    const d = await postJsonOrThrow("/api/chat-exports/clear", { confirm: true }, "Échec");
    if (!d?.ok) throw new Error(d?.msg || "Échec");
    showToast(d.msg || "Exports supprimés.", 4200, 'success');
  } catch (e) {
    showToast("Erreur : " + (e?.message || "inconnue"), 5200, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = old || _tr("Supprimer les exports"); }
  }
}

async function resetVectorDB() {
  if (!confirm(_tr("Supprimer toutes les bases vectorielles locales ?\nCette action efface tous les index Chroma disponibles, pas seulement l'index actif.\nL'historique des chats et les PDF restent.\nIl faudra réinitialiser le RAG après."))) return;

  const btn = document.getElementById("reset-vectordb-btn");
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>'; }

  try {
    const d = await postJsonOrThrow("/api/reset-vectordb", { confirm: true }, "Échec");
    if (!d?.ok) throw new Error(d?.msg || "Échec");

    state.ragReady = false;
    state.autoInitAttempted = false;
    state.startupStatus = await fetchStartupStatus();
    renderStartupStatus(state.startupStatus);
    _resetRagStatusUI();
    updateFirstInitButtonVisibility();
    scheduleKnowledgeGraphReload(80);
    showToast(d.msg || "Bases vectorielles supprimées.", 4800, 'success');
  } catch (e) {
    showToast("Erreur : " + (e?.message || "inconnue"), 5200, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = _tr("Supprimer les bases vectorielles"); }
  }
}

// ---------------------------------------------
//  INDEX TRANSFER (export / check / import)
// ---------------------------------------------
let _indexTransferToken = '';
let _indexTransferReport = null;
// Set when the startup assistant handed over to the import: cancelling goes back to it.
let _indexTransferFromStartup = false;

function startIndexImportFromStartup() {
  _indexTransferFromStartup = true;
  return inspectVectorIndex();
}

function _returnToStartupAssistant() {
  if (!_indexTransferFromStartup) return;
  _indexTransferFromStartup = false;
  if (typeof openStartupAssistant === 'function') openStartupAssistant();
}

function _setIndexTransferStatus(message, tone = 'cyan') {
  const status = document.getElementById('index-transfer-status');
  if (!status) return;
  status.className = `settings-status status-${tone}`;
  status.textContent = String(message || '');
  status.style.display = message ? 'block' : 'none';
}

function _formatTransferBytes(bytes) {
  const value = Number(bytes || 0);
  const english = typeof window.getLanguage === 'function' && window.getLanguage() === 'en';
  if (value >= 1024 ** 3) {
    const size = (value / 1024 ** 3).toFixed(1);
    return english ? `${size} GB` : `${size.replace('.', ',')} Go`;
  }
  return `${Math.max(1, Math.round(value / 1024 ** 2))} ${english ? 'MB' : 'Mo'}`;
}

function _setIndexTransferBusy(busy) {
  ['index-export-btn', 'index-import-btn', 'index-transfer-include-documents', 'index-transfer-auto-download', 'index-transfer-adopt'].forEach((id) => {
    const button = document.getElementById(id);
    if (button) button.disabled = busy;
  });
  document.querySelectorAll('#index-transfer-report button').forEach((button) => {
    button.disabled = busy || (button.dataset.indexTransferAction === 'import' && !_indexTransferReport?.can_import);
  });
}

// While a transfer request is in flight, the server publishes its progress.
let _indexTransferProgressTimer = 0;

function _renderIndexTransferProgress(progress) {
  const wrap = document.getElementById('index-transfer-progress');
  const bar = document.getElementById('index-transfer-progress-bar');
  const text = document.getElementById('index-transfer-progress-text');
  if (!wrap || !bar || !text) return;
  if (!progress?.active || !progress.phase) {
    wrap.hidden = true;
    return;
  }
  const total = Number(progress.total || 0);
  const done = Number(progress.done || 0);
  const pct = total > 0 ? Math.min(100, Math.floor((done * 100) / total)) : null;
  bar.classList.toggle('is-indeterminate', pct === null);
  bar.querySelector('span').style.width = pct === null ? '' : `${pct}%`;
  if (pct === null) bar.removeAttribute('aria-valuenow');
  else bar.setAttribute('aria-valuenow', String(pct));
  const key = pct === null ? `indexTransfer.progress.${progress.phase}Start` : `indexTransfer.progress.${progress.phase}`;
  text.textContent = _ti(key, {
    model: progress.label || '',
    done: _formatTransferBytes(done),
    total: _formatTransferBytes(total),
    pct: pct ?? 0,
  });
  wrap.hidden = false;
}

async function _pollIndexTransferProgress() {
  try {
    const response = await fetch('/api/index-transfer/progress');
    const progress = await response.json();
    if (_indexTransferProgressTimer) _renderIndexTransferProgress(progress);
  } catch (_) {
    // The next tick tries again.
  }
}

function _startIndexTransferProgress() {
  _stopIndexTransferProgress();
  _indexTransferProgressTimer = window.setInterval(_pollIndexTransferProgress, 500);
}

function _stopIndexTransferProgress() {
  if (_indexTransferProgressTimer) window.clearInterval(_indexTransferProgressTimer);
  _indexTransferProgressTimer = 0;
  _renderIndexTransferProgress(null);
}

function _indexTransferIncludesDocuments() {
  const box = document.getElementById('index-transfer-include-documents');
  return box ? box.checked : true;
}

// File sizes only: shown next to the option, and before a large export.
async function _loadIndexTransferEstimate() {
  const target = document.getElementById('index-transfer-estimate');
  try {
    const response = await fetch('/api/index-transfer/export-estimate');
    const d = await response.json();
    if (!response.ok || d?.ok === false) {
      if (target) target.textContent = '';
      return null;
    }
    if (target) {
      target.textContent = d.documents_found
        ? _ti('indexTransfer.estimate', { count: d.documents_found, size: _formatTransferBytes(d.documents_bytes) })
        : '';
    }
    return d;
  } catch (_) {
    return null;
  }
}

document.addEventListener('toggle', (event) => {
  if (event.target?.id === 'index-transfer-options' && event.target.open) void _loadIndexTransferEstimate();
}, true);

async function exportVectorIndex() {
  const includeDocuments = _indexTransferIncludesDocuments();
  _setIndexTransferBusy(true);
  _setIndexTransferStatus(_ti('indexTransfer.exporting'));
  try {
    const estimate = includeDocuments ? await _loadIndexTransferEstimate() : null;
    if (estimate?.documents_found) {
      _setIndexTransferStatus(_ti('indexTransfer.exportingDocuments', {
        count: estimate.documents_found,
        size: _formatTransferBytes(Number(estimate.documents_bytes || 0) + Number(estimate.index_bytes || 0)),
      }));
    }
    _startIndexTransferProgress();
    const d = await postJsonOrThrow('/api/index-transfer/export', {
      include_documents: includeDocuments,
    }, 'Export impossible');
    if (d.cancelled) {
      _setIndexTransferStatus('');
      return;
    }
    const message = [_ti(d.documents_included ? 'indexTransfer.exportedWithDocuments' : 'indexTransfer.exported', {
      count: d.document_count,
      documents: d.documents_included,
      size: _formatTransferBytes(d.bytes),
      path: d.archive,
    })];
    // FAT32 USB sticks refuse files of 4 GB or more.
    if (Number(d.bytes || 0) >= 4 * 1024 ** 3) message.push(_ti('indexTransfer.largeFile'));
    _setIndexTransferStatus(message.join(' '), 'green');
  } catch (e) {
    _setIndexTransferStatus(_ti('indexTransfer.failed', { error: e?.message || '?' }), 'red');
  } finally {
    _stopIndexTransferProgress();
    _setIndexTransferBusy(false);
  }
}

async function inspectVectorIndex() {
  _hideIndexTransferReport();
  _setIndexTransferBusy(true);
  _setIndexTransferStatus(_ti('indexTransfer.inspecting'));
  try {
    const d = await postJsonOrThrow('/api/index-transfer/inspect', {}, 'Vérification impossible');
    if (d.cancelled) {
      _setIndexTransferStatus('');
      _returnToStartupAssistant();
      return;
    }
    _indexTransferToken = d.token;
    _setIndexTransferStatus('');
    _renderIndexTransferReport(d.report);
  } catch (e) {
    _setIndexTransferStatus(_ti('indexTransfer.failed', { error: e?.message || '?' }), 'red');
  } finally {
    _setIndexTransferBusy(false);
  }
}

function _hideIndexTransferReport() {
  _indexTransferToken = '';
  _indexTransferReport = null;
  const container = document.getElementById('index-transfer-report');
  if (container) {
    container.hidden = true;
    container.innerHTML = '';
  }
}

function _indexTransferDocumentList(summaryKey, rows, total) {
  if (!rows?.length) return '';
  const items = rows.map((row) => (
    `<li>${escapeHtml(row.title)}<span>${escapeHtml(row.rel_path)}</span></li>`
  )).join('');
  const more = total > rows.length
    ? `<li>${escapeHtml(_ti('indexTransfer.listTruncated', { count: total - rows.length }))}</li>`
    : '';
  return `<details><summary class="disclosure">${escapeHtml(_ti(summaryKey, { count: total }))}</summary><ul>${items}${more}</ul></details>`;
}

// The simple view fetches a missing model itself when the option allows it.
function _indexTransferPlansDownload(report) {
  const embedding = report?.embedding;
  const auto = document.getElementById('index-transfer-auto-download');
  return Boolean(embedding?.action === 'download' && embedding.can_download && (auto ? auto.checked : true));
}

function _indexTransferAdopts() {
  const adopt = document.getElementById('index-transfer-adopt');
  return adopt ? adopt.checked : true;
}

// What the import button will do, in order.
function _indexTransferPlan(report, download) {
  const embedding = report.embedding;
  const copy = report.documents_copy;
  const steps = [];
  if (download) steps.push(_ti('indexTransfer.planDownload', { model: embedding.model_name }));
  if (copy?.copy_count) {
    steps.push(_ti(copy.set_data_path ? 'indexTransfer.planCopyNewFolder' : 'indexTransfer.planCopy', {
      count: copy.copy_count, size: _formatTransferBytes(copy.copy_bytes), folder: copy.target,
    }));
  }
  steps.push(_ti('indexTransfer.planInstall', { count: report.index.document_count }));
  if (report.records?.use) steps.push(_ti('indexTransfer.planRecords', { count: report.records.count }));
  if (!embedding.same_as_current && _indexTransferAdopts() && (download || embedding.action === 'switch')) {
    steps.push(_ti('indexTransfer.planSwitch', { model: embedding.model_name }));
  }
  if (report.documents?.reindex_count) {
    steps.push(_ti('indexTransfer.planReindex', { count: report.documents.reindex_count }));
  }
  return steps;
}

function _indexTransferModelCard(embedding, downloadPlanned = false) {
  const lines = [
    `<p>${escapeHtml(_ti('indexTransfer.modelUsed', { model: embedding.model_name, provider: embedding.provider_label }))}</p>`,
  ];
  if (embedding.action === 'equivalent') {
    lines.push(`<p class="is-ok">✓ ${escapeHtml(_ti('indexTransfer.modelEquivalent', {
      model: embedding.current_model_name, provider: embedding.current_provider_label,
    }))}</p>`);
  } else if (embedding.action === 'provider_mismatch') {
    lines.push(`<p class="is-warn">${escapeHtml(_ti('indexTransfer.modelOtherService', { provider: embedding.current_provider_label }))}</p>`);
  } else if (embedding.installed === true) {
    lines.push(`<p class="is-ok">✓ ${escapeHtml(_ti('indexTransfer.modelInstalled'))}</p>`);
  } else if (embedding.installed === false) {
    lines.push(`<p class="is-warn">${escapeHtml(_ti('indexTransfer.modelMissing'))}</p>`);
  } else {
    lines.push(`<p class="is-warn">${escapeHtml(_ti('indexTransfer.modelUnknown'))}</p>`);
  }
  if (!embedding.same_as_current && embedding.action !== 'equivalent' && embedding.current_model_name) {
    lines.push(`<p>${escapeHtml(_ti('indexTransfer.modelCurrent', { model: embedding.current_model_name }))}</p>`);
  }
  if (embedding.can_download && !downloadPlanned) {
    lines.push(`<button class="btn btn-cyan btn-small" type="button" data-index-transfer-action="pull">${escapeHtml(_ti('indexTransfer.download', { model: embedding.model_name }))}</button>`);
  }
  if (embedding.action === 'download' && embedding.install_command) {
    lines.push(`<p><code>${escapeHtml(embedding.install_command)}</code></p>`);
    lines.push(`<button class="btn btn-ghost btn-small" type="button" data-index-transfer-action="copy" data-command="${escapeHtml(embedding.install_command)}">${escapeHtml(_ti('indexTransfer.copyCommand'))}</button>`);
  }
  return `<section class="index-transfer-card"><h4>${escapeHtml(_ti('indexTransfer.modelHeading'))}</h4>${lines.join('')}</section>`;
}

function _indexTransferDocumentsCard(documents, copy = null) {
  const lines = [];
  const newFolder = copy?.mode === 'new_folder';
  if (!documents.data_path_exists && !newFolder) {
    lines.push(`<p class="is-warn">${escapeHtml(_ti('indexTransfer.documentsNoFolder'))}</p>`);
  } else {
    const tone = documents.missing_count ? 'is-warn' : 'is-ok';
    lines.push(`<p class="${tone}">${escapeHtml(_ti('indexTransfer.documentsFound', {
      found: documents.found, total: documents.total, path: documents.data_path,
    }))}</p>`);
    if (documents.copied_count) {
      lines.push(`<p class="is-ok">${escapeHtml(_ti('indexTransfer.documentsCopied', { count: documents.copied_count }))}</p>`);
    }
    lines.push(`<p>${escapeHtml(_ti('indexTransfer.documentsUnchanged', { count: documents.unchanged_count }))}</p>`);
    if (documents.reindex_count) {
      lines.push(`<p class="is-warn">${escapeHtml(_ti('indexTransfer.documentsReindex', { count: documents.reindex_count }))}</p>`);
    }
    if (documents.new_local_count) {
      lines.push(`<p>${escapeHtml(_ti('indexTransfer.documentsNew', { count: documents.new_local_count }))}</p>`);
    }
  }
  lines.push(_indexTransferDocumentList('indexTransfer.missingList', documents.missing, documents.missing_count));
  lines.push(_indexTransferDocumentList('indexTransfer.changedList', documents.content_changed, documents.content_changed_count));
  return `<section class="index-transfer-card"><h4>${escapeHtml(_ti('indexTransfer.documentsHeading'))}</h4>${lines.join('')}</section>`;
}

function _indexTransferLibraryCard(library, documents, records = null) {
  const lines = [];
  const usesZotero = library.exported_source === 'zotero' || library.current_source === 'zotero';
  if (records?.use) {
    lines.push(`<p class="is-ok">✓ ${escapeHtml(_ti('indexTransfer.libraryRecords', { count: records.count }))}</p>`);
  } else if (library.exported_source === 'none' && library.current_source === 'none') {
    lines.push(`<p>${escapeHtml(_ti('indexTransfer.libraryNone'))}</p>`);
  } else {
    if (usesZotero) {
      if (library.same_account === true) {
        lines.push(`<p class="is-ok">✓ ${escapeHtml(_ti('indexTransfer.librarySame'))}</p>`);
      } else if (library.same_account === false) {
        lines.push(`<p class="is-warn">${escapeHtml(_ti('indexTransfer.libraryDifferent'))}</p>`);
      } else {
        lines.push(`<p>${escapeHtml(_ti('indexTransfer.libraryUnknown'))}</p>`);
      }
    }
    if (library.exported_source !== library.current_source) {
      lines.push(`<p class="is-warn">${escapeHtml(_ti('indexTransfer.librarySources', {
        exported: library.exported_source, current: library.current_source,
      }))}</p>`);
    }
    lines.push(`<p>${escapeHtml(_ti('indexTransfer.libraryCounts', {
      exported: library.exported_item_count, current: library.current_item_count,
    }))}</p>`);
    if (documents.metadata_changed_count) {
      lines.push(`<p class="is-warn">${escapeHtml(_ti('indexTransfer.libraryMetadataChanged', { count: documents.metadata_changed_count }))}</p>`);
    }
    lines.push(_indexTransferDocumentList('indexTransfer.metadataList', documents.metadata_changed, documents.metadata_changed_count));
  }
  return `<section class="index-transfer-card"><h4>${escapeHtml(_ti('indexTransfer.libraryHeading'))}</h4>${lines.join('')}</section>`;
}

function _indexTransferIssueList(issues) {
  if (!issues.length) return '';
  const items = issues.map((issue) => (
    `<li class="is-${escapeHtml(issue.level)}">${escapeHtml(issue.message)}</li>`
  )).join('');
  return `<ul class="index-transfer-issues">${items}</ul>`;
}

// Simple view: verdict, what the button will do, and only the points that need
// the user. The three cards and the informative notes wait in "Voir le détail".
function _renderIndexTransferReport(report) {
  const container = document.getElementById('index-transfer-report');
  if (!container || !report) return;
  _indexTransferReport = report;
  const detailOpen = Boolean(container.querySelector('.index-transfer-detail')?.open);
  const download = _indexTransferPlansDownload(report);
  // The planned download answers the "model missing" warning.
  const issues = (report.issues || []).filter((issue) => !(download && issue.code === 'model_missing'));
  const shown = issues.filter((issue) => issue.level !== 'info');
  const notes = issues.filter((issue) => issue.level === 'info');
  let verdict = ['green', download ? 'indexTransfer.verdictReadyDownload' : 'indexTransfer.verdictReady'];
  if (!report.can_import) verdict = ['red', 'indexTransfer.verdictBlocked'];
  else if (shown.length) verdict = ['amber', 'indexTransfer.verdictAttention'];
  const created = report.archive.created_at ? new Date(report.archive.created_at) : null;
  const meta = _ti('indexTransfer.reportMeta', {
    name: report.archive.name,
    date: created && !Number.isNaN(created.getTime()) ? created.toLocaleDateString() : '?',
    count: report.index.document_count,
  });
  const plan = report.can_import
    ? `<ol class="index-transfer-plan">${_indexTransferPlan(report, download).map((step) => `<li>${escapeHtml(step)}</li>`).join('')}</ol>`
    : '';
  const confirmLabel = download
    ? _ti('indexTransfer.confirmDownload', { model: report.embedding.model_name })
    : _ti('indexTransfer.confirm');
  container.innerHTML = `
    <div class="index-transfer-head"><strong>${escapeHtml(_ti('indexTransfer.reportTitle'))}</strong><span>${escapeHtml(meta)}</span></div>
    <div class="settings-status status-${verdict[0]}">${escapeHtml(_ti(verdict[1]))}</div>
    ${plan}
    ${_indexTransferIssueList(shown)}
    <details class="index-transfer-detail"${detailOpen ? ' open' : ''}>
      <summary class="disclosure">${escapeHtml(_ti('indexTransfer.detail'))}</summary>
      <div class="index-transfer-detail-body">
        <div class="index-transfer-grid">
          ${_indexTransferModelCard(report.embedding, download)}
          ${_indexTransferDocumentsCard(report.documents, report.documents_copy)}
          ${_indexTransferLibraryCard(report.library, report.documents, report.records)}
        </div>
        ${_indexTransferIssueList(notes)}
      </div>
    </details>
    <div class="index-transfer-actions">
      <button class="btn btn-indigo" type="button" data-index-transfer-action="import">${escapeHtml(confirmLabel)}</button>
      <button class="btn btn-ghost" type="button" data-index-transfer-action="cancel">${escapeHtml(_ti('indexTransfer.cancel'))}</button>
    </div>`;
  container.hidden = false;
  if (!container.dataset.bound) {
    container.dataset.bound = '1';
    container.addEventListener('click', _onIndexTransferReportClick);
  }
  // The options change the plan and the button: redraw the open report.
  ['index-transfer-auto-download', 'index-transfer-adopt'].forEach((id) => {
    const option = document.getElementById(id);
    if (!option || option.dataset.indexTransferBound) return;
    option.dataset.indexTransferBound = '1';
    option.addEventListener('change', () => {
      if (_indexTransferReport) _renderIndexTransferReport(_indexTransferReport);
    });
  });
  _setIndexTransferBusy(false);
}

function _onIndexTransferReportClick(event) {
  const button = event.target.closest('[data-index-transfer-action]');
  if (!button || button.disabled) return;
  const action = button.dataset.indexTransferAction;
  if (action === 'cancel') {
    _hideIndexTransferReport();
    _returnToStartupAssistant();
  } else if (action === 'import') confirmIndexTransferImport();
  else if (action === 'pull') pullIndexTransferModel();
  else if (action === 'copy') {
    navigator.clipboard.writeText(button.dataset.command || '')
      .then(() => showToast(_ti('startup.copied'), 2200, 'success'))
      .catch(() => showToast(_ti('startup.copyError'), 3200, 'error'));
  }
}

async function pullIndexTransferModel() {
  if (!_indexTransferToken || !_indexTransferReport) return;
  const model = _indexTransferReport.embedding.model_name;
  if (!confirm(_ti('indexTransfer.downloadPrompt', { model }))) return;
  _setIndexTransferBusy(true);
  _setIndexTransferStatus(_ti('indexTransfer.downloading', { model }));
  _startIndexTransferProgress();
  try {
    const d = await postJsonOrThrow('/api/index-transfer/pull-model', {
      token: _indexTransferToken, confirm: true,
    }, 'Téléchargement impossible');
    _renderIndexTransferReport(d.report);
    _setIndexTransferStatus(d.report.embedding.installed ? _ti('indexTransfer.downloaded', { model }) : '', 'green');
  } catch (e) {
    _setIndexTransferStatus(_ti('indexTransfer.failed', { error: e?.message || '?' }), 'red');
  } finally {
    _stopIndexTransferProgress();
    _setIndexTransferBusy(false);
  }
}

// One click: download the missing model if planned, then install the index.
// The button's label names the download, so no second question is asked;
// only replacing an index that already exists here asks for confirmation.
async function confirmIndexTransferImport() {
  const report = _indexTransferReport;
  if (!_indexTransferToken || !report?.can_import) return;
  if (report.existing_index?.exists && !confirm(_ti('indexTransfer.confirmPrompt'))) return;
  const adopt = _indexTransferAdopts();
  const download = _indexTransferPlansDownload(report);
  _setIndexTransferBusy(true);
  _startIndexTransferProgress();
  try {
    if (download) {
      const model = report.embedding.model_name;
      _setIndexTransferStatus(_ti('indexTransfer.downloadStep', { model }));
      const pulled = await postJsonOrThrow('/api/index-transfer/pull-model', {
        token: _indexTransferToken, confirm: true,
      }, 'Téléchargement impossible');
      if (pulled.report.embedding.action === 'download' || !pulled.report.can_import) {
        _renderIndexTransferReport(pulled.report);
        _setIndexTransferStatus(_ti('indexTransfer.downloadIncomplete', { model }), 'red');
        return;
      }
      _indexTransferReport = pulled.report;
    }
    _setIndexTransferStatus(_ti(download ? 'indexTransfer.importStep' : 'indexTransfer.importing'));
    const d = await postJsonOrThrow('/api/index-transfer/import', {
      token: _indexTransferToken, confirm: true, adopt_settings: adopt,
    }, 'Import impossible');
    _hideIndexTransferReport();
    // An active index is loaded right away, with the usual progress display.
    // An inactive one is not: loading would build a new index for the current model.
    const copied = d.documents?.copied
      ? ` ${_ti('indexTransfer.importedDocuments', { count: d.documents.copied, folder: d.documents.target })}`
      : '';
    if (d.active) {
      _setIndexTransferStatus(_ti('indexTransfer.importedActive', { count: d.document_count }) + copied, 'green');
    } else {
      _setIndexTransferStatus(_ti('indexTransfer.importedInactive', {
        count: d.document_count, model: d.embedding?.model_name || '',
      }) + copied, 'amber');
    }
    if (d.documents?.data_path_set) {
      // The import created the PDF folder: show it as the chosen one.
      state.dataPathConfigured = true;
      if (state.appSettings) {
        state.appSettings.data_path = d.documents.target;
        state.appSettings.data_path_configured = true;
      }
      if (typeof syncPdfPathInputs === 'function') syncPdfPathInputs(d.documents.target);
    }
    if (d.records?.used && d.records.path) {
      // The archive's records became the metadata source, like a BibTeX file.
      if (typeof setMetadataSource === 'function') setMetadataSource('bibtex', d.records.path);
      const metadataInput = document.getElementById('zotero-path');
      if (metadataInput) metadataInput.value = d.records.path;
      if (state.appSettings) state.appSettings.zotero_path = d.records.path;
    }
    if (_indexTransferFromStartup) {
      _indexTransferFromStartup = false;
      // The imported index replaces the assistant's preparation step.
      if (d.active && typeof persistStartupOnboardingDone === 'function') await persistStartupOnboardingDone();
      if (d.active && typeof _rememberStartupGuideDone === 'function') _rememberStartupGuideDone();
    }
    state.ragReady = false;
    state.autoInitAttempted = false;
    if (typeof loadRagSettings === 'function') await loadRagSettings();
    if (typeof loadEmbeddingModels === 'function') loadEmbeddingModels();
    state.startupStatus = await fetchStartupStatus();
    renderStartupStatus(state.startupStatus);
    _resetRagStatusUI();
    updateFirstInitButtonVisibility();
    scheduleKnowledgeGraphReload(80);
    if (d.active) initRAG(false);
  } catch (e) {
    _setIndexTransferStatus(_ti('indexTransfer.failed', { error: e?.message || '?' }), 'red');
  } finally {
    _stopIndexTransferProgress();
    _setIndexTransferBusy(false);
  }
}
