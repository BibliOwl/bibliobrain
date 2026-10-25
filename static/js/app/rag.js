
// ---------------------------------------------
//  RAG INIT
// ---------------------------------------------
let _ragRefreshPreview = null;
let _ragRefreshCheckRunning = false;
let _ragRefreshCheckedAt = 0;
let _ragRefreshRevision = 0;

// The button in the Library header. Changes found by the scan also reach the
// sidebar status pill ("{n} new PDFs" · Add them), from any tab.
// The label and the badge are separate spans: applyI18n rewrites the label
// only, and the badge carries the pending count or the progress.
// Called by renderRagStatus(): to refresh both, call that one.
function renderRagRefreshControls() {
  const busy = Boolean(state.ragInitBusy);
  const pending = ['new_count', 'modified_count', 'removed_count']
    .reduce((count, key) => count + (Number(_ragRefreshPreview?.[key]) || 0), 0);
  const needsRefresh = !busy && (pending > 0 || Boolean(_ragRefreshPreview?.full_rebuild_required));
  const progress = Number(state.ragUiState?.progress);
  const labelKey = busy ? 'library.ragUpdating' : 'library.reloadRag';
  let badge = '';
  if (busy && state.ragUiState?.progress != null && Number.isFinite(progress)) {
    badge = `${Math.round(progress * 100)}%`;
  } else if (needsRefresh && pending > 0) {
    badge = String(pending);
  }
  document.querySelectorAll('.btn-rag-reload').forEach((button) => {
    button.disabled = busy;
    button.setAttribute('aria-busy', String(busy));
    button.classList.toggle('is-pending', needsRefresh);
    const labelEl = button.querySelector('.rag-reload-label');
    if (labelEl) {
      labelEl.dataset.i18n = labelKey;
      labelEl.textContent = _ti(labelKey);
    }
    const badgeEl = button.querySelector('.rag-reload-badge');
    if (badgeEl) {
      badgeEl.textContent = badge;
      badgeEl.hidden = !badge;
    }
  });
  const notice = document.getElementById('rag-refresh-notice');
  if (notice) notice.hidden = !needsRefresh;
}

// PDFs added, changed or removed since the last load, as the pill's text and
// action. The status card lists the files (_ragDetailView).
function _ragPendingChanges() {
  const preview = _ragRefreshPreview;
  if (state.ragInitBusy || !preview) return null;
  const added = Number(preview.new_count) || 0;
  const changed = (Number(preview.modified_count) || 0) + (Number(preview.removed_count) || 0);
  const run = () => void reloadLibraryRag();
  if (preview.full_rebuild_required) {
    return { text: _ti('status.rebuildNeeded'), action: { label: _ti('status.update'), onClick: run } };
  }
  if (added) {
    return { text: _ti('status.newPdfs', { n: formatCount(added) }), action: { label: _ti('status.addPdfs'), onClick: run } };
  }
  if (changed) {
    return { text: _ti('status.changedPdfs', { n: formatCount(changed) }), action: { label: _ti('status.update'), onClick: run } };
  }
  return null;
}

async function checkRagRefreshNeeded(force = false) {
  if (document.hidden || state.ragInitBusy || !state.ragReady || _ragRefreshCheckRunning) return;
  if (!force && Date.now() - _ragRefreshCheckedAt < 30000) return;
  _ragRefreshCheckRunning = true;
  _ragRefreshCheckedAt = Date.now();
  const revision = _ragRefreshRevision;
  try {
    const preview = await previewRagReload();
    if (revision === _ragRefreshRevision && !state.ragInitBusy) {
      _ragRefreshPreview = preview;
      renderRagStatus();
    }
  } catch (_) {
    // A failed scan must not claim the index is up to date or block the UI.
  } finally {
    _ragRefreshCheckRunning = false;
  }
}

// The scan reads every PDF and the Zotero database (~2 s on a 500-paper
// library): run it when the user comes back to the window, not on a timer.
window.addEventListener('focus', () => checkRagRefreshNeeded());
document.addEventListener('visibilitychange', () => checkRagRefreshNeeded());
window.addEventListener('bibliobrain:languagechange', () => renderRagStatus());

// The OCR job holds the same server lock as /api/init, so a reload started
// while it runs is refused (409). Pause it first, with the user's consent,
// and tell the caller to resume it afterwards.
async function _fetchOcrStatus() {
  try {
    const response = await fetch('/api/ocr/status', { cache: 'no-store' });
    return response.ok ? await response.json() : null;
  } catch (_) {
    return null;
  }
}

async function pauseOcrForRagReload({ timeoutMs = 180000, intervalMs = 1500 } = {}) {
  const status = await _fetchOcrStatus();
  if (!status?.active) return { proceed: true, resumeOcr: false };
  if (!window.confirm(_ti('library.ragOcrPauseConfirm'))) return { proceed: false, resumeOcr: false };
  await fetch('/api/ocr/pause', { method: 'POST' }).catch(() => null);
  showToast(_ti('library.ragOcrPausing'), 4000, 'info');
  window.refreshOcrStatus?.();
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, intervalMs));
    const current = await _fetchOcrStatus();
    if (current && !current.active) return { proceed: true, resumeOcr: true };
  }
  showToast(_ti('library.ragOcrPauseTimeout'), 6000, 'error');
  return { proceed: false, resumeOcr: false };
}

async function resumeOcrAfterRagReload() {
  try {
    const response = await fetch('/api/ocr/start', { method: 'POST' });
    const data = await response.json().catch(() => ({}));
    window.refreshOcrStatus?.();
    if (response.ok && data.ok) return;
  } catch (_) {
    // Reported below.
  }
  showToast(_ti('library.ragOcrResumeFailed'), 7000, 'error');
}

// setRagProgressSummary() is deprecated — use ragStatusProgress() instead.
// Kept as a thin wrapper for backward compatibility.
function setRagProgressSummary(_textElement, label, detail = '', frac = null) {
  // Ignore _textElement — the new system derives text from state.
  if (label && state.ragUiState.label !== label) {
    // Only update label if it changed (during init progress)
    state.ragUiState.label = String(label);
  }
  ragStatusProgress(frac, detail);
}

function _formatRagReloadPreview(preview) {
  const summary = String(preview?.summary || '').trim();
  if (summary) return summary;
  if (preview?.full_rebuild_required) return _tr('Rebuild complet requis avant le rechargement.');
  return _tr(`Mise a jour incrementale: ${preview?.new_count || 0} nouveaux, ${preview?.modified_count || 0} modifies, ${preview?.removed_count || 0} supprimes.`);
}

function isBackendConnectionError(error) {
  const message = String(error?.message || error || '').toLowerCase();
  return error instanceof TypeError
    && /failed to fetch|networkerror|network request failed|load failed/.test(message);
}

async function describeBackendConnectionError(fallbackKey = 'startup.buildFailed') {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 1800);
  try {
    const response = await fetch('/api/provider-info', {
      cache: 'no-store',
      signal: controller.signal,
    });
    if (response.ok) return _ti(fallbackKey);
  } catch {}
  finally {
    window.clearTimeout(timeout);
  }
  return _ti('app.backendDisconnected');
}

window.isBackendConnectionError = isBackendConnectionError;
window.describeBackendConnectionError = describeBackendConnectionError;

async function previewRagReload() {
  const chunkSize = parseInt(document.getElementById('chunk-size')?.value || '1000');
  const params = new URLSearchParams();
  if (Number.isFinite(chunkSize) && chunkSize > 0) params.set('chunk_size', String(chunkSize));
  const suffix = params.toString() ? `?${params.toString()}` : '';
  const response = await fetch(`/api/rag/reload-preview${suffix}`);
  if (!response.ok) throw new Error(`Preview RAG impossible (${response.status})`);
  return response.json();
}

// What the running load displays, so a resume after a pause can restore it.
// `streaming` turns true on the first server progress event: init_rag clears
// any pause flag when it starts, so pausing earlier would be lost.
// `startedAt` and `plan` (the reload preview) feed the detail popover.
const _ragLoadContext = {
  busyLabel: 'startup.preparing',
  activityLabel: 'Préparation du RAG',
  lastDesc: '',
  streaming: false,
  revealTimer: null,
  startedAt: 0,
  plan: null,
};

// ---- RAG section of the sidebar status card (renderSidebarStatus in app.js) ----
// What the load is doing, for how long, and which documents it covers.
function _ragElapsedText(ms) {
  const seconds = Math.max(1, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${String(seconds % 60).padStart(2, '0')} s`;
  return `${Math.floor(minutes / 60)} h ${String(minutes % 60).padStart(2, '0')} min`;
}

function _ragPlanFiles(plan) {
  const groups = [['new', plan?.new_paths], ['modified', plan?.modified_paths], ['removed', plan?.removed_paths]];
  return groups.flatMap(([change, paths]) => (Array.isArray(paths) ? paths : [])
    .map((path) => ({ change, text: String(path).split(/[\\/]/).pop() })));
}

function _ragDetailView() {
  const busy = Boolean(state.ragInitBusy);
  const { label, detail, progress } = state.ragUiState;
  const fraction = Number(progress);
  const percent = busy && progress !== null && Number.isFinite(fraction)
    ? Math.round(Math.max(0, Math.min(1, fraction)) * 100)
    : null;

  // While loading: the changes this run applies. Otherwise: those waiting.
  const plan = busy ? _ragLoadContext.plan : _ragRefreshPreview;
  const files = _ragPlanFiles(plan);
  let planText = '';
  if (plan?.full_rebuild_required) {
    planText = _ti('library.ragDetailRebuild');
  } else if (files.length) {
    planText = _ti('library.ragDetailCounts', {
      new: Number(plan.new_count) || 0,
      modified: Number(plan.modified_count) || 0,
      removed: Number(plan.removed_count) || 0,
    });
  }
  const shown = plan?.full_rebuild_required ? [] : files.slice(0, 6);
  // Files that could not be read keep their earlier chunks (if any) and are
  // not offered again until they change or the app is updated.
  const failed = (Array.isArray(_ragRefreshPreview?.failed_files) ? _ragRefreshPreview.failed_files : [])
    .map((entry) => {
      const name = String(entry?.rel_path || '').split(/[\\/]/).pop();
      const reason = String(entry?.reason || '');
      return { change: 'failed', text: name, title: reason ? `${name} — ${reason}` : name };
    });
  const failedShown = failed.slice(0, 6);
  const pending = busy ? null : _ragPendingChanges();
  const actions = [];
  if (busy && _ragLoadContext.streaming) {
    actions.push({
      label: _ti(state.ragLoadPaused ? 'status.resume' : 'status.pause'),
      onClick: () => void toggleRagPause(),
    });
  } else if (pending) {
    actions.push({ ...pending.action, primary: true });
  } else if (!busy && !state.ragReady && state.ragFirstInitAction) {
    actions.push({
      label: _ti(state.ragFirstInitAction === 'repair' ? 'status.ragRepair' : 'status.ragPrepare'),
      onClick: () => runFirstRagInit(),
      disabled: state.ragInitAvailable === false,
      primary: true,
    });
  }

  let title = _ti('status.library');
  let status = _ragStatusText(label);
  if (busy) {
    title = _ti('status.ragUpdating');
    status = state.ragLoadPaused ? _ragStatusText('settings.paused') : _ragStatusText(detail);
  } else if (pending) {
    // The reload has to pause a running OCR pass first (pauseOcrForRagReload).
    status = statusTaskTone('ocr') === 'working' ? _ti('status.waitingOcr') : pending.text;
  } else if (state.ragReady) {
    const count = libraryArticleCount();
    status = count ? _ti('status.libraryReady', { n: formatCount(count) }) : _ragStatusText(label);
  } else if (state.ragFirstInitAction) {
    status = _ti('status.ragNotReady');
  }
  return {
    title,
    detail: status,
    percent,
    facts: [
      // The last message explains a failure; once ready it is only technical detail.
      { term: _ti('library.ragDetailLast'), value: busy || state.ragReady ? '' : _ragStatusText(detail) },
      {
        term: _ti('library.ragDetailElapsed'),
        value: busy && _ragLoadContext.startedAt ? _ragElapsedText(Date.now() - _ragLoadContext.startedAt) : '',
      },
      {
        term: _ti(busy ? 'library.ragDetailPlan' : 'library.ragDetailPending'),
        value: planText,
        items: shown,
        more: files.length > shown.length && shown.length
          ? _ti('library.ragDetailMore', { count: files.length - shown.length })
          : '',
      },
      {
        term: _ti('library.ragDetailFailed'),
        value: failed.length ? _ti('library.ragDetailFailedCount', { n: formatCount(failed.length) }) : '',
        items: failedShown,
        more: failed.length > failedShown.length
          ? _ti('library.ragDetailMore', { count: failed.length - failedShown.length })
          : '',
      },
    ],
    actions,
  };
}

// The activity row stands for the RAG load in the collapsed sidebar.
const _ragActivityOptions = statusActivityOptions;

function _renderRagPauseControls() {
  const busy = Boolean(state.ragInitBusy && _ragLoadContext.streaming);
  const paused = busy && Boolean(state.ragLoadPaused);
  const labelKey = paused ? 'settings.resumeReload' : 'settings.pauseReload';
  const settingsBtn = document.getElementById('pause-reload-btn');
  if (settingsBtn) {
    settingsBtn.disabled = !busy;
    settingsBtn.setAttribute('aria-pressed', String(paused));
    settingsBtn.setAttribute('data-i18n', labelKey);
    settingsBtn.textContent = _ti(labelKey);
  }
  // The status card offers the same pause / resume action.
  if (typeof renderSidebarStatus === 'function') renderSidebarStatus();
}

function _setRagInitBusy(busy) {
  state.ragInitBusy = Boolean(busy);
  _ragRefreshRevision += 1;
  renderRagStatus();
  state.ragLoadPaused = false;
  _ragLoadContext.streaming = false;
  clearTimeout(_ragLoadContext.revealTimer);
  const stopBtn = document.getElementById('stop-reload-btn');
  if (stopBtn) stopBtn.disabled = !busy;
  _renderRagPauseControls();
  if (typeof updateFirstInitButtonVisibility === 'function') updateFirstInitButtonVisibility();
}

function _showRagPaused(frac) {
  const detail = _ti('settings.pausedDetail');
  ragStatusSet('warning', 'settings.paused', 'settings.pausedDetail', frac);
  activitySet('rag', 'RAG en pause', detail, frac, { ..._ragActivityOptions, paused: true });
  const progressDesc = document.getElementById('init-progress-desc');
  if (progressDesc) progressDesc.textContent = detail;
}

async function toggleRagPause() {
  if (!state.ragInitBusy) return;
  const resume = Boolean(state.ragLoadPaused);
  const buttons = document.querySelectorAll('.rag-pause-toggle');
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const response = await fetch(resume ? '/api/resume-init' : '/api/pause-init', { method: 'POST' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.msg || `HTTP ${response.status}`);
    // The load may have finished while the request was in flight.
    if (!state.ragInitBusy) return;
    state.ragLoadPaused = Boolean(data.paused);
    const frac = state.ragUiState.progress;
    if (state.ragLoadPaused) {
      _showRagPaused(frac);
    } else {
      const desc = _ragLoadContext.lastDesc;
      ragStatusSet('preparing', _ragLoadContext.busyLabel, desc, frac);
      activitySet('rag', _ragLoadContext.activityLabel, desc, frac, _ragActivityOptions);
      const progressDesc = document.getElementById('init-progress-desc');
      if (progressDesc) progressDesc.textContent = desc;
    }
  } catch (e) {
    showToast(_tr('Erreur : ') + e.message, 4200, 'error');
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
    _renderRagPauseControls();
  }
}

async function initRAG(forceReload, options = {}) {
  const silentSuccess = Boolean(options.silentSuccess);
  const silentError = Boolean(options.silentError);
  const onProgress = typeof options.onProgress === 'function' ? options.onProgress : null;
  const onDone = typeof options.onDone === 'function' ? options.onDone : null;
  // Empty when no chat model is installed yet: the documents are prepared anyway.
  const model     = optionalSelectedModel() || '';
  const threshold = parseFloat(document.getElementById('threshold').value);
  const initBtn   = document.getElementById('init-btn');
  const reloadBtn = document.getElementById('reload-btn');
  const progressWrap = document.getElementById('init-progress');
  const progressBar  = document.getElementById('init-progress-bar');
  const progressDesc = document.getElementById('init-progress-desc');
  const wasReady = Boolean(state.ragReady);
  _setRagInitBusy(true);
  initBtn.disabled = reloadBtn.disabled = true;
  const busyLabel = forceReload ? 'library.ragReloading' : 'startup.preparing';
  const activityLabel = forceReload ? 'Rechargement du RAG' : 'Préparation du RAG';
  ragStatusSet('preparing', busyLabel);
  progressWrap.style.display = 'block';
  progressBar.style.width = '4%';
  const initDesc = forceReload ? _tr('Préparation du rechargement…') : _tr('Préparation de l\'initialisation…');
  _ragLoadContext.busyLabel = busyLabel;
  _ragLoadContext.activityLabel = activityLabel;
  _ragLoadContext.lastDesc = initDesc;
  _ragLoadContext.startedAt = Date.now();
  // The last scan until this reload's own preview replaces it.
  _ragLoadContext.plan = forceReload ? _ragRefreshPreview : null;
  progressDesc.textContent = initDesc;
  ragStatusProgress(null, initDesc);
  activitySet('rag', activityLabel, initDesc, null, _ragActivityOptions);

  try {
    await syncOllamaBaseUrl({ silent: true });
    progressBar.style.width = '8%';
    const checkDesc = _tr('Vérification de la connexion au provider…');
    progressDesc.textContent = checkDesc;
    ragStatusProgress(null, checkDesc);

    const health = await checkOllamaHealth(true);
    if (health && health.required && !health.ok) {
      _setRagInitBusy(false);
      initBtn.disabled = reloadBtn.disabled = false;
      ragStatusError('app.error', health.msg);
      progressWrap.style.display = 'none';
      activityClear('rag');
      updateFirstInitButtonVisibility();
      return { ok: false, msg: health.msg };
    }

    if (forceReload && !options.skipReloadPreview) {
      progressBar.style.width = '10%';
      const previewDesc = _tr('Analyse des changements PDF/Zotero…');
      progressDesc.textContent = previewDesc;
      ragStatusProgress(null, previewDesc);
      const preview = await previewRagReload();
      _ragLoadContext.plan = preview;
      const previewMessage = _formatRagReloadPreview(preview);
      progressDesc.textContent = previewMessage;
      ragStatusProgress(null, previewMessage);
      if (preview.full_rebuild_required) {
        ragStatusWarning('settings.confirmRebuild', previewMessage);
        const confirmed = window.confirm(`${previewMessage}\n\n${_tr('Lancer quand meme le rechargement ?')}`);
        if (!confirmed) {
          _setRagInitBusy(false);
          initBtn.disabled = reloadBtn.disabled = false;
          ragStatusSet('idle', 'settings.cancelled', previewMessage);
          progressWrap.style.display = 'none';
          activityClear('rag');
          updateFirstInitButtonVisibility();
          return { ok: false, cancelled: true, msg: previewMessage };
        }
        // Resume preparing state after confirmation
        ragStatusSet('preparing', busyLabel, previewMessage);
      }
    }

    const response = await fetch('/api/init', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        model, threshold, force_reload: forceReload,
        max_chunks: parseInt(document.getElementById('max-chunks').value),
        chunk_size: parseInt(document.getElementById('chunk-size').value),
      }),
    });
    if (response.status === 409) {
      // OCR, an index import or another tab holds the server lock. readSSE
      // would label this "Chat impossible", and the loaded index is still
      // usable: put the status back instead of reporting an error.
      const lockedMessage = _ti('library.ragInitLocked');
      _setRagInitBusy(false);
      if (wasReady) ragStatusReady('ui.ready');
      else ragStatusSet('idle', 'sidebar.notInitialized', lockedMessage);
      progressWrap.style.display = 'none';
      activityClear('rag');
      updateFirstInitButtonVisibility();
      initBtn.disabled = reloadBtn.disabled = false;
      if (!silentError) showToast(lockedMessage, 6200, 'error');
      if (onDone) onDone({ ok: false, busy: true, msg: lockedMessage });
      return { ok: false, busy: true, msg: lockedMessage };
    }
    for await (const event of readSSE(response)) {
      if (event.type === 'progress') {
        progressBar.style.width = (event.frac * 100).toFixed(1) + '%';
        _ragLoadContext.lastDesc = event.desc;
        if (!_ragLoadContext.streaming) {
          _ragLoadContext.streaming = true;
          // Loading an up-to-date index takes well under a second: don't
          // flash a pause button there is no time to use.
          _ragLoadContext.revealTimer = setTimeout(_renderRagPauseControls, 800);
        }
        if (state.ragLoadPaused) {
          // A batch already in flight when the pause was requested still
          // reports; keep the paused wording, only move the bar.
          _showRagPaused(event.frac);
        } else {
          progressDesc.textContent = event.desc;
          ragStatusProgress(event.frac, event.desc);
          activitySet('rag', activityLabel, event.desc, event.frac, _ragActivityOptions);
        }
        if (onProgress) onProgress(event);
      } else if (event.type === 'done') {
        activityClear('rag');
        _setRagInitBusy(false);
        const stopped = /arrete|pause/i.test(event.msg || '');
        if (event.ok) {
          _ragRefreshPreview = null;
          ragStatusReady('ui.ready', event.msg);
          // The server starts the OCR pass after a load when it has work.
          window.refreshOcrStatus?.();
          void checkRagRefreshNeeded(true);
        } else if (stopped) {
          ragStatusWarning('settings.stopped', event.msg);
        } else {
          ragStatusError('app.error', event.msg);
        }
        progressBar.style.width = '100%';
        progressDesc.textContent = event.msg;
        if (event.ok) await persistFirstRagInitDone();
        updateFirstInitButtonVisibility();
        if (event.ok && !silentSuccess) showToast(event.msg, 4000, 'success');
        else if (!event.ok && !silentError) showToast(event.msg, 5200, 'error');
        setTimeout(() => { progressWrap.style.display = 'none'; }, 1800);
        initBtn.disabled = reloadBtn.disabled = false;
        if (onDone) onDone(event);
        return event;
      }
    }
  } catch(e) {
    const errorMessage = isBackendConnectionError(e)
      ? await describeBackendConnectionError('startup.buildFailed')
      : (_tr('Erreur : ') + e.message);
    _setRagInitBusy(false);
    ragStatusError('app.error', errorMessage);
    updateFirstInitButtonVisibility();
    if (!silentError) showToast(errorMessage, 6200, 'error');
    initBtn.disabled = reloadBtn.disabled = false;
    activityClear('rag');
    if (onDone) onDone({ ok: false, msg: errorMessage });
    return { ok: false, msg: errorMessage };
  }
  activityClear('rag');
  _setRagInitBusy(false);
  initBtn.disabled = reloadBtn.disabled = false;
  updateFirstInitButtonVisibility();
  const lastMsg = state.ragUiState.detail || '';
  if (onDone) onDone({ ok: state.ragReady, msg: lastMsg });
  return { ok: state.ragReady, msg: lastMsg };
}

async function stopRagReload() {
  const stopBtn = document.getElementById('stop-reload-btn');
  const progressDesc = document.getElementById('init-progress-desc');

  if (!state.ragInitBusy) {
    showToast(_tr('Aucun rechargement en cours.'), 2600, 'error');
    return;
  }

  stopBtn.disabled = true;
  const stoppingLabel = _tr('Arrêt demandé…');
  ragStatusProgress(null, stoppingLabel);
  progressDesc.textContent = stoppingLabel;

  try {
    const response = await fetch('/api/stop-init', { method: 'POST' });
    const data = await response.json();
    const msg = data.msg || _tr('Arrêt demandé.');
    // The server lifts any pause to let the load wind down; nothing left to pause.
    state.ragLoadPaused = false;
    _ragLoadContext.streaming = false;
    _renderRagPauseControls();
    ragStatusWarning('settings.stopped', msg);
    progressDesc.textContent = msg;
    showToast(msg, 3200, 'success');
  } catch (e) {
    stopBtn.disabled = false;
    const errorMsg = _tr('Erreur : ') + e.message;
    ragStatusError('app.error', errorMsg);
    progressDesc.textContent = errorMsg;
    showToast(errorMsg, 4200, 'error');
  }
}

// Documents indexed with broken PDF text (detached LaTeX accents, ligatures):
// the server marks them, then the usual incremental reload re-embeds only them.
(function initTextRepairPanel() {
  const byId = (id) => document.getElementById(id);
  const setStatus = (text) => { const el = byId('text-repair-status'); if (el) el.textContent = text; };

  async function scan() {
    const scanBtn = byId('text-repair-scan');
    const runBtn = byId('text-repair-run');
    if (scanBtn) scanBtn.disabled = true;
    if (runBtn) runBtn.hidden = true;
    setStatus(_tr('Analyse de l’index… (environ 10 s)'));
    try {
      const response = await fetch('/api/rag/text-artifacts', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.msg || `HTTP ${response.status}`);
      if (!data.document_count) {
        setStatus(_tr('Aucun document concerné : le texte indexé est propre.'));
        return;
      }
      const examples = (data.documents || []).slice(0, 3).map((item) => item.title).filter(Boolean).join(' ; ');
      setStatus(`${data.document_count} ${_tr('document(s) concerné(s)')}, ${data.chunk_count} ${_tr('extrait(s) à réindexer')}${examples ? ` — ${_tr('par exemple')} ${examples}` : ''}.`);
      if (runBtn) runBtn.hidden = false;
    } catch (error) {
      setStatus(`${_tr('Vérification impossible')} : ${error.message}`);
    } finally {
      if (scanBtn) scanBtn.disabled = false;
    }
  }

  async function reindex() {
    const runBtn = byId('text-repair-run');
    if (runBtn) runBtn.disabled = true;
    try {
      const response = await fetch('/api/rag/text-artifacts/reindex', { method: 'POST' });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.msg || `HTTP ${response.status}`);
      setStatus(`${data.marked} ${_tr('document(s) en cours de réindexation…')}`);
      if (runBtn) runBtn.hidden = true;
      // Keep the reload preview: it asks before any full rebuild (e.g. a
      // changed chunk size), which this targeted repair must never trigger silently.
      const result = await initRAG(true);
      setStatus(result?.ok
        ? _tr('Réindexation terminée. Relancez « Vérifier » pour contrôler.')
        : `${_tr('Réindexation interrompue')} : ${result?.msg || ''}`);
    } catch (error) {
      setStatus(`${_tr('Réindexation impossible')} : ${error.message}`);
    } finally {
      if (runBtn) runBtn.disabled = false;
    }
  }

  function bind() {
    byId('text-repair-scan')?.addEventListener('click', scan);
    byId('text-repair-run')?.addEventListener('click', reindex);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();

// Zotero records with a doubtful title/year/authors: list them, and on demand
// fetch a proposed correction the user applies in Zotero (read-only here).
(function initMetadataCheckPanel() {
  const byId = (id) => document.getElementById(id);
  const MAX_SHOWN = 40;

  function proposalText(proposal) {
    const parts = [proposal.title];
    if (proposal.authors?.length) parts.push(proposal.authors.join(', '));
    if (proposal.year) parts.push(proposal.year);
    if (proposal.doi) parts.push(`DOI ${proposal.doi}`);
    return parts.filter(Boolean).join(' · ');
  }

  async function propose(item, row, button) {
    button.disabled = true;
    const out = row.querySelector('.metadata-check-proposal');
    out.textContent = _tr('Recherche d’une correction…');
    try {
      const response = await fetch(`/api/library/metadata-fix?path=${encodeURIComponent(item.rel_path)}`, { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.msg || `HTTP ${response.status}`);
      const proposal = data.proposal || {};
      if (!proposal.source) {
        out.textContent = _tr('Aucune correction fiable trouvée : vérifiez la notice dans Zotero.');
        return;
      }
      const origin = { openalex: 'OpenAlex', crossref: 'Crossref', page: _tr('première page du PDF') }[proposal.source] || proposal.source;
      out.textContent = `${_tr('Proposition')} (${origin}) : ${proposalText(proposal)}`;
      const copy = document.createElement('button');
      copy.type = 'button';
      copy.className = 'btn btn-ghost metadata-check-copy';
      copy.textContent = _tr('Copier le titre');
      copy.addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(proposal.title); showToast(_tr('Titre copié.'), 1800, 'success'); }
        catch (_) { showToast(_tr('Copie impossible.'), 2400, 'error'); }
      });
      out.append(' ', copy);
    } catch (error) {
      out.textContent = `${_tr('Correction impossible')} : ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }

  function renderRow(item) {
    const row = document.createElement('li');
    row.className = 'metadata-check-item';
    const title = document.createElement('strong');
    title.textContent = item.title;
    const meta = document.createElement('span');
    meta.className = 'metadata-check-issues';
    meta.textContent = item.issues.join(' · ');
    const actions = document.createElement('div');
    actions.className = 'btn-row';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-ghost';
    button.textContent = _tr('Proposer une correction');
    button.addEventListener('click', () => propose(item, row, button));
    actions.append(button);
    if (item.zotero_select_uri) {
      const link = document.createElement('a');
      link.className = 'btn btn-ghost';
      link.href = item.zotero_select_uri;
      link.textContent = _tr('Afficher dans Zotero');
      actions.append(link);
    }
    const proposal = document.createElement('p');
    proposal.className = 'metadata-check-proposal';
    row.append(title, meta, actions, proposal);
    return row;
  }

  async function scan() {
    const button = byId('metadata-check-scan');
    const list = byId('metadata-check-list');
    const status = byId('metadata-check-status');
    if (button) button.disabled = true;
    if (status) status.textContent = _tr('Analyse de la bibliothèque…');
    try {
      const response = await fetch('/api/library/metadata-issues', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.msg || `HTTP ${response.status}`);
      const items = Array.isArray(data.items) ? data.items : [];
      if (status) {
        status.textContent = items.length
          ? `${data.count} ${_tr('notice(s) à vérifier')}${data.count > MAX_SHOWN ? ` (${MAX_SHOWN} ${_tr('affichées')})` : ''}.`
          : _tr('Aucune notice douteuse trouvée.');
      }
      if (list) {
        list.replaceChildren(...items.slice(0, MAX_SHOWN).map(renderRow));
        list.hidden = !items.length;
      }
    } catch (error) {
      if (status) status.textContent = `${_tr('Vérification impossible')} : ${error.message}`;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function renderDuplicateGroup(group) {
    const row = document.createElement('li');
    row.className = 'metadata-check-item';
    const title = document.createElement('strong');
    title.textContent = group.items[0]?.title || '';
    const reason = document.createElement('span');
    reason.className = 'metadata-check-issues';
    reason.textContent = group.same_record
      ? `${group.reason} : ${_tr('supprimez les pièces jointes en trop dans Zotero')}`
      : `${group.reason} : ${_tr('fusionnez-les avec Zotero (Doublons)')}`;
    const actions = document.createElement('div');
    actions.className = 'btn-row';
    const links = group.same_record ? group.items.slice(0, 1) : group.items.slice(0, 4);
    links.filter((item) => item.zotero_select_uri).forEach((item, index) => {
      const link = document.createElement('a');
      link.className = 'btn btn-ghost';
      link.href = item.zotero_select_uri;
      link.textContent = group.same_record ? _tr('Afficher dans Zotero') : `${_tr('Notice')} ${index + 1}`;
      actions.append(link);
    });
    row.append(title, reason, actions);
    return row;
  }

  async function scanDuplicates() {
    const rowEl = byId('duplicate-check-row');
    const list = byId('duplicate-check-list');
    const status = byId('duplicate-check-status');
    try {
      const response = await fetch('/api/library/duplicates', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.msg || `HTTP ${response.status}`);
      const groups = Array.isArray(data.groups) ? data.groups : [];
      if (rowEl) rowEl.hidden = false;
      if (status) {
        status.textContent = groups.length
          ? `${data.group_count} ${_tr('document(s) en plusieurs exemplaires')}, ${data.extra_files} ${_tr('fichier(s) en trop indexé(s)')}.`
          : _tr('Aucun doublon trouvé.');
      }
      if (list) {
        list.replaceChildren(...groups.slice(0, MAX_SHOWN).map(renderDuplicateGroup));
        list.hidden = !groups.length;
      }
    } catch (error) {
      if (status) status.textContent = `${_tr('Vérification impossible')} : ${error.message}`;
    }
  }

  function bind() {
    byId('metadata-check-scan')?.addEventListener('click', () => { scan(); scanDuplicates(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
