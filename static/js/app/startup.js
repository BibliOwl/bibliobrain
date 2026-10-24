// ---------------------------------------------
//  ZOTERO
// ---------------------------------------------
function inferMetadataSource(path) {
  const value = String(path || '').trim().toLowerCase();
  if (!value) return 'none';
  // .json: records imported with an index, used like a BibTeX file.
  return /\.(bib|bibtex|txt|json)$/.test(value) ? 'bibtex' : 'zotero';
}

function setMetadataSource(source, path = '') {
  const normalized = ['none', 'bibtex', 'zotero'].includes(source)
    ? source
    : inferMetadataSource(path);
  state.metadataSource = normalized;
  state.startupMetadataSource = normalized;
  if (state.appSettings) state.appSettings.metadata_source = normalized;
  updateZoteroUiVisibility();
  updatePdfMetadataEnrichmentUi();
  if (typeof refreshSettingsOverview === 'function') refreshSettingsOverview();
  return normalized;
}

function isZoteroInUse() {
  return state.metadataSource === 'zotero';
}

function updatePdfMetadataEnrichmentUi() {
  const input = document.getElementById('auto-enrich-pdf-metadata');
  const status = document.getElementById('auto-enrich-pdf-metadata-status');
  const source = state.metadataSource || 'none';
  const zoteroConnected = source === 'zotero';
  if (input) {
    input.disabled = zoteroConnected;
    if (zoteroConnected) input.checked = false;
  }
  if (status) {
    const key = zoteroConnected
      ? 'settings.autoEnrichZoteroDisabledHint'
      : source === 'bibtex'
        ? 'settings.autoEnrichBibtexHint'
        : 'settings.autoEnrichStandaloneHint';
    status.textContent = _ti(key);
  }
}

function updateZoteroUiVisibility() {
  const active = isZoteroInUse();
  const filters = document.getElementById('chat-filters-panel');
  const libraryButton = document.getElementById('library-open-zotero-btn');
  const notebookOption = document.querySelector('#nb-create-mode option[value="zotero_collection"]');
  if (filters) {
    filters.hidden = !active;
    if (!active) filters.open = false;
  }
  if (libraryButton) libraryButton.hidden = !active;
  if (notebookOption) {
    notebookOption.hidden = !active;
    notebookOption.disabled = !active;
  }
  const notebookMode = document.getElementById('nb-create-mode');
  if (!active && notebookMode?.value === 'zotero_collection') {
    notebookMode.value = 'blank';
    onNbCreateModeChange();
  }
  if (!active) {
    clearChatFilters();
    document.querySelectorAll('.source-open-zotero-btn').forEach((element) => element.remove());
  } else {
    refreshRenderedSourceZoteroLinks();
  }
  _updateNbCollectionRefreshAction();
}

function _applyZoteroStorage(d) {
  const btn = document.getElementById('btn-use-zotero');
  refreshRenderedSourceZoteroLinks();
  if (d.storage) {
    state.zoteroStorage = d.storage;
    btn.disabled = false;
    btn.title = d.storage;
  } else {
    state.zoteroStorage = '';
    btn.disabled = true;
    btn.title = _tr('Detectez Zotero d abord');
  }
  if (typeof refreshSettingsOverview === 'function') refreshSettingsOverview();
}
async function zoteroDetect() {
  const button = document.getElementById('zotero-detect-btn');
  if (button) button.disabled = true;
  showZoteroStatus(_ti('settings.detectingAndConnectingZotero'));
  try {
    const r = await fetch('/api/zotero-detect');
    const d = await r.json();
    document.getElementById('zotero-path').value = d.path;
    showZoteroStatus(d.status);
    if (!r.ok || !d.path) {
      setMetadataSource('none');
      return;
    }
    setMetadataSource('zotero', d.path);
    if (d.rows) populateLibrary(d.rows, d.paths);
    _applyZoteroStorage(d);
    if (d.facets) _applyZoteroFacets(d.facets);
    await syncAppSettings({ silent: true });
  } catch(e) {
    showToast(_tr('Erreur détection Zotero'));
  } finally {
    if (button) button.disabled = false;
  }
}
function normalizeUserPathInput(value) {
  let text = String(value || '').trim();
  const quotePairs = {
    '"': '"',
    "'": "'",
    '\u201c': '\u201d',
    '\u2018': '\u2019',
    '\u00ab': '\u00bb',
  };
  while (text.length >= 2 && quotePairs[text[0]] === text[text.length - 1]) {
    text = text.slice(1, -1).trim();
  }
  return text;
}

// Until a folder is chosen, the backend reports Bibliobrain's own, still empty
// data folder. The assistant asks where the PDFs are: it must not answer for
// the user with a folder that holds none.
function isPdfFolderConfigured() {
  return state.appSettings?.data_path_configured !== false && state.dataPathConfigured !== false;
}

function syncPdfPathInputs(value, options = {}) {
  const path = normalizeUserPathInput(value);
  for (const id of ['data-path', 'startup-data-path']) {
    const input = document.getElementById(id);
    if (!input) continue;
    if (id === 'startup-data-path' && options.updateState !== false
      && (state.startupDataPathTouched || !isPdfFolderConfigured())) continue;
    input.value = path;
  }
  if (options.updateState !== false) {
    if (state.appSettings) state.appSettings.data_path = path;
    if (state.startupStatus?.settings) state.startupStatus.settings.data_path = path;
  }
  if (typeof refreshSettingsOverview === 'function') refreshSettingsOverview();
  return path;
}

async function applyPdfDataPath(value) {
  const path = normalizeUserPathInput(value);
  if (!path) {
    return { ok: false, path, msg: _tr('Le dossier des PDF est requis.') };
  }
  try {
    const response = await fetch('/api/set-data-path', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path }),
    });
    const data = await response.json();
    if (!response.ok || data?.ok === false) {
      return {
        ...data,
        ok: false,
        path,
        msg: data?.msg || data?.detail || _tr('Dossier de données introuvable.'),
      };
    }
    state.dataPathConfigured = true;
    if (state.appSettings) state.appSettings.data_path_configured = true;
    syncPdfPathInputs(path);
    if (data?.rows) populateLibrary(data.rows, data.paths);
    if (data?.facets) _applyZoteroFacets(data.facets);
    return { ...data, ok: true, path };
  } catch (error) {
    return {
      ok: false,
      path,
      error,
      msg: _tr('Erreur changement de dossier'),
    };
  }
}

async function selectStartupPdfDirectory() {
  const button = document.getElementById('startup-data-path-browse-btn');
  const input = document.getElementById('startup-data-path');
  const status = document.getElementById('startup-library-status');
  const previousStatus = status
    ? { text: status.textContent, className: status.className }
    : null;
  if (button) button.disabled = true;
  setStartupGuideStatus(
    'startup-library-status',
    _ti('startup.selectingPdfFolder'),
    'checking',
  );
  try {
    const response = await fetch('/api/select-data-directory', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path: normalizeUserPathInput(input?.value) }),
    });
    const data = await response.json();
    if (!response.ok || data?.ok === false) {
      const message = data?.error === 'directory_picker_busy'
        ? _ti('startup.pdfFolderPickerBusy')
        : _ti('startup.pdfFolderPickerError');
      setStartupGuideStatus(
        'startup-library-status',
        message,
        'error',
      );
      return null;
    }
    if (data?.cancelled || !data?.path) {
      if (status && previousStatus) {
        status.textContent = previousStatus.text;
        status.className = previousStatus.className;
        status.hidden = !previousStatus.text;
      }
      return null;
    }
    syncPdfPathInputs(data.path, { updateState: false });
    state.startupDataPathTouched = true;
    state.startupZoteroNoticeShown = false;
    setStartupGuideStatus('startup-library-status', '');
    document.getElementById('startup-next-btn')?.focus();
    return data.path;
  } catch (error) {
    setStartupGuideStatus(
      'startup-library-status',
      _ti('startup.pdfFolderPickerError'),
      'error',
    );
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

async function selectMetadataFile({ inputId, buttonId, onStatus, updateStartupSource = false }) {
  const input = document.getElementById(inputId);
  const button = document.getElementById(buttonId);
  if (button) button.disabled = true;
  onStatus(_ti('settings.selectingMetadataFile'), 'checking');
  try {
    const response = await fetch('/api/select-metadata-file', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path: normalizeUserPathInput(input?.value) }),
    });
    const data = await response.json();
    if (!response.ok || data?.ok === false) {
      const message = data?.error === 'file_picker_busy'
        ? _ti('settings.metadataFilePickerBusy')
        : _ti('settings.metadataFilePickerError');
      onStatus(message, 'error');
      return null;
    }
    if (data?.cancelled || !data?.path) {
      onStatus('', '');
      return null;
    }

    const path = normalizeUserPathInput(data.path);
    if (input) input.value = path;
    const source = inferMetadataSource(path);
    setMetadataSource(source, path);
    if (updateStartupSource) setStartupMetadataSource(source);
    onStatus('', '');
    input?.focus();
    return path;
  } catch (error) {
    onStatus(_ti('settings.metadataFilePickerError'), 'error');
    return null;
  } finally {
    if (button) button.disabled = false;
  }
}

async function selectSettingsMetadataFile() {
  return selectMetadataFile({
    inputId: 'zotero-path',
    buttonId: 'zotero-path-browse-btn',
    onStatus: (message) => showZoteroStatus(message),
  });
}

async function selectStartupMetadataFile() {
  return selectMetadataFile({
    inputId: 'startup-zotero-path',
    buttonId: 'startup-metadata-path-browse-btn',
    onStatus: (message, kind) => setStartupGuideStatus(
      'startup-library-status', message, kind || 'checking',
    ),
    updateStartupSource: true,
  });
}

async function zoteroLoad() {
  const input = document.getElementById('zotero-path');
  const path = normalizeUserPathInput(input.value);
  input.value = path;
  if (!path) {
    showZoteroStatus(_ti('settings.metadataPathRequired'));
    input.focus();
    return;
  }
  const button = document.getElementById('zotero-load-btn');
  if (button) button.disabled = true;
  showZoteroStatus(_ti('settings.connectingMetadataFile'));
  try {
    const r = await fetch('/api/zotero-load', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path}),
    });
    const d = await r.json();
    showZoteroStatus(d.status);
    if (!r.ok || d.ok === false) return;
    setMetadataSource(inferMetadataSource(path), path);
    if (d.rows) populateLibrary(d.rows, d.paths);
    _applyZoteroStorage(d);
    if (d.facets) _applyZoteroFacets(d.facets);
    await syncAppSettings({ silent: true });
  } catch(e) {
    showToast(_tr('Erreur chargement Zotero'));
  } finally {
    if (button) button.disabled = false;
  }
}
function useZoteroStorage() {
  if (!state.zoteroStorage) { showToast(_tr('Storage Zotero non détecté.')); return; }
  syncPdfPathInputs(state.zoteroStorage, { updateState: false });
  void setDataPath();
}
async function setDataPath() {
  const input = document.getElementById('data-path');
  const path = normalizeUserPathInput(input.value);
  input.value = path;
  const statusEl = document.getElementById('data-path-status');
  const result = await applyPdfDataPath(path);
  if (statusEl) {
    statusEl.textContent = describePdfFolderResult(result);
    statusEl.style.display = 'block';
  }
  if (!result.ok) showToast(result.msg || _tr('Erreur changement de dossier'));
  return result;
}
// Before the first preparation nothing is indexed yet: say what was found on
// disk, sub-folders included, rather than "0 indexed".
function describePdfFolderResult(result) {
  if (!result?.ok || typeof result.document_count !== 'number') return result?.msg || '';
  const total = Number(result.document_count || 0);
  const prepared = Math.min(Number(result.indexed_count || 0), total);
  if (!total) return _ti('settings.docs.folderEmpty') + (result.persist_warning || '');
  let text = _ti('settings.docs.folderSaved', { count: total.toLocaleString() });
  if (prepared < total) {
    text += ' ' + _ti('settings.docs.folderPending', { count: (total - prepared).toLocaleString() });
  }
  return text + (result.persist_warning || '');
}
function showZoteroStatus(msg) {
  const el = document.getElementById('zotero-status');
  if (!el) return;
  el.textContent = msg || '';
  el.style.display = msg ? 'block' : 'none';
}

async function fetchVectorDbStatus() {
  try {
    const r = await fetch('/api/vectordb-status');
    const d = await r.json();
    state.vectorDbStatus = d;
    const warning = getVectorDbEmbeddingWarning(d);
    if (warning) {
      const embeddingStatus = document.getElementById('embedding-model-status');
      const tone = d?.warning_level === 'info' ? 'cyan' : 'red';
      setMiniStatus(embeddingStatus, warning, tone);
    }
    return d;
  } catch (e) {
    state.vectorDbStatus = null;
    return null;
  }
}

function getVectorDbEmbeddingWarning(status = null) {
  const payload = status || state.vectorDbStatus;
  if (!payload) return '';
  return String(payload.warning || '').trim();
}

async function fetchStartupStatus() {
  if (state.startupStatusPromise) return state.startupStatusPromise;
  const request = (async () => {
    try {
      const r = await fetch('/api/startup-status');
      const d = await r.json();
      if (d?.ok) {
        state.startupStatus = d;
        if (d.vector_db) state.vectorDbStatus = d.vector_db;
        return d;
      }
    } catch (e) {
    }
    return null;
  })();
  state.startupStatusPromise = request;
  try {
    return await request;
  } finally {
    if (state.startupStatusPromise === request) state.startupStatusPromise = null;
  }
}

function cancelStartupAutoInit() {
  if (!state.startupAutoInitTimer) return;
  clearTimeout(state.startupAutoInitTimer);
  state.startupAutoInitTimer = null;
  renderStartupStatus(state.startupStatus);
}

function scheduleStartupAutoInit(delayMs = 0) {
  cancelStartupAutoInit();
  if (delayMs <= 0) {
    runStartupAutoInit(false);
    return;
  }
  state.startupAutoInitTimer = setTimeout(() => {
    state.startupAutoInitTimer = null;
    runStartupAutoInit(false);
  }, delayMs);
  renderStartupStatus(state.startupStatus, { key: 'startup.statusBuildSoon', tone: 'checking' });
}

// One-line summary shown in Settings -> Preparation. The step-by-step flow
// lives in the startup assistant; this line only says where things stand.
function renderStartupStatus(snapshot = state.startupStatus, override = {}) {
  const rag = snapshot?.rag || {};
  const action = String(snapshot?.recommended_action || '').trim();
  const busy = Boolean(
    state.ragInitBusy || state.startupAutoInitRunning || state.startupGuideRagRunning,
  );

  let key = 'startup.statusChecking';
  let tone = 'checking';
  if (override.key || override.detail) {
    key = override.key || '';
    tone = override.tone || '';
  } else if (rag.ready) {
    key = 'startup.statusReady';
    tone = 'ready';
  } else if (busy) {
    key = 'startup.statusBuilding';
    tone = 'checking';
  } else if (action === 'manual_rebuild') {
    key = 'startup.statusRepair';
    tone = 'warn';
  } else if (action === 'configure') {
    key = 'startup.statusConfigure';
    tone = 'warn';
  } else if (action === 'index_rag') {
    key = 'startup.statusIndexMissing';
    tone = 'warn';
  } else if (action === 'load_rag') {
    key = 'startup.statusIndexPresent';
    tone = '';
  }

  setStartupGuideStatus('startup-status-line', override.detail || _ti(key), tone);
  refreshSettingsOverview();
  if (typeof _refreshChatEmptyState === 'function') _refreshChatEmptyState();
  if (typeof refreshOnboardingChecklist === 'function') refreshOnboardingChecklist();
  return tone;
}

async function runStartupAutoInit(userRequested = false) {
  if (state.startupAutoInitTimer) {
    clearTimeout(state.startupAutoInitTimer);
    state.startupAutoInitTimer = null;
  }
  if (state.ragInitBusy || state.startupAutoInitRunning || state.streaming) return null;
  // No chat model is not a reason to wait: preparing the documents needs only
  // the indexing model.

  const snapshot = state.startupStatus || await fetchStartupStatus();
  if (snapshot?.rag?.ready) {
    state.ragReady = true;
    _refreshChatEmptyState();
    renderStartupStatus(snapshot);
    return snapshot;
  }

  const action = String(snapshot?.recommended_action || '').trim();
  if (!userRequested && (action === 'manual_rebuild' || action === 'configure')) {
    renderStartupStatus(snapshot);
    return snapshot;
  }

  state.startupAutoInitRunning = true;
  state.autoInitAttempted = true;
  if (state.startupHelpShown) updateStartupGuideControls();
  renderStartupStatus(snapshot, { key: 'startup.statusBuilding', tone: 'checking' });
  const forceReload = userRequested && action === 'manual_rebuild';
  const result = await initRAG(forceReload, { silentSuccess: true, silentError: !userRequested });
  state.startupAutoInitRunning = false;
  const refreshed = await fetchStartupStatus();
  renderStartupStatus(refreshed || state.startupStatus);
  updateFirstInitButtonVisibility();
  if (state.startupHelpShown) {
    refreshStartupBuildStatus();
    updateStartupGuideControls();
  }
  if (result?.ok && !userRequested) {
    showToast(_ti('app.readyToast'), 3200, 'success');
  }
  return result;
}

// ---------------------------------------------
//  STARTUP ASSISTANT (2 steps: PDF folder, then preparation)
// ---------------------------------------------
const STARTUP_GUIDE_STEPS = ['library', 'build'];
const STARTUP_GUIDE_DONE_KEY = 'startup-assistant-help-dismissed-v2';

function hideStartupBootSplash() {
  const splash = document.getElementById('startup-boot-splash');
  if (splash) splash.hidden = true;
}

function _rememberStartupGuideDone() {
  try {
    localStorage.setItem(LS_PREFIX + STARTUP_GUIDE_DONE_KEY, '1');
  } catch (_) {
  }
}

// The assistant opens by itself only until a first preparation succeeds.
function isStartupGuideNeeded(snapshot = state.startupStatus) {
  if (!snapshot) return false;
  if (snapshot.rag?.ready) return false;
  const settings = snapshot.settings || {};
  return !settings.onboarding_done_v1 && !settings.first_rag_init_done;
}

function _startupGuideBuilding() {
  return Boolean(
    state.startupGuideRagRunning || state.ragInitBusy || state.startupAutoInitRunning,
  );
}

function _startupRagReady() {
  return Boolean(state.startupStatus?.rag?.ready || state.ragReady);
}

// Already set up (folder chosen, a preparation done before): the assistant
// skips the welcome step and only reprepares, saying that nothing is lost.
function _startupLibraryAlreadySet(snapshot = state.startupStatus) {
  if (!isPdfFolderConfigured()) return false;
  const settings = snapshot?.settings || {};
  return Boolean(
    settings.first_rag_init_done || settings.onboarding_done_v1 || Number(snapshot?.library_count) > 0,
  );
}

function _applyStartupGuideMode() {
  const resume = Boolean(state.startupGuideResume) && state.startupGuideStep === 'build';
  const indexStatus = String(state.startupStatus?.vector_db?.status || '');
  const mismatch = indexStatus === 'embedding_mismatch';
  const texts = [
    ['startup-help-title', resume ? 'startup.resumeTitle' : 'startup.title'],
    ['startup-help-subtitle', resume ? 'startup.resumeSubtitle' : 'startup.subtitle'],
    // No index at all: everything is read again, the usual text says so.
    ['startup-build-help', resume && indexStatus !== 'missing'
      ? (mismatch ? 'startup.resumeHelpModelChanged' : 'startup.resumeHelp')
      : 'startup.buildHelp'],
  ];
  texts.forEach(([id, key]) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.dataset.i18n = key;
    el.textContent = _ti(key);
  });
}

function showStartupHelp(force = false) {
  if (!force && !isStartupGuideNeeded()) return;
  const overlay = document.getElementById('startup-help-overlay');
  const modal = document.getElementById('startup-help-modal');
  if (!overlay || !modal) return;
  if (!state.startupHelpShown) {
    state.startupHelpReturnFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  }
  initStartupGuide();
  const resume = _startupLibraryAlreadySet();
  state.startupGuideResume = resume;
  setStartupStep(_startupGuideBuilding() || resume ? 'build' : 'library');
  overlay.setAttribute('aria-hidden', 'false');
  overlay.classList.add('open');
  modal.classList.add('open');
  document.body.classList.add('startup-help-open');
  hideStartupBootSplash();
  state.startupHelpShown = true;
  window.setTimeout(() => {
    modal.focus();
  }, 40);
}

function openStartupAssistant() {
  state.startupHelpOpenedByUser = true;
  if (document.getElementById('settings-panel')?.classList.contains('open')) {
    toggleSettings();
  }
  showStartupHelp(true);
}

async function showStartupHelpOnPageLoad() {
  let locallyDismissed = false;
  try {
    locallyDismissed = localStorage.getItem(LS_PREFIX + STARTUP_GUIDE_DONE_KEY) === '1';
  } catch (_) {
  }

  // Do not make the first visible feedback depend on a potentially slow
  // backend pre-flight. The snapshot then decides whether the guide stays.
  if (!locallyDismissed && !state.startupHelpAutoChecked) {
    state.startupHelpAutoChecked = true;
    showStartupHelp(true);
  }

  const snapshot = state.startupStatus || await fetchStartupStatus();
  if (!snapshot) return null;
  renderStartupStatus(snapshot);
  if (!isStartupGuideNeeded(snapshot)) {
    state.startupHelpAutoChecked = true;
    _rememberStartupGuideDone();
    if (state.startupHelpShown && !state.startupHelpOpenedByUser && !_startupGuideBuilding()) {
      hideStartupHelp({ restoreFocus: false });
    }
    return snapshot;
  }
  if (!state.startupHelpAutoChecked) {
    state.startupHelpAutoChecked = true;
    showStartupHelp(true);
  } else if (state.startupHelpShown && state.startupGuideStep === 'library') {
    initStartupGuide();
  }
  return snapshot;
}

async function persistStartupOnboardingDone() {
  try {
    const r = await fetch('/api/app-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ onboarding_done_v1: true }),
    });
    const d = await r.json();
    if (d?.ok) {
      state.appSettings = d;
      if (state.startupStatus?.settings) {
        state.startupStatus.settings.onboarding_done_v1 = true;
      }
    }
    return d;
  } catch (e) {
    return null;
  }
}

function hideStartupHelp({ restoreFocus = true } = {}) {
  const overlay = document.getElementById('startup-help-overlay');
  const modal = document.getElementById('startup-help-modal');
  overlay?.classList.remove('open');
  overlay?.setAttribute('aria-hidden', 'true');
  modal?.classList.remove('open');
  document.body.classList.remove('startup-help-open');
  state.startupHelpShown = false;
  state.startupHelpOpenedByUser = false;
  if (restoreFocus && state.startupHelpReturnFocus?.isConnected) {
    window.setTimeout(() => state.startupHelpReturnFocus?.focus(), 0);
  }
  state.startupHelpReturnFocus = null;
  // Held back while the assistant was open.
  window.setTimeout(() => window.maybeShowSmallModelNotice?.(), 0);
}

// "Later" and "Explore while it prepares" share this exit. Closing never
// interrupts a running preparation; it keeps going in the background.
function requestHideStartupHelp() {
  if (_startupBuildDone()) {
    void finishStartupGuide();
    return;
  }
  const building = _startupGuideBuilding() && !state.ragReady;
  hideStartupHelp();
  if (building) showToast(_ti('startup.buildContinuesToast'), 3600, 'info');
}

function _handleStartupHelpKeydown(event) {
  const modal = document.getElementById('startup-help-modal');
  if (!modal?.classList.contains('open')) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    requestHideStartupHelp();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = Array.from(modal.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), select:not([disabled]), summary, [href], [tabindex]:not([tabindex="-1"])',
  )).filter((element) => !element.hidden && element.offsetParent !== null);
  if (!focusable.length) {
    event.preventDefault();
    modal.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (!modal.contains(document.activeElement) || document.activeElement === modal) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

document.addEventListener('keydown', _handleStartupHelpKeydown);
document.addEventListener('DOMContentLoaded', () => {
  void showStartupHelpOnPageLoad();
}, { once: true });
window.addEventListener('bibliobrain:languagechange', () => {
  renderStartupStatus(state.startupStatus);
  if (!state.startupHelpShown) return;
  setStartupMetadataSource(getStartupMetadataSource());
  if (state.startupGuideStep === 'build' && !state.startupGuideFailed) refreshStartupBuildStatus();
  updateStartupGuideControls();
});

// Setup ends in the chat, ready to type. The tutorial never starts on its
// own: "Premiers pas" in the sidebar and Settings offer it to whoever wants it.
async function finishStartupGuide() {
  await persistStartupOnboardingDone();
  _rememberStartupGuideDone();
  hideStartupHelp({ restoreFocus: false });
  switchTab('chat');
  window.setTimeout(() => document.getElementById('msg-input')?.focus(), 120);
  showToast(_ti('startup.completedToast'), 3200, 'success');
}

async function copyStartupCommand(elementId) {
  const value = String(document.getElementById(elementId)?.textContent || '').trim();
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    showToast(_ti('startup.copied'), 2200, 'success');
  } catch (error) {
    showToast(_ti('startup.copyError'), 3200, 'error');
  }
}

window.addEventListener('beforeunload', (event) => {
  abortActiveStreamsOnPageExit();
  if (!(state.ragInitBusy || state.startupGuideRagRunning) || state.ragReady) return;
  event.preventDefault();
  event.returnValue = '';
});
window.addEventListener('pagehide', () => {
  abortActiveStreamsOnPageExit();
  closeBibliobrainWindowSession();
});
window.addEventListener('pageshow', () => {
  _pageExitAbortSent = false;
  registerBibliobrainWindow();
});

function setStartupGuideStatus(id, message, tone = '', { detail = '' } = {}) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = message || '';
  el.title = detail || '';
  el.className = `startup-guide-status${tone ? ' ' + tone : ''}`;
  el.hidden = !message;
}

function _setStartupBuildProblem(visible) {
  const problem = document.getElementById('startup-build-problem');
  if (problem) problem.hidden = !visible;
}

function _startupBuildDone() {
  return state.startupGuideStep === 'build'
    && _startupRagReady()
    && !_startupGuideBuilding()
    && !state.startupGuidePreparing
    && !state.startupGuideLibraryChanged;
}

function updateStartupGuideControls() {
  const step = state.startupGuideStep === 'build' ? 'build' : 'library';
  const kicker = document.getElementById('startup-help-kicker');
  const later = document.getElementById('startup-later-btn');
  const next = document.getElementById('startup-next-btn');
  const changeFolder = document.getElementById('startup-change-folder-btn');
  const building = _startupGuideBuilding();
  const checking = Boolean(state.startupGuidePreparing);
  const buildDone = _startupRagReady() && !building && !checking && !state.startupGuideLibraryChanged;

  if (kicker) {
    kicker.hidden = Boolean(state.startupGuideResume) && step === 'build';
    kicker.textContent = _ti('startup.stepCounter', {
      current: step === 'build' ? 2 : 1,
      total: STARTUP_GUIDE_STEPS.length,
    });
  }
  if (changeFolder) changeFolder.hidden = step !== 'build' || building || checking;
  if (!later || !next) return;

  const focusWasOnFooter = document.activeElement === later || document.activeElement === next;
  if (step === 'library') {
    later.hidden = false;
    later.textContent = _ti('startup.later');
    next.hidden = false;
    next.disabled = Boolean(state.startupLibrarySaving);
    next.textContent = _ti(state.startupLibrarySaving ? 'startup.checking' : 'startup.continue');
  } else if (buildDone) {
    later.hidden = true;
    next.hidden = false;
    next.disabled = false;
    next.textContent = _ti('startup.start');
  } else if (building || checking) {
    later.hidden = false;
    later.textContent = _ti(building ? 'startup.exploreDuringBuild' : 'startup.later');
    next.hidden = true;
  } else {
    later.hidden = false;
    later.textContent = _ti('startup.later');
    next.hidden = false;
    next.disabled = false;
    next.textContent = _ti('startup.retry');
  }
  if (focusWasOnFooter && (next.hidden || later.hidden)) {
    (next.hidden ? later : next).focus();
  }
}

function _renderStartupDocumentCount() {
  const count = document.getElementById('startup-build-count');
  if (!count) return;
  const value = state.startupDocumentCount;
  count.hidden = !Number.isInteger(value) || value <= 0;
  count.textContent = count.hidden
    ? ''
    : _ti(value === 1 ? 'startup.documentCountOne' : 'startup.documentCount', { count: value });
}

// Backend progress lines are technical (batches, chunks, workers, file
// names). The assistant shows one plain sentence; the raw line stays in the
// tooltip for anyone who wants it.
function _startupProgressMessage(percent) {
  const value = Math.round(Math.max(0, Math.min(100, Number(percent) || 0)));
  const count = state.startupDocumentCount;
  return Number.isInteger(count) && count > 0
    ? _ti('startup.buildingProgressCount', { count, percent: value })
    : _ti('startup.buildingProgress', { percent: value });
}

function setStartupStep(stepName, { focusHeading = false } = {}) {
  const step = STARTUP_GUIDE_STEPS.includes(stepName) ? stepName : 'library';
  state.startupGuideStep = step;
  // Choosing another folder is a real setup again: the usual two steps.
  if (step === 'library') state.startupGuideResume = false;
  _applyStartupGuideMode();
  document.querySelectorAll('[data-startup-step]').forEach((panel) => {
    const active = panel.getAttribute('data-startup-step') === step;
    panel.classList.toggle('active', active);
    panel.hidden = !active;
  });
  if (step === 'build') {
    const folder = document.getElementById('startup-build-folder');
    if (folder) {
      folder.textContent = normalizeUserPathInput(
        state.appSettings?.data_path || document.getElementById('startup-data-path')?.value || '',
      );
    }
    _renderStartupDocumentCount();
  }
  updateStartupGuideControls();
  if (focusHeading) {
    window.setTimeout(() => document.querySelector(`[data-startup-step="${step}"] [data-startup-step-heading]`)?.focus(), 0);
  }
  if (step === 'build') void runStartupBuildStep();
}

async function nextStartupStep() {
  if (state.startupGuideStep !== 'build') {
    const saved = await saveStartupLibrary();
    if (!saved) return;
    // Leaving this step records the user's answer about the daily update check:
    // nothing is checked before an answer exists.
    if (typeof saveUpdateCheckChoice === 'function') {
      saveUpdateCheckChoice(Boolean(document.getElementById('startup-update-check')?.checked));
    }
    setStartupStep('build', { focusHeading: true });
    return;
  }
  if (_startupRagReady() && !_startupGuideBuilding() && !state.startupGuideLibraryChanged) {
    await finishStartupGuide();
    return;
  }
  await runStartupBuildStep();
}

// Already set up on another computer: the exported index takes the place of
// the preparation step. A folder typed here is kept, even empty: the import
// copies the archive's PDFs into it. Without one, the import creates a folder
// in Documents when the archive holds the PDFs.
async function startStartupIndexImport() {
  if (normalizeUserPathInput(document.getElementById('startup-data-path')?.value)) {
    const saved = await saveStartupLibrary({ allowEmptyFolder: true });
    if (!saved) return;
  }
  if (typeof saveUpdateCheckChoice === 'function') {
    saveUpdateCheckChoice(Boolean(document.getElementById('startup-update-check')?.checked));
  }
  hideStartupHelp({ restoreFocus: false });
  openSettingsAt('documents', 'index-import-btn');
  await startIndexImportFromStartup();
}

function inferStartupMetadataSource(path) {
  return inferMetadataSource(path);
}

function getStartupMetadataSource() {
  return document.querySelector('input[name="startup-metadata-source"]:checked')?.value || 'none';
}

function setStartupMetadataSource(source, { focus = false } = {}) {
  const selected = ['bibtex', 'zotero', 'none'].includes(source) ? source : 'none';
  state.startupMetadataSource = selected;
  document.querySelectorAll('input[name="startup-metadata-source"]').forEach((input) => {
    input.checked = input.value === selected;
  });

  const panel = document.getElementById('startup-metadata-path-panel');
  const input = document.getElementById('startup-zotero-path');
  const label = document.getElementById('startup-metadata-path-label');
  const detectButton = document.getElementById('startup-detect-library-btn');
  const hasPath = selected !== 'none';
  if (panel) panel.hidden = !hasPath;
  if (detectButton) detectButton.hidden = selected !== 'zotero';
  // Zotero already provides the metadata: the online lookup is never used with it.
  const onlineChoice = document.getElementById('startup-online-metadata-choice');
  if (onlineChoice) onlineChoice.hidden = selected === 'zotero';
  if (state.startupZoteroNoticeShown && selected !== 'zotero') {
    state.startupZoteroNoticeShown = false;
    setStartupGuideStatus('startup-library-status', '');
  }

  if (input && selected === 'bibtex') {
    input.placeholder = _ti('startup.bibtexPlaceholder');
    input.setAttribute('aria-label', _ti('startup.bibtexPath'));
  } else if (input && selected === 'zotero') {
    input.placeholder = _ti('startup.zoteroPlaceholder');
    input.setAttribute('aria-label', _ti('startup.zoteroPath'));
  }
  if (label && selected !== 'none') {
    label.textContent = _ti(selected === 'bibtex' ? 'startup.bibtexPath' : 'startup.zoteroPath');
  }
  if (focus && hasPath) window.setTimeout(() => input?.focus(), 0);
}

function selectAndDetectStartupZotero() {
  setStartupMetadataSource('zotero');
  void detectStartupLibrary();
}

function initStartupGuide() {
  const zotero = document.getElementById('startup-zotero-path');
  const data = document.getElementById('startup-data-path');
  if (data && data.dataset.startupBound !== 'true') {
    data.addEventListener('input', () => {
      state.startupDataPathTouched = true;
    });
    data.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      void nextStartupStep();
    });
    data.dataset.startupBound = 'true';
  }
  const onlineMetadata = document.getElementById('startup-online-metadata');
  if (onlineMetadata && onlineMetadata.dataset.startupBound !== 'true') {
    // Off until the user ticks it: the lookup sends the titles of their documents.
    onlineMetadata.checked = Boolean(
      state.appSettings?.allow_external_api_requests && state.appSettings?.auto_enrich_pdf_metadata,
    );
    onlineMetadata.dataset.startupBound = 'true';
  }
  const configuredMetadataPath = zotero?.value
    || document.getElementById('zotero-path')?.value
    || state.appSettings?.zotero_path
    || '';
  if (zotero) zotero.value = configuredMetadataPath;
  const configuredDataPath = state.appSettings?.data_path
    || state.startupStatus?.settings?.data_path
    || document.getElementById('data-path')?.value
    || '';
  if (data && !state.startupDataPathTouched && isPdfFolderConfigured()) {
    data.value = normalizeUserPathInput(configuredDataPath);
  }
  setStartupMetadataSource(
    state.appSettings?.metadata_source
      || state.startupMetadataSource
      || inferStartupMetadataSource(configuredMetadataPath),
  );
  void autoDetectStartupZotero();
  updateStartupGuideControls();
}

// First run only: look for Zotero quietly so the user does not have to know
// what a metadata source is. Nothing is saved until "Continue".
async function autoDetectStartupZotero() {
  if (state.startupZoteroAutoDetect) return;
  state.startupZoteroAutoDetect = 'pending';
  let decided = true;
  try {
    const snapshot = state.startupStatus || await fetchStartupStatus();
    if (!snapshot) {
      decided = false;
      return;
    }
    if (!isStartupGuideNeeded(snapshot)) return;
    let settings = state.appSettings;
    if (!settings) {
      const settingsResponse = await fetch('/api/app-settings', { cache: 'no-store' });
      settings = await settingsResponse.json();
    }
    const configuredSource = String(settings?.metadata_source || 'none').trim().toLowerCase();
    if (configuredSource !== 'none' || String(settings?.zotero_path || '').trim()) return;

    const response = await fetch('/api/zotero-detect');
    const data = await response.json();
    if (!response.ok || !data?.path) return;
    if (state.startupGuideStep !== 'library' || getStartupMetadataSource() !== 'none') return;

    const zoteroInput = document.getElementById('startup-zotero-path');
    if (zoteroInput) zoteroInput.value = data.path;
    setStartupMetadataSource('zotero');
    const dataInput = document.getElementById('startup-data-path');
    const usesZoteroPdfs = Boolean(dataInput && data.storage && !state.startupDataPathTouched);
    if (usesZoteroPdfs) dataInput.value = normalizeUserPathInput(data.storage);
    setStartupGuideStatus(
      'startup-library-status',
      _ti(usesZoteroPdfs ? 'startup.zoteroDetectedWithPdfs' : 'startup.zoteroDetected'),
      'ready',
    );
    state.startupZoteroNoticeShown = true;
  } catch (_) {
    // Detection is a convenience: the PDF folder alone is enough.
  } finally {
    // Without a backend snapshot nothing was decided: allow a later retry.
    state.startupZoteroAutoDetect = decided ? 'done' : '';
  }
}

async function detectStartupLibrary() {
  const button = document.getElementById('startup-detect-library-btn');
  if (button) button.disabled = true;
  state.startupZoteroNoticeShown = false;
  setStartupGuideStatus('startup-library-status', _ti('startup.detectingLibrary'), 'checking');
  try {
    const response = await fetch('/api/zotero-detect');
    const data = await response.json();
    const zoteroInput = document.getElementById('startup-zotero-path');
    const dataInput = document.getElementById('startup-data-path');
    if (data?.path) {
      if (zoteroInput) zoteroInput.value = data.path;
      const settingsInput = document.getElementById('zotero-path');
      if (settingsInput) settingsInput.value = data.path;
    }
    if (dataInput && data?.storage) {
      syncPdfPathInputs(data.storage, { updateState: false });
    }
    if (data?.rows) populateLibrary(data.rows, data.paths);
    if (data?.facets) _applyZoteroFacets(data.facets);
    _applyZoteroStorage(data || {});
    showZoteroStatus(data?.status || '');
    setStartupGuideStatus(
      'startup-library-status',
      data?.path
        ? _ti(data?.storage ? 'startup.zoteroDetectedWithPdfs' : 'startup.zoteroDetected')
        : _ti('startup.zoteroNotFound'),
      data?.path ? 'ready' : 'warn',
    );
  } catch (error) {
    setStartupGuideStatus('startup-library-status', _ti('startup.zoteroDetectionError'), 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

// The answer is written into the two Settings switches that syncAppSettings
// saves, before preparation starts: indexing is when the lookup runs.
// Unticking never turns the web switch off, other features may rely on it.
function applyStartupOnlineMetadataChoice(metadataSource) {
  const choice = document.getElementById('startup-online-metadata');
  if (!choice || metadataSource === 'zotero') return;
  const enrich = document.getElementById('auto-enrich-pdf-metadata');
  const allowWeb = document.getElementById('allow-external-discovery');
  if (enrich) enrich.checked = choice.checked;
  if (allowWeb && choice.checked) allowWeb.checked = true;
}

async function saveStartupLibrary({ allowEmptyFolder = false } = {}) {
  if (state.startupLibrarySaving) return false;
  const metadataSource = getStartupMetadataSource();
  const zoteroInput = document.getElementById('startup-zotero-path');
  const dataInput = document.getElementById('startup-data-path');
  const zoteroPath = metadataSource === 'none' ? '' : normalizeUserPathInput(zoteroInput?.value);
  const dataPath = normalizeUserPathInput(dataInput?.value);
  if (dataInput) dataInput.value = dataPath;
  if (!dataPath) {
    setStartupGuideStatus('startup-library-status', _ti('startup.pdfRequired'), 'error');
    dataInput?.focus();
    return false;
  }
  if (metadataSource !== 'none' && !zoteroPath) {
    setStartupGuideStatus('startup-library-status', _ti('startup.metadataPathRequired'), 'error');
    const details = document.getElementById('startup-metadata-details');
    if (details) details.open = true;
    zoteroInput?.focus();
    return false;
  }

  const previousDataPath = normalizeUserPathInput(state.appSettings?.data_path || '');
  const previousSource = String(state.appSettings?.metadata_source || 'none').trim().toLowerCase();
  const previousMetadataPath = normalizeUserPathInput(state.appSettings?.zotero_path || '');
  const libraryChanged = dataPath !== previousDataPath
    || metadataSource !== previousSource
    || (metadataSource !== 'none' && zoteroPath !== previousMetadataPath);

  if (zoteroInput) zoteroInput.value = zoteroPath;
  const settingsZoteroInput = document.getElementById('zotero-path');
  if (settingsZoteroInput) settingsZoteroInput.value = zoteroPath;
  setMetadataSource(metadataSource, zoteroPath);

  state.startupLibrarySaving = true;
  updateStartupGuideControls();
  state.startupZoteroNoticeShown = false;
  setStartupGuideStatus('startup-library-status', _ti('startup.validatingLibrary'), 'checking');
  try {
    if (zoteroPath) {
      const r = await fetch('/api/zotero-load', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ path: zoteroPath }),
      });
      const d = await r.json();
      if (!r.ok || d.ok === false) {
        setStartupGuideStatus('startup-library-status', d.status || d.detail || _ti('startup.metadataNotFound'), 'error');
        return false;
      }
      if (d.path) document.getElementById('zotero-path').value = d.path;
      if (d.rows) populateLibrary(d.rows, d.paths);
      if (d.facets) _applyZoteroFacets(d.facets);
      _applyZoteroStorage(d);
      showZoteroStatus(d.status || '');
    }
    const dataPathResult = await applyPdfDataPath(dataPath);
    if (!dataPathResult.ok) {
      setStartupGuideStatus('startup-library-status', _ti('startup.pdfNotFound'), 'error');
      dataInput?.focus();
      return false;
    }
    // A wrong or empty folder is caught here rather than as a vague failure
    // at the end of preparation.
    const documentCount = Number.isInteger(dataPathResult.document_count)
      ? dataPathResult.document_count
      : null;
    state.startupDocumentCount = documentCount;
    if (documentCount === 0 && !allowEmptyFolder) {
      setStartupGuideStatus('startup-library-status', _ti('startup.pdfFolderEmpty'), 'error');
      dataInput?.focus();
      return false;
    }
    applyStartupOnlineMetadataChoice(metadataSource);
    await syncAppSettings({ silent: true });
    if (typeof updateDiscoveryPrivacyUi === 'function') updateDiscoveryPrivacyUi();
    if (libraryChanged) state.startupGuideLibraryChanged = true;
    state.startupDataPathTouched = false;
    setStartupGuideStatus('startup-library-status', '');
    return true;
  } catch (e) {
    setStartupGuideStatus('startup-library-status', _ti('startup.libraryValidationError'), 'error');
    return false;
  } finally {
    state.startupLibrarySaving = false;
    updateStartupGuideControls();
  }
}

// Only Ollama and the indexing model block the preparation: without a chat
// model the PDFs are prepared anyway and a notice says how to add one.
async function checkStartupEngine() {
  const help = document.getElementById('startup-ollama-help-wrap');
  const chatNotice = document.getElementById('startup-chat-model-notice');
  if (help) help.hidden = true;
  if (chatNotice) chatNotice.hidden = true;
  setStartupGuideStatus('startup-engine-status', '');
  try {
    await syncOllamaBaseUrl({ silent: true });
    const health = await checkOllamaHealth(false);
    if (health?.required && !health.ok) {
      if (help) help.hidden = false;
      setStartupGuideStatus(
        'startup-engine-status',
        `${health.msg || _ti('startup.ollamaUnavailable')} ${_ti('startup.ollamaRetryHelp')}`,
        'error',
      );
      return false;
    }
    const models = await loadModels({ silent: true });
    // A newer check replaced this one; the build validates the index model itself.
    if (models?.stale) return true;
    if (!models || models.error) {
      setStartupGuideStatus('startup-engine-status', _ti('startup.modelsCheckFailed'), 'error');
      return false;
    }
    const indexHealth = models.health;
    if (indexHealth?.required && !indexHealth.ok) {
      // The detail stays visible: testers report problems with screenshots.
      const detail = String(indexHealth.msg || '').trim();
      setStartupGuideStatus(
        'startup-engine-status',
        detail ? `${_ti('startup.embeddingProbeFailed')} (${detail})` : _ti('startup.embeddingProbeFailed'),
        'error',
      );
      return false;
    }
    if (!models.models?.length && chatNotice) chatNotice.hidden = false;
    return true;
  } catch (e) {
    if (help) help.hidden = false;
    setStartupGuideStatus('startup-engine-status', _ti('startup.engineConnectionError'), 'error');
    return false;
  }
}

// Only speaks up when something blocks preparation. When the configured
// indexing model is installed (the normal case) the user never sees it.
async function loadStartupEmbeddingModels() {
  const select = document.getElementById('startup-embedding-select');
  const choice = document.getElementById('startup-embedding-choice');
  const commandWrap = document.getElementById('startup-embedding-command-wrap');
  const command = document.getElementById('startup-embedding-command');
  if (choice) choice.hidden = true;
  if (commandWrap) commandWrap.hidden = true;
  setStartupGuideStatus('startup-embedding-status', '');
  try {
    const r = await fetch('/api/ollama-local-models');
    const d = await r.json();
    if (normalizeProviderMode(d?.provider || embeddingProviderMode()) === 'remote') {
      return { ok: true, models: [] };
    }
    const models = Array.isArray(d.models) ? d.models.map(String).filter(Boolean) : [];
    const configured = String(
      state.providerInfo?.required_embedding_model || d.required_embedding_model || '',
    ).trim();
    const backendRecommended = String(d.recommended_embedding_model || '').trim();
    if (command) command.textContent = `ollama pull ${configured || backendRecommended || 'embeddinggemma'}`;
    if (!models.length) {
      if (commandWrap) commandWrap.hidden = false;
      setStartupGuideStatus('startup-embedding-status', _ti('startup.noEmbeddingHelp'), 'error');
      return { ok: false, models };
    }
    const normalizeModelName = (model) => String(model || '').trim().toLowerCase().split(':', 1)[0];
    const configuredNormalized = normalizeModelName(configured);
    const installedConfigured = configured
      ? (models.find((model) => (
          model === configured || normalizeModelName(model) === configuredNormalized
        )) || '')
      : '';
    const installedRecommended = backendRecommended
      ? (models.find((model) => (
          model === backendRecommended
          || normalizeModelName(model) === normalizeModelName(backendRecommended)
        )) || '')
      : '';
    const recommended = installedConfigured
      || installedRecommended
      || models.find((model) => normalizeModelName(model) === 'embeddinggemma')
      || models.find((model) => /qwen3[-_:]?embedding/i.test(model))
      || models.find((model) => /nomic/i.test(model))
      || models.find((model) => /bge|e5|gte|mxbai|minilm/i.test(model))
      || models.find((model) => /embed|embedding/i.test(model))
      || models[0];
    if (select) {
      select.innerHTML = models.map((model) => {
        const suffix = model === recommended ? ` (${_ti('startup.recommended')})` : '';
        return `<option value="${escapeAttr(model)}">${escapeHtml(model + suffix)}</option>`;
      }).join('');
      select.value = recommended;
    }
    if (configured && !installedConfigured) {
      if (choice) choice.hidden = false;
      if (commandWrap) commandWrap.hidden = false;
      setStartupGuideStatus(
        'startup-embedding-status',
        _ti('startup.embeddingNotInstalled', { model: configured }),
        'error',
      );
      return { ok: false, models, recommended, configured: '' };
    }
    return {
      ok: true,
      models,
      recommended,
      configured: installedConfigured,
    };
  } catch (e) {
    if (commandWrap) commandWrap.hidden = false;
    setStartupGuideStatus('startup-embedding-status', _ti('startup.embeddingLoadError'), 'error');
    return { ok: false, models: [] };
  }
}

async function saveStartupEmbeddingModel() {
  const select = document.getElementById('startup-embedding-select');
  const model = String(select?.value || '').trim();
  if (!model) {
    setStartupGuideStatus('startup-embedding-status', _ti('startup.selectEmbedding'), 'error');
    return false;
  }
  try {
    const r = await fetch('/api/set-embedding-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    });
    const d = await r.json();
    if (!d.ok) {
      setStartupGuideStatus('startup-embedding-status', d.msg || _ti('startup.invalidEmbedding'), 'error');
      return false;
    }
    state.providerInfo = d.info || state.providerInfo;
    await loadProviderInfo();
    await loadEmbeddingModels({ silent: true });
    void runStartupBuildStep();
    return true;
  } catch (e) {
    setStartupGuideStatus('startup-embedding-status', _ti('startup.embeddingSaveError'), 'error');
    return false;
  }
}

async function prepareStartupBuildStep() {
  if (state.startupGuidePreparing || state.startupGuideRagRunning) return false;
  state.startupGuidePreparing = true;
  state.startupGuideChecksReady = false;
  state.startupGuideFailed = false;
  document.getElementById('startup-help-modal')?.setAttribute('aria-busy', 'true');
  _setStartupBuildProblem(false);
  setStartupBuildProgress(0);
  setStartupGuideStatus('startup-build-status', _ti('startup.checking'), 'checking');
  updateStartupGuideControls();
  try {
    const [embeddingResult, engineReady] = await Promise.all([
      loadStartupEmbeddingModels(),
      checkStartupEngine(),
    ]);
    state.startupGuideChecksReady = Boolean(embeddingResult?.ok) && Boolean(engineReady);
    if (!state.startupGuideChecksReady) {
      state.startupGuideFailed = true;
      setStartupGuideStatus('startup-build-status', _ti('startup.checksIncomplete'), 'error');
      _setStartupBuildProblem(true);
    }
    return state.startupGuideChecksReady;
  } finally {
    state.startupGuidePreparing = false;
    document.getElementById('startup-help-modal')?.removeAttribute('aria-busy');
    updateStartupGuideControls();
  }
}

async function runStartupBuildStep() {
  if (state.startupGuidePreparing || state.startupGuideRagRunning) return null;
  if (state.ragInitBusy || state.startupAutoInitRunning) {
    return _followExternalStartupBuild();
  }
  if (_startupRagReady() && !state.startupGuideLibraryChanged) {
    state.startupGuideFailed = false;
    _setStartupBuildProblem(false);
    refreshStartupBuildStatus();
    return { ok: true };
  }
  const checksReady = await prepareStartupBuildStep();
  if (!checksReady) return null;
  return startStartupRagBuild();
}

// A preparation started elsewhere (sidebar button, automatic start) is
// mirrored here instead of being launched a second time.
async function _followExternalStartupBuild() {
  if (state.startupGuideFollowing) return null;
  state.startupGuideFollowing = true;
  state.startupGuideFailed = false;
  _setStartupBuildProblem(false);
  setStartupGuideStatus('startup-build-status', _ti('startup.building'), 'checking');
  updateStartupGuideControls();
  try {
    while (state.ragInitBusy || state.startupAutoInitRunning) {
      const sidebarWidth = parseFloat(document.getElementById('init-progress-bar')?.style.width || '0');
      const percent = Math.max(4, sidebarWidth || 0);
      setStartupBuildProgress(percent);
      setStartupGuideStatus('startup-build-status', _startupProgressMessage(percent), 'checking');
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    const refreshed = await fetchStartupStatus();
    if (refreshed) renderStartupStatus(refreshed);
    if (_startupRagReady()) {
      refreshStartupBuildStatus();
    } else {
      state.startupGuideFailed = true;
      setStartupBuildProgress(0);
      setStartupGuideStatus('startup-build-status', _ti('startup.buildFailed'), 'error');
    }
    return refreshed;
  } finally {
    state.startupGuideFollowing = false;
    updateStartupGuideControls();
  }
}

function setStartupBuildProgress(percent) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  const progress = document.getElementById('startup-build-progress');
  const bar = document.getElementById('startup-build-progress-bar');
  if (bar) bar.style.width = `${value.toFixed(1)}%`;
  if (progress) {
    progress.setAttribute('aria-valuenow', String(Math.round(value)));
    progress.setAttribute('aria-valuetext', `${Math.round(value)}%`);
  }
}

function refreshStartupBuildStatus() {
  const building = _startupGuideBuilding();
  const ragReady = _startupRagReady();
  if (building) {
    setStartupGuideStatus('startup-build-status', _ti('startup.building'), 'checking');
  } else if (ragReady && !state.startupGuideLibraryChanged) {
    setStartupBuildProgress(100);
    setStartupGuideStatus('startup-build-status', _ti('startup.ragReady'), 'ready');
  }
  updateStartupGuideControls();
}

async function startStartupRagBuild() {
  if (state.ragInitBusy || state.startupGuideRagRunning) return null;
  const action = String(state.startupStatus?.recommended_action || '').trim();
  const reloadExistingIndex = action === 'manual_rebuild' || _startupRagReady();
  state.startupGuideRagRunning = true;
  state.startupGuideFailed = false;
  setStartupGuideStatus('startup-build-status', _ti('startup.building'), 'checking');
  setStartupBuildProgress(4);
  renderStartupStatus(state.startupStatus);
  updateStartupGuideControls();
  let result = null;
  try {
    result = await initRAG(reloadExistingIndex, {
      silentSuccess: true,
      silentError: true,
      onProgress: (event) => {
        const percent = Math.max(4, Number(event.frac || 0) * 100);
        setStartupBuildProgress(percent);
        setStartupGuideStatus('startup-build-status', _startupProgressMessage(percent), 'checking', {
          detail: event.desc || '',
        });
      },
    });
  } finally {
    state.startupGuideRagRunning = false;
  }

  if (result?.ok) {
    state.ragReady = true;
    state.startupGuideLibraryChanged = false;
    setStartupBuildProgress(100);
    setStartupGuideStatus('startup-build-status', _ti('startup.ragReady'), 'ready');
    await persistStartupOnboardingDone();
    _rememberStartupGuideDone();
    const refreshed = await fetchStartupStatus();
    if (refreshed) renderStartupStatus(refreshed);
    if (!state.startupHelpShown) showToast(_ti('startup.completedToast'), 3600, 'success');
  } else {
    state.startupGuideFailed = true;
    setStartupBuildProgress(0);
    const message = result?.msg || _ti('startup.buildFailed');
    setStartupGuideStatus('startup-build-status', message, result?.cancelled ? 'warn' : 'error');
    renderStartupStatus(state.startupStatus);
    if (!state.startupHelpShown && !result?.cancelled) showToast(message, 5200, 'error');
  }
  updateStartupGuideControls();
  if (result?.ok && state.startupHelpShown) {
    const modal = document.getElementById('startup-help-modal');
    if (!modal?.contains(document.activeElement) || document.activeElement === modal
      || document.activeElement?.hasAttribute('data-startup-step-heading')) {
      document.getElementById('startup-next-btn')?.focus();
    }
  }
  return result;
}

async function bootstrapZoteroState() {
  const savedZotero = String(document.getElementById('zotero-path')?.value || '').trim();
  const source = setMetadataSource(
    state.appSettings?.metadata_source || inferMetadataSource(savedZotero),
    savedZotero,
  );
  if (source === 'none') {
    _applyZoteroStorage({});
    _applyZoteroFacets({ tags: [], collections: [], stats: { items: 0, tags: 0, collections: 0 } });
    showZoteroStatus('Zotero non utilisé. Métadonnées PDF extraites automatiquement.');
    return { ok: true, skipped: true, metadata_source: 'none' };
  }
  try {
    const response = await fetch(
      savedZotero ? '/api/zotero-load' : '/api/zotero-detect',
      savedZotero
        ? {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ path: savedZotero }),
          }
        : undefined,
    );
    const data = await response.json();
    setMetadataSource(source, data.path || savedZotero);
    if (data.path) document.getElementById('zotero-path').value = data.path;
    else if (savedZotero) document.getElementById('zotero-path').value = savedZotero;
    showZoteroStatus(data.status || 'Zotero non configuré.');
    if (data.rows) populateLibrary(data.rows, data.paths);
    _applyZoteroStorage(data);
    if (data.facets) _applyZoteroFacets(data.facets);
    if (data.stats) renderLibraryStats(data.stats);
    return data;
  } catch (e) {
    showZoteroStatus('Erreur chargement Zotero.');
    return null;
  }
}

async function bootstrapDataPath() {
  try {
    const r = await fetch('/api/data-path');
    const d = await r.json();
    state.dataPathConfigured = d.configured !== false;
    syncPdfPathInputs(d.path || '');
    return d;
  } catch (e) {
    return null;
  }
}

async function bootApplicationAuto() {
  if (state.bootInProgress) return;
  state.bootInProgress = true;
  state.autoInitAttempted = false;
  ragStatusSet('starting', 'sidebar.starting', 'startup.statusChecking');
  renderStartupStatus(null, { key: 'startup.statusChecking', tone: 'checking' });
  // The writing editor does not depend on Ollama or the RAG: never let a slow
  // or failing startup step leave it unbound (raw paste, draft not restored).
  Promise.resolve(initDraftingWorkspace()).catch((error) => {
    console.warn('Drafting workspace init failed.', error);
  });

  try {
    await syncOllamaBaseUrl({ silent: true });
    ragStatusProgress(null, 'startup.statusLoadingModels');
    const modelsPromise = loadModels({ silent: true });
    const startupPromise = state.startupStatus
      ? Promise.resolve(state.startupStatus)
      : fetchStartupStatus();
    const [modelsInfo, initialStartup] = await Promise.all([
      modelsPromise,
      startupPromise,
    ]);
    const savedModel = typeof getPersistedChatModelPreference === 'function'
      ? getPersistedChatModelPreference()
      : (localStorage.getItem(LS_PREFIX + 'model') || '');
    if (savedModel) {
      const sel = document.getElementById('model-select');
      const opt = [...sel.options].find(o => o.value === savedModel);
      if (opt) sel.value = savedModel;
      if (typeof renderModelChip === 'function') renderModelChip();
    }

    // If RAG is already ready, set it immediately and skip the decision tree below.
    if (initialStartup?.rag?.ready) {
      const runtimeModel = String(initialStartup.rag.model || '').trim();
      const sel = document.getElementById('model-select');
      if (runtimeModel && sel && Array.from(sel.options).some((opt) => opt.value === runtimeModel)) {
        sel.value = runtimeModel;
        if (typeof rememberSelectedChatModel === 'function') rememberSelectedChatModel(runtimeModel);
        else localStorage.setItem(LS_PREFIX + 'model', runtimeModel);
        updateModelWarning(runtimeModel);
      }
      state.ragReady = true;
      _refreshChatEmptyState();
      ragStatusReady('ui.ready', initialStartup.rag.message || _ti('startup.statusReady'));
      renderStartupStatus(initialStartup);
    }
    if (typeof warmChatModel === 'function') {
      warmChatModel(document.getElementById('model-select')?.value);
    }

    // Load GPU, analysis types, Zotero state, and data path in parallel.
    // Do NOT update the status badge while loading these — if RAG is already
    // ready above, we must not overwrite "Prêt" with an intermediate message.
    Promise.resolve(loadGPU()).catch(() => null);
    const analysisTypesPromise = loadAnalysisTypes().catch(() => null);

    const [zoteroState] = await Promise.all([
      bootstrapZoteroState(),
      bootstrapDataPath(),
    ]);
    const hasLibrarySnapshot = Array.isArray(zoteroState?.rows)
      && Array.isArray(zoteroState?.paths);
    const backgroundDataPromise = Promise.all([
      hasLibrarySnapshot ? Promise.resolve(zoteroState) : loadLibrary(),
      analysisTypesPromise,
    ]).catch(() => null);

    // Set the FINAL status only once, after all checks are done.
    const startup = initialStartup || state.startupStatus;
    renderStartupStatus(startup);
    if (!state.startupHelpAutoChecked) void showStartupHelpOnPageLoad();

    if (state.ragReady || startup?.rag?.ready) {
      // Already set above — do not overwrite.
      state.ragReady = true;
      _refreshChatEmptyState();
    } else if (!modelsInfo || modelsInfo.error
      || (modelsInfo.health?.required && !modelsInfo.health.ok)) {
      // Ollama or the indexing model fails; a missing chat model alone does not
      // stop the documents from being prepared.
      ragStatusError('app.error', modelsInfo?.health?.msg || _ti('startup.modelsCheckFailed'));
    } else {
      const action = String(startup?.recommended_action || '').trim();
      if ((action === 'load_rag' || action === 'index_rag') && isStartupGuideNeeded(startup)) {
        ragStatusWarning('startup.indicatorFirstRun', 'startup.statusIndexMissing');
      } else if (action === 'load_rag' || action === 'index_rag') {
        ragStatusSet('preparing', 'sidebar.preparing', 'startup.statusBuilding');
        const firstBuild = action === 'index_rag' && !startup?.settings?.first_rag_init_done;
        scheduleStartupAutoInit(firstBuild ? 2400 : 0);
      } else if (action === 'manual_rebuild') {
        ragStatusWarning('startup.needsRebuild', startup?.vector_db?.warning || 'startup.statusRepair');
      } else {
        ragStatusWarning('startup.needsConfig', 'startup.statusConfigure');
      }
    }
    updateFirstInitButtonVisibility();
    void backgroundDataPromise.finally(() => scheduleKnowledgeGraphReload(140));
  } catch (e) {
    ragStatusError('app.error', e?.message || 'startup.buildFailed');
    renderStartupStatus(state.startupStatus, {
      detail: e?.message || _ti('startup.buildFailed'),
      tone: 'error',
    });
    showToast(_tr('Erreur de démarrage: ') + (e?.message || _tr('inconnue')), 5200, 'error');
  } finally {
    state.bootInProgress = false;
    hideStartupBootSplash();
  }
}
