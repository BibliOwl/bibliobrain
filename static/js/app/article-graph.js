// Cartographies › "Depuis des articles": pick library papers, then draw them
// with the citation graph used by the paper sheet and notebooks
// (citation-graph.js), shown inline in this screen.

const ARTICLE_GRAPH_MAX = 60;
const ARTICLE_GRAPH_RESULTS_MAX = 8;

const _articleGraph = {
  paths: [],
  bound: false,
  notebooksLoaded: false,
};

function _articleGraphNormalize(value) {
  return String(value || '')
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
    .toLowerCase();
}

function _articleGraphItem(path) {
  const index = state.libPathIndex?.get(path);
  const row = Number.isInteger(index) ? (state.libRows?.[index] || []) : [];
  const clean = (value) => {
    const text = String(value || '').trim();
    return text && text !== '-' ? text : '';
  };
  return {
    path,
    title: clean(row[0]) || String(path).split(/[\\/]/).pop() || path,
    author: clean(row[1]),
    year: clean(row[2]),
  };
}

function _articleGraphInLibrary(path) {
  return Boolean(path) && Boolean(state.libPathIndex?.has(path));
}

function _articleGraphSetStatus(message, kind = '') {
  const status = document.getElementById('article-graph-status');
  if (!status) return;
  status.textContent = message || '';
  status.dataset.kind = kind;
}

function _articleGraphAnswerPaths() {
  const sources = Array.isArray(state.lastRagRetrieval?.sources) ? state.lastRagRetrieval.sources : [];
  const paths = sources.map((source) => String(source?.file || source?.path || '').trim());
  return [...new Set(paths.filter(_articleGraphInLibrary))];
}

function _articleGraphLibrarySelectionPaths() {
  if (typeof _selectedLibraryArticles !== 'function') return [];
  return _selectedLibraryArticles().map((item) => item.path).filter(_articleGraphInLibrary);
}

function _articleGraphAdd(paths) {
  const current = new Set(_articleGraph.paths);
  let added = 0;
  let refused = 0;
  for (const path of paths) {
    if (!_articleGraphInLibrary(path) || current.has(path)) continue;
    if (current.size >= ARTICLE_GRAPH_MAX) {
      refused += 1;
      continue;
    }
    current.add(path);
    _articleGraph.paths.push(path);
    added += 1;
  }
  if (refused) {
    _articleGraphSetStatus(_ti('articleGraph.tooMany', { max: ARTICLE_GRAPH_MAX }), 'error');
  } else if (paths.length && !added) {
    _articleGraphSetStatus(_ti(paths.some(_articleGraphInLibrary) ? 'articleGraph.nothingAdded' : 'articleGraph.noneInLibrary'));
  } else {
    _articleGraphSetStatus('');
  }
  _articleGraphRender();
  return added;
}

function _articleGraphRemove(path) {
  _articleGraph.paths = _articleGraph.paths.filter((item) => item !== path);
  _articleGraphSetStatus('');
  _articleGraphRender();
}

function _articleGraphClear() {
  _articleGraph.paths = [];
  _articleGraphSetStatus('');
  _articleGraphRender();
  document.getElementById('article-graph-search')?.focus();
}

function _articleGraphMatches(query) {
  const terms = _articleGraphNormalize(query).split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  const matches = [];
  for (const path of state.libPaths || []) {
    const item = _articleGraphItem(path);
    const haystack = _articleGraphNormalize(`${item.title} ${item.author} ${item.year}`);
    if (terms.every((term) => haystack.includes(term))) matches.push(item);
    if (matches.length >= ARTICLE_GRAPH_RESULTS_MAX) break;
  }
  return matches;
}

function _articleGraphRenderResults() {
  const results = document.getElementById('article-graph-results');
  if (!results) return;
  const query = String(document.getElementById('article-graph-search')?.value || '').trim();
  if (!(state.libPaths || []).length) {
    results.innerHTML = `<div class="article-graph-empty">${escapeHtml(_ti('articleGraph.libraryEmpty'))}</div>`;
    return;
  }
  if (!query) {
    results.innerHTML = `<div class="article-graph-empty">${escapeHtml(_ti('articleGraph.searchHint'))}</div>`;
    return;
  }
  const matches = _articleGraphMatches(query);
  if (!matches.length) {
    results.innerHTML = `<div class="article-graph-empty">${escapeHtml(_ti('articleGraph.noMatch'))}</div>`;
    return;
  }
  const chosen = new Set(_articleGraph.paths);
  results.innerHTML = matches.map((item) => {
    const isChosen = chosen.has(item.path);
    const meta = [item.author, item.year].filter(Boolean).join(' · ');
    return `<button type="button" class="article-graph-result${isChosen ? ' is-selected' : ''}" role="option" aria-selected="${isChosen ? 'true' : 'false'}" data-article-graph-path="${escapeHtml(item.path)}">
      <span class="article-graph-result-main">
        <span class="article-graph-result-title">${escapeHtml(item.title)}</span>
        ${meta ? `<span class="article-graph-result-meta">${escapeHtml(meta)}</span>` : ''}
      </span>
      <span class="article-graph-result-action" aria-hidden="true">${isChosen ? '✓' : '+'}</span>
    </button>`;
  }).join('');
}

function _articleGraphRenderSelected() {
  const list = document.getElementById('article-graph-selected');
  const count = document.getElementById('article-graph-count');
  const clear = document.getElementById('article-graph-clear');
  const build = document.getElementById('article-graph-build');
  const total = _articleGraph.paths.length;
  if (count) {
    count.textContent = total
      ? _ti(total === 1 ? 'articleGraph.countOne' : 'articleGraph.countMany', { count: total })
      : _ti('articleGraph.countNone');
  }
  if (clear) clear.hidden = !total;
  if (build) build.disabled = !total;
  if (!list) return;
  if (!total) {
    list.innerHTML = '';
    return;
  }
  list.innerHTML = _articleGraph.paths.map((path) => {
    const item = _articleGraphItem(path);
    const label = [item.author, item.year].filter(Boolean).join(' ') || item.title;
    const removeLabel = _ti('articleGraph.remove', { title: item.title });
    return `<li class="article-graph-chip" title="${escapeHtml(item.title)}">
      <span class="article-graph-chip-label">${escapeHtml(label)}</span>
      <button type="button" data-article-graph-remove="${escapeHtml(path)}" aria-label="${escapeHtml(removeLabel)}" title="${escapeHtml(removeLabel)}">×</button>
    </li>`;
  }).join('');
}

function _articleGraphRenderShortcuts() {
  const fromLibrary = document.getElementById('article-graph-from-library');
  const fromAnswer = document.getElementById('article-graph-from-answer');
  const libraryCount = _articleGraphLibrarySelectionPaths().length;
  const answerCount = _articleGraphAnswerPaths().length;
  if (fromLibrary) {
    fromLibrary.disabled = !libraryCount;
    fromLibrary.querySelector('.article-graph-shortcut-count').textContent = String(libraryCount);
  }
  if (fromAnswer) {
    fromAnswer.disabled = !answerCount;
    fromAnswer.querySelector('.article-graph-shortcut-count').textContent = String(answerCount);
  }
}

function _articleGraphRender() {
  _articleGraphRenderSelected();
  _articleGraphRenderResults();
  _articleGraphRenderShortcuts();
}

async function _articleGraphLoadNotebooks() {
  const select = document.getElementById('article-graph-notebook');
  if (!select || _articleGraph.notebooksLoaded) return;
  try {
    const response = await fetch('/api/notebooks?limit=500', { cache: 'no-store' });
    if (!response.ok) throw new Error();
    const payload = await response.json();
    const notebooks = Array.isArray(payload?.notebooks) ? payload.notebooks : [];
    select.innerHTML = `<option value="">${escapeHtml(_ti('articleGraph.fromNotebook'))}</option>`
      + notebooks.map((nb) => `<option value="${escapeHtml(nb.id)}">${escapeHtml(nb.title || nb.id)}</option>`).join('');
    select.disabled = !notebooks.length;
    _articleGraph.notebooksLoaded = true;
  } catch (_error) {
    select.disabled = true;
  }
}

async function _articleGraphAddNotebook(notebookId) {
  if (!notebookId) return;
  try {
    const response = await fetch(`/api/notebooks/${encodeURIComponent(notebookId)}`, { cache: 'no-store' });
    if (!response.ok) throw new Error();
    const notebook = await response.json();
    const paths = (notebook?.sources || [])
      .map((source) => String(source?.source_path || '').trim())
      .filter(Boolean);
    if (!paths.length) {
      _articleGraphSetStatus(_ti('articleGraph.notebookEmpty'));
      return;
    }
    _articleGraphAdd(paths);
  } catch (_error) {
    _articleGraphSetStatus(_ti('articleGraph.notebookFailed'), 'error');
  }
}

function _articleGraphBindUiOnce() {
  if (_articleGraph.bound) return;
  _articleGraph.bound = true;
  const search = document.getElementById('article-graph-search');
  search?.addEventListener('input', () => _articleGraphRenderResults());
  search?.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    const first = document.querySelector('#article-graph-results [data-article-graph-path]');
    if (first) {
      _articleGraphAdd([first.dataset.articleGraphPath]);
      search.select();
    }
  });
  document.getElementById('article-graph-results')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-article-graph-path]');
    if (!button) return;
    const path = button.dataset.articleGraphPath;
    if (_articleGraph.paths.includes(path)) _articleGraphRemove(path);
    else _articleGraphAdd([path]);
  });
  document.getElementById('article-graph-selected')?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-article-graph-remove]');
    if (button) _articleGraphRemove(button.dataset.articleGraphRemove);
  });
  document.getElementById('article-graph-clear')?.addEventListener('click', () => _articleGraphClear());
  document.getElementById('article-graph-from-library')?.addEventListener('click', () => {
    _articleGraphAdd(_articleGraphLibrarySelectionPaths());
  });
  document.getElementById('article-graph-from-answer')?.addEventListener('click', () => {
    _articleGraphAdd(_articleGraphAnswerPaths());
  });
  const notebookSelect = document.getElementById('article-graph-notebook');
  notebookSelect?.addEventListener('change', async () => {
    const notebookId = notebookSelect.value;
    notebookSelect.value = '';
    await _articleGraphAddNotebook(notebookId);
  });
  document.getElementById('article-graph-form')?.addEventListener('submit', (event) => {
    event.preventDefault();
    buildArticleGraph();
  });
}

// Called each time the "Depuis des articles" view is shown.
function showArticleGraphView() {
  _articleGraphBindUiOnce();
  _articleGraphRender();
  _articleGraphLoadNotebooks();
}

async function buildArticleGraph() {
  const paths = _articleGraph.paths.filter(_articleGraphInLibrary);
  if (!paths.length) {
    _articleGraphSetStatus(_ti('articleGraph.pickFirst'), 'error');
    document.getElementById('article-graph-search')?.focus();
    return;
  }
  const first = _articleGraphItem(paths[0]);
  const title = paths.length === 1
    ? first.title
    : _ti('articleGraph.graphTitle', { count: paths.length });
  await _openCitationGraphFrom('/api/library/citation-graph', {
    emptyMsg: _ti(paths.length === 1 ? 'citationGraph.emptyArticle' : 'articleGraph.emptySelection'),
    scope: { kind: paths.length === 1 ? 'article' : 'selection', title },
    returnFocusEl: document.getElementById('article-graph-build'),
    embedded: 'articles',
    fetchOptions: {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths }),
    },
  });
}

// Library selection bar › "Graphe de citations".
function openArticleGraphFromLibrarySelection() {
  const paths = _articleGraphLibrarySelectionPaths();
  if (!paths.length) {
    showToast(_ti('library.selectionEmpty'), 2800);
    return;
  }
  if (paths.length > ARTICLE_GRAPH_MAX) {
    showToast(_ti('articleGraph.tooMany', { max: ARTICLE_GRAPH_MAX }), 4200, 'error');
    return;
  }
  _articleGraph.paths = [...paths];
  _articleGraphSetStatus('');
  switchTab('connections');
  setCartographyView('articles');
  buildArticleGraph();
}
