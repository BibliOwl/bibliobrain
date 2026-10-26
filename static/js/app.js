// ---------------------------------------------
//  STATE
// ---------------------------------------------

const state = {
  ragReady: false,
  ragInitBusy: false,
  ragLoadPaused: false,
  // 'prepare' or 'repair' while the library waits for its first load (pill action).
  ragFirstInitAction: '',
  ragInitAvailable: true,
  libraryStats: null,
  ollamaHealth: null,
  autoInitAttempted: false,
  bootInProgress: false,
  startupStatus: null,
  startupStatusPromise: null,
  startupAutoInitRunning: false,
  startupAutoInitTimer: null,
  startupHelpShown: false,
  startupHelpAutoChecked: false,
  startupGuideStep: 'library',
  startupHelpOpenedByUser: false,
  startupLibrarySaving: false,
  startupDataPathTouched: false,
  startupZoteroAutoDetect: '',
  startupZoteroNoticeShown: false,
  startupMetadataSource: '',
  metadataSource: 'none',
  startupGuideRagRunning: false,
  startupGuidePreparing: false,
  startupGuideChecksReady: false,
  startupGuideFailed: false,
  startupGuideFollowing: false,
  startupGuideLibraryChanged: false,
  startupHelpReturnFocus: null,
  appSettings: null,
  capabilities: { rag_connections: true, rag_retrieval_graph: true },
  streaming: false,
  chatSessions: [],
  activeChatId: '',
  activeChatTitle: '',
  chatViewRevision: 0,
  nextChatSourceNumber: 1,
  chatSourceNumberByKey: new Map(),
  chatSourceKeyByNumber: new Map(),
  chatAbortController: null,
  activeStreamRequestId: '',
  activeStreamModel: '',
  activeStreamChatId: '',
  chatArticleScope: { path: '', title: '', once: false, paths: [], titles: [] },
  chatArticleScopeRevision: 0,
  chatForceVisualOnce: false,
  libRows: [],
  libPaths: [],
  libMetadata: [],
  visibleLibPaths: [],
  selectedRow: -1,
  libSelectedPaths: new Set(),
  libSelectionAnchor: '',
  currentSummaryPath: '',
  currentCitations: null,
  analysisByArticle: {},
  analysisPrompts: [],
  analysisViewRequestKey: '',
  libraryDetailRequestKey: '',
  citationRequestKey: '',
  lastDraftingSuggestions: [],
  drafting: {
    citationStartNumber: 1,
    selectionRange: null,
    previewCitationId: '',
    previewTokenId: '',
    searchAbortController: null,
    searching: false,
    reviewAbortController: null,
    reviewAbortRequestId: '',
    reviewAbortModel: '',
    citationIndex: new Map(),
    citationOrder: [],
    pendingSuggestionId: '',
    // Citation search progress: one entry per analysed sentence.
    sentenceLog: [],
    citationRun: null,
    // Searches outside the library, by sentence key: { status, message }.
    externalSearches: new Map(),
    externalSearchController: null,
    openSuggestionDetails: new Set(),
    // "Source d'origine ?" checks, by citation id + sentence: { status, result }.
    origins: new Map(),
    lastAssistantText: '',
    lastQueryText: '',
    lastProofreadText: '',
    lastArgumentSuggestions: [],
    lastArgumentSources: [],
    lastArgumentPayload: null,
    lastArgumentRequest: null,
    argumentUndo: null,
    argumentVersionSaved: false,
    notebookOptionsLoaded: false,
    notebookOptionsLoading: false,
    notebookOptions: [],
    lastNotebookAnswer: '',
    lastNotebookSources: [],
    lastNotebookTitle: '',
    reviewBusy: false,
    reviewActiveAction: '',
    activeView: 'citations',
    reviewView: 'proofread',
    notebookViewUsed: false,
  },
  zoteroStorage: '',
  zoteroFacets: { tags: [], collections: [], stats: { items: 0, tags: 0, collections: 0 } },
  providerInfo: null,
  vectorDbStatus: null,
  knowledgeGraph: null,
  knowledgeGraphAdj: new Map(),
  knowledgeGraphDirty: true,
  knowledgeGraphFocusNodeId: '',
  knowledgeGraphFocusPath: [],
  knowledgeGraphVisiblePaths: [],
  knowledgeGraphNotebookList: [],
  knowledgeGraphNodeClickTimer: null,
  knowledgeGraphLinkRaf: null,
  knowledgeGraphAnimateFromDepth: null,
  knowledgeGraphView: { scale: 1, tx: 0, ty: 0 },
  knowledgeGraphUiBound: false,
  knowledgeGraphDrag: { active: false, moved: false, lastX: 0, lastY: 0 },
  knowledgeGraphSearchQuery: '',
  knowledgeGraphSearchMatches: [],
  knowledgeGraphComponentFilter: '',
  knowledgeGraphCommunityId: '',
  lastRagRetrieval: null,
  notebookImportContext: null,
  discoveryResults: [],
  discoveryHistory: [],
  activeDiscoveryHistoryId: '',
  discoveryNotebooksLoaded: false,
  discoveryNotebookContext: null,
  discoveryAbortController: null,
  discoveryActiveModel: '',
  discoveryLoading: false,
  discoveryProgressTimer: null,
  researchTrends: {
    data: null,
    metric: 'count',
    loading: false,
    abortController: null,
  },
  bugReportReturnFocus: null,
  bugReportScreenshotDataUrl: '',
  bugReportScreenshotMeta: null,
  recentClientErrors: [],
};

function isLightPerformanceMode() {
  return document.documentElement.getAttribute('data-performance-mode') === 'light';
}

function streamRenderIntervalMs() {
  return isLightPerformanceMode() ? 140 : 50;
}

function isVisualUpdateVisible(element) {
  return !document.hidden && Boolean(element?.isConnected && element.getClientRects().length);
}

// ---------------------------------------------
//  I18N HELPER
// ---------------------------------------------
function _tr(msg) {
  return window.BibliobrainI18n?.translateRaw
    ? window.BibliobrainI18n.translateRaw(String(msg || ''))
    : msg;
}

function _ti(key, params = null) {
  return window.BibliobrainI18n?.t
    ? window.BibliobrainI18n.t(String(key || ''), params || undefined)
    : String(key || '');
}

function _recordClientError(type, message, source = '', location = '') {
  let safeSource = '';
  try {
    const parsed = new URL(String(source || ''), window.location.origin);
    safeSource = parsed.pathname.split('/').slice(-3).join('/');
  } catch (_) { /* source omitted */ }
  state.recentClientErrors.push({
    timestamp: new Date().toISOString(),
    type: String(type || 'error').slice(0, 40),
    message: String(message || 'Unknown client error').slice(0, 1000),
    source: safeSource.slice(0, 300),
    location: String(location || '').slice(0, 80),
  });
  if (state.recentClientErrors.length > 30) state.recentClientErrors.shift();
}

window.addEventListener('error', (event) => {
  _recordClientError(
    'window.error',
    event.message,
    event.filename,
    `${event.lineno || 0}:${event.colno || 0}`,
  );
});
window.addEventListener('unhandledrejection', (event) => {
  const reason = event.reason;
  _recordClientError('unhandledrejection', reason?.message || String(reason || 'Rejected promise'));
});
const _nativeConsoleError = console.error.bind(console);
console.error = (...args) => {
  const message = args.map((item) => item?.message || String(item || '')).join(' ');
  _recordClientError('console.error', message);
  _nativeConsoleError(...args);
};

// ---------------------------------------------
//  CENTRE D'ACTIVITÉ (sidebar)
//  Regroupe les tâches longues (init RAG, reranker)
//  dans une seule zone au lieu d'indicateurs éparpillés.
// ---------------------------------------------
const _activityTasks = new Map();

// onClick turns the row into a button (e.g. open the task's settings).
function activitySet(id, label, detail = '', frac = null, { paused = false, onClick = null } = {}) {
  _activityTasks.set(id, { label, detail, frac, paused, onClick });
  _renderActivity();
}

function activityClear(id) {
  if (_activityTasks.delete(id)) _renderActivity();
}

function _renderActivity() {
  const wrap = document.getElementById('sidebar-activity');
  if (!wrap) return;
  wrap.hidden = !_activityTasks.size;
  wrap.innerHTML = '';
  // The CSS hides the rows of the work the sidebar status pill shows while
  // the sidebar is expanded.
  wrap.classList.toggle('is-pill-only', [..._activityTasks.keys()].every((id) => _STATUS_TASK_ORDER.includes(id)));
  _activityTasks.forEach(({ label, detail, frac, paused, onClick }, id) => {
    const row = document.createElement(onClick ? 'button' : 'div');
    row.className = paused ? 'activity-item is-paused' : 'activity-item';
    row.dataset.activity = id;
    if (onClick) {
      row.type = 'button';
      row.classList.add('is-action');
      row.addEventListener('click', onClick);
    }
    if (detail) row.title = _tr(detail);
    const spinner = document.createElement('span');
    spinner.className = 'spinner';
    row.appendChild(spinner);
    const text = document.createElement('span');
    text.className = 'activity-label';
    text.textContent = _tr(label);
    row.appendChild(text);
    if (Number.isFinite(Number(frac)) && frac !== null) {
      const pct = document.createElement('span');
      pct.className = 'activity-pct';
      pct.textContent = `${Math.round(Number(frac) * 100)}%`;
      row.appendChild(pct);
    }
    wrap.appendChild(row);
  });
}
window.activitySet = activitySet;
window.activityClear = activityClear;

// ---------------------------------------------
//  SIDEBAR STATUS — one pill for all background work
//  The AI engine, the library (RAG), the OCR pass and the reranker each
//  register a task with a tone. The pill shows the one that matters most
//  (blocked > needs > working > ready), "+N" for the others that are not
//  ready; clicking it opens the Activity card that lists them all.
// ---------------------------------------------
const _STATUS_TASK_ORDER = ['ollama', 'rag', 'ocr', 'reranker'];
const _STATUS_TONES = ['blocked', 'needs', 'working', 'ready'];
const _statusTasks = new Map();
let _statusPanel = null;

/**
 * Register, update or (with null) remove a background task.
 * @param {string} id - 'ollama', 'rag', 'ocr' or 'reranker'
 * @param {?{tone: 'ready'|'working'|'needs'|'blocked', text: string, title?: string,
 *   percent?: ?number, action?: {label: string, onClick: Function, disabled?: boolean},
 *   view: Function}} task
 *   percent: progress bar under the pill text while working.
 *   action: a button inside the pill, shown for 'needs' and 'blocked' only.
 *   yieldsToWork: a 'needs' task that only asks to resume paused work; it
 *   gives the pill to any task in progress and stays in "+N" and the card.
 *   view() returns {title, detail, eta?, percent?, facts?: [{term, value, items?, more?}],
 *   actions?: [{label, onClick, primary?}]} for the Activity card.
 */
function setStatusTask(id, task) {
  if (task) _statusTasks.set(id, task);
  else _statusTasks.delete(id);
  renderSidebarStatus();
}

function statusTaskTone(id) {
  return _statusTasks.get(id)?.tone || '';
}

function _statusToneRank(task, someoneWorks = false) {
  const rank = _STATUS_TONES.indexOf(task?.tone);
  // Work that waits on the user only to be resumed (an interrupted OCR pass)
  // steps behind work in progress, so a running library update stays in view.
  if (someoneWorks && task?.yieldsToWork && task.tone === 'needs') {
    return _STATUS_TONES.indexOf('working') + 0.5;
  }
  return rank < 0 ? _STATUS_TONES.length : rank;
}

// Most urgent first; _STATUS_TASK_ORDER breaks ties.
function _sortedStatusTaskIds() {
  const someoneWorks = [..._statusTasks.values()].some((task) => task?.tone === 'working');
  const rank = (id) => _statusToneRank(_statusTasks.get(id), someoneWorks);
  return _STATUS_TASK_ORDER
    .filter((id) => _statusTasks.has(id))
    .sort((a, b) => rank(a) - rank(b)
      || _STATUS_TASK_ORDER.indexOf(a) - _STATUS_TASK_ORDER.indexOf(b));
}

function _shownStatusTaskId() {
  const [first] = _sortedStatusTaskIds();
  // When everything is fine the pill speaks for the library ("Ready · n articles").
  if (!first || (_statusTasks.get(first).tone === 'ready' && _statusTasks.has('rag'))) return 'rag';
  return first;
}

function _statusPercent(value) {
  const number = Number(value);
  return value !== null && value !== undefined && Number.isFinite(number)
    ? Math.round(Math.max(0, Math.min(100, number)))
    : null;
}

function renderSidebarStatus() {
  const shownId = _shownStatusTaskId();
  const task = _statusTasks.get(shownId);
  // No task registered yet (boot): neutral, never the green of "ready".
  const tone = task?.tone || 'idle';
  const text = document.getElementById('rag-status-text');
  const badge = document.getElementById('status-more-badge');
  const row = document.getElementById('rag-status-row');
  const bar = document.getElementById('rag-status-bar');
  const actionBtn = document.getElementById('rag-status-action');
  if (row) {
    row.dataset.shown = shownId;
    row.dataset.tone = tone;
  }
  if (task && text) {
    text.textContent = task.text;
    text.title = task.title || task.text;
  }
  const percent = tone === 'working' ? _statusPercent(task?.percent) : null;
  if (bar) {
    bar.hidden = percent === null;
    if (percent !== null) bar.firstElementChild.style.width = `${percent}%`;
  }
  const action = (tone === 'needs' || tone === 'blocked') && task?.action?.label ? task.action : null;
  if (actionBtn) {
    actionBtn.hidden = !action;
    actionBtn.textContent = action?.label || '';
    actionBtn.disabled = Boolean(action?.disabled);
    // A click on the button runs the action; the rest of the pill opens the card.
    actionBtn.onclick = action ? (event) => { event.stopPropagation(); action.onClick?.(); } : null;
  }
  const others = _STATUS_TASK_ORDER
    .filter((id) => id !== shownId && _statusTasks.has(id) && _statusTasks.get(id).tone !== 'ready').length;
  if (badge) {
    badge.hidden = !others;
    badge.textContent = `+${others}`;
  }
  if (text) {
    const detail = task?.title && task.title !== task.text ? ` — ${task.title}` : '';
    const more = others ? ` — ${_ti('sidebar.statusMore', { count: others })}` : '';
    text.setAttribute('aria-label', `${text.textContent}${detail}${more}`);
  }
  _renderStatusPanel();
}

function _statusPanelFacts(facts) {
  return facts.filter((fact) => fact.value).map((fact) => {
    const row = document.createElement('div');
    const term = document.createElement('dt');
    term.textContent = fact.term;
    const value = document.createElement('dd');
    value.textContent = fact.value;
    row.append(term, value);
    const items = fact.items || [];
    if (items.length) {
      const list = document.createElement('ul');
      list.className = 'status-detail-files';
      items.forEach(({ change, text, title }) => {
        const item = document.createElement('li');
        item.dataset.change = change;
        item.textContent = text;
        item.title = title || text;
        list.appendChild(item);
      });
      if (fact.more) {
        const more = document.createElement('li');
        more.className = 'is-more';
        more.textContent = fact.more;
        list.appendChild(more);
      }
      row.appendChild(list);
    }
    return row;
  });
}

function _statusPanelSection(id, tone, view) {
  const section = document.createElement('section');
  section.className = 'status-detail-section';
  section.dataset.task = id;
  section.dataset.tone = tone;
  const head = document.createElement('div');
  head.className = 'status-detail-head';
  const dot = document.createElement('span');
  dot.className = 'status-detail-dot';
  dot.setAttribute('aria-hidden', 'true');
  const title = document.createElement('h3');
  title.textContent = view.title;
  head.append(dot, title);
  if (view.eta) {
    const eta = document.createElement('span');
    eta.className = 'status-detail-eta';
    eta.textContent = view.eta;
    head.appendChild(eta);
  }
  section.appendChild(head);
  const detail = view.detail ?? view.status;
  if (detail) {
    const status = document.createElement('p');
    status.className = 'status-detail-status';
    status.textContent = detail;
    section.appendChild(status);
  }
  const percent = _statusPercent(view.percent);
  if (percent !== null) {
    const bar = document.createElement('div');
    bar.className = 'status-detail-bar';
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    bar.setAttribute('aria-valuenow', String(percent));
    bar.setAttribute('aria-label', view.title);
    const fill = document.createElement('span');
    fill.style.width = `${percent}%`;
    bar.appendChild(fill);
    section.appendChild(bar);
  }
  const facts = _statusPanelFacts(view.facts || []);
  if (facts.length) {
    const list = document.createElement('dl');
    list.className = 'status-detail-facts';
    list.append(...facts);
    section.appendChild(list);
  }
  const actions = (view.actions || []).filter((action) => action?.label);
  if (actions.length) {
    const row = document.createElement('div');
    row.className = 'status-detail-actions';
    actions.forEach(({ label, onClick, primary, disabled }) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = primary ? 'status-detail-btn is-primary' : 'status-detail-btn';
      button.textContent = label;
      button.disabled = Boolean(disabled);
      button.addEventListener('click', () => onClick?.());
      row.appendChild(button);
    });
    section.appendChild(row);
  }
  return section;
}

function _statusPanelAnchor() {
  const row = document.getElementById('rag-status-row');
  // The collapsed sidebar shrinks the pill to its dot: the card opens beside it.
  return row || document.getElementById('sidebar-activity');
}

function _renderStatusPanel() {
  const panel = _statusPanel;
  if (!panel) return;
  const { el } = panel;
  el.querySelector('h2').textContent = _ti('sidebar.activityTitle');
  el.querySelector('.status-detail-settings').textContent = _ti('status.openSettings');
  const sections = _sortedStatusTaskIds()
    .map((id) => [id, _statusTasks.get(id), _statusTasks.get(id)?.view?.()])
    .filter(([, , view]) => view)
    .map(([id, task, view]) => _statusPanelSection(id, task.tone || 'ready', view));
  el.querySelector('.status-detail-sections').replaceChildren(...sections);
  // The content changes height as the work reports: keep the card on screen.
  const anchor = el.isConnected && _statusPanelAnchor();
  if (anchor && typeof _onbPlacePopover === 'function') {
    _onbPlacePopover(el, anchor.getBoundingClientRect());
  }
}

function closeStatusPanel() {
  const panel = _statusPanel;
  if (!panel) return;
  _statusPanel = null;
  panel.cleanup();
  panel.el.remove();
  document.getElementById('rag-status-summary')?.setAttribute('aria-expanded', 'false');
}

function toggleStatusPanel() {
  if (_statusPanel) {
    closeStatusPanel();
    return;
  }
  const anchor = _statusPanelAnchor();
  if (!anchor || typeof _onbPlacePopover !== 'function') return;
  const el = document.createElement('div');
  el.className = 'ft-card onb-popover status-detail';
  el.id = 'status-detail';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'false');
  el.setAttribute('aria-labelledby', 'status-detail-title');
  el.innerHTML = `
    <div class="status-detail-header">
      <h2 id="status-detail-title"></h2>
      <button type="button" class="status-detail-settings"></button>
    </div>
    <div class="status-detail-sections" aria-live="polite"></div>`;
  el.querySelector('.status-detail-settings').addEventListener('click', () => {
    closeStatusPanel();
    if (!document.getElementById('settings-panel')?.classList.contains('open')) toggleSettings();
  });
  _statusPanel = { el, cleanup: () => {} };
  _renderStatusPanel();
  document.body.appendChild(el);
  _onbPlacePopover(el, anchor.getBoundingClientRect());
  _statusPanel.cleanup = _onbTrackPopover(el, anchor, closeStatusPanel);
  document.getElementById('rag-status-summary')?.setAttribute('aria-expanded', 'true');
  window.setTimeout(() => el.querySelector('.status-detail-settings')?.focus(), 0);
}

// The activity rows stand for the pill in the collapsed sidebar.
const statusActivityOptions = { onClick: () => toggleStatusPanel() };
window.setStatusTask = setStatusTask;
window.statusTaskTone = statusTaskTone;
window.toggleStatusPanel = toggleStatusPanel;
window.closeStatusPanel = closeStatusPanel;
window.statusActivityOptions = statusActivityOptions;

// Indexed articles, from the Library stats once loaded, else the startup snapshot.
function libraryArticleCount() {
  const stats = state.libraryStats;
  const count = Number(stats?.indexed_document_count ?? stats?.indexed_pdf_count ?? state.startupStatus?.library_count ?? 0);
  return Number.isFinite(count) && count > 0 ? count : 0;
}

function formatCount(value) {
  const locale = window.BibliobrainI18n?.getLanguage?.() === 'en' ? 'en-US' : 'fr-FR';
  return Number(value || 0).toLocaleString(locale);
}
window.libraryArticleCount = libraryArticleCount;
window.formatCount = formatCount;

// ---------------------------------------------
//  RAG STATUS — single source of truth
//  Toute l'UI du badge (#rag-status-dot, #rag-status-text, #rag-status-box)
//  est dérivée de state.ragUiState. Plus d'écritures DOM directes.
// ---------------------------------------------

const RAG_PHASES = ['idle', 'starting', 'loading', 'preparing', 'ready', 'error', 'warning'];

state.ragUiState = {
  phase: 'idle',       // one of RAG_PHASES
  label: 'sidebar.notInitialized',  // i18n key or raw string
  detail: '',          // i18n key or raw string (tooltip / status box)
  progress: null,      // 0..1 or null
};

function _ragPhaseBoxTone(phase) {
  if (phase === 'ready') return 'green';
  if (phase === 'loading' || phase === 'preparing' || phase === 'starting') return 'cyan';
  if (phase === 'warning') return 'amber';
  return 'red';  // error, idle with no detail
}

let _ragStatusRenderTimer = null;

function _ragStatusText(value) {
  const raw = String(value || '');
  if (!raw) return '';
  const translatedKey = _ti(raw);
  return translatedKey !== raw ? translatedKey : _tr(raw);
}

// The library's state as a status-pill task: working while a load runs,
// "needs" with an action when the user must act (first preparation, new PDFs),
// blocked on an error, ready otherwise.
function _ragStatusTask(phase, translatedLabel, translatedDetail, percentage) {
  const busy = Boolean(state.ragInitBusy);
  const loadingPhase = ['starting', 'loading', 'preparing'].includes(phase);
  const task = {
    tone: 'ready',
    text: translatedLabel,
    title: translatedDetail || translatedLabel,
    percent: percentage,
    view: () => (typeof _ragDetailView === 'function' ? _ragDetailView() : null),
  };
  if (busy || loadingPhase) {
    task.tone = 'working';
    if (busy && loadingPhase) {
      task.text = percentage !== null
        ? _ti('status.ragLoading', { pct: percentage })
        : _ti('status.ragLoadingNoPct');
    } else if (percentage !== null) {
      task.text = `${translatedLabel} · ${percentage}%`;
    }
    return task;
  }
  if (phase === 'error') return { ...task, tone: 'blocked' };
  if (phase === 'ready') {
    const pending = typeof _ragPendingChanges === 'function' ? _ragPendingChanges() : null;
    if (pending) return { ...task, tone: 'needs', text: pending.text, action: pending.action };
    const count = libraryArticleCount();
    if (count) task.text = _ti('status.ready', { n: formatCount(count) });
    return task;
  }
  // Not loaded: idle, stopped or waiting for a repair / the setup.
  const firstInit = state.ragFirstInitAction;
  if (firstInit) {
    // Short text: the action button leaves room for about fifteen characters.
    task.text = _ti('status.ragNotReadyShort');
    task.title = _ti('status.ragNotReady');
    task.action = {
      label: _ti(firstInit === 'repair' ? 'status.ragRepair' : 'status.ragPrepare'),
      onClick: () => runFirstRagInit(),
      disabled: state.ragInitAvailable === false,
    };
  }
  return { ...task, tone: 'needs' };
}

function renderRagStatus() {
  if (typeof renderRagRefreshControls === 'function') renderRagRefreshControls();
  const { phase, label, detail, progress } = state.ragUiState;
  const statusBox = document.getElementById('rag-status-box');
  const translatedLabel = _ragStatusText(label);
  const translatedDetail = _ragStatusText(detail);

  const numericFraction = Number(progress);
  const hasProgress = progress !== null && Number.isFinite(numericFraction);
  const percentage = hasProgress
    ? Math.round(Math.max(0, Math.min(1, numericFraction)) * 100)
    : null;
  setStatusTask('rag', _ragStatusTask(phase, translatedLabel, translatedDetail, percentage));
  if (typeof _syncChatEmptyState === 'function') _syncChatEmptyState();
  if (typeof _syncLibraryEmptyRow === 'function') _syncLibraryEmptyRow();

  // Status box — detail box in settings panel
  if (!statusBox) return;
  if (!translatedDetail) {
    statusBox.style.display = 'none';
    return;
  }
  statusBox.className = `status-mini status-${_ragPhaseBoxTone(phase)}`;
  statusBox.textContent = translatedDetail;
  statusBox.style.display = 'block';
}

function _scheduleRagStatusRender() {
  if (_ragStatusRenderTimer) return;
  _ragStatusRenderTimer = requestAnimationFrame(() => {
    _ragStatusRenderTimer = null;
    renderRagStatus();
  });
}

/**
 * Set the RAG status state and render immediately.
 * @param {'idle'|'starting'|'loading'|'preparing'|'ready'|'error'|'warning'} phase
 * @param {string} label - i18n key or raw string
 * @param {string} [detail] - i18n key or raw string
 * @param {number|null} [progress] - 0..1 or null
 */
function ragStatusSet(phase, label, detail = '', progress = null) {
  const normalizedPhase = RAG_PHASES.includes(phase) ? phase : 'idle';
  state.ragUiState.phase = normalizedPhase;
  state.ragUiState.label = String(label || 'sidebar.notInitialized');
  state.ragUiState.detail = String(detail || '');
  state.ragUiState.progress = (progress !== null && Number.isFinite(Number(progress)))
    ? Math.max(0, Math.min(1, Number(progress)))
    : null;

  // Sync ragReady flag
  state.ragReady = normalizedPhase === 'ready';

  renderRagStatus();
}

/**
 * Update progress during an ongoing operation.
 * Does not change the phase or label — only progress and optional detail.
 */
function ragStatusProgress(frac, detail = '') {
  const numericFraction = Number(frac);
  state.ragUiState.progress = Number.isFinite(numericFraction)
    ? Math.max(0, Math.min(1, numericFraction))
    : null;
  if (detail) state.ragUiState.detail = String(detail);
  _scheduleRagStatusRender();
}

/**
 * Convenience: set ready state.
 */
function ragStatusReady(label, detail = '') {
  ragStatusSet('ready', String(label || 'ui.ready'), String(detail));
  if (typeof checkRagRefreshNeeded === 'function') void checkRagRefreshNeeded();
}

/**
 * Convenience: set error state.
 */
function ragStatusError(label, detail = '') {
  ragStatusSet('error', String(label || 'Erreur'), String(detail));
}

/**
 * Convenience: set warning state.
 */
function ragStatusWarning(label, detail = '') {
  ragStatusSet('warning', String(label), String(detail));
}

/**
 * Reset to idle and clear UI. Use when you need to re-fetch status from server.
 */
function ragStatusReset() {
  ragStatusSet('idle', 'sidebar.notInitialized');
}

window.ragStatusSet = ragStatusSet;
window.ragStatusProgress = ragStatusProgress;
window.ragStatusReady = ragStatusReady;
window.ragStatusError = ragStatusError;
window.ragStatusWarning = ragStatusWarning;
window.ragStatusReset = ragStatusReset;
window.renderRagStatus = renderRagStatus;
