// ---------------------------------------------
//  LIBRARY
// ---------------------------------------------
// Every reload button's label, badge and disabled state come from
// renderRagRefreshControls(), driven by state.ragInitBusy.
async function reloadLibraryRag() {
  if (state.ragInitBusy) {
    showToast(
      window.BibliobrainI18n?.t?.('library.ragReloadBusy')
        || _tr('Le rechargement du RAG est déjà en cours.'),
      3200,
      'info',
    );
    return { ok: false, busy: true };
  }

  // Waiting for the OCR to pause can take a page's worth of time.
  if (state.ragReloadPreparing) return { ok: false, busy: true };
  state.ragReloadPreparing = true;
  let pauseResult;
  try {
    pauseResult = await pauseOcrForRagReload();
  } finally {
    state.ragReloadPreparing = false;
  }
  const { proceed, resumeOcr } = pauseResult;
  if (!proceed) return { ok: false, cancelled: true };

  try {
    const result = await initRAG(true, { silentSuccess: true });
    if (result?.ok) {
      await Promise.all([
        loadLibrary(),
        loadZoteroFacets(),
      ]);
      showToast(
        window.BibliobrainI18n?.t?.('library.ragReloaded')
        || _tr('RAG actualisé. Bibliothèque actualisée.'),
        4000,
        'success',
      );
    }
    return result;
  } finally {
    if (resumeOcr) await resumeOcrAfterRagReload();
    renderRagRefreshControls();
  }
}

async function loadLibrary() {
  void checkRagRefreshNeeded();
  const tbody = document.getElementById('lib-tbody');
  let showedSkeleton = false;
  if (tbody && !tbody.children.length) {
    showedSkeleton = true;
    tbody.innerHTML = Array.from({ length: 8 }, (_, i) => `
      <tr aria-hidden="true">
        <td><div class="skeleton skeleton-line" style="width:${78 - (i % 3) * 14}%"></div></td>
        <td><div class="skeleton skeleton-line" style="width:70px"></div></td>
        <td><div class="skeleton skeleton-line" style="width:60%"></div></td>
        <td><div class="skeleton skeleton-line" style="width:34px"></div></td>
        <td><div class="skeleton skeleton-line" style="width:20px"></div></td>
      </tr>`).join('');
  }
  try {
    const d = await fetchAllPaginated(
      '/api/library',
      ['rows', 'paths', 'metadata'],
      { limit: 1000, fallback: 'Chargement bibliothèque impossible' },
    );
    populateLibrary(
      Array.isArray(d.rows) ? d.rows : [],
      Array.isArray(d.paths) ? d.paths : [],
      Array.isArray(d.metadata) ? d.metadata : [],
    );
    if (d.facets) _applyZoteroFacets(d.facets);
    renderLibraryStats(d.stats);
  } catch(e) {
    if (showedSkeleton && tbody) tbody.innerHTML = '';
    showToast(e?.message || 'Erreur chargement bibliothèque', 5200, 'error');
  }
}

function renderLibraryStats(stats) {
  // The status pill and the empty chat quote the number of indexed articles.
  if (stats) {
    state.libraryStats = stats;
    renderRagStatus();
    if (typeof _refreshChatEmptyState === 'function') _refreshChatEmptyState();
  }
  const count = document.getElementById('lib-count');
  if (count && stats) {
    const indexed = Number(stats.indexed_document_count ?? stats.indexed_pdf_count ?? 0);
    count.textContent = _ti('library.articleCount', { n: formatCount(indexed) });
  }
  const root = document.getElementById('lib-stats');
  if (!root) return;

  const totalDocumentCount = Number(stats?.total_document_count ?? stats?.total_pdf_count ?? 0);
  const indexedDocumentCount = Number(stats?.indexed_document_count ?? stats?.indexed_pdf_count ?? 0);
  const indexedManifestCount = Number(stats?.indexed_manifest_count || 0);
  const zoteroItemCount = Number(stats?.zotero_item_count || 0);

  root.innerHTML = [
    `<span class="lib-stat-pill"><strong>${indexedDocumentCount}</strong> documents indexés</span>`,
    `<span class="lib-stat-pill"><strong>${totalDocumentCount}</strong> documents détectés</span>`,
    `<span class="lib-stat-pill"><strong>${zoteroItemCount}</strong> items Zotero</span>`,
    `<span class="lib-stat-pill"><strong>${indexedManifestCount}</strong> entrées manifest</span>`,
  ].join('');
}

const _sort = { col: -1, asc: true };
// Reassigned by knowledge-graph.js in the shared classic-script scope.
// eslint-disable-next-line prefer-const
let _kgLoadTimer = null;

const LAYOUT_RESIZER_CONFIGS = {
  sidebar: {
    axes: ['x'],
    container: '#app',
    target: '#sidebar',
    variable: () => '--sidebar-w',
    unit: 'px',
    defaultValue: () => 252,
    bounds: () => ({ min: 236, max: 340 }),
    disabled: () => _layoutIsMobile() || document.getElementById('sidebar')?.classList.contains('collapsed'),
  },
  library: {
    axes: ['x', 'y'],
    container: '#library-layout',
    target: '#library-main',
    axis: () => _layoutMainWidth() < 980 ? 'y' : 'x',
    variable: axis => axis === 'x' ? '--library-primary-w' : '--library-primary-h',
    unit: 'ratio',
    defaultValue: axis => axis === 'x' ? 0.63 : 0.58,
    bounds: ctx => ctx.axis === 'x'
      ? { min: 500, max: ctx.total - 8 - 350 }
      : { min: 300, max: ctx.total - 8 - 280 },
    disabled: () => _layoutIsMobile(),
  },
  'drafting-main': {
    axes: ['x', 'y'],
    container: '.drafting-workbench',
    target: '.drafting-editor-shell',
    // Follow the CSS container query: a single grid column means the editor
    // and the side panel are stacked, so the handle resizes the editor height.
    axis: container => getComputedStyle(container).gridTemplateColumns.trim().split(/\s+/).length < 2 ? 'y' : 'x',
    variable: axis => axis === 'x' ? '--drafting-editor-w' : '--drafting-editor-h',
    unit: axis => axis === 'x' ? 'ratio' : 'px',
    defaultValue: axis => axis === 'x' ? 0.65 : 420,
    bounds: ctx => ctx.axis === 'x'
      ? { min: 460, max: ctx.total - 8 - 300 }
      : { min: 220, max: Math.max(220, ctx.total - 8 - 160) },
    disabled: ctx => _layoutIsMobile() || (ctx.axis === 'x' && ctx.total < 768),
  },
  'drafting-bibliography': {
    axes: ['y'],
    container: '#drafting-assistant',
    target: '#drafting-bibliography-shell',
    variable: () => '--drafting-bibliography-h',
    unit: 'px',
    direction: -1,
    defaultValue: () => 92,
    bounds: () => {
      const workbench = document.querySelector('.drafting-workbench')?.getBoundingClientRect().height || 0;
      const bibliography = document.getElementById('drafting-bibliography-shell')?.getBoundingClientRect().height || 0;
      return { min: 92, max: Math.max(92, Math.min(320, workbench + bibliography - 320)) };
    },
    disabled: () => _layoutIsMobile(),
  },
  'knowledge-graph': {
    axes: ['x', 'y'],
    container: '#knowledge-graph-wrap',
    target: '#knowledge-graph-stage',
    axis: () => _layoutMainWidth() <= 900 ? 'y' : 'x',
    variable: axis => axis === 'x' ? '--knowledge-graph-primary-w' : '--knowledge-graph-primary-h',
    unit: 'ratio',
    defaultValue: axis => axis === 'x' ? 0.68 : 0.60,
    bounds: ctx => ctx.axis === 'x'
      ? { min: 360, max: ctx.total - 8 - 300 }
      : { min: 220, max: ctx.total - 8 - 180 },
    disabled: () => _layoutIsMobile(),
  },
  'notebook-sources': {
    axes: ['x'],
    container: '#nb-columns',
    target: '#nb-col-sources',
    variable: () => '--nb-sources-w',
    unit: 'ratio',
    defaultValue: () => 0.30,
    bounds: ctx => ({ min: 220, max: ctx.total - 16 - 220 - 220 }),
    disabled: () => _layoutIsMobile()
      || !document.getElementById('nb-workspace')?.classList.contains('open')
      || document.getElementById('nb-col-sources')?.classList.contains('collapsed')
      || document.getElementById('nb-col-notes')?.classList.contains('collapsed'),
  },
  'notebook-notes': {
    axes: ['x'],
    container: '#nb-columns',
    target: '#nb-col-notes',
    variable: () => '--nb-notes-w',
    unit: 'ratio',
    defaultValue: () => 0.30,
    bounds: ctx => {
      const sourcesWidth = document.getElementById('nb-col-sources')?.getBoundingClientRect().width || 0;
      return { min: 220, max: ctx.total - sourcesWidth - 16 - 220 };
    },
    disabled: () => _layoutIsMobile()
      || !document.getElementById('nb-workspace')?.classList.contains('open')
      || document.getElementById('nb-col-notes')?.classList.contains('collapsed')
      || document.getElementById('nb-col-chat')?.classList.contains('collapsed'),
  },
};

let _layoutResizeState = null;
let _layoutResizeFrame = 0;

function _layoutIsMobile() {
  return window.matchMedia('(max-width: 760px)').matches;
}

function _layoutMainWidth() {
  return document.getElementById('main')?.getBoundingClientRect().width || window.innerWidth;
}

function _layoutStorageKey(key, axis) {
  return LS_PREFIX + `layout-size-${key}-${axis}`;
}

function _layoutUnit(config, axis) {
  return typeof config.unit === 'function' ? config.unit(axis) : config.unit;
}

function _layoutClamp(value, min, max) {
  const safeMin = Number.isFinite(min) ? min : 0;
  const safeMax = Number.isFinite(max) ? Math.max(safeMin, max) : Number.POSITIVE_INFINITY;
  return Math.max(safeMin, Math.min(safeMax, Number(value) || safeMin));
}

function _layoutResolveContext(key) {
  const config = LAYOUT_RESIZER_CONFIGS[key];
  const handle = document.querySelector(`[data-layout-resizer="${key}"]`);
  const container = config ? document.querySelector(config.container) : null;
  const target = config ? document.querySelector(config.target) : null;
  if (!config || !handle || !container || !target) return null;
  const axis = typeof config.axis === 'function' ? config.axis(container) : (config.axes[0] || 'x');
  const containerRect = container.getBoundingClientRect();
  const total = axis === 'x' ? containerRect.width : containerRect.height;
  const context = {
    key,
    config,
    handle,
    container,
    target,
    axis,
    total,
    variable: config.variable(axis),
    direction: config.direction || 1,
  };
  context.disabled = total <= 0 || Boolean(config.disabled?.(context));
  return context;
}

function _layoutBounds(context) {
  const raw = context.config.bounds?.(context) || { min: 0, max: context.total };
  const min = Number(raw.min) || 0;
  const max = Math.max(min, Number(raw.max) || min);
  return { min, max };
}

function _layoutSetFromPixels(context, pixels, persist = false) {
  if (!context || context.total <= 0) return;
  const { min, max } = _layoutBounds(context);
  const clamped = _layoutClamp(pixels, min, max);
  const isRatio = _layoutUnit(context.config, context.axis) === 'ratio';
  const value = isRatio ? clamped / context.total : clamped;
  const cssValue = isRatio
    ? `${(value * 100).toFixed(4)}%`
    : `${Math.round(value)}px`;
  document.documentElement.style.setProperty(context.variable, cssValue);
  if (persist) {
    try { localStorage.setItem(_layoutStorageKey(context.key, context.axis), String(value)); } catch (_) {}
  }
  _layoutUpdateHandle(context, clamped);
  if (context.key === 'knowledge-graph' && typeof _scheduleKnowledgeGraphLinksRender === 'function') {
    _scheduleKnowledgeGraphLinksRender();
  }
}

function _layoutApplyRawValue(key, axis, value) {
  const config = LAYOUT_RESIZER_CONFIGS[key];
  if (!config) return;
  const variable = config.variable(axis);
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return;
  const cssValue = _layoutUnit(config, axis) === 'ratio'
    ? `${(_layoutClamp(numeric, 0.05, 0.95) * 100).toFixed(4)}%`
    : `${Math.round(Math.max(0, numeric))}px`;
  document.documentElement.style.setProperty(variable, cssValue);
}

function _layoutApplyDefault(key, axis, persist = false) {
  const config = LAYOUT_RESIZER_CONFIGS[key];
  if (!config) return;
  const value = config.defaultValue(axis);
  _layoutApplyRawValue(key, axis, value);
  try {
    const storageKey = _layoutStorageKey(key, axis);
    if (persist) localStorage.removeItem(storageKey);
  } catch (_) {}
  window.requestAnimationFrame(refreshLayoutResizers);
}

function _layoutUpdateHandle(context, currentPixels = null) {
  if (!context) return;
  const { handle, axis } = context;
  handle.dataset.axis = axis;
  handle.setAttribute('aria-orientation', axis === 'x' ? 'vertical' : 'horizontal');
  handle.classList.toggle('is-disabled', context.disabled);
  handle.setAttribute('aria-disabled', String(context.disabled));
  handle.tabIndex = context.disabled ? -1 : 0;
  if (context.disabled) return;
  const { min, max } = _layoutBounds(context);
  const rect = context.target.getBoundingClientRect();
  const current = Number.isFinite(currentPixels)
    ? currentPixels
    : (axis === 'x' ? rect.width : rect.height);
  handle.setAttribute('aria-valuemin', String(Math.round(min)));
  handle.setAttribute('aria-valuemax', String(Math.round(max)));
  handle.setAttribute('aria-valuenow', String(Math.round(current)));
  handle.title = _ti('layout.resizeHint');
}

function refreshLayoutResizers() {
  document.querySelectorAll('[data-layout-resizer]').forEach((handle) => {
    _layoutUpdateHandle(_layoutResolveContext(handle.dataset.layoutResizer || ''));
  });
}

function _layoutStartResize(event) {
  if (event.button !== 0) return;
  const key = event.currentTarget?.dataset?.layoutResizer || '';
  const context = _layoutResolveContext(key);
  if (!context || context.disabled) return;
  const rect = context.target.getBoundingClientRect();
  _layoutResizeState = {
    context,
    pointerId: event.pointerId,
    startCoordinate: context.axis === 'x' ? event.clientX : event.clientY,
    startSize: context.axis === 'x' ? rect.width : rect.height,
  };
  event.currentTarget.setPointerCapture?.(event.pointerId);
  event.currentTarget.classList.add('is-active');
  document.body.classList.add('layout-resizing', `layout-resizing-${context.axis}`);
  event.preventDefault();
}

function _layoutMoveResize(event) {
  if (!_layoutResizeState || event.pointerId !== _layoutResizeState.pointerId) return;
  const { context, startCoordinate, startSize } = _layoutResizeState;
  const coordinate = context.axis === 'x' ? event.clientX : event.clientY;
  const next = startSize + ((coordinate - startCoordinate) * context.direction);
  if (_layoutResizeFrame) cancelAnimationFrame(_layoutResizeFrame);
  _layoutResizeFrame = requestAnimationFrame(() => {
    _layoutResizeFrame = 0;
    _layoutSetFromPixels(context, next, false);
  });
  event.preventDefault();
}

function _layoutStopResize(event) {
  if (!_layoutResizeState) return;
  if (event?.pointerId != null && event.pointerId !== _layoutResizeState.pointerId) return;
  const { context } = _layoutResizeState;
  const rect = context.target.getBoundingClientRect();
  const current = context.axis === 'x' ? rect.width : rect.height;
  _layoutSetFromPixels(context, current, true);
  context.handle.classList.remove('is-active');
  document.body.classList.remove('layout-resizing', 'layout-resizing-x', 'layout-resizing-y');
  _layoutResizeState = null;
}

function _layoutHandleKeydown(event) {
  const key = event.currentTarget?.dataset?.layoutResizer || '';
  const context = _layoutResolveContext(key);
  if (!context || context.disabled) return;
  const keys = context.axis === 'x' ? ['ArrowLeft', 'ArrowRight'] : ['ArrowUp', 'ArrowDown'];
  if (!keys.includes(event.key)) return;
  const step = event.shiftKey ? 24 : 8;
  const direction = (event.key === keys[0] ? -1 : 1) * context.direction;
  const rect = context.target.getBoundingClientRect();
  const current = context.axis === 'x' ? rect.width : rect.height;
  _layoutSetFromPixels(context, current + (direction * step), true);
  event.preventDefault();
}

function _layoutResetHandle(event) {
  const key = event.currentTarget?.dataset?.layoutResizer || '';
  const context = _layoutResolveContext(key);
  if (!context || context.disabled) return;
  _layoutApplyDefault(key, context.axis, true);
  event.preventDefault();
}

function initLayoutResizers() {
  Object.entries(LAYOUT_RESIZER_CONFIGS).forEach(([key, config]) => {
    config.axes.forEach((axis) => {
      let value = config.defaultValue(axis);
      try {
        const stored = Number(localStorage.getItem(_layoutStorageKey(key, axis)));
        if (Number.isFinite(stored) && stored > 0) value = stored;
      } catch (_) {}
      _layoutApplyRawValue(key, axis, value);
    });
  });
  document.querySelectorAll('[data-layout-resizer]').forEach((handle) => {
    if (handle.dataset.layoutBound === '1') return;
    handle.dataset.layoutBound = '1';
    handle.addEventListener('pointerdown', _layoutStartResize);
    handle.addEventListener('keydown', _layoutHandleKeydown);
    handle.addEventListener('dblclick', _layoutResetHandle);
  });
  window.addEventListener('pointermove', _layoutMoveResize, { passive: false });
  window.addEventListener('pointerup', _layoutStopResize);
  window.addEventListener('pointercancel', _layoutStopResize);
  try { localStorage.removeItem(LS_PREFIX + 'lib-table-height-px'); } catch (_) {}
  refreshLayoutResizers();
}

function resetLayoutSizes() {
  Object.entries(LAYOUT_RESIZER_CONFIGS).forEach(([key, config]) => {
    config.axes.forEach((axis) => _layoutApplyDefault(key, axis, true));
  });
  showToast(_tr('Disposition réinitialisée.'), 2600, 'success');
}

const LIBRARY_SHOW_ONLY_STORAGE_KEY = 'library-show-only';
const _libraryShowOnly = {
  itemTypes: new Set(),
  extensions: new Set(),
  collections: new Set(),
  themes: new Set(),
  includeSubcollections: true,
  analyses: '',
  metadata: '',
  ragExcluded: '',
  yearFrom: null,
  yearTo: null,
};

function _libraryItemTypeKey(row) {
  const raw = String(row?.[3] ?? '').trim();
  return raw && raw !== '-' ? raw : '';
}

function _libraryItemTypeLabel(rawType) {
  const type = String(rawType || '').trim();
  if (!type || type === '-') return _ti('library.itemType.unknown');
  const key = `library.itemType.${type}`;
  const label = _ti(key);
  if (label && label !== key) return label;
  // Unknown Zotero type: "encyclopediaEntry" -> "Encyclopedia entry".
  const spaced = type.replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function _libraryFileExtension(path) {
  const name = String(path || '').split(/[\\/]/).pop() || '';
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

// row[9] holds every author ("Doe, Jane; Roe, Ann"); row[1] only the lead author.
function _libraryRowAuthors(row) {
  const all = String(row?.[9] ?? '').trim();
  if (all && all !== '-') return all.split(';').map(name => name.trim()).filter(Boolean);
  const lead = String(row?.[1] ?? '').trim();
  return lead && lead !== '-' ? [lead] : [];
}

function _libraryAuthorsFull(row) {
  return _libraryRowAuthors(row).join('; ');
}

function _libraryAuthorsCompact(row, max = 2) {
  const authors = _libraryRowAuthors(row);
  if (authors.length <= max + 1) return authors.join('; ');
  return `${authors.slice(0, max).join('; ')} +${authors.length - max}`;
}

function _libraryRowHasAnalysis(row) {
  const value = String(row?.[7] ?? '').trim();
  return Boolean(value) && value !== '-';
}

function _libraryRowYear(row) {
  const match = String(row?.[2] ?? '').match(/\d{4}/);
  return match ? Number(match[0]) : null;
}

function _libraryHierarchyPath(value) {
  return String(value || '').trim().replace(/\s*\/\s*/g, ' / ').toLowerCase();
}

function _libraryHierarchyPrefixes(value) {
  const parts = String(value || '').split(/\s*\/\s*/).map(part => part.trim()).filter(Boolean);
  return parts.map((_, index) => parts.slice(0, index + 1).join(' / '));
}

function _libraryMetadataThemePath(metadata) {
  return Array.isArray(metadata?.theme_path) ? metadata.theme_path.filter(Boolean).map(String) : [];
}

function _libraryThemeLabel(metadata) {
  const path = _libraryMetadataThemePath(metadata);
  return path.length ? path[path.length - 1] : '';
}

function _libraryShowOnlyActiveCount() {
  const f = _libraryShowOnly;
  return (f.itemTypes.size ? 1 : 0)
    + (f.extensions.size ? 1 : 0)
    + (f.collections.size ? 1 : 0)
    + (f.themes.size ? 1 : 0)
    + (f.analyses ? 1 : 0)
    + (f.metadata ? 1 : 0)
    + (f.ragExcluded ? 1 : 0)
    + (f.yearFrom !== null || f.yearTo !== null ? 1 : 0);
}

function _libraryRowMatchesShowOnly(row, path, metadata = {}) {
  const f = _libraryShowOnly;
  if (f.itemTypes.size && !f.itemTypes.has(_libraryItemTypeKey(row))) return false;
  if (f.extensions.size && !f.extensions.has(_libraryFileExtension(path))) return false;
  if (f.collections.size) {
    const actual = Array.isArray(metadata.collections) ? metadata.collections : [];
    const matchesCollection = [...f.collections].some((selected) => {
      const wanted = _libraryHierarchyPath(selected);
      return actual.some((collection) => {
        const candidate = _libraryHierarchyPath(collection);
        return candidate === wanted || (f.includeSubcollections && candidate.startsWith(`${wanted} / `));
      });
    });
    if (!matchesCollection) return false;
  }
  if (f.themes.size) {
    const actualTheme = _libraryHierarchyPath(_libraryMetadataThemePath(metadata).join(' / '));
    if (![...f.themes].some((theme) => {
      const wanted = _libraryHierarchyPath(theme);
      return actualTheme === wanted || actualTheme.startsWith(`${wanted} / `);
    })) return false;
  }
  if (f.analyses && (f.analyses === 'with') !== _libraryRowHasAnalysis(row)) return false;
  // Documents without a Zotero match have no item type (see list_library).
  if (f.metadata && (f.metadata === 'with') !== Boolean(_libraryItemTypeKey(row))) return false;
  if (f.ragExcluded && (f.ragExcluded === 'excluded') !== Boolean(metadata.rag_excluded)) return false;
  if (f.yearFrom !== null || f.yearTo !== null) {
    const year = _libraryRowYear(row);
    if (year === null) return false;
    if (f.yearFrom !== null && year < f.yearFrom) return false;
    if (f.yearTo !== null && year > f.yearTo) return false;
  }
  return true;
}

function _saveLibraryShowOnly() {
  const f = _libraryShowOnly;
  try {
    localStorage.setItem(LS_PREFIX + LIBRARY_SHOW_ONLY_STORAGE_KEY, JSON.stringify({
      itemTypes: [...f.itemTypes],
      extensions: [...f.extensions],
      collections: [...f.collections],
      themes: [...f.themes],
      includeSubcollections: f.includeSubcollections,
      analyses: f.analyses,
      metadata: f.metadata,
      ragExcluded: f.ragExcluded,
      yearFrom: f.yearFrom,
      yearTo: f.yearTo,
    }));
  } catch (_) {}
}

function _loadLibraryShowOnly() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(LS_PREFIX + LIBRARY_SHOW_ONLY_STORAGE_KEY) || 'null');
  } catch (_) {}
  if (!saved || typeof saved !== 'object') return;
  const f = _libraryShowOnly;
  const toYear = (value) => (Number.isInteger(value) ? value : null);
  f.itemTypes = new Set(Array.isArray(saved.itemTypes) ? saved.itemTypes.map(String) : []);
  f.extensions = new Set(Array.isArray(saved.extensions) ? saved.extensions.map(String) : []);
  f.collections = new Set(Array.isArray(saved.collections) ? saved.collections.map(String) : []);
  f.themes = new Set(Array.isArray(saved.themes) ? saved.themes.map(String) : []);
  f.includeSubcollections = saved.includeSubcollections !== false;
  f.analyses = ['with', 'without'].includes(saved.analyses) ? saved.analyses : '';
  f.metadata = ['with', 'without'].includes(saved.metadata) ? saved.metadata : '';
  f.ragExcluded = ['excluded', 'included'].includes(saved.ragExcluded) ? saved.ragExcluded : '';
  f.yearFrom = toYear(saved.yearFrom);
  f.yearTo = toYear(saved.yearTo);
}

function _renderLibraryShowOnlyOptions() {
  const rows = Array.isArray(state.libRows) ? state.libRows : [];
  const paths = Array.isArray(state.libPaths) ? state.libPaths : [];
  const metadata = Array.isArray(state.libMetadata) ? state.libMetadata : [];
  const f = _libraryShowOnly;

  const typeCounts = new Map();
  const extensionCounts = new Map();
  const collectionDocuments = new Map();
  const themeDocuments = new Map();
  rows.forEach((row, i) => {
    const type = _libraryItemTypeKey(row);
    typeCounts.set(type, (typeCounts.get(type) || 0) + 1);
    const extension = _libraryFileExtension(paths[i]);
    if (extension) extensionCounts.set(extension, (extensionCounts.get(extension) || 0) + 1);
    const itemMetadata = metadata[i] || {};
    const collectionPrefixes = new Set(
      (Array.isArray(itemMetadata.collections) ? itemMetadata.collections : [])
        .flatMap(_libraryHierarchyPrefixes),
    );
    collectionPrefixes.forEach((collection) => {
      collectionDocuments.set(collection, (collectionDocuments.get(collection) || 0) + 1);
    });
    const themePrefixes = _libraryHierarchyPrefixes(_libraryMetadataThemePath(itemMetadata).join(' / '));
    themePrefixes.forEach((theme) => {
      themeDocuments.set(theme, (themeDocuments.get(theme) || 0) + 1);
    });
  });
  // Keep a selected value visible even if the reloaded library no longer contains it.
  f.itemTypes.forEach((type) => { if (!typeCounts.has(type)) typeCounts.set(type, 0); });
  f.extensions.forEach((ext) => { if (!extensionCounts.has(ext)) extensionCounts.set(ext, 0); });
  f.collections.forEach((path) => { if (!collectionDocuments.has(path)) collectionDocuments.set(path, 0); });
  f.themes.forEach((path) => { if (!themeDocuments.has(path)) themeDocuments.set(path, 0); });

  const renderOptions = (entries, group, labelFor, selected) => {
    if (!entries.length) {
      return `<span class="lib-show-only-empty">${escapeHtml(_ti('library.showOnlyEmpty'))}</span>`;
    }
    return entries.map(([value, count]) => {
      const optionLabel = labelFor(value);
      return `
        <label>
          <input type="checkbox" data-show-only-group="${group}" value="${escapeAttr(value)}" aria-label="${escapeAttr(_ti('library.filterBy', { label: optionLabel }))}"${selected.has(value) ? ' checked' : ''}>
          <span>${escapeHtml(optionLabel)}</span>
          <span class="lib-show-only-option-count">${count}</span>
        </label>`;
    }).join('');
  };

  const typeEntries = [...typeCounts.entries()].sort((a, b) => (
    Number(a[0] === '') - Number(b[0] === '')
    || _libraryItemTypeLabel(a[0]).localeCompare(_libraryItemTypeLabel(b[0]))
  ));
  const extensionEntries = [...extensionCounts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  const hierarchySort = (a, b) => a[0].localeCompare(b[0]);
  const collectionEntries = [...collectionDocuments.entries()].sort(hierarchySort);
  const themeEntries = [...themeDocuments.entries()].sort(hierarchySort);
  const renderHierarchyOptions = (entries, group, selected) => {
    if (!entries.length) return `<span class="lib-show-only-empty">${escapeHtml(_ti('library.showOnlyEmpty'))}</span>`;
    return entries.map(([value, count]) => {
      const parts = value.split(' / ');
      const label = parts[parts.length - 1];
      return `
        <label class="lib-show-only-hierarchy-option" style="--filter-depth:${parts.length - 1}" title="${escapeAttr(value)}">
          <input type="checkbox" data-show-only-group="${group}" value="${escapeAttr(value)}"${selected.has(value) ? ' checked' : ''}>
          <span>${escapeHtml(label)}</span>
          <span class="lib-show-only-option-count">${count}</span>
        </label>`;
    }).join('');
  };

  const typesRoot = document.getElementById('lib-show-only-item-types');
  if (typesRoot) typesRoot.innerHTML = renderOptions(typeEntries, 'itemTypes', _libraryItemTypeLabel, f.itemTypes);
  const extensionsRoot = document.getElementById('lib-show-only-extensions');
  if (extensionsRoot) {
    extensionsRoot.innerHTML = renderOptions(extensionEntries, 'extensions', (ext) => ext.toUpperCase(), f.extensions);
  }
  const collectionsRoot = document.getElementById('lib-show-only-collections');
  if (collectionsRoot) collectionsRoot.innerHTML = renderHierarchyOptions(collectionEntries, 'collections', f.collections);
  const themesRoot = document.getElementById('lib-show-only-themes');
  if (themesRoot) themesRoot.innerHTML = renderHierarchyOptions(themeEntries, 'themes', f.themes);
  const descendants = document.getElementById('lib-show-only-subcollections');
  if (descendants) descendants.checked = f.includeSubcollections;

  document.querySelectorAll('input[name="lib-show-only-analyses"]').forEach((input) => {
    input.checked = input.value === f.analyses;
  });
  document.querySelectorAll('input[name="lib-show-only-metadata"]').forEach((input) => {
    input.checked = input.value === f.metadata;
  });
  document.querySelectorAll('input[name="lib-show-only-rag"]').forEach((input) => {
    input.checked = input.value === f.ragExcluded;
  });
  const yearFrom = document.getElementById('lib-show-only-year-from');
  if (yearFrom && document.activeElement !== yearFrom) yearFrom.value = f.yearFrom ?? '';
  const yearTo = document.getElementById('lib-show-only-year-to');
  if (yearTo && document.activeElement !== yearTo) yearTo.value = f.yearTo ?? '';
  _updateLibraryShowOnlyBadge();
}

function _updateLibraryShowOnlyBadge() {
  const count = _libraryShowOnlyActiveCount();
  document.getElementById('lib-show-only')?.classList.toggle('is-active', count > 0);
  const badge = document.getElementById('lib-show-only-count');
  if (!badge) return;
  badge.hidden = count === 0;
  badge.textContent = count ? String(count) : '';
  badge.title = count ? _ti('library.showOnlyActive', { count }) : '';
  _renderLibraryFilterChips();
}

function onLibraryShowOnlyChange(event) {
  const target = event?.target;
  if (!(target instanceof HTMLInputElement)) return;
  const f = _libraryShowOnly;
  if (target.type === 'number') {
    const raw = target.value.trim();
    const year = /^\d{1,4}$/.test(raw) ? Number(raw) : null;
    if (target.id === 'lib-show-only-year-from') f.yearFrom = year;
    else if (target.id === 'lib-show-only-year-to') f.yearTo = year;
    else return;
  } else if (event.type !== 'change') {
    return;
  } else if (target.type === 'checkbox') {
    if (target.id === 'lib-show-only-subcollections') {
      f.includeSubcollections = target.checked;
      _saveLibraryShowOnly();
      applyLibraryView();
      return;
    }
    const bucket = f[target.dataset.showOnlyGroup];
    if (!(bucket instanceof Set)) return;
    if (target.checked) bucket.add(target.value);
    else bucket.delete(target.value);
  } else if (target.type === 'radio') {
    if (target.name === 'lib-show-only-analyses') f.analyses = target.value;
    else if (target.name === 'lib-show-only-metadata') f.metadata = target.value;
    else if (target.name === 'lib-show-only-rag') f.ragExcluded = target.value;
    else return;
  } else {
    return;
  }
  _saveLibraryShowOnly();
  _updateLibraryShowOnlyBadge();
  applyLibraryView();
}

function resetLibraryShowOnly() {
  const f = _libraryShowOnly;
  f.itemTypes.clear();
  f.extensions.clear();
  f.collections.clear();
  f.themes.clear();
  f.includeSubcollections = true;
  f.analyses = '';
  f.metadata = '';
  f.ragExcluded = '';
  f.yearFrom = null;
  f.yearTo = null;
  _saveLibraryShowOnly();
  const yearFrom = document.getElementById('lib-show-only-year-from');
  if (yearFrom) yearFrom.value = '';
  const yearTo = document.getElementById('lib-show-only-year-to');
  if (yearTo) yearTo.value = '';
  _renderLibraryShowOnlyOptions();
  applyLibraryView();
}

// Returns an ascending comparison; `missingLast` values stay at the end in both directions.
function _compareLibraryRows(a, b, col) {
  let va = col === 'theme' ? _libraryThemeLabel(a.metadata) : (a.row[col] ?? '');
  let vb = col === 'theme' ? _libraryThemeLabel(b.metadata) : (b.row[col] ?? '');
  let aMissing = false;
  let bMissing = false;
  if (col === 2) { va = parseInt(va)||0; vb = parseInt(vb)||0; }
  else if (col === 3) {
    aMissing = !_libraryItemTypeKey(a.row);
    bMissing = !_libraryItemTypeKey(b.row);
    va = _libraryItemTypeLabel(va).toLowerCase();
    vb = _libraryItemTypeLabel(vb).toLowerCase();
  }
  else if (col === 6) { va = parseFloat(va)||0; vb = parseFloat(vb)||0; }
  else if (col === 8) {
    va = Date.parse(va);
    vb = Date.parse(vb);
    aMissing = Number.isNaN(va);
    bMissing = Number.isNaN(vb);
  }
  else {
    va = va.toString().toLowerCase(); vb = vb.toString().toLowerCase();
    if (col === 'theme') { aMissing = !va; bMissing = !vb; }
  }
  if (aMissing || bMissing) return { missing: aMissing === bMissing ? 0 : (aMissing ? 1 : -1), order: 0 };
  return { missing: 0, order: va < vb ? -1 : va > vb ? 1 : 0 };
}

function applyLibraryView() {
  if (!state.libRows) return;
  const q = (document.getElementById('lib-search')?.value || '').trim().toLowerCase();
  let entries = state.libPaths.map((path, i) => ({
    row: state.libRows[i],
    path,
    metadata: state.libMetadata?.[i] || {},
  }));
  if (q) {
    entries = entries.filter(({ row, metadata }) => (
      row.some(cell => String(cell).toLowerCase().includes(q))
      || _libraryItemTypeLabel(row[3]).toLowerCase().includes(q)
      || _libraryMetadataThemePath(metadata).join(' / ').toLowerCase().includes(q)
      || (metadata.collections || []).some(value => String(value).toLowerCase().includes(q))
    ));
  }
  if (_libraryShowOnlyActiveCount()) {
    entries = entries.filter(({ row, path, metadata }) => _libraryRowMatchesShowOnly(row, path, metadata));
  }
  if (_sort.col !== -1) {
    const col = _sort.col;
    const direction = _sort.asc ? 1 : -1;
    entries.sort((a, b) => {
      const cmp = _compareLibraryRows(a, b, col);
      return cmp.missing || direction * cmp.order;
    });
  }
  _renderLibraryRows(
    entries.map(e => e.row),
    entries.map(e => e.path),
    entries.map(e => e.metadata),
  );
}

function filterLibrary(_query) {
  applyLibraryView();
}

let _libraryFilterTimer = null;
function queueLibraryFilter(query) {
  window.clearTimeout(_libraryFilterTimer);
  _libraryFilterTimer = window.setTimeout(() => filterLibrary(query), 120);
}

// The exports live in the "⋯" menu next to "Update RAG": occasional actions
// that should not take a row of the toolbar.
function _libraryMoreMenuNodes() {
  return {
    toggle: document.getElementById('lib-more-toggle'),
    popover: document.getElementById('lib-more-popover'),
  };
}

function closeLibraryMoreMenu({ focusToggle = false } = {}) {
  const { toggle, popover } = _libraryMoreMenuNodes();
  if (!popover || popover.hidden) return;
  popover.hidden = true;
  toggle?.setAttribute('aria-expanded', 'false');
  if (focusToggle) toggle?.focus();
}

function toggleLibraryMoreMenu(event) {
  event?.preventDefault?.();
  event?.stopPropagation?.();
  const { toggle, popover } = _libraryMoreMenuNodes();
  if (!toggle || !popover) return;
  if (!popover.hidden) {
    closeLibraryMoreMenu();
    return;
  }
  popover.hidden = false;
  toggle.setAttribute('aria-expanded', 'true');
  popover.querySelector('[role="menuitem"]:not(:disabled)')?.focus();
}

document.addEventListener('click', (event) => {
  if (!event.target.closest?.('#lib-more-menu')) closeLibraryMoreMenu();
});

// The <details> menus (columns, article actions, selection actions) close on an
// outside click, on Escape, and after a menu item is used.
document.addEventListener('click', (event) => {
  document.querySelectorAll('.lib-columns[open], .lib-detail-more[open], .lib-selection-more[open]').forEach((menu) => {
    const insideItem = event.target.closest?.('.lib-detail-menu-item, .lib-selection-menu-item');
    if (!event.composedPath().includes(menu) || (insideItem && menu.contains(insideItem))) menu.removeAttribute('open');
  });
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  document.querySelectorAll('.lib-columns[open], .lib-detail-more[open], .lib-selection-more[open]').forEach((menu) => {
    menu.removeAttribute('open');
    menu.querySelector('summary')?.focus();
  });
});

document.addEventListener('keydown', (event) => {
  const { popover } = _libraryMoreMenuNodes();
  if (!popover || popover.hidden || !popover.contains(document.activeElement)) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeLibraryMoreMenu({ focusToggle: true });
    return;
  }
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
  event.preventDefault();
  const items = [...popover.querySelectorAll('[role="menuitem"]:not(:disabled)')];
  const step = event.key === 'ArrowDown' ? 1 : -1;
  const next = (items.indexOf(document.activeElement) + step + items.length) % items.length;
  items[next]?.focus();
});

async function exportLibrary(exportFormat) {
  closeLibraryMoreMenu();
  const paths = Array.isArray(state.visibleLibPaths) ? state.visibleLibPaths : [];
  if (!paths.length) {
    showToast(_tr('Aucun article à exporter.'), 4200, 'error');
    return;
  }

  const toggle = document.getElementById('lib-more-toggle');
  if (toggle) {
    toggle.disabled = true;
    toggle.setAttribute('aria-busy', 'true');
  }

  try {
    const response = await fetch('/api/library/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ export_format: exportFormat, paths }),
    });
    if (!response.ok) {
      let message = '';
      try {
        const payload = await response.json();
        message = payload?.detail || payload?.error || payload?.msg || '';
      } catch (_error) {}
      throw new Error(message || _tr("L'export de la bibliothèque a échoué."));
    }

    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
    const filename = filenameMatch?.[1]
      || (exportFormat === 'bibtex' ? 'bibliobrain-articles.bib' : 'bibliobrain-articles.md');
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast(_tr('Export téléchargé.'), 3600, 'success');
  } catch (error) {
    showToast(error?.message || _tr("L'export de la bibliothèque a échoué."), 5200, 'error');
  } finally {
    if (toggle) {
      toggle.disabled = false;
      toggle.removeAttribute('aria-busy');
    }
  }
}

function switchLibraryDetailTab(name, btn = null) {
  const target = String(name || 'overview').trim().toLowerCase();
  if (target !== 'analysis') toggleAnalysisExpanded(false);
  document.querySelectorAll('.library-detail-tab').forEach((tabBtn) => {
    const key = String(tabBtn.dataset.libTab || '').toLowerCase();
    tabBtn.classList.toggle('active', key === target);
  });
  document.querySelectorAll('.library-detail-pane').forEach((pane) => {
    const key = pane.id.replace('library-pane-', '').toLowerCase();
    pane.classList.toggle('active', key === target);
  });
  if (btn) btn.blur();
}

let analysisSectionPlaceholder = null;

function toggleAnalysisExpanded(force = null) {
  const section = document.getElementById('analysis-section');
  const button = document.getElementById('analysis-expand-btn');
  if (!section || !button) return;

  const expanded = force === null
    ? !section.classList.contains('is-expanded')
    : Boolean(force);
  const labelKey = expanded ? 'library.collapseSummary' : 'library.expandSummary';
  const titleKey = expanded ? 'library.collapseSummaryTitle' : 'library.expandSummaryTitle';
  const label = button.querySelector('span');

  if (expanded && !analysisSectionPlaceholder?.isConnected && section.parentNode) {
    analysisSectionPlaceholder = document.createComment('analysis-section-placeholder');
    section.parentNode.insertBefore(analysisSectionPlaceholder, section);
    document.body.appendChild(section);
  }

  section.classList.toggle('is-expanded', expanded);
  document.body.classList.toggle('analysis-summary-expanded', expanded);
  button.setAttribute('aria-expanded', String(expanded));
  button.setAttribute('data-i18n-title', titleKey);
  button.setAttribute('title', _ti(titleKey));
  if (label) {
    label.setAttribute('data-i18n', labelKey);
    label.textContent = _ti(labelKey);
  }

  if (!expanded) {
    if (analysisSectionPlaceholder?.parentNode) {
      analysisSectionPlaceholder.parentNode.insertBefore(section, analysisSectionPlaceholder);
      analysisSectionPlaceholder.remove();
    }
    analysisSectionPlaceholder = null;
  }
}

function _setLibraryDetailEmptyState() {
  const root = document.getElementById('library-detail');
  const title = document.getElementById('library-detail-title');
  const subtitle = document.getElementById('library-detail-subtitle');
  const facts = document.getElementById('library-facts');
  const meta = document.getElementById('meta-content');
  const note = document.getElementById('analysis-article-note');
  const result = document.getElementById('analysis-result');
  const openPdfBtn = document.getElementById('library-open-pdf-btn');
  const zoteroBtn = document.getElementById('library-open-zotero-btn');

  root?.classList.add('empty');
  if (title) title.textContent = '-';
  if (subtitle) subtitle.textContent = 'Cliquez sur un article pour voir son contenu.';
  if (facts) facts.innerHTML = '';
  if (meta) meta.innerHTML = '<span style="color:var(--muted)">' + _tr('Les métadonnées apparaîtront ici.') + '</span>';
  if (note) note.textContent = 'Aucun article sélectionné.';
  if (result) result.innerHTML = '<span style="color:var(--muted);font-style:italic">' + _tr('Sélectionnez un article pour lancer une analyse.') + '</span>';
  _setActionLinkState(openPdfBtn, false);
  _setActionLinkState(zoteroBtn, false);
  for (const id of ['library-citation-graph-btn', 'library-citation-graph-card']) {
    const cgBtn = document.getElementById(id);
    if (cgBtn) cgBtn.disabled = true;
  }
  const readHereBtn = document.getElementById('library-read-here-btn');
  if (readHereBtn) readHereBtn.disabled = true;
  const askBtn = document.getElementById('library-ask-article-btn');
  if (askBtn) askBtn.disabled = true;
  switchLibraryDetailTab('overview');
  _renderCitationPanel(null);
  _updateAnalysisNotebookButton();
}

function _renderLibraryFacts(row) {
  const facts = document.getElementById('library-facts');
  if (!facts) return;
  const entries = [
    [_ti('library.factAuthor'), _libraryAuthorsFull(row) || '-'],
    [_ti('library.factYear'), row?.[2] || '-'],
    [_ti('library.factType'), _libraryItemTypeLabel(row?.[3])],
    [_ti('library.factTags'), row?.[4] || '-'],
    [_ti('library.factCollections'), row?.[5] || '-'],
    [_ti('library.factSize'), row?.[6] || '-'],
    [_ti('library.factCachedAnalyses'), row?.[7] || '-'],
    [_ti('library.colDateAdded'), row?.[8] || '-'],
  ];
  facts.innerHTML = entries.map(([label, value]) => `
    <div class="library-fact">
      <span class="library-fact-label">${escapeHtml(label)}</span>
      <div class="library-fact-value">${escapeHtml(value || '-')}</div>
    </div>
  `).join('');
}

function _renderLibraryDetailHeader(index) {
  const root = document.getElementById('library-detail');
  const row = state.libRows[index] || [];
  const relPath = state.libPaths[index] || '';
  const title = document.getElementById('library-detail-title');
  const subtitle = document.getElementById('library-detail-subtitle');
  const note = document.getElementById('analysis-article-note');
  const openPdfBtn = document.getElementById('library-open-pdf-btn');

  root?.classList.remove('empty');
  if (title) title.textContent = row[0] || relPath || 'Article sans titre';
  if (subtitle) {
    const parts = [];
    const authors = _libraryAuthorsCompact(row, 3);
    if (authors) parts.push(authors);
    if (row[2] && row[2] !== '-') parts.push(row[2]);
    if (row[3] && row[3] !== '-') parts.push(row[3]);
    subtitle.textContent = parts.length ? parts.join(' • ') : relPath;
  }
  if (note) {
    note.textContent = relPath
      ? `Article sélectionné : ${row[0] || relPath}`
      : 'Aucun article sélectionné.';
  }
  _setActionLinkState(openPdfBtn, Boolean(relPath), '/api/open-pdf?path=' + encodeURIComponent(relPath));
  for (const id of ['library-citation-graph-btn', 'library-citation-graph-card']) {
    const cgBtn = document.getElementById(id);
    if (cgBtn) cgBtn.disabled = !relPath;
  }
  const readHereBtn = document.getElementById('library-read-here-btn');
  if (readHereBtn) readHereBtn.disabled = !relPath;
  const askBtn = document.getElementById('library-ask-article-btn');
  if (askBtn) askBtn.disabled = !relPath;
  _renderLibraryFacts(row);
}

function _libraryRowFallbackMarkdown(row, relPath) {
  const title = String(row?.[0] || relPath || 'Article sans titre').trim();
  const entries = [
    [_ti('library.factAuthor'), _libraryAuthorsFull(row)],
    [_ti('library.factYear'), row?.[2] || ''],
    [_ti('library.factType'), row?.[3] || ''],
    [_ti('library.factTags'), row?.[4] || ''],
    [_ti('library.factCollections'), row?.[5] || ''],
    [_ti('library.factSize'), row?.[6] || ''],
    [_ti('library.factCachedAnalyses'), row?.[7] || ''],
    [_ti('library.colDateAdded'), row?.[8] || ''],
  ].filter(([, value]) => String(value || '').trim() && String(value || '').trim() !== '-');
  const body = entries
    .map(([label, value]) => `**${label}:** ${String(value).trim()}`)
    .join('\n\n');
  return [`## ${title}`, body].filter(Boolean).join('\n\n');
}

function _cachedAnalysisTypesForRow(row) {
  const value = String(row?.[7] || '').trim();
  if (!value || value === '-') return [];
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

const VISUAL_ANALYSIS_VIEW_STORAGE_KEY = 'iassistant_visual-analysis-view-paths';

function _visualAnalysisViewPaths() {
  try {
    const paths = JSON.parse(localStorage.getItem(VISUAL_ANALYSIS_VIEW_STORAGE_KEY) || '[]');
    return new Set(Array.isArray(paths) ? paths.map((path) => String(path || '').trim()).filter(Boolean) : []);
  } catch (_error) {
    return new Set();
  }
}

function _rememberArticleAnalysisView(relPath, view) {
  const key = String(relPath || '').trim();
  if (!key) return;
  try {
    const paths = _visualAnalysisViewPaths();
    if (view === 'visual') paths.add(key);
    else paths.delete(key);
    localStorage.setItem(VISUAL_ANALYSIS_VIEW_STORAGE_KEY, JSON.stringify(Array.from(paths).slice(-250)));
  } catch (_error) {
    // In-memory restoration still works when local storage is unavailable.
  }
}

function _ensureArticleAnalysisState(relPath) {
  const key = String(relPath || '').trim();
  if (!key) return { byType: {}, fetched: {}, lastType: '', visualText: '', visualFetched: false, lastView: 'analysis' };
  if (!state.analysisByArticle[key]) {
    state.analysisByArticle[key] = {
      byType: {},
      fetched: {},
      lastType: '',
      visualText: '',
      visualFetched: false,
      lastView: _visualAnalysisViewPaths().has(key) ? 'visual' : 'analysis',
    };
  }
  return state.analysisByArticle[key];
}

function _setAnalysisResultHtml(html) {
  const result = document.getElementById('analysis-result');
  if (result) result.innerHTML = html;
}

async function analyzeSelectedPaperVisuals() {
  const relPath = _currentSelectedArticlePath();
  const mode = String(state.appSettings?.visual_analysis_mode || 'off');
  const model = String(
    document.getElementById('vision-model-select')?.value
      || state.appSettings?.vision_model
      || ''
  ).trim();
  const analysisModel = String(
    document.getElementById('visual-description-model-select')?.value
      || state.appSettings?.visual_description_model
      || ''
  ).trim();
  if (!relPath) {
    showToast(_tr('Sélectionnez un article d’abord.'), 3000, 'error');
    return;
  }
  if (mode === 'off') {
    showToast(_tr('Activez l’analyse visuelle dans les paramètres.'), 4200, 'error');
    return;
  }
  if (!model && !analysisModel) {
    showToast(_tr('Sélectionnez un modèle vision dans les paramètres.'), 4200, 'error');
    return;
  }
  const bucket = _ensureArticleAnalysisState(relPath);
  const previousView = bucket.lastView;
  bucket.lastView = 'visual';
  _rememberArticleAnalysisView(relPath, 'visual');
  const requestKey = `visual::${relPath}::${Date.now()}`;
  state.analysisViewRequestKey = requestKey;
  const button = document.getElementById('visual-analysis-btn');
  if (button) {
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span> Lecture…';
  }
  _setAnalysisResultHtml(
    '<span style="color:var(--muted);font-style:italic"><span class="spinner"></span> ' +
    escapeHtml(_tr('Détection et lecture des figures…')) + '</span>'
  );
  try {
    const response = await fetch('/api/visual-analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: relPath, model, analysis_model: analysisModel }),
    });
    const payload = await response.json();
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.error || `HTTP ${response.status}`);
    }
    bucket.visualText = String(payload.markdown || '');
    bucket.visualFetched = true;
    if (state.analysisViewRequestKey !== requestKey || _currentSelectedArticlePath() !== relPath) return;
    _renderStoredVisualAnalysis(bucket.visualText);
    showToast(
      payload.cached
        ? _tr('Analyse visuelle chargée depuis le cache.')
        : payload.partial
          ? _tr('Analyse visuelle partielle : un des deux passages a échoué.')
        : _tr(`${payload.results?.length || 0} page(s) avec figures analysée(s).`),
      3800,
      payload.partial ? 'info' : 'success'
    );
  } catch (error) {
    bucket.lastView = previousView === 'visual' && bucket.visualText ? 'visual' : 'analysis';
    _rememberArticleAnalysisView(relPath, bucket.lastView);
    const message = String(error?.message || error || 'Erreur inconnue');
    const friendly = message === 'pdf_renderer_missing'
      ? _tr('Le composant de rendu PDF optionnel est manquant. Réinstallez les dépendances de Bibliobrain.')
      : message === 'visual_analysis_disabled'
        ? _tr('L’analyse visuelle est désactivée dans les paramètres.')
        : message.startsWith('vision_model_blind:')
          ? _tr(`Le modèle ${message.slice('vision_model_blind:'.length)} ne perçoit pas les images avec cette version d’Ollama. Choisissez un autre modèle vision dans les paramètres.`)
          : _tr(`Analyse visuelle impossible : ${message}`);
    if (state.analysisViewRequestKey === requestKey && _currentSelectedArticlePath() === relPath) {
      _renderEmptyAnalysisMessage(friendly);
      showToast(friendly, 5200, 'error');
    }
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = _tr('Lire les figures');
    }
    if (typeof updateVisualAnalysisUi === 'function') updateVisualAnalysisUi();
  }
}

window.analyzeSelectedPaperVisuals = analyzeSelectedPaperVisuals;

function _setActionLinkState(link, enabled, href = '#') {
  if (!link) return;
  link.setAttribute('href', enabled ? href : '#');
  link.setAttribute('aria-disabled', enabled ? 'false' : 'true');
  link.tabIndex = enabled ? 0 : -1;
  link.classList.toggle('disabled', !enabled);
}

function _selectedArticlePathFromState() {
  if (Number.isInteger(state.selectedRow) && state.selectedRow >= 0) {
    return state.libPaths[state.selectedRow] || '';
  }
  return '';
}

function _currentSelectedArticlePath() {
  return _selectedArticlePathFromState()
    || document.getElementById('article-select')?.value
    || state.currentSummaryPath
    || '';
}

async function openSelectedArticleChat() {
  const relPath = _currentSelectedArticlePath();
  if (!relPath) {
    showToast(_tr('Sélectionnez un article d\'abord.'), 2800);
    return false;
  }
  if (state.streaming) await stopCurrentChat(true, 'context_switch');

  const row = state.libRows[state.selectedRow] || [];
  const title = String(row[0] || document.getElementById('library-detail-title')?.textContent || relPath).trim();
  startNewChat();
  setChatArticleScope(relPath, title);
  switchTab('chat');
  window.setTimeout(() => document.getElementById('msg-input')?.focus(), 80);
  return false;
}

function _syncArticleSelectValue(relPath, label = '') {
  const sel = document.getElementById('article-select');
  if (!sel) return;
  const value = String(relPath || '');
  if (!value) {
    sel.value = '';
    return;
  }
  const hasOption = Array.from(sel.options).some((opt) => opt.value === value);
  if (!hasOption) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label || value;
    sel.appendChild(opt);
  }
  sel.value = value;
}

function _updateAnalysisNotebookButton() {
  const btn = document.getElementById('analysis-copy-notebook-btn');
  if (!btn) return;
  const relPath = _currentSelectedArticlePath();
  const analysisType = document.getElementById('analysis-type-select')?.value || '';
  const bucket = relPath ? _ensureArticleAnalysisState(relPath) : null;
  const rawText = bucket?.lastView === 'visual'
    ? String(bucket.visualText || '').trim()
    : bucket && analysisType
      ? String(bucket.byType?.[analysisType] || '').trim()
      : '';
  const text = stripThinkingMarkup(rawText);
  btn.disabled = !text;
}

function _renderStoredAnalysis(text) {
  const result = document.getElementById('analysis-result');
  if (!result) return;
  renderRichAssistantOutput(result, text, { normalizeCitations: false });
  _updateAnalysisNotebookButton();
}

function _renderStoredVisualAnalysis(text) {
  _setAnalysisResultHtml(parseMarkdown(String(text || '')));
  _updateAnalysisNotebookButton();
}

function _renderEmptyAnalysisMessage(message) {
  _setAnalysisResultHtml(`<span style="color:var(--muted);font-style:italic">${escapeHtml(message)}</span>`);
  _updateAnalysisNotebookButton();
}

async function refreshAnalysisForSelection() {
  const relPath = _currentSelectedArticlePath();
  const analysisType = document.getElementById('analysis-type-select')?.value || '';
  const note = document.getElementById('analysis-article-note');
  const title = document.getElementById('library-detail-title')?.textContent?.trim() || relPath;

  if (note) {
    note.textContent = relPath
      ? `Article sélectionné : ${title || relPath}`
      : 'Aucun article sélectionné.';
  }

  if (!relPath) {
    _renderEmptyAnalysisMessage("Sélectionnez un article pour lancer une analyse.");
    return;
  }
  const bucket = _ensureArticleAnalysisState(relPath);
  if (bucket.lastView === 'visual') {
    const existingVisual = String(bucket.visualText || '').trim();
    if (existingVisual) {
      _renderStoredVisualAnalysis(existingVisual);
      return;
    }
    if (!bucket.visualFetched) {
      const visualRequestKey = `visual-cache::${relPath}::${Date.now()}`;
      state.analysisViewRequestKey = visualRequestKey;
      _setAnalysisResultHtml('<span style="color:var(--muted);font-style:italic"><span class="spinner"></span> Restauration de la lecture des figures…</span>');
      _updateAnalysisNotebookButton();
      try {
        const url = `/api/visual-analysis-cached?path=${encodeURIComponent(relPath)}`;
        const data = await fetchJsonOrThrow(url, {}, 'Chargement de la lecture des figures impossible');
        if (state.analysisViewRequestKey !== visualRequestKey || _currentSelectedArticlePath() !== relPath) return;
        bucket.visualFetched = true;
        if (data?.found && data?.markdown) {
          bucket.visualText = String(data.markdown);
          _renderStoredVisualAnalysis(bucket.visualText);
          return;
        }
        bucket.lastView = 'analysis';
        _rememberArticleAnalysisView(relPath, 'analysis');
      } catch (error) {
        if (state.analysisViewRequestKey !== visualRequestKey || _currentSelectedArticlePath() !== relPath) return;
        bucket.lastView = 'analysis';
        _rememberArticleAnalysisView(relPath, 'analysis');
        console.warn('Visual analysis cache restoration failed:', error);
      }
    }
  }
  if (!analysisType) {
    _renderEmptyAnalysisMessage("Choisissez un type d'analyse.");
    return;
  }

  bucket.lastView = 'analysis';
  bucket.lastType = analysisType;
  const existing = String(bucket.byType?.[analysisType] || '').trim();
  if (existing) {
    _renderStoredAnalysis(existing);
    return;
  }
  if (bucket.fetched?.[analysisType]) {
    _renderEmptyAnalysisMessage("Aucune analyse enregistrée pour cet article avec ce type.");
    return;
  }

  const requestKey = `${relPath}::${analysisType}::${Date.now()}`;
  state.analysisViewRequestKey = requestKey;
  _setAnalysisResultHtml('<span style="color:var(--muted);font-style:italic"><span class="spinner"></span> Vérification des analyses déjà enregistrées…</span>');
  _updateAnalysisNotebookButton();

  try {
    const url = `/api/analysis-cached?path=${encodeURIComponent(relPath)}&analysis_type=${encodeURIComponent(analysisType)}`;
    const data = await fetchJsonOrThrow(url, {}, "Chargement de l'analyse enregistrée impossible");
    if (state.analysisViewRequestKey !== requestKey) return;
    bucket.fetched[analysisType] = true;
    if (data?.found && data?.text) {
      bucket.byType[analysisType] = data.text;
      _renderStoredAnalysis(data.text);
      return;
    }
    _renderEmptyAnalysisMessage("Aucune analyse enregistrée pour cet article avec ce type.");
  } catch (e) {
    if (state.analysisViewRequestKey !== requestKey) return;
    _renderEmptyAnalysisMessage(e?.message || "Impossible de charger l'analyse enregistrée.");
  }
}

function populateLibrary(rows, paths, metadata = []) {
  const previousSelection = _currentSelectedArticlePath();
  state.libRows = rows;
  state.libPaths = paths;
  state.libMetadata = paths.map((_, index) => metadata[index] || {});
  state.libPathIndex = new Map(paths.map((path, index) => [path, index]));
  _pruneLibrarySelection();
  state.knowledgeGraphDirty = true;
  _sort.col = -1;
  _renderLibraryColumnsMenu();
  _renderLibraryShowOnlyOptions();
  applyLibraryView();
  const sel = document.getElementById('article-select');
  sel.innerHTML = '<option value="">' + _tr('- Sélectionnez un article -') + '</option>' +
    paths.map((p, i) => `<option value="${escapeAttr(p)}">${escapeHtml(rows[i] ? rows[i][0] : p)}</option>`).join('');
  if (previousSelection && paths.includes(previousSelection)) {
    const idx = paths.indexOf(previousSelection);
    if (idx >= 0) {
      selectRow(idx, { preserveTab: true, skipScroll: true });
    }
  } else {
    state.selectedRow = -1;
    state.currentSummaryPath = '';
    _syncArticleSelectValue('');
    _setLibraryDetailEmptyState();
  }
  refreshAnalysisForSelection();
  _updateLibraryNotebookAction();
  scheduleKnowledgeGraphReload(140);
}

// ---------------------------------------------
//  MULTI-SELECTION (checkboxes, Ctrl/Shift+click)
// ---------------------------------------------
// Matches ChatRequest.article_paths max_length on the server.
const LIBRARY_CHAT_SELECTION_MAX = 200;

function _librarySelection() {
  if (!(state.libSelectedPaths instanceof Set)) state.libSelectedPaths = new Set();
  return state.libSelectedPaths;
}

function _pruneLibrarySelection() {
  const selection = _librarySelection();
  for (const path of [...selection]) {
    if (!state.libPathIndex?.has(path)) selection.delete(path);
  }
  if (state.libSelectionAnchor && !state.libPathIndex?.has(state.libSelectionAnchor)) {
    state.libSelectionAnchor = '';
  }
  _renderLibrarySelectionBar();
}

function _selectedLibraryArticles() {
  const selection = _librarySelection();
  return (state.libPaths || [])
    .map((path, index) => ({ path, title: String(state.libRows?.[index]?.[0] || path).trim() }))
    .filter((item) => selection.has(item.path));
}

function _syncLibraryRowSelection(path) {
  const index = state.libPathIndex?.get(path);
  if (!Number.isInteger(index)) return;
  const row = document.querySelector(`#lib-tbody tr[data-i="${index}"]`);
  if (!row) return;
  const checked = _librarySelection().has(path);
  row.classList.toggle('multi-selected', checked);
  const box = row.querySelector('.lib-row-check');
  if (box) box.checked = checked;
}

function _setLibraryPathSelected(path, selected) {
  if (!path) return;
  const selection = _librarySelection();
  if (selected) selection.add(path);
  else selection.delete(path);
  _syncLibraryRowSelection(path);
}

function _selectLibraryRange(toPath) {
  const visible = Array.isArray(state.visibleLibPaths) ? state.visibleLibPaths : [];
  const from = visible.indexOf(state.libSelectionAnchor || '');
  const to = visible.indexOf(toPath);
  if (from < 0 || to < 0) {
    _setLibraryPathSelected(toPath, true);
    return;
  }
  const [start, end] = from <= to ? [from, to] : [to, from];
  for (const path of visible.slice(start, end + 1)) _setLibraryPathSelected(path, true);
}

function onLibraryRowClick(event, index) {
  const path = state.libPaths?.[index] || '';
  if (!path) return;
  const onCheckbox = event?.target instanceof Element && event.target.closest('.lib-row-check');
  if (onCheckbox) {
    if (event.shiftKey && state.libSelectionAnchor) _selectLibraryRange(path);
    else _setLibraryPathSelected(path, event.target.checked);
    state.libSelectionAnchor = path;
    _renderLibrarySelectionBar();
    return;
  }
  if (event?.shiftKey && state.libSelectionAnchor) {
    event.preventDefault();
    window.getSelection?.()?.removeAllRanges?.();
    _selectLibraryRange(path);
    state.libSelectionAnchor = path;
    _renderLibrarySelectionBar();
    return;
  }
  if (event?.ctrlKey || event?.metaKey) {
    event.preventDefault();
    _setLibraryPathSelected(path, !_librarySelection().has(path));
    state.libSelectionAnchor = path;
    _renderLibrarySelectionBar();
    return;
  }
  state.libSelectionAnchor = path;
  selectRow(index);
}

function onLibraryRowKeydown(event, index) {
  if (event?.target instanceof Element && event.target.closest('.lib-row-check')) return;
  handleClickableCardKey(event, () => selectRow(index));
}

function selectAllVisibleLibraryArticles() {
  const visible = Array.isArray(state.visibleLibPaths) ? state.visibleLibPaths : [];
  for (const path of visible) _setLibraryPathSelected(path, true);
  _renderLibrarySelectionBar();
}

function clearLibrarySelection() {
  const selection = _librarySelection();
  const paths = [...selection];
  selection.clear();
  paths.forEach(_syncLibraryRowSelection);
  _renderLibrarySelectionBar();
}

function _renderLibrarySelectionBar() {
  const bar = document.getElementById('lib-selection-bar');
  if (!bar) return;
  const count = _librarySelection().size;
  const visible = Array.isArray(state.visibleLibPaths) ? state.visibleLibPaths : [];
  bar.hidden = count === 0;
  document.getElementById('lib-table')?.classList.toggle('has-selection', count > 0);
  const label = document.getElementById('lib-selection-count');
  if (label) label.textContent = _ti('library.selectionCount', { count });
  const allBtn = document.getElementById('lib-selection-all-btn');
  if (allBtn) {
    allBtn.hidden = !visible.length || visible.every((path) => _librarySelection().has(path));
    allBtn.textContent = _ti('library.selectAllVisible', { count: visible.length });
  }
  // Offer only the action that changes something for the ticked papers.
  const selectedPaths = [..._librarySelection()];
  const excludedCount = selectedPaths.filter(_isLibraryPathRagExcluded).length;
  const excludeBtn = document.getElementById('lib-selection-exclude-btn');
  if (excludeBtn) excludeBtn.hidden = count > 0 && excludedCount === count;
  const includeBtn = document.getElementById('lib-selection-include-btn');
  if (includeBtn) includeBtn.hidden = excludedCount === 0;
}

function _isLibraryPathRagExcluded(path) {
  const index = state.libPathIndex?.get(path);
  return Number.isInteger(index) && Boolean(state.libMetadata?.[index]?.rag_excluded);
}

async function setLibrarySelectionRagExcluded(excluded) {
  const articles = _selectedLibraryArticles();
  if (!articles.length) {
    showToast(_ti('library.selectionEmpty'), 2800);
    return;
  }
  const paths = articles.map((item) => item.path);
  try {
    const response = await fetch('/api/library/rag-exclusion', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths, excluded }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.message || data.detail || _ti('library.ragExclusionFailed'));
  } catch (error) {
    showToast(error?.message || _ti('library.ragExclusionFailed'), 5200, 'error');
    return;
  }
  for (const path of paths) {
    const index = state.libPathIndex?.get(path);
    if (Number.isInteger(index) && state.libMetadata?.[index]) state.libMetadata[index].rag_excluded = excluded;
  }
  applyLibraryView();
  showToast(_ti(excluded ? 'library.ragExcludedToast' : 'library.ragIncludedToast', { count: paths.length }), 3600);
}

async function askChatAboutLibrarySelection() {
  const articles = _selectedLibraryArticles();
  if (!articles.length) {
    showToast(_ti('library.selectionEmpty'), 2800);
    return false;
  }
  if (articles.length > LIBRARY_CHAT_SELECTION_MAX) {
    showToast(_ti('library.selectionTooManyForChat', { max: LIBRARY_CHAT_SELECTION_MAX }), 4200, 'error');
    return false;
  }
  if (state.streaming) await stopCurrentChat(true, 'context_switch');
  startNewChat();
  setChatArticlesScope(articles);
  switchTab('chat');
  window.setTimeout(() => document.getElementById('msg-input')?.focus(), 80);
  return false;
}

function openLibrarySelectionNotebookModal(preferredMode = '') {
  const articles = _selectedLibraryArticles();
  if (!articles.length) {
    showToast(_ti('library.selectionEmpty'), 2800);
    return;
  }
  const firstTitle = articles[0].title;
  _setNotebookImportContext({
    source: 'library',
    action: 'import_paths',
    paths: articles.map((item) => item.path),
    modalTitle: _ti('library.selectionNotebookTitle'),
    focusCountText: _ti('library.selectionNotebookCount', { count: articles.length }),
    defaultNewTitle: articles.length === 1
      ? (firstTitle.length > 72 ? `${firstTitle.slice(0, 69)}…` : firstTitle)
      : _ti('library.selectionNotebookDefault'),
    emptyToast: _ti('library.selectionEmpty'),
  });
  _openNotebookImportModal(preferredMode);
}

function _updateLibraryNotebookAction() {
  const btn = document.getElementById('analysis-notebook-btn');
  const relPath = _currentSelectedArticlePath();
  if (!btn) return;
  btn.disabled = !relPath;
}

// ---------------------------------------------
//  COLUMNS (shown / hidden / order, saved in this browser)
// ---------------------------------------------
const LIBRARY_COLUMNS_STORAGE_KEY = 'library-columns';
const LIBRARY_COLUMNS_MAX = 6;
const LIBRARY_COLUMNS_DEFAULT = ['title', 'year', 'theme', 'analysis'];
const LIBRARY_COLUMN_DEFS = {
  title: { labelKey: 'library.colTitleAuthors', sort: 0, fixed: true },
  year: { labelKey: 'library.colYear', sort: 2 },
  theme: { labelKey: 'library.colTheme', sort: 'theme' },
  analysis: { labelKey: 'library.colAnalysis', sort: 7 },
  type: { labelKey: 'library.colType', sort: 3 },
  tags: { labelKey: 'library.colTags', sort: 4 },
  collection: { labelKey: 'library.colCollection', sort: 5 },
  added: { labelKey: 'library.colDateAdded', sort: 8 },
  size: { labelKey: 'library.colSize', sort: 6 },
};
let _libraryColumns = [...LIBRARY_COLUMNS_DEFAULT];

function _sanitizeLibraryColumns(list) {
  const seen = new Set();
  const cleaned = [];
  (Array.isArray(list) ? list : []).forEach((key) => {
    if (LIBRARY_COLUMN_DEFS[key] && key !== 'title' && !seen.has(key)) {
      seen.add(key);
      cleaned.push(key);
    }
  });
  return ['title', ...cleaned].slice(0, LIBRARY_COLUMNS_MAX);
}

function _loadLibraryColumns() {
  let saved = null;
  try {
    saved = JSON.parse(localStorage.getItem(LS_PREFIX + LIBRARY_COLUMNS_STORAGE_KEY) || 'null');
  } catch (_) {}
  _libraryColumns = saved ? _sanitizeLibraryColumns(saved) : [...LIBRARY_COLUMNS_DEFAULT];
}

function _saveLibraryColumns() {
  try {
    localStorage.setItem(LS_PREFIX + LIBRARY_COLUMNS_STORAGE_KEY, JSON.stringify(_libraryColumns));
  } catch (_) {}
}

function _libraryColumnsAreDefault() {
  return _libraryColumns.join() === LIBRARY_COLUMNS_DEFAULT.join();
}

function _libraryColumnLabel(key) {
  return _ti(LIBRARY_COLUMN_DEFS[key].labelKey);
}

function _renderLibraryHead() {
  const row = document.getElementById('lib-thead-row');
  if (!row) return;
  row.innerHTML = _libraryColumns.map((key) => {
    const def = LIBRARY_COLUMN_DEFS[key];
    const sorted = _sort.col === def.sort;
    const sortClass = sorted ? (_sort.asc ? ' class="sort-asc"' : ' class="sort-desc"') : '';
    const ariaSort = sorted ? (_sort.asc ? 'ascending' : 'descending') : 'none';
    return `<th data-col="${escapeAttr(def.sort)}" data-lib-col="${key}" tabindex="0" role="button" aria-sort="${ariaSort}"${sortClass}`
      + ` ${bindBbHandler('click', function() { sortTable(def.sort); })}`
      + ` ${bindBbHandler('keydown', function(event) { handleSortableHeaderKey(event, def.sort); })}`
      + `>${escapeHtml(_libraryColumnLabel(key))}</th>`;
  }).join('');
}

function _libraryCellHtml(key, row, path, metadata, ctx) {
  switch (key) {
    case 'title': {
      const authors = _libraryAuthorsCompact(row) || (row[1] && row[1] !== '-' ? row[1] : '');
      const extension = String(path || '').split('.').pop().toUpperCase() || 'DOC';
      return `<td class="lib-title-cell">
        <input type="checkbox" class="lib-row-check" aria-label="${escapeAttr(_ti('library.tickArticle', { title: row[0] || path }))}"${ctx.selected ? ' checked' : ''}>
        <div class="lib-title-block">
          <div class="lib-title-line"><span class="lib-title-text">${escapeHtml(row[0])}</span>${ctx.ragExcluded ? `<span class="lib-rag-excluded-pill" title="${escapeAttr(_ti('library.ragExcludedTitle'))}">${escapeHtml(_ti('library.ragExcluded'))}</span>` : ''}</div>
          ${authors ? `<div class="lib-title-authors" title="${escapeAttr(_libraryAuthorsFull(row))}">${escapeHtml(authors)}</div>` : ''}
        </div>
        <a class="row-open-btn" href="${ctx.documentUrl}" target="_blank" ${bindBbHandler('click', function(event) { event.stopPropagation(); })} title="Ouvrir le document">
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          ${escapeHtml(extension)}
        </a>
      </td>`;
    }
    case 'year':
      return `<td class="lib-year-cell" data-label="${escapeAttr(_ti('library.colYear'))}">${row[2] && row[2] !== '-' ? escapeHtml(row[2]) : ''}</td>`;
    case 'theme': {
      const themePath = _libraryMetadataThemePath(metadata);
      return `<td class="lib-theme-cell" data-label="${escapeAttr(_ti('library.colTheme'))}" title="${escapeAttr(themePath.join(' / '))}">${escapeHtml(themePath.length ? themePath[themePath.length - 1] : '-')}</td>`;
    }
    case 'analysis': {
      const done = _libraryRowHasAnalysis(row);
      return `<td class="lib-analysis-cell${done ? ' is-done' : ''}" data-label="${escapeAttr(_ti('library.colAnalysis'))}"${done ? ` title="${escapeAttr(row[7])}"` : ''}>${done ? '<span class="lib-analysis-dot" aria-hidden="true"></span>' : ''}${escapeHtml(_ti(done ? 'library.analysisDone' : 'library.analysisNone'))}</td>`;
    }
    case 'type':
      return `<td class="lib-type-cell" data-label="${escapeAttr(_ti('library.colType'))}"><span class="lib-type-pill${_libraryItemTypeKey(row) ? '' : ' is-missing'}">${escapeHtml(_libraryItemTypeLabel(row[3]))}</span></td>`;
    case 'tags':
      return `<td class="lib-text-cell" data-label="${escapeAttr(_ti('library.colTags'))}" title="${escapeAttr(row[4] || '')}">${escapeHtml(row[4] || '-')}</td>`;
    case 'collection':
      return `<td class="lib-text-cell" data-label="${escapeAttr(_ti('library.colCollection'))}" title="${escapeAttr(row[5] || '')}">${escapeHtml(row[5] || '-')}</td>`;
    case 'added':
      return `<td class="lib-date-added-cell" data-label="${escapeAttr(_ti('library.colDateAdded'))}">${escapeHtml(row[8] || '-')}</td>`;
    case 'size':
      return `<td class="lib-text-cell" data-label="${escapeAttr(_ti('library.colSize'))}">${escapeHtml(row[6] || '-')}</td>`;
    default:
      return '<td></td>';
  }
}

function _libraryEmptySetup() {
  return !state.ragReady && !state.ragInitBusy ? (state.ragFirstInitAction || '') : '';
}

// Nothing indexed: say what the list will hold, with the status pill's own
// "Préparer" / "Réparer" action while the library is not loaded.
function _libraryEmptyRowHtml(colspan) {
  const setup = _libraryEmptySetup();
  const actionKey = setup === 'repair' ? 'chat.repairAction' : 'chat.prepareAction';
  const action = setup
    ? `<button type="button" class="btn btn-primary empty-state-action"${state.ragInitAvailable === false ? ' disabled' : ''} ${bindBbHandler('click', function() { runFirstRagInit(); })}>${escapeHtml(_ti(actionKey))}</button>`
    : `<button type="button" class="btn btn-primary empty-state-action" ${bindBbHandler('click', function() { pickLibraryFiles(); })}>${escapeHtml(_ti('library.addFiles'))}</button>`;
  return `
    <tr class="lib-empty-row lib-empty-library" data-setup="${setup}" aria-live="polite"><td colspan="${colspan}">
      <div class="empty-state">
        <h3 class="empty-state-title">${escapeHtml(_ti('library.emptyTitle'))}</h3>
        <p class="empty-state-text">${escapeHtml(_ti('library.emptyHint'))}</p>
        ${action}
      </div>
    </td></tr>`;
}

// Called on every library status render: the empty row follows the action
// the status pill offers.
function _syncLibraryEmptyRow() {
  const row = document.querySelector('#lib-tbody .lib-empty-library');
  if (row && row.dataset.setup !== _libraryEmptySetup()) row.outerHTML = _libraryEmptyRowHtml(_libraryColumns.length);
}

function _renderLibraryRows(rows, paths, metadata = []) {
  state.visibleLibPaths = Array.isArray(paths) ? [...paths] : [];
  const renderRows = rows.slice(0, 500);
  const renderPaths = paths.slice(0, 500);
  const tbody = document.getElementById('lib-tbody');
  const selection = _librarySelection();
  const colspan = _libraryColumns.length;
  _renderLibraryHead();
  tbody.innerHTML = renderRows.map((row, i) => {
    const path = renderPaths[i];
    const origIdx = state.libPathIndex?.get(path) ?? -1;
    const ragExcluded = Boolean(metadata[i]?.rag_excluded);
    const selected = selection.has(path);
    const rowClasses = [selected ? 'multi-selected' : '', ragExcluded ? 'rag-excluded' : ''].filter(Boolean).join(' ');
    const ctx = { selected, ragExcluded, documentUrl: '/api/open-pdf?path=' + encodeURIComponent(path) };
    return `
    <tr ${bindBbHandler('click', function(event) { onLibraryRowClick(event, origIdx); })} ${bindBbHandler('keydown', function(event) { onLibraryRowKeydown(event, origIdx); })} tabindex="0" role="button" data-i="${origIdx}"${rowClasses ? ` class="${rowClasses}"` : ''}>
      ${_libraryColumns.map((key) => _libraryCellHtml(key, row, path, metadata[i] || {}, ctx)).join('')}
    </tr>`;
  }).join('');
  if (!rows.length && state.libRows?.length) {
    tbody.innerHTML = `<tr class="lib-empty-row" aria-live="polite"><td colspan="${colspan}">${escapeHtml(_ti('library.noMatchingDocuments'))}</td></tr>`;
  } else if (!rows.length) {
    tbody.innerHTML = _libraryEmptyRowHtml(colspan);
  }
  if (rows.length > renderRows.length) {
    tbody.insertAdjacentHTML(
      'beforeend',
      `<tr aria-live="polite"><td colspan="${colspan}">${escapeHtml(
        _tr(`${renderRows.length} articles affichés sur ${rows.length}. Affinez la recherche pour voir les autres.`),
      )}</td></tr>`,
    );
  }

  if (Number.isInteger(state.selectedRow) && state.selectedRow >= 0) {
    const selectedRow = document.querySelector(`#lib-tbody tr[data-i="${state.selectedRow}"]`);
    if (selectedRow) selectedRow.classList.add('selected');
  }
  _renderLibrarySelectionBar();
}

function _renderLibraryColumnsMenu() {
  const root = document.getElementById('lib-columns-panel');
  const badge = document.getElementById('lib-columns-count');
  if (badge) badge.textContent = String(_libraryColumns.length);
  if (!root) return;
  const grip = '<svg width="10" height="14" viewBox="0 0 10 14" fill="currentColor" aria-hidden="true"><circle cx="3" cy="3" r="1.3"/><circle cx="7" cy="3" r="1.3"/><circle cx="3" cy="7" r="1.3"/><circle cx="7" cy="7" r="1.3"/><circle cx="3" cy="11" r="1.3"/><circle cx="7" cy="11" r="1.3"/></svg>';
  const full = _libraryColumns.length >= LIBRARY_COLUMNS_MAX;
  const shown = _libraryColumns.map((key) => {
    const def = LIBRARY_COLUMN_DEFS[key];
    if (def.fixed) {
      return `<div class="lib-col-item is-fixed"><span class="lib-col-grip"></span><span class="lib-col-name">${escapeHtml(_libraryColumnLabel(key))}</span><span class="lib-col-note">${escapeHtml(_ti('library.columnsAlways'))}</span></div>`;
    }
    return `<div class="lib-col-item" draggable="true" data-col-key="${key}" tabindex="0" ${bindBbHandler('dragstart', function(event) { _libraryColumnDragStart(event, key); })} ${bindBbHandler('dragover', function(event) { event.preventDefault(); })} ${bindBbHandler('drop', function(event) { _libraryColumnDrop(event, key); })} ${bindBbHandler('keydown', function(event) { _libraryColumnKeydown(event, key); })}>
      <span class="lib-col-grip">${grip}</span>
      <label class="lib-col-name"><input type="checkbox" checked ${bindBbHandler('change', function() { toggleLibraryColumn(key); })}> <span>${escapeHtml(_libraryColumnLabel(key))}</span></label>
    </div>`;
  }).join('');
  const hidden = Object.keys(LIBRARY_COLUMN_DEFS).filter((key) => !_libraryColumns.includes(key)).map((key) => (
    `<div class="lib-col-item"><span class="lib-col-grip"></span><label class="lib-col-name"><input type="checkbox"${full ? ' disabled' : ''} ${bindBbHandler('change', function() { toggleLibraryColumn(key); })}> <span>${escapeHtml(_libraryColumnLabel(key))}</span></label></div>`
  )).join('');
  root.innerHTML = `
    <div class="lib-col-heading">${escapeHtml(_ti('library.columnsShown'))}</div>${shown}
    ${hidden ? `<div class="lib-col-sep"></div><div class="lib-col-heading">${escapeHtml(_ti('library.columnsHidden'))}</div>${hidden}` : ''}
    <div class="lib-col-footer"><span>${escapeHtml(_ti('library.columnsLimit', { n: _libraryColumns.length, max: LIBRARY_COLUMNS_MAX }))}</span>
      <button type="button" class="lib-col-reset"${_libraryColumnsAreDefault() ? ' disabled' : ''} ${bindBbHandler('click', function() { resetLibraryColumns(); })}>${escapeHtml(_ti('library.columnsReset'))}</button></div>`;
}

function _setLibraryColumns(list) {
  _libraryColumns = _sanitizeLibraryColumns(list);
  _saveLibraryColumns();
  _renderLibraryColumnsMenu();
  applyLibraryView();
}

function toggleLibraryColumn(key) {
  if (!LIBRARY_COLUMN_DEFS[key] || LIBRARY_COLUMN_DEFS[key].fixed) return;
  if (_libraryColumns.includes(key)) {
    // A hidden column cannot stay the sort key.
    if (_sort.col === LIBRARY_COLUMN_DEFS[key].sort) _sort.col = -1;
    _setLibraryColumns(_libraryColumns.filter((item) => item !== key));
  } else if (_libraryColumns.length < LIBRARY_COLUMNS_MAX) {
    _setLibraryColumns([..._libraryColumns, key]);
  } else {
    _renderLibraryColumnsMenu();
  }
}

function resetLibraryColumns() {
  const stillVisible = LIBRARY_COLUMNS_DEFAULT.map((key) => LIBRARY_COLUMN_DEFS[key].sort);
  if (_sort.col !== -1 && !stillVisible.includes(_sort.col)) _sort.col = -1;
  _setLibraryColumns(LIBRARY_COLUMNS_DEFAULT);
}

let _libraryColumnDragKey = '';
function _libraryColumnDragStart(event, key) {
  _libraryColumnDragKey = key;
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', key);
  }
}

function _moveLibraryColumn(key, targetKey) {
  if (!key || key === targetKey) return;
  const from = _libraryColumns.indexOf(key);
  const to = _libraryColumns.indexOf(targetKey);
  if (from < 1 || to < 1) return;
  const next = _libraryColumns.filter((item) => item !== key);
  next.splice(to, 0, key);
  _setLibraryColumns(next);
}

function _libraryColumnDrop(event, targetKey) {
  event.preventDefault();
  _moveLibraryColumn(_libraryColumnDragKey, targetKey);
  _libraryColumnDragKey = '';
}

function _libraryColumnKeydown(event, key) {
  if (!event.altKey || (event.key !== 'ArrowUp' && event.key !== 'ArrowDown')) return;
  event.preventDefault();
  const at = _libraryColumns.indexOf(key);
  const target = _libraryColumns[at + (event.key === 'ArrowUp' ? -1 : 1)];
  if (!target) return;
  _moveLibraryColumn(key, target);
  document.querySelector(`#lib-columns-panel [data-col-key="${key}"]`)?.focus();
}

// Active filters as removable chips under the search row.
function _libraryFilterChips() {
  const f = _libraryShowOnly;
  const chips = [];
  const fromSet = (bucket, labelFor, prefixKey) => {
    f[bucket].forEach((value) => {
      chips.push({ label: `${prefixKey ? `${_ti(prefixKey)}: ` : ''}${labelFor(value)}`, remove: () => f[bucket].delete(value) });
    });
  };
  fromSet('itemTypes', _libraryItemTypeLabel, '');
  fromSet('extensions', (ext) => ext.toUpperCase(), '');
  fromSet('collections', (value) => value.split(' / ').pop(), 'library.chipCollection');
  fromSet('themes', (value) => value.split(' / ').pop(), 'library.colTheme');
  const radio = (field, labels) => {
    if (f[field]) chips.push({ label: _ti(labels[f[field]]), remove: () => { f[field] = ''; } });
  };
  radio('analyses', { with: 'library.showOnlyAnalysesWith', without: 'library.showOnlyAnalysesWithout' });
  radio('metadata', { with: 'library.showOnlyMetadataWith', without: 'library.showOnlyMetadataWithout' });
  radio('ragExcluded', { excluded: 'library.showOnlyRagExcluded', included: 'library.showOnlyRagIncluded' });
  if (f.yearFrom !== null || f.yearTo !== null) {
    const label = f.yearFrom !== null && f.yearTo !== null ? `${f.yearFrom}–${f.yearTo}`
      : f.yearFrom !== null ? `≥ ${f.yearFrom}` : `≤ ${f.yearTo}`;
    chips.push({ label, remove: () => { f.yearFrom = null; f.yearTo = null; } });
  }
  return chips;
}

function _renderLibraryFilterChips() {
  const root = document.getElementById('lib-filter-chips');
  if (!root) return;
  const chips = _libraryFilterChips();
  root.hidden = chips.length === 0;
  root.innerHTML = chips.map((chip, index) => `
    <span class="lib-filter-chip">${escapeHtml(chip.label)}<button type="button" aria-label="${escapeAttr(_ti('library.removeFilter', { label: chip.label }))}" ${bindBbHandler('click', function() { removeLibraryFilterChip(index); })}>×</button></span>`).join('')
    + (chips.length ? `<button type="button" class="lib-filter-clear" ${bindBbHandler('click', function() { resetLibraryShowOnly(); })}>${escapeHtml(_ti('library.filtersClear'))}</button>` : '');
}

function removeLibraryFilterChip(index) {
  const chip = _libraryFilterChips()[index];
  if (!chip) return;
  chip.remove();
  _saveLibraryShowOnly();
  _renderLibraryShowOnlyOptions();
  applyLibraryView();
}

function sortTable(col) {
  if (!state.libRows) return;
  _sort.asc = (_sort.col === col) ? !_sort.asc : true;
  _sort.col = col;
  applyLibraryView();
}

function handleSortableHeaderKey(event, col) {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  event.preventDefault();
  sortTable(col);
}

async function selectRow(i, options = {}) {
  const preserveTab = Boolean(options.preserveTab);
  const skipScroll = Boolean(options.skipScroll);
  const relPath = state.libPaths[i] || '';
  if (!relPath) return;

  document.querySelectorAll('#lib-tbody tr').forEach(tr => tr.classList.remove('selected'));
  const row = document.querySelector(`#lib-tbody tr[data-i="${i}"]`);
  if (row) row.classList.add('selected');
  state.selectedRow = i;
  state.currentSummaryPath = relPath;
  _syncArticleSelectValue(relPath, state.libRows[i]?.[0] || relPath);
  _renderLibraryDetailHeader(i);
  const typeSelect = document.getElementById('analysis-type-select');
  const bucket = _ensureArticleAnalysisState(relPath);
  if (typeSelect) {
    const hasLastType = bucket.lastType && Array.from(typeSelect.options).some((opt) => opt.value === bucket.lastType);
    const cachedTypes = _cachedAnalysisTypesForRow(state.libRows[i] || []);
    const cachedType = cachedTypes.find((type) => Array.from(typeSelect.options).some((opt) => opt.value === type));
    if (hasLastType) typeSelect.value = bucket.lastType;
    else if (cachedType) typeSelect.value = cachedType;
    else if (!typeSelect.value && typeSelect.options.length) typeSelect.selectedIndex = 0;
  }
  loadItemCitations(relPath);
  _updateLibraryNotebookAction();
  if (!preserveTab) switchLibraryDetailTab('overview');
  if (!skipScroll) {
    if (isMobileNavViewport()) {
      window.requestAnimationFrame(() => {
        document.getElementById('library-detail')?.scrollIntoView({ block: 'start', behavior: preferredScrollBehavior() });
      });
    } else if (row) {
      row.scrollIntoView({ block: 'nearest', behavior: preferredScrollBehavior() });
    }
  }
  refreshAnalysisForSelection();
  const content = document.getElementById('meta-content');
  const detailRequestKey = `${relPath}::${Date.now()}`;
  state.libraryDetailRequestKey = detailRequestKey;
  content.innerHTML = parseMarkdown(_libraryRowFallbackMarkdown(state.libRows[i] || [], relPath));
  try {
    const d = await fetchJsonOrThrow(
      '/api/item-detail?path=' + encodeURIComponent(relPath),
      { timeoutMs: 12000 },
      'Chargement métadonnées impossible',
    );
    if (state.libraryDetailRequestKey !== detailRequestKey || _currentSelectedArticlePath() !== relPath) return;
    content.innerHTML = parseMarkdown(d.markdown || '');
    renderMath(content);
  } catch(e) {
    if (state.libraryDetailRequestKey !== detailRequestKey || _currentSelectedArticlePath() !== relPath) return;
    content.innerHTML = `<span style="color:var(--muted)">${escapeHtml(e?.message || _tr('Erreur chargement métadonnées.'))}</span>`;
  }
}

function scrollLibraryListIntoView() {
  document.getElementById('library-main')?.scrollIntoView({ block: 'start', behavior: preferredScrollBehavior() });
}

async function loadAnalysisTypes() {
  try {
    const language = _analysisPromptLanguage();
    const d = await fetchJsonOrThrow(
      `/api/analysis-types?language=${encodeURIComponent(language)}`,
      {},
      "Chargement des types d'analyse impossible",
    );
    const select = document.getElementById('analysis-type-select');
    const currentPath = _currentSelectedArticlePath();
    const bucket = currentPath ? _ensureArticleAnalysisState(currentPath) : null;
    const cachedTypes = _cachedAnalysisTypesForRow(state.libRows[state.selectedRow] || []);
    const preferred = bucket?.lastType
      || cachedTypes.find((type) => Array.from(select?.options || []).some((opt) => opt.value === type))
      || select?.value
      || '';
    const typeOptions = Array.isArray(d.type_options) && d.type_options.length
      ? d.type_options
      : (Array.isArray(d.types) ? d.types : []).map((type) => ({ type, label: type }));
    select.innerHTML = typeOptions
      .map((item) => `<option value="${escapeHtml(item.type)}">${escapeHtml(item.label || item.type)}</option>`)
      .join('');
    if (preferred && Array.from(select.options).some((opt) => opt.value === preferred)) {
      select.value = preferred;
    } else {
      const cachedType = cachedTypes.find((type) => Array.from(select.options).some((opt) => opt.value === type));
      if (cachedType) select.value = cachedType;
    }
    loadAnalysisPrompts({ preferredType: select?.value || '' });
    refreshAnalysisForSelection();
  } catch (e) {
    console.error('loadAnalysisTypes failed:', e);
    showToast(_tr("Impossible de charger les types d'analyse."), 3200, 'error');
  }
}

function _analysisPromptStatus(message = '', kind = 'cyan') {
  const status = document.getElementById('analysis-prompt-status');
  if (!status) return;
  const msg = String(message || '').trim();
  status.textContent = msg;
  status.style.display = msg ? 'block' : 'none';
  status.className = `settings-status status-${kind}`;
}

function _analysisPromptLanguage() {
  const language = String(
    window.BibliobrainI18n?.getLanguage?.()
      || document.documentElement.lang
      || 'fr',
  ).trim().toLowerCase();
  return language === 'en' ? 'en' : 'fr';
}

function _currentAnalysisPromptPayload() {
  const type = document.getElementById('analysis-prompt-type-select')?.value || '';
  return (state.analysisPrompts || []).find((item) => item.type === type) || null;
}

function _renderAnalysisPromptEditor(type = '') {
  if (settingsDraftIsDirty('prompt')) return;
  const select = document.getElementById('analysis-prompt-type-select');
  const editor = document.getElementById('analysis-prompt-editor');
  const defaultEditor = document.getElementById('analysis-prompt-default');
  if (!select || !editor || !defaultEditor) return;
  const selectedType = type || select.value || state.analysisPrompts?.[0]?.type || '';
  if (selectedType && Array.from(select.options).some((opt) => opt.value === selectedType)) {
    select.value = selectedType;
  }
  const payload = _currentAnalysisPromptPayload();
  editor.value = payload?.prompt || '';
  defaultEditor.value = payload?.default_prompt || '';
  if (!payload) {
    _analysisPromptStatus(
      _analysisPromptLanguage() === 'en' ? 'No prompt loaded.' : 'Aucun prompt chargé.',
      'amber',
    );
    return;
  }
  const english = _analysisPromptLanguage() === 'en';
  _analysisPromptStatus(
    payload.custom
      ? (
        english
          ? `Custom prompt active — fingerprint ${payload.fingerprint || '-'}`
          : `Prompt personnalisé actif — empreinte ${payload.fingerprint || '-'}`
      )
      : (
        english
          ? `Default prompt — fingerprint ${payload.fingerprint || '-'}`
          : `Prompt par défaut — empreinte ${payload.fingerprint || '-'}`
      ),
    payload.custom ? 'amber' : 'cyan',
  );
}

async function loadAnalysisPrompts(options = {}) {
  if (settingsDraftIsDirty('prompt')) return;
  const preferredType = String(options.preferredType || '').trim();
  const select = document.getElementById('analysis-prompt-type-select');
  if (!select) return;
  try {
    const language = _analysisPromptLanguage();
    const data = await fetchJsonOrThrow(
      `/api/analysis-prompts?language=${encodeURIComponent(language)}`,
      {},
      language === 'en' ? 'Unable to load prompts' : 'Chargement des prompts impossible',
    );
    if (settingsDraftIsDirty('prompt')) return;
    state.analysisPrompts = Array.isArray(data.prompts) ? data.prompts : [];
    const current = preferredType || select.value || document.getElementById('analysis-type-select')?.value || '';
    select.innerHTML = state.analysisPrompts
      .map((item) => {
        const suffix = item.custom ? ' *' : '';
        return `<option value="${escapeHtml(item.type)}">${escapeHtml((item.label || item.type) + suffix)}</option>`;
      })
      .join('');
    _renderAnalysisPromptEditor(current);
  } catch (e) {
    _analysisPromptStatus(
      e?.message || (
        _analysisPromptLanguage() === 'en'
          ? 'Unable to load prompts.'
          : 'Chargement des prompts impossible.'
      ),
      'red',
    );
  }
}

function selectAnalysisPromptForEdit(type) {
  _renderAnalysisPromptEditor(type);
}

function saveAnalysisPromptOverride() {
  return updateAnalysisPromptOverride(false);
}

function resetAnalysisPromptOverride() {
  return updateAnalysisPromptOverride(true);
}

async function updateAnalysisPromptOverride(reset) {
  const type = document.getElementById('analysis-prompt-type-select')?.value || '';
  if (!type) return;
  const prompt = reset ? '' : document.getElementById('analysis-prompt-editor')?.value || '';
  const language = _analysisPromptLanguage();
  const english = language === 'en';
  const failure = reset
    ? (english ? 'Unable to reset' : 'Réinitialisation impossible')
    : (english ? 'Unable to save' : 'Sauvegarde impossible');
  const success = reset
    ? (english ? 'Default prompt restored.' : 'Prompt par défaut restauré.')
    : (english
      ? 'Prompt saved. The cache for this analysis type will be regenerated on the next run.'
      : 'Prompt sauvegardé. Le cache de ce type sera régénéré au prochain calcul.');
  try {
    const data = await postJsonOrThrow(
      reset ? '/api/analysis-prompts/reset' : '/api/analysis-prompts',
      { analysis_type: type, prompt, language },
      failure,
    );
    if (!data?.ok) throw new Error(data?.msg || failure);
    const changedDuringSave = !reset && document.getElementById('analysis-prompt-editor')?.value !== prompt;
    setSettingsDraft('prompt', changedDuringSave);
    await loadAnalysisPrompts({ preferredType: type });
    await loadLibrary();
    showToast(success, reset ? 3200 : 4200, 'success');
  } catch (e) {
    _analysisPromptStatus(e?.message || failure + '.', 'red');
  }
}

window.addEventListener('bibliobrain:languagechange', () => {
  void loadAnalysisTypes();
  _renderLibraryShowOnlyOptions();
  applyLibraryView();
});

_loadLibraryShowOnly();
_loadLibraryColumns();

document.addEventListener('click', (event) => {
  const panel = document.getElementById('lib-show-only');
  if (panel?.open && event.target instanceof Node && !panel.contains(event.target)) panel.open = false;
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && document.getElementById('analysis-section')?.classList.contains('is-expanded')) {
    toggleAnalysisExpanded(false);
    document.getElementById('analysis-expand-btn')?.focus();
    return;
  }
  const panel = document.getElementById('lib-show-only');
  if (event.key !== 'Escape' || !panel?.open) return;
  panel.open = false;
  panel.querySelector('summary')?.focus();
});

function openAnalysisPromptSettings() {
  const selectedType = document.getElementById('analysis-type-select')?.value || '';
  const panel = document.getElementById('settings-panel');
  if (!panel?.classList.contains('open')) {
    toggleSettings();
  }
  switchSettingsTab('expert-prompts');
  loadAnalysisPrompts({ preferredType: selectedType });
  setTimeout(() => document.getElementById('analysis-prompt-editor')?.focus(), 120);
}

function _renderCitationPanel(citations) {
  const panel = document.getElementById('citation-actions');
  const keyPill = document.getElementById('citation-key-pill');
  const preview = document.getElementById('citation-preview');
  const empty = document.getElementById('citation-empty');
  const zoteroBtn = document.getElementById('library-open-zotero-btn');
  state.currentCitations = citations || null;
  if (!panel || !keyPill || !preview) return;

  if (!citations) {
    panel.style.display = 'none';
    if (empty) empty.style.display = 'block';
    _setActionLinkState(zoteroBtn, false);
    keyPill.textContent = 'Citation key: -';
    preview.textContent = '';
    return;
  }

  keyPill.textContent = `Citation key: ${citations.citation_key || '-'}`;
  preview.textContent = citations.apa || '';
  panel.style.display = 'flex';
  if (empty) empty.style.display = 'none';
  if (zoteroBtn) {
    const uri = String(citations.zotero_select_uri || '').trim();
    _setActionLinkState(zoteroBtn, Boolean(uri), uri || '#');
  }
}

function openCurrentInZotero(event) {
  const uri = String(state.currentCitations?.zotero_select_uri || '').trim();
  if (!uri) {
    event?.preventDefault?.();
    showToast(_tr("Lien Zotero indisponible pour cet article."), 3200, 'error');
    return false;
  }
  window.location.assign(uri);
  return false;
}

async function loadItemCitations(relPath) {
  if (!relPath) {
    state.citationRequestKey = '';
    _renderCitationPanel(null);
    return;
  }
  state.currentSummaryPath = relPath;
  _syncArticleSelectValue(relPath);
  const requestKey = `${relPath}::${Date.now()}`;
  state.citationRequestKey = requestKey;
  try {
    const d = await fetchJsonOrThrow(
      '/api/item-citations?path=' + encodeURIComponent(relPath),
      { timeoutMs: 12000 },
      'Chargement citations impossible',
    );
    if (state.citationRequestKey !== requestKey || _currentSelectedArticlePath() !== relPath) return;
    _renderCitationPanel(d);
    state.currentSummaryPath = relPath;
  } catch(e) {
    if (state.citationRequestKey !== requestKey || _currentSelectedArticlePath() !== relPath) return;
    _renderCitationPanel(null);
  }
}

async function copyCurrentCitation(format) {
  const citations = state.currentCitations;
  if (!citations) {
    showToast(_tr('Aucune citation disponible.'));
    return;
  }
  const value = format === 'bibtex' ? citations.bibtex : citations.apa;
  if (!value) {
    showToast(_tr('Citation vide.'));
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
    showToast(_tr(format === 'bibtex' ? 'BibTeX copié.' : 'Citation APA copiée.'), 2500, 'success');
  } catch(e) {
    const temp = document.createElement('textarea');
    temp.value = value;
    document.body.appendChild(temp);
    temp.select();
    document.execCommand('copy');
    temp.remove();
    showToast(_tr(format === 'bibtex' ? 'BibTeX copié.' : 'Citation APA copiée.'), 2500, 'success');
  }
}
