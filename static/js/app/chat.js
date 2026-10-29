//  CHAT
// ---------------------------------------------
function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    if (state.streaming) {
      _recordClientError(
        'chat.enter_ignored',
        'Enter ignored while a chat response is streaming.',
      );
      showToast(
        _tr('Une réponse est déjà en cours. Utilisez le bouton carré pour l’interrompre.'),
        2600,
        'info',
      );
      return;
    }
    sendMessage();
  }
}
function handleNbKey(e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault();
    if (_nb.streaming) {
      _recordClientError(
        'notebook.enter_ignored',
        'Enter ignored while a notebook response is streaming.',
      );
      showToast(
        _tr('Une réponse est déjà en cours. Utilisez le bouton carré pour l’interrompre.'),
        2600,
        'info',
      );
      return;
    }
    sendNbMessage();
  }
}
// #messages keeps room under its last line for the floating composer.
(function trackChatComposerHeight() {
  const composer = document.getElementById('chat-composer');
  const main = document.getElementById('chat-main');
  if (!composer || !main || typeof ResizeObserver !== 'function') return;
  const messages = document.getElementById('messages');
  new ResizeObserver(() => {
    const atBottom = messages
      && messages.scrollHeight - messages.scrollTop - messages.clientHeight < 24;
    main.style.setProperty('--chat-composer-h', `${Math.ceil(composer.offsetHeight)}px`);
    // A taller composer must not cover the line being read at the bottom.
    if (atBottom) messages.scrollTop = messages.scrollHeight;
  }).observe(composer);
})();

// Grows with the text up to the CSS max-height, then scrolls (like ChatGPT/Claude).
function autoResize(el) {
  if (!el) return;
  el.style.height = 'auto';
  const style = getComputedStyle(el);
  const borders = (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
  const max = parseFloat(style.maxHeight);
  const wanted = el.scrollHeight + borders;
  const limited = Number.isFinite(max) && max > 0 ? Math.min(wanted, max) : wanted;
  el.style.height = limited + 'px';
  el.style.overflowY = wanted > limited + 1 ? 'auto' : 'hidden';
}

// Composer menus (model chip, Options): one open at a time. Escape or a click
// outside closes it; Escape gives the focus back to its button.
let _composerMenu = null;

function closeComposerMenu({ focus = false } = {}) {
  const current = _composerMenu;
  if (!current) return;
  _composerMenu = null;
  current.menu.hidden = true;
  current.button.setAttribute('aria-expanded', 'false');
  document.removeEventListener('pointerdown', current.onPointerDown, true);
  document.removeEventListener('keydown', current.onKeydown, true);
  if (focus) current.button.focus();
}

function toggleComposerMenu(buttonId, menuId, render = null) {
  const button = document.getElementById(buttonId);
  const menu = document.getElementById(menuId);
  if (!button || !menu) return;
  const wasOpen = _composerMenu?.menu === menu;
  closeComposerMenu();
  if (wasOpen) return;
  if (typeof render === 'function') render(menu);
  menu.hidden = false;
  button.setAttribute('aria-expanded', 'true');
  const onPointerDown = (event) => {
    if (!menu.contains(event.target) && !button.contains(event.target)) closeComposerMenu();
  };
  const onKeydown = (event) => {
    if (event.key !== 'Escape') return;
    event.stopPropagation();
    closeComposerMenu({ focus: true });
  };
  document.addEventListener('pointerdown', onPointerDown, true);
  document.addEventListener('keydown', onKeydown, true);
  _composerMenu = { button, menu, onPointerDown, onKeydown };
  menu.querySelector('[aria-checked="true"], button:not([disabled]), summary, input')?.focus();
}

function toggleChatOptions() {
  toggleComposerMenu('chat-options-btn', 'chat-options-menu');
  if (!document.getElementById('chat-options-menu')?.hidden) loadChatArticleSearch();
}

// Count active source filters and figure reading.
function updateChatOptionsCount() {
  const counter = document.getElementById('chat-options-count');
  if (!counter) return;
  const filled = ['chat-tags-filter', 'chat-collections-filter']
    .filter((id) => String(document.getElementById(id)?.value || '').trim()).length;
  const count = filled + (_chatArticleScopePaths().length ? 1 : 0)
    + (state.chatForceVisualOnce ? 1 : 0);
  counter.hidden = !count;
  counter.textContent = count ? ` · ${count}` : '';
  document.getElementById('chat-options-btn')?.classList.toggle('is-active', count > 0);
}
window.closeComposerMenu = closeComposerMenu;
window.updateChatOptionsCount = updateChatOptionsCount;

let _chatArticleSearchItems = [];
let _chatArticleSearchRevision = 0;
let _chatArticleSearchStatus = '';

async function loadChatArticleSearch() {
  const revision = ++_chatArticleSearchRevision;
  _chatArticleSearchItems = [];
  _chatArticleSearchStatus = 'chat.articleSearchLoading';
  renderChatArticleSearch();
  try {
    const data = await fetchAllPaginated('/api/library?indexed_only=true', ['rows', 'paths', 'metadata'], {
      limit: 1000,
      fallback: _ti('chat.articleSearchError'),
    });
    if (revision !== _chatArticleSearchRevision) return;
    _chatArticleSearchItems = data.paths.flatMap((path, index) => {
      const row = data.rows[index];
      if (!path || !Array.isArray(row) || data.metadata[index]?.rag_excluded) return [];
      return [{ path: String(path), title: String(row[0] || path), author: row[1], year: row[2] }];
    });
    _chatArticleSearchStatus = '';
  } catch {
    if (revision !== _chatArticleSearchRevision) return;
    _chatArticleSearchStatus = 'chat.articleSearchError';
  }
  renderChatArticleSearch();
}

// Case- and accent-insensitive form of a string, one entry per source
// character so a match can be mapped back for highlighting.
function _chatArticleFold(value) {
  return Array.from(String(value || ''), (char) => char.normalize('NFD')
    .replace(/\p{M}/gu, '').toLocaleLowerCase());
}

// Append text to a node with every search term wrapped in <mark>.
function _appendChatArticleHighlight(node, text, terms) {
  const source = Array.from(String(text || ''));
  const folded = _chatArticleFold(text);
  const flat = folded.join('');
  const owner = folded.flatMap((part, index) => Array(part.length).fill(index));
  const marked = new Array(source.length).fill(false);
  terms.forEach((term) => {
    let at = flat.indexOf(term);
    while (at >= 0) {
      for (let i = at; i < at + term.length; i += 1) marked[owner[i]] = true;
      at = flat.indexOf(term, at + term.length);
    }
  });
  let run = '';
  let runMarked = false;
  const flush = () => {
    if (!run) return;
    if (runMarked) {
      const mark = document.createElement('mark');
      mark.textContent = run;
      node.appendChild(mark);
    } else {
      node.appendChild(document.createTextNode(run));
    }
    run = '';
  };
  source.forEach((char, index) => {
    if (marked[index] !== runMarked) {
      flush();
      runMarked = marked[index];
    }
    run += char;
  });
  flush();
}

function renderChatArticleSearch() {
  const input = document.getElementById('chat-article-search');
  const results = document.getElementById('chat-article-search-results');
  const status = document.getElementById('chat-article-search-status');
  if (!input || !results || !status) return;
  const normalize = (value) => _chatArticleFold(value).join('');
  const terms = normalize(input.value).trim().split(/\s+/).filter(Boolean);
  results.replaceChildren();
  results.hidden = !terms.length;
  if (_chatArticleSearchStatus || !terms.length) {
    status.dataset.i18n = _chatArticleSearchStatus || 'chat.articleSearchHint';
    status.textContent = _ti(status.dataset.i18n);
    return;
  }
  // Every word must appear in the title, the authors or the year; papers
  // whose title holds every word come first.
  const matches = _chatArticleSearchItems.flatMap((item) => {
    const title = normalize(item.title);
    const haystack = `${title} ${normalize(item.author)} ${normalize(item.year)}`;
    if (!terms.every((term) => haystack.includes(term))) return [];
    return [{ item, inTitle: terms.every((term) => title.includes(term)) }];
  }).sort((a, b) => Number(b.inTitle) - Number(a.inTitle)).map(({ item }) => item);
  status.dataset.i18n = matches.length > 20 ? 'chat.articleSearchRefine'
    : matches.length ? 'chat.articleSearchSelect' : 'chat.articleSearchEmpty';
  status.textContent = _ti(status.dataset.i18n);
  matches.slice(0, 20).forEach((item) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chat-article-search-result';
    button.title = item.path;
    const title = document.createElement('strong');
    _appendChatArticleHighlight(title, item.title, terms);
    const meta = document.createElement('span');
    const metaText = [item.author, item.year].filter((value) => value && value !== '-').join(' · ');
    _appendChatArticleHighlight(meta, metaText || item.path, terms);
    button.append(title, meta);
    button.addEventListener('click', () => {
      setChatArticleScope(item.path, item.title);
      input.value = '';
      renderChatArticleSearch();
      closeComposerMenu();
      document.getElementById('msg-input')?.focus();
    });
    results.appendChild(button);
  });
}

function applyChatHint(text, hintAction = '') {
  const input = document.getElementById('msg-input');
  if (!input) return;
  const value = String(text || '');
  input.value = value;
  input.dataset.hintAction = String(hintAction || '').trim().toLowerCase();
  input.dataset.hintText = value;
  autoResize(input);
  closeComposerMenu();
  input.focus();
  const placeholderIndex = value.indexOf('...');
  if (placeholderIndex >= 0 && typeof input.setSelectionRange === 'function') {
    input.setSelectionRange(placeholderIndex, placeholderIndex + 3);
  } else if (typeof input.setSelectionRange === 'function') {
    input.setSelectionRange(value.length, value.length);
  }
}

function resolveChatHintAction(input, message) {
  const action = String(input?.dataset?.hintAction || '').trim().toLowerCase();
  const template = String(input?.dataset?.hintText || '');
  if (!action || !template) return '';
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const current = normalize(message);
  const placeholder = template.indexOf('...');
  if (placeholder < 0) {
    const original = normalize(template);
    return current === original || current.startsWith(original + ' ') ? action : '';
  }
  const prefix = normalize(template.slice(0, placeholder));
  const suffix = normalize(template.slice(placeholder + 3));
  // Keep the selected action while filling its subject, but not after replacing
  // the request with a different task (e.g. bibliography -> writing).
  return prefix && current.startsWith(prefix) && (!suffix || current.endsWith(suffix))
    ? action
    : '';
}

function applyChatHintByKey(key, hintAction = '') {
  const fallback = String(key || '');
  const text = typeof window.t === 'function' ? window.t(fallback) : fallback;
  applyChatHint(text, hintAction);
}

function _clipboardHasImage(clipboardData) {
  if (!clipboardData) return false;
  const items = Array.from(clipboardData.items || []);
  if (items.some((item) => String(item?.type || '').toLowerCase().startsWith('image/'))) return true;
  const files = Array.from(clipboardData.files || []);
  return files.some((file) => String(file?.type || '').toLowerCase().startsWith('image/'));
}

function _notifyImagePasteBlocked() {
  showToast(_tr("Le collage d'images est désactivé dans les champs de texte."), 3200, 'info');
}

function _blockImageOnlyPasteInTextField(event) {
  const clipboardData = event.clipboardData;
  if (!_clipboardHasImage(clipboardData)) return;
  const text = String(clipboardData?.getData('text/plain') || '').trim();
  if (text) return;
  event.preventDefault();
  _notifyImagePasteBlocked();
}

function initTextFieldPasteGuards() {
  document.querySelectorAll('textarea, input[type="text"]').forEach((field) => {
    if (field.dataset.imagePasteGuard === '1') return;
    field.dataset.imagePasteGuard = '1';
    field.addEventListener('paste', _blockImageOnlyPasteInTextField);
  });
}

function shouldShowAllAssistantSources(answerOrigin) {
  const value = String(answerOrigin || '').trim();
  return value === 'paper_list' || value === 'article_discovery';
}


function resetChatSourceRegistry() {
  state.nextChatSourceNumber = 1;
  state.chatSourceNumberByKey = new Map();
  state.chatSourceKeyByNumber = new Map();
}

function _normalizeChatSourceIdentityPart(value) {
  return String(value ?? '').trim().replace(/\s+/g, ' ').toLowerCase();
}

function chatSourceIdentityKey(source) {
  if (!source || typeof source !== 'object') return '';

  const chunkUid = _normalizeChatSourceIdentityPart(source.chunk_uid);
  if (chunkUid) return `chunk_uid:${chunkUid}`;

  const articleId = _normalizeChatSourceIdentityPart(source.article_id);
  const chunkId = _normalizeChatSourceIdentityPart(source.chunk_id);
  if (articleId && chunkId) return `article_chunk:${articleId}:${chunkId}`;

  const sourcePath = _normalizeChatSourceIdentityPart(source.source_path);
  const page = _normalizeChatSourceIdentityPart(source.page);
  const title = _normalizeChatSourceIdentityPart(source.title);
  if (sourcePath && (page || title)) {
    return `source_path:${sourcePath}|${page}|${title}`;
  }

  const file = _normalizeChatSourceIdentityPart(source.file);
  const snippet = _normalizeChatSourceIdentityPart(source.snippet || source.content).slice(0, 240);
  if (file && (page || title || snippet)) {
    return `file:${file}|${page}|${title}|${snippet}`;
  }

  const rawRefLabel = _normalizeChatSourceIdentityPart(source.raw_ref_label || source.ref_label);
  const index = _normalizeChatSourceIdentityPart(source.index);
  if (rawRefLabel || index || title || snippet) {
    return `fallback:${rawRefLabel}|${index}|${title}|${snippet}`;
  }
  return '';
}

function ensureChatSourceRefLabel(source, fallbackIndex = 1) {
  const key = chatSourceIdentityKey(source);
  if (!key) {
    return sourceRefLabel(source, fallbackIndex);
  }
  let sourceNumber = state.chatSourceNumberByKey.get(key);
  if (!Number.isFinite(sourceNumber)) {
    sourceNumber = Number.isFinite(Number(state.nextChatSourceNumber))
      ? Math.max(1, parseInt(state.nextChatSourceNumber, 10))
      : 1;
    state.chatSourceNumberByKey.set(key, sourceNumber);
    state.chatSourceKeyByNumber.set(sourceNumber, key);
    state.nextChatSourceNumber = sourceNumber + 1;
  }
  return `S${sourceNumber}`;
}

function normalizeChatSourcesForRender(sources = []) {
  if (!Array.isArray(sources)) return [];
  return sources.map((source, index) => {
    if (!source || typeof source !== 'object') return source;
    const fallbackIndex = index + 1;
    const rawRefLabel = String(
      source.raw_ref_label
      || source.ref_label
      || `S${fallbackIndex}`
    ).trim().toUpperCase();
    const chatRefLabel = ensureChatSourceRefLabel(
      { ...source, raw_ref_label: rawRefLabel },
      fallbackIndex,
    );
    return {
      ...source,
      raw_ref_label: rawRefLabel,
      chat_ref_label: chatRefLabel,
      ref_label: chatRefLabel,
    };
  });
}

function collectSourceLabelAliases(source, fallbackIndex = 1) {
  const labels = new Set();
  const addLabel = (value) => {
    const normalized = normalizeAssistantCitationLabel(value);
    if (normalized) {
      labels.add(normalized);
      return;
    }
    const raw = String(value || '').trim().toUpperCase();
    if (raw) labels.add(raw);
  };

  addLabel(source?.chat_ref_label);
  addLabel(source?.ref_label);
  addLabel(source?.raw_ref_label);
  const refLabel = sourceRefLabel(source, fallbackIndex);
  addLabel(refLabel);
  // Notebook sources are cited [2], but models often write [S2].
  if (/^\d+$/.test(refLabel)) labels.add(`S${refLabel}`);

  const hasExplicitLabel = Boolean(
    String(source?.chat_ref_label || source?.ref_label || source?.raw_ref_label || '').trim()
  );
  const numericIndex = Number.isFinite(Number(source?.index)) ? Number(source.index) : NaN;
  if (!hasExplicitLabel && Number.isFinite(numericIndex) && numericIndex >= 1) {
    labels.add(String(parseInt(numericIndex, 10)));
    labels.add(`S${parseInt(numericIndex, 10)}`);
  }
  return labels;
}

function mergeCitationAnchorsIntoSources(sources, anchorsMap) {
  if (!Array.isArray(sources)) return [];
  const map = anchorsMap && typeof anchorsMap === 'object' ? anchorsMap : {};
  const labelKeys = Object.keys(map);
  if (!labelKeys.length) return sources;
  return sources.map((source, index) => {
    if (!source || typeof source !== 'object') return source;
    const aliases = collectSourceLabelAliases(source, index + 1);
    let collected = [];
    aliases.forEach((alias) => {
      const list = map[alias];
      if (Array.isArray(list)) collected = collected.concat(list);
    });
    if (!collected.length) return source;
    const seen = new Set();
    const dedup = [];
    collected.forEach((anchor) => {
      const key = `${anchor?.chunk_uid || ''}::${anchor?.text || ''}`;
      if (seen.has(key)) return;
      seen.add(key);
      dedup.push(anchor);
    });
    return { ...source, anchors: dedup };
  });
}

function extractAssistantCitationLabels(text) {
  const labels = [];
  const seen = new Set();
  const value = String(text || '');
  const matches = value.matchAll(/\[([^\]]+)\]/g);
  for (const match of matches) {
    const raw = String(match[1] || '');
    const parts = raw.split(',').map((part) => part.trim()).filter(Boolean);
    for (const part of parts) {
      const labelMatch = part.match(/^S(\d+)$/i);
      let label = '';
      if (labelMatch) {
        label = `S${parseInt(labelMatch[1], 10)}`;
      } else if (/^\d+$/.test(part)) {
        label = String(parseInt(part, 10));
      } else if (/^[A-Za-z0-9:_\-.]+:\d+$/i.test(part)) {
        label = part.toUpperCase();
      } else {
        continue;
      }
      if (seen.has(label)) continue;
      seen.add(label);
      labels.push(label);
    }
  }
  return labels;
}

function filterSourcesByCitationLabels(sources, labels) {
  const rows = Array.isArray(sources) ? sources : [];
  const orderedLabels = Array.isArray(labels) ? labels : [];
  if (!rows.length) return [];
  if (!orderedLabels.length) return rows;

  const byLabel = new Map();
  rows.forEach((row, index) => {
    collectSourceLabelAliases(row, index + 1).forEach((label) => {
      if (label && !byLabel.has(label)) byLabel.set(label, row);
    });
  });

  const filtered = orderedLabels
    .map((label) => byLabel.get(String(label).trim().toUpperCase()))
    .filter(Boolean);

  if (!filtered.length) return rows;
  if (filtered.length < Math.min(orderedLabels.length, rows.length)) return rows;
  return filtered;
}

function isChatSourceRecord(source) {
  if (!source || typeof source !== 'object') return false;
  return Boolean(
    String(source.ref_label || '').trim()
    || String(source.file || '').trim()
    || String(source.source_path || '').trim()
    || String(source.title || '').trim()
    || String(source.content || '').trim()
    || String(source.snippet || '').trim()
    || String(source.page || '').trim()
  );
}

function sourceDisplayTitle(source) {
  return String(source?.title || '').trim()
    || String(source?.file || '').trim()
    || String(source?.source_path || '').trim()
    || 'Source';
}

function sourceDisplayAuthors(source) {
  return String(source?.authors || '').trim() || String(source?.author || '').trim();
}

function sourceMetaLine(source) {
  const metaParts = [];
  const authors = sourceDisplayAuthors(source);
  if (authors) metaParts.push(authors);
  if (source?.year) metaParts.push(`(${source.year})`);
  if (source?.journal) metaParts.push(String(source.journal).trim());
  return metaParts.join(' · ') || String(source?.citation || '').trim();
}

function sourceLocationLine(source) {
  const bits = [];
  if (source?.file) bits.push(String(source.file).trim());
  if (source?.source_path) bits.push(String(source.source_path).trim());
  if (source?.doi) bits.push(`DOI ${String(source.doi).trim()}`);
  return bits.join(' · ');
}

function sourceRefLabel(source, fallbackIndex = 1) {
  const chatRefLabel = String(source?.chat_ref_label || '').trim();
  if (chatRefLabel) return chatRefLabel.toUpperCase();
  const explicit = String(source?.ref_label || '').trim();
  if (explicit) return explicit.toUpperCase();
  const numericIndex = Number(source?.index);
  if (Number.isFinite(numericIndex) && numericIndex >= 1) return `S${parseInt(numericIndex, 10)}`;
  const notebookLike = Boolean(
    String(source?.source_path || '').trim()
    || String(source?.content || '').trim()
  ) && !String(source?.file || '').trim();
  return notebookLike ? String(fallbackIndex) : `S${fallbackIndex}`;
}

function sourceCompactMeta(source) {
  const parts = [];
  const author = String(source?.author || '').trim();
  if (author) parts.push(author);
  if (source?.year) parts.push(String(source.year).trim());
  if (!parts.length) {
    const authors = sourceDisplayAuthors(source);
    if (authors) parts.push(authors.split(',')[0].trim());
  }
  return parts.join(' · ');
}

function sourceOpenLibrary(source) {
  const path = assistantCitationSourcePath(source);
  if (!path) return;
  goToSource(path);
}

const sourceZoteroUriCache = new Map();

function sourceActionPath(source) {
  return assistantCitationSourcePath(source)
    .replace(/\s*[\\/]\s*/g, '/')
    .trim();
}

function filterNextQuestionBySource(source) {
  const path = sourceActionPath(source);
  if (!path) {
    showToast(_tr('Cette source ne peut pas être utilisée comme filtre.'), 3200, 'error');
    return;
  }
  setChatArticleScope(path, sourceDisplayTitle(source), { once: true });
  showToast(_tr('La prochaine question sera limitée à cette référence.'), 3200, 'success');
  document.getElementById('msg-input')?.focus();
}

function loadSourceZoteroUri(source) {
  const directUri = String(source?.zotero_select_uri || '').trim();
  if (directUri) return Promise.resolve(directUri);

  const path = sourceActionPath(source);
  if (!path) return Promise.resolve('');
  if (sourceZoteroUriCache.has(path)) return sourceZoteroUriCache.get(path);

  const request = fetchJsonOrThrow(
    '/api/item-citations?path=' + encodeURIComponent(path),
    {},
    _tr('Chargement du lien Zotero impossible')
  )
    .then((citations) => {
      const uri = String(citations?.zotero_select_uri || '').trim();
      if (!uri) sourceZoteroUriCache.delete(path);
      return uri;
    })
    .catch((error) => {
      sourceZoteroUriCache.delete(path);
      throw error;
    });
  sourceZoteroUriCache.set(path, request);
  return request;
}

async function hydrateSourceZoteroLink(link, source) {
  if (!link) return;
  try {
    const uri = await loadSourceZoteroUri(source);
    if (!link.isConnected) return;
    if (!uri) {
      link.dataset.zoteroState = 'unavailable';
      link.title = _tr('Lien Zotero indisponible pour cet article.');
      return;
    }
    link.href = uri;
    link.setAttribute('aria-disabled', 'false');
    link.removeAttribute('tabindex');
    link.dataset.zoteroState = 'ready';
  } catch (error) {
    if (!link.isConnected) return;
    link.dataset.zoteroState = 'error';
    link.title = error?.message || _tr('Chargement du lien Zotero impossible');
  }
}

function refreshRenderedSourceZoteroLinks() {
  sourceZoteroUriCache.clear();
  if (!isZoteroInUse()) return;
  document.querySelectorAll('a.source-open-zotero-btn[data-source-path]').forEach((link) => {
    const path = String(link.dataset.sourcePath || '').trim();
    if (!path) return;
    link.href = '#';
    link.setAttribute('aria-disabled', 'true');
    link.tabIndex = -1;
    link.dataset.zoteroState = 'loading';
    link.title = '';
    hydrateSourceZoteroLink(link, {
      file: path,
      zotero_select_uri: link.dataset.zoteroUri || '',
    });
  });
}

function flashSourceCard(card) {
  if (!card) return;
  card.classList.remove('is-highlighted');
  void card.offsetWidth;
  card.classList.add('is-highlighted');
  if (card._sourceHighlightTimer) window.clearTimeout(card._sourceHighlightTimer);
  card._sourceHighlightTimer = window.setTimeout(() => {
    card.classList.remove('is-highlighted');
    card._sourceHighlightTimer = null;
  }, 1800);
}

function revealCitationSource(label, root = null) {
  const normalizedLabel = normalizeAssistantCitationLabel(label);
  if (!normalizedLabel) return;
  window.dispatchEvent(new CustomEvent('bibliobrain:milestone', { detail: { name: 'source-opened' } }));

  const bubble = root?.closest('.bubble, .nb-bubble') || null;
  if (bubble?.classList.contains('bubble') && Array.isArray(bubble._chatSources)) {
    openChatSourcesPanel(bubble, { label: normalizedLabel, trigger: root });
    return;
  }
  if (bubble) {
    const inlineBlock = bubble.querySelector('.assistant-sources-block');
    if (inlineBlock) inlineBlock.open = true;
    const inlineCard = Array.from(
      bubble.querySelectorAll('.assistant-source-item[data-source-label]')
    ).find((node) => String(node.dataset.sourceLabel || '').trim().toUpperCase() === normalizedLabel);
    if (inlineCard) {
      flashSourceCard(inlineCard);
      inlineCard.scrollIntoView({ behavior: preferredScrollBehavior(), block: 'nearest' });
      return;
    }
  }

  if (bubble?.classList.contains('nb-bubble')) {
    const notebookCard = Array.from(
      document.querySelectorAll('#nb-sources-list .nb-source-item[data-source-label]')
    ).find((node) => String(node.dataset.sourceLabel || '').trim().toUpperCase() === normalizedLabel);
    if (notebookCard) {
      flashSourceCard(notebookCard);
      notebookCard.scrollIntoView({ behavior: preferredScrollBehavior(), block: 'nearest' });
      return;
    }
  }

}

function sourcePageLabel(source) {
  const page = String(source?.page ?? '').trim();
  // format_sources_api sends "?" when the chunk has no page.
  if (!page || page === '?') return '';
  const locationLabel = String(source?.location_label || '').trim();
  if (locationLabel) return locationLabel;
  const contentType = String(source?.content_type || '').trim();
  const pdfPage = sourcePageNumber(source);
  return contentType === 'presentation_slide' ? `diapo ${page}`
    : contentType === 'ebook_chapter' ? `chap. ${page}`
      : contentType === 'document_section' ? `section ${page}`
        : pdfPage ? `p.${pdfPage}` : `p.${page}`;
}

function createSourceCard(source, options = {}) {
  const fallbackIndex = Number(options.fallbackIndex || 1);
  const card = document.createElement('article');
  card.className = options.inline ? 'source-item assistant-source-item' : 'source-item';
  const refLabel = sourceRefLabel(source, fallbackIndex);
  card.dataset.sourceLabel = refLabel;

  const top = document.createElement('div');
  top.className = 'source-topline';

  const refBadge = document.createElement('span');
  refBadge.className = 'source-ref-badge';
  refBadge.textContent = refLabel;
  top.appendChild(refBadge);

  const body = document.createElement('div');
  body.className = 'source-content';

  const heading = document.createElement('div');
  heading.className = 'source-heading';

  const titleRow = document.createElement('div');
  titleRow.className = 'source-title-row';

  const title = document.createElement('div');
  title.className = 'source-title';
  title.textContent = sourceDisplayTitle(source);
  titleRow.appendChild(title);

  const pageLabel = sourcePageLabel(source);
  if (pageLabel) {
    const pageBadge = document.createElement('span');
    pageBadge.className = 'source-page-badge';
    pageBadge.textContent = pageLabel;
    titleRow.appendChild(pageBadge);
  }
  heading.appendChild(titleRow);

  const meta = sourceMetaLine(source);
  if (meta) {
    const metaEl = document.createElement('div');
    metaEl.className = 'source-meta';
    metaEl.textContent = meta;
    heading.appendChild(metaEl);
  }

  const location = sourceLocationLine(source);
  if (location) {
    const locationEl = document.createElement('div');
    locationEl.className = 'source-location';
    locationEl.textContent = location;
    heading.appendChild(locationEl);
  }
  body.appendChild(heading);

  const snippetValue = String(source?.snippet || '').trim();
  const contentValue = String(source?.content || '').trim();
  const displaySnippet = snippetValue || contentValue;
  if (displaySnippet) {
    const snippet = document.createElement('div');
    snippet.className = 'source-snippet';
    const clipped = displaySnippet.length > 360
      ? `${displaySnippet.slice(0, 360).trim()}…`
      : displaySnippet;
    snippet.textContent = /[.?!…]$/.test(clipped) ? clipped : `${clipped}…`;
    body.appendChild(snippet);
  }

  const anchorBox = createSourceAnchorBox(source);
  if (anchorBox) body.appendChild(anchorBox);

  const actions = document.createElement('div');
  actions.className = 'source-actions';
  appendSourceActions(actions, source);
  body.appendChild(actions);

  top.appendChild(body);
  card.appendChild(top);
  card.addEventListener('click', () => flashSourceCard(card));
  return card;
}

// The anchor is the sentence of the cited passage closest to the answer
// (fuzzy match, src/rag/citation_anchors.py). The score is that closeness,
// not a check that the sentence supports the claim: label it as such.
function createSourceAnchorBox(source) {
  const anchors = Array.isArray(source?.anchors) ? source.anchors : [];
  const lines = anchors.slice(0, 3).filter((anchor) => String(anchor?.text || '').trim());
  if (!lines.length) return null;
  const anchorBox = document.createElement('div');
  anchorBox.className = 'source-anchor-box';
  const heading = document.createElement('div');
  heading.className = 'source-anchor-heading';
  heading.textContent = lines.length > 1 ? _tr('Phrases citées') : _tr('Phrase citée');
  anchorBox.appendChild(heading);
  lines.forEach((anchor) => {
    const line = document.createElement('div');
    line.className = 'source-anchor-line';
    line.textContent = `« ${String(anchor.text).trim()} »`;
    const score = Number(anchor?.score);
    if (Number.isFinite(score)) {
      const pill = document.createElement('span');
      pill.className = 'source-anchor-score';
      pill.textContent = _ti('chat.sources.closeness', { percent: (score * 100).toFixed(0) });
      pill.title = _ti('chat.sources.closenessHelp');
      line.appendChild(pill);
    }
    anchorBox.appendChild(line);
  });
  return anchorBox;
}

// The index stores a PDF page as pypdf numbers it, from 0, and its label
// ("Page 126") from 1. The PDF viewer counts from 1 like the label.
function sourcePageNumber(source) {
  const contentType = String(source?.content_type || '').trim();
  // Slides, chapters and sections are numbered in their own units, not PDF pages.
  if (['presentation_slide', 'ebook_chapter', 'document_section'].includes(contentType)) return 0;
  const labelled = String(source?.location_label || '').trim().match(/^page\s+(\d+)$/i);
  if (labelled) {
    const page = parseInt(labelled[1], 10);
    return page >= 1 ? page : 0;
  }
  const raw = String(source?.page ?? '').trim();
  if (!/^\d+$/.test(raw)) return 0;
  return parseInt(raw, 10) + 1;
}

function sourceOpenDocumentUrl(source, path = sourceActionPath(source)) {
  const page = /\.pdf$/i.test(path) ? sourcePageNumber(source) : 0;
  return '/api/open-pdf?path=' + encodeURIComponent(path) + (page ? `#page=${page}` : '');
}

function appendSourceActions(actions, source) {
  const path = sourceActionPath(source);
  if (path) {
    const libraryBtn = document.createElement('button');
    libraryBtn.type = 'button';
    libraryBtn.className = 'source-open-btn';
    libraryBtn.textContent = _tr('Bibliothèque');
    libraryBtn.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      sourceOpenLibrary(source);
    });
    actions.appendChild(libraryBtn);

    const filterBtn = document.createElement('button');
    filterBtn.type = 'button';
    filterBtn.className = 'source-open-btn source-filter-btn';
    filterBtn.dataset.sourcePath = path;
    const filterActive = Boolean(
      state.chatArticleScope?.once
      && String(state.chatArticleScope?.path || '') === path
    );
    filterBtn.classList.toggle('is-active', filterActive);
    filterBtn.setAttribute('aria-pressed', filterActive ? 'true' : 'false');
    filterBtn.textContent = _tr('Filtrer la prochaine question');
    filterBtn.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      filterNextQuestionBySource(source);
    });
    actions.appendChild(filterBtn);

    const pdfBtn = document.createElement('a');
    pdfBtn.className = 'source-open-btn source-open-pdf-btn';
    pdfBtn.href = sourceOpenDocumentUrl(source, path);
    pdfBtn.target = '_blank';
    pdfBtn.rel = 'noopener';
    pdfBtn.textContent = /\.pdf$/i.test(path) ? _tr('Ouvrir le PDF') : _tr('Ouvrir le document');
    pdfBtn.addEventListener('click', (event) => event.stopPropagation());
    actions.appendChild(pdfBtn);

    if (isZoteroInUse()) {
      const zoteroBtn = document.createElement('a');
      zoteroBtn.className = 'source-open-btn source-open-zotero-btn';
      zoteroBtn.href = '#';
      zoteroBtn.setAttribute('aria-disabled', 'true');
      zoteroBtn.tabIndex = -1;
      zoteroBtn.dataset.zoteroState = 'loading';
      zoteroBtn.dataset.sourcePath = path;
      zoteroBtn.dataset.zoteroUri = String(source?.zotero_select_uri || '').trim();
      zoteroBtn.textContent = _tr('Afficher dans Zotero');
      zoteroBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        if (zoteroBtn.getAttribute('aria-disabled') === 'true') {
          event.preventDefault();
          const message = zoteroBtn.dataset.zoteroState === 'loading'
            ? _tr('Lien Zotero en cours de chargement.')
            : _tr('Lien Zotero indisponible pour cet article.');
          showToast(message, 3200, 'error');
        }
      });
      actions.appendChild(zoteroBtn);
      hydrateSourceZoteroLink(zoteroBtn, source);
    }
  }
}

function renderSourceCards(container, sources, options = {}) {
  if (!container) return;
  const rows = Array.isArray(sources) ? sources.filter(isChatSourceRecord) : [];
  rows.forEach((source, index) => {
    container.appendChild(createSourceCard(source, {
      inline: Boolean(options.inline),
      fallbackIndex: index + 1,
    }));
  });
}

// ---------------------------------------------
//  SOURCES PANEL: the sources of one chat answer, beside the conversation.
//  A citation opens it on its passage; "N sources" opens the full list.
//  Notebook and drafting answers keep their inline source cards.
// ---------------------------------------------
const chatSourcesPanel = {
  bubble: null,
  selectedIndex: -1,
  view: 'detail',
  trigger: null,
};

function _chatSourcesPanelEl() {
  let panel = document.getElementById('chat-sources-panel');
  if (panel) return panel;
  const layout = document.getElementById('chat-layout');
  if (!layout) return null;
  const backdrop = document.createElement('div');
  backdrop.id = 'chat-sources-backdrop';
  backdrop.setAttribute('aria-hidden', 'true');
  layout.appendChild(backdrop);
  panel = document.createElement('aside');
  panel.id = 'chat-sources-panel';
  panel.hidden = true;
  panel.setAttribute('aria-labelledby', 'chat-sources-title');
  panel.innerHTML = `
    <div class="csp-head">
      <h2 id="chat-sources-title" tabindex="-1"></h2>
      <button type="button" class="csp-close">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    </div>
    <div class="csp-views" role="group">
      <button type="button" class="csp-view-btn" data-csp-view="detail"></button>
      <button type="button" class="csp-view-btn" data-csp-view="all"></button>
    </div>
    <div class="csp-body"></div>`;
  panel.querySelector('.csp-close').addEventListener('click', () => closeChatSourcesPanel());
  panel.querySelectorAll('.csp-view-btn').forEach((button) => {
    button.addEventListener('click', () => {
      chatSourcesPanel.view = button.dataset.cspView === 'all' ? 'all' : 'detail';
      renderChatSourcesPanel();
    });
  });
  panel.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      closeChatSourcesPanel();
    }
  });
  layout.appendChild(panel);
  // Capture outside clicks even when a conversation control stops bubbling.
  // The opening click has already passed this phase, so it keeps the panel open.
  document.addEventListener('click', (event) => {
    if (panel.hidden || !window.matchMedia('(max-width: 1180px)').matches) return;
    if (panel.contains(event.target)) return;
    closeChatSourcesPanel({ restoreFocus: event.target === backdrop });
  }, true);
  return panel;
}

function _chatSourcesForBubble(bubble) {
  return Array.isArray(bubble?._chatSources) ? bubble._chatSources : [];
}

function _chatSourceIndexForLabel(sources, label) {
  const wanted = normalizeAssistantCitationLabel(label) || String(label || '').trim().toUpperCase();
  if (!wanted) return -1;
  return sources.findIndex((source, index) => collectSourceLabelAliases(source, index + 1).has(wanted));
}

function openChatSourcesPanel(bubble, options = {}) {
  const sources = _chatSourcesForBubble(bubble);
  const panel = _chatSourcesPanelEl();
  if (!panel || !sources.length) return;
  const index = options.label ? _chatSourceIndexForLabel(sources, options.label) : -1;
  chatSourcesPanel.bubble = bubble;
  chatSourcesPanel.selectedIndex = index >= 0 ? index : 0;
  chatSourcesPanel.view = index >= 0 ? 'detail' : (options.view || 'all');
  chatSourcesPanel.trigger = options.trigger || null;
  panel.hidden = false;
  document.getElementById('chat-layout')?.classList.add('has-sources-panel');
  renderChatSourcesPanel();
  window.dispatchEvent(new CustomEvent('bibliobrain:milestone', { detail: { name: 'source-opened' } }));
  panel.querySelector('#chat-sources-title')?.focus({ preventScroll: true });
}

function closeChatSourcesPanel(options = {}) {
  const panel = document.getElementById('chat-sources-panel');
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  document.getElementById('chat-layout')?.classList.remove('has-sources-panel');
  _markActiveChatCitation(null, '');
  const bubble = chatSourcesPanel.bubble;
  let trigger = chatSourcesPanel.trigger;
  chatSourcesPanel.bubble = null;
  chatSourcesPanel.trigger = null;
  if (options.restoreFocus === false) return;
  // A re-rendered answer replaced the original link: use its successor.
  if (!trigger?.isConnected && bubble?.isConnected) {
    const label = String(trigger?.dataset?.sourceLabel || '').trim().toUpperCase();
    trigger = Array.from(bubble.querySelectorAll('a[data-source-label]'))
      .find((node) => String(node.dataset.sourceLabel || '').trim().toUpperCase() === label)
      || bubble.querySelector('.assistant-sources-open');
  }
  if (trigger?.isConnected) trigger.focus({ preventScroll: true });
}

// Keeps the panel in step with the answer it shows: a re-render (citation
// anchors arriving after the stream, debug metrics) refreshes it, a removed
// answer (new chat, other conversation) closes it.
function syncChatSourcesPanel(bubble) {
  if (!chatSourcesPanel.bubble) return;
  if (bubble && bubble !== chatSourcesPanel.bubble) return;
  if (!chatSourcesPanel.bubble.isConnected || !_chatSourcesForBubble(chatSourcesPanel.bubble).length) {
    closeChatSourcesPanel({ restoreFocus: false });
    return;
  }
  renderChatSourcesPanel();
}

function _markActiveChatCitation(bubble, label) {
  document.querySelectorAll('#messages .is-source-active').forEach((node) => node.classList.remove('is-source-active'));
  if (!bubble || !label) return;
  bubble.querySelectorAll('[data-source-label]').forEach((node) => {
    if (String(node.dataset.sourceLabel || '').trim().toUpperCase() === label) node.classList.add('is-source-active');
  });
}

function renderChatSourcesPanel() {
  const panel = document.getElementById('chat-sources-panel');
  const bubble = chatSourcesPanel.bubble;
  const sources = _chatSourcesForBubble(bubble);
  if (!panel || !sources.length) return;
  if (chatSourcesPanel.selectedIndex >= sources.length) chatSourcesPanel.selectedIndex = 0;
  const showAll = Boolean(bubble._chatSourcesShowAll);
  // Re-rendering replaces the focused row or title: keep focus in the panel.
  const hadFocus = panel.contains(document.activeElement);

  panel.querySelector('#chat-sources-title').textContent = _ti(showAll ? 'chat.sources.panelTitleFound' : 'chat.sources.panelTitle');
  const close = panel.querySelector('.csp-close');
  close.setAttribute('aria-label', _ti('chat.sources.close'));
  close.title = _ti('chat.sources.close');
  panel.querySelector('.csp-views').setAttribute('aria-label', _ti('chat.sources.viewGroup'));
  panel.querySelectorAll('.csp-view-btn').forEach((button) => {
    const isAll = button.dataset.cspView === 'all';
    button.textContent = isAll ? _ti('chat.sources.viewAll', { count: sources.length }) : _ti('chat.sources.viewDetail');
    button.setAttribute('aria-pressed', String((chatSourcesPanel.view === 'all') === isAll));
  });

  const body = panel.querySelector('.csp-body');
  body.innerHTML = '';
  const selected = sources[chatSourcesPanel.selectedIndex];
  const selectedLabel = sourceRefLabel(selected, chatSourcesPanel.selectedIndex + 1);
  if (chatSourcesPanel.view === 'all') {
    const list = document.createElement('ol');
    list.className = 'csp-list';
    sources.forEach((source, index) => list.appendChild(_chatSourceListRow(source, index, index === chatSourcesPanel.selectedIndex)));
    body.appendChild(list);
    _markActiveChatCitation(null, '');
  } else {
    body.appendChild(_chatSourceDetail(selected, chatSourcesPanel.selectedIndex));
    const others = sources.map((source, index) => ({ source, index })).filter(({ index }) => index !== chatSourcesPanel.selectedIndex);
    if (others.length) {
      const heading = document.createElement('h3');
      heading.className = 'csp-others-title';
      heading.textContent = _ti('chat.sources.others');
      body.appendChild(heading);
      const list = document.createElement('ol');
      list.className = 'csp-list';
      others.forEach(({ source, index }) => list.appendChild(_chatSourceListRow(source, index, false)));
      body.appendChild(list);
    }
    _markActiveChatCitation(bubble, selectedLabel);
  }
  if (hadFocus && !panel.contains(document.activeElement)) {
    panel.querySelector('#chat-sources-title')?.focus({ preventScroll: true });
  }
}

function _chatSourceListRow(source, index, isSelected) {
  const item = document.createElement('li');
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'csp-row';
  if (isSelected) button.setAttribute('aria-current', 'true');
  const badge = document.createElement('span');
  badge.className = 'csp-badge';
  badge.textContent = sourceRefLabel(source, index + 1);
  const text = document.createElement('span');
  text.className = 'csp-row-text';
  const title = document.createElement('span');
  title.className = 'csp-row-title';
  title.textContent = sourceDisplayTitle(source);
  text.appendChild(title);
  const meta = [sourceCompactMeta(source), sourcePageLabel(source)].filter(Boolean).join(' · ');
  if (meta) {
    const metaEl = document.createElement('span');
    metaEl.className = 'csp-row-meta';
    metaEl.textContent = meta;
    text.appendChild(metaEl);
  }
  button.append(badge, text);
  button.addEventListener('click', () => {
    chatSourcesPanel.selectedIndex = index;
    chatSourcesPanel.view = 'detail';
    renderChatSourcesPanel();
    document.querySelector('#chat-sources-panel .csp-detail-title')?.focus({ preventScroll: true });
  });
  item.appendChild(button);
  return item;
}

// The passage with the cited sentence marked, when it is found verbatim.
// Chat sources carry a 320-character snippet (format_sources_api), so the
// cited sentence often lies past it: it then shows only in the anchor box.
function _chatSourcePassage(source) {
  const text = String(source?.content || source?.snippet || '').trim();
  if (!text) return null;
  const clipped = text.length > 1400 ? `${text.slice(0, 1400).trim()}…` : text;
  const passage = document.createElement('div');
  passage.className = 'csp-passage';
  const anchorText = String(source?.anchors?.[0]?.text || '').trim();
  const at = anchorText ? clipped.indexOf(anchorText) : -1;
  if (at < 0) {
    passage.textContent = clipped;
    return passage;
  }
  const mark = document.createElement('mark');
  mark.textContent = anchorText;
  passage.append(clipped.slice(0, at), mark, clipped.slice(at + anchorText.length));
  return passage;
}

function _chatSourceDetail(source, index) {
  const detail = document.createElement('article');
  detail.className = 'csp-detail';
  const label = sourceRefLabel(source, index + 1);
  detail.dataset.sourceLabel = label;

  const pills = document.createElement('div');
  pills.className = 'csp-pills';
  const badge = document.createElement('span');
  badge.className = 'csp-badge is-strong';
  badge.textContent = label;
  pills.appendChild(badge);
  const pageLabel = sourcePageLabel(source);
  if (pageLabel) {
    const page = document.createElement('span');
    page.className = 'source-page-badge';
    page.textContent = pageLabel;
    pills.appendChild(page);
  }
  detail.appendChild(pills);

  const title = document.createElement('h3');
  title.className = 'csp-detail-title';
  title.tabIndex = -1;
  title.textContent = sourceDisplayTitle(source);
  detail.appendChild(title);
  const meta = sourceMetaLine(source);
  if (meta) {
    const metaEl = document.createElement('p');
    metaEl.className = 'csp-meta';
    metaEl.textContent = meta;
    detail.appendChild(metaEl);
  }

  const anchorBox = createSourceAnchorBox(source);
  if (anchorBox) detail.appendChild(anchorBox);

  const passage = _chatSourcePassage(source);
  if (passage) {
    const heading = document.createElement('div');
    heading.className = 'csp-section-label';
    heading.textContent = _ti('chat.sources.passage');
    detail.append(heading, passage);
  }

  const actions = document.createElement('div');
  actions.className = 'csp-actions';
  const path = sourceActionPath(source);
  if (path) {
    const open = document.createElement('a');
    open.className = 'csp-action is-primary';
    open.href = sourceOpenDocumentUrl(source, path);
    open.target = '_blank';
    open.rel = 'noopener';
    const pageNumber = /\.pdf$/i.test(path) ? sourcePageNumber(source) : 0;
    open.textContent = pageNumber
      ? _ti('chat.sources.openAtPage', { page: pageNumber })
      : (/\.pdf$/i.test(path) ? _tr('Ouvrir le PDF') : _tr('Ouvrir le document'));
    actions.appendChild(open);

    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'csp-action';
    copy.textContent = _ti('chat.sources.copyCitation');
    copy.addEventListener('click', () => copyChatSourceCitation(source, copy));
    actions.appendChild(copy);
  }
  detail.appendChild(actions);

  // Library, next-question filter and Zotero: the same actions as the cards.
  const more = document.createElement('div');
  more.className = 'source-actions csp-more-actions';
  appendSourceActions(more, source);
  more.querySelector('.source-open-pdf-btn')?.remove();
  if (more.childElementCount) detail.appendChild(more);
  return detail;
}

async function copyChatSourceCitation(source, button = null) {
  const path = sourceActionPath(source);
  let text = '';
  if (button) button.disabled = true;
  try {
    const citations = await fetchJsonOrThrow(
      '/api/item-citations?path=' + encodeURIComponent(path),
      {},
      _ti('chat.sources.citationCopyFailed'),
    );
    text = String(citations?.apa || '').trim();
  } catch (_) {
    text = '';
  } finally {
    if (button) button.disabled = false;
  }
  if (!text) text = [sourceMetaLine(source), sourceDisplayTitle(source)].filter(Boolean).join('. ');
  const copied = await copyTextWithFallback(text);
  showToast(_ti(copied ? 'chat.sources.citationCopied' : 'chat.sources.citationCopyFailed'), 2600, copied ? 'success' : 'error');
}

function goToSource(displayFile) {
  const normalize = s => s.replace(/\s*\/\s*/g, '/').replace(/\\/g, '/').toLowerCase();
  const target = normalize(displayFile);
  const idx = state.libPaths.findIndex(p => normalize(p) === target);
  if (idx === -1) { showToast(_tr('Article non trouvé dans la bibliothèque.')); return; }
  const libBtn = document.querySelector('.nav-item[data-tab="library"]');
  switchTab('library', libBtn);
  // Attend que la table soit peinte (un délai fixe rate sur grosses bibliothèques).
  const tryFocusRow = (attempt) => {
    const row = document.querySelector(`#lib-tbody tr[data-i="${idx}"]`);
    if (!row && attempt < 30) {
      window.requestAnimationFrame(() => tryFocusRow(attempt + 1));
      return;
    }
    if (row) {
      row.scrollIntoView({ behavior: preferredScrollBehavior(), block: 'center' });
      document.querySelectorAll('#lib-tbody tr').forEach(tr => tr.classList.remove('selected', 'highlight-flash'));
      row.classList.add('selected', 'highlight-flash');
    }
    selectRow(idx);
  };
  window.requestAnimationFrame(() => tryFocusRow(0));
}

// ---------------------------------------------
function _chatSendIconHtml() {
  return `
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <line x1="22" y1="2" x2="11" y2="13"></line>
      <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
    </svg>
  `;
}

function _chatStopIconHtml() {
  return `
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="2"></rect>
    </svg>
  `;
}

function _setSendButtonIdle() {
  const btn = document.getElementById('send-btn');
  if (!btn) return;
  btn.classList.remove('is-stop');
  btn.innerHTML = _chatSendIconHtml();
  // The i18n observer rewrites title and aria-label from these keys after any
  // change, so they must name the button's current role.
  btn.setAttribute('data-i18n-title', 'chat.sendTitle');
  btn.setAttribute('data-i18n-aria-label', 'chat.sendAria');
  btn.title = _tr('Envoyer');
  btn.setAttribute('aria-label', _tr('Envoyer le message'));
}

function _setSendButtonStop() {
  const btn = document.getElementById('send-btn');
  if (!btn) return;
  btn.classList.add('is-stop');
  btn.innerHTML = _chatStopIconHtml();
  btn.setAttribute('data-i18n-title', 'chat.stopTitle');
  btn.setAttribute('data-i18n-aria-label', 'chat.stopAria');
  btn.title = _tr('Arrêter');
  btn.setAttribute('aria-label', _tr('Arrêter la génération'));
}

function handleClickableCardKey(event, action) {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  event.preventDefault();
  if (typeof action === 'function') action();
}

// First run left unfinished ("Later", or preparation still running in the
// background): the examples cannot work yet, so offer the way back instead.
function _chatEmptySetupHtml(tr) {
  const building = typeof _startupGuideBuilding === 'function' && _startupGuideBuilding();
  const prefix = building ? 'chat.building' : 'chat.setup';
  return `
    <div id="chat-empty">
      <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.3">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
      <h2 data-i18n="${prefix}Title">${escapeHtml(tr(`${prefix}Title`, ''))}</h2>
      <p data-i18n="${prefix}Desc">${escapeHtml(tr(`${prefix}Desc`, ''))}</p>
      <button type="button" class="btn btn-primary chat-empty-setup-btn" data-i18n="${prefix}Action" ${bindBbHandler('click', function() { openStartupAssistant(); })}>${escapeHtml(tr(`${prefix}Action`, ''))}</button>
    </div>`;
}

// The four starters of the empty chat. The input gets an editable prompt whose
// "..." part is selected (applyChatHint); hintAction routes it to its tool.
// "Cite" lists the library's articles on a subject: the bibliography action
// only covers the sources already cited in the current chat.
const CHAT_STARTERS = [
  { id: 'find', prompt: 'chat.starterFindPrompt', hintAction: 'local_search' },
  { id: 'summarize', prompt: 'chat.starterSummarizePrompt', hintAction: 'literature_review' },
  { id: 'compare', prompt: 'chat.starterComparePrompt', hintAction: 'compare' },
  { id: 'cite', prompt: 'chat.starterCitePrompt', hintAction: 'local_search' },
];

function _chatStarterKey(id, suffix) {
  return `chat.starter${id[0].toUpperCase()}${id.slice(1)}${suffix}`;
}

// What the empty chat says about the library, following the status pill:
// 'loading' while a load runs, 'prepare' / 'repair' when the pill offers that
// action, 'idle' otherwise.
function _chatEmptyRagState() {
  if (state.ragReady) {
    const hasChatModel = typeof currentChatModelName !== 'function' || Boolean(currentChatModelName());
    return hasChatModel ? 'ready' : 'no-chat-model';
  }
  const loadingPhase = ['starting', 'loading', 'preparing'].includes(state.ragUiState?.phase);
  if (state.ragInitBusy || loadingPhase) return 'loading';
  return state.ragFirstInitAction || 'idle';
}

function _chatEmptyHtml() {
  const tr = (key, fallback) => (typeof window.t === 'function' ? window.t(key) : fallback);
  if (!state.ragReady && typeof isStartupGuideNeeded === 'function' && isStartupGuideNeeded()) {
    return _chatEmptySetupHtml(tr);
  }
  // Not loaded yet: starters could not answer. Say so, with the status pill's
  // own "Préparer" / "Réparer" action when it offers one.
  if (!state.ragReady) {
    const ragState = _chatEmptyRagState();
    const loading = ragState === 'loading';
    const titleKey = loading ? 'chat.loadingTitle' : 'chat.notReadyTitle';
    const descKey = loading ? 'chat.loadingDesc' : 'chat.readyDesc';
    const actionKey = ragState === 'repair' ? 'chat.repairAction' : 'chat.prepareAction';
    const action = ragState === 'prepare' || ragState === 'repair'
      ? `<button type="button" class="btn btn-primary chat-empty-setup-btn" data-i18n="${actionKey}"${state.ragInitAvailable === false ? ' disabled' : ''} ${bindBbHandler('click', function() { runFirstRagInit(); })}>${escapeHtml(tr(actionKey, ''))}</button>`
      : '';
    return `
      <div id="chat-empty" data-rag-state="${ragState}">
        <h1 data-i18n="${titleKey}">${escapeHtml(tr(titleKey, ''))}</h1>
        <p data-i18n="${descKey}">${escapeHtml(tr(descKey, ''))}</p>
        ${action}
      </div>`;
  }
  // The documents are ready but nothing can answer yet.
  if (_chatEmptyRagState() === 'no-chat-model') {
    return `
      <div id="chat-empty" data-rag-state="no-chat-model">
        <h1 data-i18n="chat.noChatModelTitle">${escapeHtml(tr('chat.noChatModelTitle', ''))}</h1>
        <p data-i18n="chat.noChatModelDesc">${escapeHtml(tr('chat.noChatModelDesc', ''))}</p>
        <button type="button" class="btn btn-primary chat-empty-setup-btn" data-i18n="model.smallConnectApi" ${bindBbHandler('click', function() { _openAiSettings(); })}>${escapeHtml(tr('model.smallConnectApi', ''))}</button>
      </div>`;
  }
  const count = typeof libraryArticleCount === 'function' ? libraryArticleCount() : 0;
  const descKey = count ? 'chat.emptyDesc' : 'chat.emptyDescNoCount';
  const countText = count ? formatCount(count) : '';
  const cards = CHAT_STARTERS.map(({ id, prompt, hintAction }) => {
    const labelKey = _chatStarterKey(id, 'Label');
    const textKey = _chatStarterKey(id, '');
    return `
      <button type="button" class="chat-starter-card" data-starter="${id}" ${bindBbHandler('click', function() { applyChatHintByKey(prompt, hintAction); })}>
        <span class="chat-starter-label" data-i18n="${labelKey}">${escapeHtml(tr(labelKey, ''))}</span>
        <span class="chat-starter-text" data-i18n-html="${textKey}">${tr(textKey, '')}</span>
      </button>`;
  }).join('');
  return `
    <div id="chat-empty" class="is-ready" data-rag-state="ready">
      <h1 data-i18n="chat.emptyTitle">${escapeHtml(tr('chat.emptyTitle', 'Interrogez votre bibliothèque'))}</h1>
      <p data-i18n="${descKey}"${count ? ` data-i18n-count="${escapeAttr(countText)}"` : ''}>${escapeHtml(_ti(descKey, { count: countText }))}</p>
      <div id="chat-starters">${cards}</div>
      <button type="button" class="chat-starters-more" data-i18n="chat.moreSuggestions" ${bindBbHandler('click', function() { toggleChatOptions(); })}>${escapeHtml(tr('chat.moreSuggestions', ''))}</button>
    </div>`;
}

function clearChat() {
  document.getElementById('messages').innerHTML = _chatEmptyHtml();
  closeChatSourcesPanel({ restoreFocus: false });
}

function _refreshChatEmptyState() {
  const empty = document.getElementById('chat-empty');
  if (empty) empty.outerHTML = _chatEmptyHtml();
}

// Called on every library status render: re-render only when the state the
// empty chat shows has changed (the setup variant is left to the assistant).
function _syncChatEmptyState() {
  const empty = document.getElementById('chat-empty');
  if (!empty?.dataset.ragState) return;
  if (empty.dataset.ragState !== _chatEmptyRagState()) _refreshChatEmptyState();
}

function _appendUserBubble(msg) {
  const msgs = document.getElementById('messages');
  const empty = document.getElementById('chat-empty');
  if (empty) empty.remove();

  const wrap = document.createElement('div');
  wrap.className = 'msg-wrap user';
  const av = document.createElement('div');
  av.className = 'avatar user';
  av.textContent = 'U';
  const bubble = document.createElement('div');
  bubble.className = 'bubble user';
  bubble.innerHTML = escapeHtml(msg).replace(/\n/g, '<br>');
  bubble.dataset.copyText = String(msg || '');
  _upsertChatCopyButton(bubble);
  wrap.appendChild(av);
  wrap.appendChild(bubble);
  msgs.appendChild(wrap);
  msgs.scrollTop = msgs.scrollHeight;
}

function _appendAssistantPlaceholder() {
  const msgs = document.getElementById('messages');
  const wrap = document.createElement('div');
  wrap.className = 'msg-wrap bot';
  const av = document.createElement('div');
  av.className = 'avatar bot';
  av.textContent = 'AI';
  const bubble = document.createElement('div');
  bubble.className = 'bubble status';
  bubble.innerHTML = '<span class="spinner"></span><span class="status-wait-text">' + _tr('Recherche…') + '</span>';
  wrap.appendChild(av);
  wrap.appendChild(bubble);
  msgs.appendChild(wrap);
  msgs.scrollTop = msgs.scrollHeight;
  return bubble;
}

function _appendHistoryMessage(role, content, meta = null, precedingUserQuery = '') {
  const msgs = document.getElementById('messages');
  const empty = document.getElementById('chat-empty');
  if (empty) empty.remove();

  const wrap = document.createElement('div');
  wrap.className = role === 'user' ? 'msg-wrap user' : 'msg-wrap bot';
  const av = document.createElement('div');
  av.className = role === 'user' ? 'avatar user' : 'avatar bot';
  av.textContent = role === 'user' ? 'U' : 'AI';
  const bubble = document.createElement('div');
  bubble.className = role === 'user' ? 'bubble user' : 'bubble bot';
  if (role === 'user') {
    bubble.innerHTML = escapeHtml(content || '').replace(/\n/g, '<br>');
    bubble.dataset.copyText = String(content || '');
  } else {
    const normalizedSources = normalizeChatSourcesForRender(Array.isArray(meta?.sources) ? meta.sources : []);
    const storedDebugMetrics = meta?.debug_metrics && typeof meta.debug_metrics === 'object'
      ? meta.debug_metrics
      : null;
    const storedDiscoverySession = meta?.discovery_session && typeof meta.discovery_session === 'object'
      ? meta.discovery_session
      : null;
    const storedExternalSearchConfirmation = meta?.external_search_confirmation && typeof meta.external_search_confirmation === 'object'
      ? meta.external_search_confirmation
      : null;
    const storedAnswerOrigin = String(
      storedDebugMetrics?.answer_origin
      || (meta?.mode === 'article_discovery' || meta?.mode === 'paper_list' ? meta.mode : '')
    ).trim();
    const ragQuery = String(meta?.rag_query || precedingUserQuery || '').trim();
    bubble._ragQuery = ragQuery;
    bubble._answerOrigin = storedAnswerOrigin;
    if (typeof rememberLastRagRetrieval === 'function') {
      rememberLastRagRetrieval(ragQuery, normalizedSources, storedAnswerOrigin);
    }
    renderRichAssistantOutput(bubble, content || '', {
      normalizeCitations: true,
      sources: normalizedSources,
      showAllSources: shouldShowAllAssistantSources(storedAnswerOrigin),
      exportFile: meta?.export_file && typeof meta.export_file === 'object' ? meta.export_file : null,
      discoverySession: storedDiscoverySession,
      externalSearchConfirmation: storedExternalSearchConfirmation,
      debugMetrics: isDebugMetricsEnabled() ? storedDebugMetrics : null,
    });
  }
  _upsertChatCopyButton(bubble);
  wrap.appendChild(av);
  wrap.appendChild(bubble);
  msgs.appendChild(wrap);
}

function _renderChatMessages(messages) {
  const msgs = document.getElementById('messages');
  msgs.innerHTML = '';
  closeChatSourcesPanel({ restoreFocus: false });
  const rows = Array.isArray(messages) ? messages : [];
  resetChatSourceRegistry();
  if (!rows.length) {
    clearChat();
    return;
  }
  let precedingUserQuery = '';
  rows.forEach((row) => {
    const role = row?.role === 'user' ? 'user' : 'assistant';
    if (role === 'user') precedingUserQuery = String(row?.content || '').trim();
    _appendHistoryMessage(
      role,
      row?.content || '',
      row?.meta || null,
      precedingUserQuery,
    );
  });
  msgs.scrollTop = msgs.scrollHeight;
}

async function copyTextWithFallback(value) {
  const text = String(value || '');
  if (!text) return false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {}

  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', 'readonly');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  area.style.pointerEvents = 'none';
  document.body.appendChild(area);
  area.focus();
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch (e) {
    ok = false;
  }
  document.body.removeChild(area);
  return ok;
}

function _upsertChatCopyButton(bubble) {
  if (!bubble || bubble.classList.contains('status')) return;
  let button = bubble.querySelector('.bubble-copy-btn');
  if (!button) {
    button = document.createElement('button');
    button.type = 'button';
    button.className = 'bubble-copy-btn';
    button.textContent = 'Copier';
    button.setAttribute('aria-label', 'Copier le message');
    button.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      const text = String(bubble.dataset.copyText || '').trim();
      if (!text) return;
      const ok = await copyTextWithFallback(text);
      button.textContent = ok ? 'Copié' : 'Échec';
      button.classList.toggle('is-copied', ok);
      window.setTimeout(() => {
        button.textContent = 'Copier';
        button.classList.remove('is-copied');
      }, 1400);
    });
    bubble.appendChild(button);
  }
}

function _fmtChatDate(iso) {
  try {
    const d = new Date(iso);
    if (Number.isNaN(+d)) return '';
    return d.toLocaleString('fr-FR', {
      day: '2-digit',
      month: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return '';
  }
}

function _fmtDiscoveryDuration(elapsedMs) {
  const ms = Number(elapsedMs || 0);
  if (!Number.isFinite(ms) || ms <= 0) return '';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms >= 10000 ? 0 : 1)} s`;
}

function onChatHistorySearchInput() {
  state.chatHistoryFilter = document.getElementById('chat-history-search')?.value || '';
  renderChatHistory();
}

const CHAT_RECENT_LIMIT = 5;

// Under Chat in the sidebar: the most recent conversations, one line each.
function _renderRecentChats() {
  const wrap = document.getElementById('sidebar-chat-recent');
  if (!wrap) return;
  const chats = Array.isArray(state.chatSessions) ? state.chatSessions : [];
  document.getElementById('sidebar-chat-history')?.toggleAttribute('hidden', !chats.length);
  wrap.innerHTML = chats.slice(0, CHAT_RECENT_LIMIT).map((chat) => {
    const title = chat.title || _tr('Chat sans titre');
    const active = chat.id === state.activeChatId;
    return `
      <div class="nav-chat-item${active ? ' active' : ''}" role="button" tabindex="0"${active ? ' aria-current="true"' : ''} ${bindBbHandler('click', function() { openChatSession(chat.id); })} ${bindBbHandler('keydown', function(event) { handleClickableCardKey(event, () => openChatSession(chat.id)); })}>
        <span class="chat-item-title" title="${escapeAttr(title)} ${escapeAttr(_tr('(double-clic pour renommer)'))}" ${bindBbHandler('dblclick', function(event) { event.stopPropagation(); startChatTitleEdit(event, chat.id); })}>${escapeHtml(title)}</span>
        <button class="chat-item-del" type="button" title="Supprimer" ${bindBbHandler('click', function(event) { event.stopPropagation(); deleteChatSession(chat.id); })} aria-label="Supprimer">&times;</button>
      </div>`;
  }).join('');
}

function renderChatHistory() {
  _renderRecentChats();
  const list = document.getElementById('chat-history-list');
  if (!list) return;
  let chats = Array.isArray(state.chatSessions) ? state.chatSessions : [];
  const filter = String(state.chatHistoryFilter || '').trim().toLowerCase();
  if (filter) {
    chats = chats.filter((c) =>
      String(c.title || '').toLowerCase().includes(filter)
      || String(c.last_message_preview || '').toLowerCase().includes(filter));
  }
  if (!chats.length) {
    list.innerHTML = '<div style="font-size:0.74em;color:var(--muted);padding:6px 2px;">'
      + _tr(filter ? 'Aucune conversation ne correspond.' : 'Aucun chat sauvegarde.') + '</div>';
    return;
  }
  // Numérote les titres identiques pour distinguer les conversations dans la liste.
  const titleCounts = new Map();
  chats.forEach((chat) => {
    const key = String(chat.title || '').trim().toLowerCase();
    titleCounts.set(key, (titleCounts.get(key) || 0) + 1);
  });
  const titleSeen = new Map();
  list.innerHTML = chats.map((chat) => {
    const activeClass = chat.id === state.activeChatId ? ' active' : '';
    const preview = stripThinkingMarkup(chat.last_message_preview || '').trim() || _tr('Conversation vide');
    const meta = _fmtChatDate(chat.updated || chat.created || '');
    const baseTitle = chat.title || _tr('Chat sans titre');
    const key = String(chat.title || '').trim().toLowerCase();
    let displayTitle = baseTitle;
    if ((titleCounts.get(key) || 0) > 1) {
      const n = (titleSeen.get(key) || 0) + 1;
      titleSeen.set(key, n);
      displayTitle = `${baseTitle} (${n})`;
    }
    return `
      <div class="chat-item${activeClass}" role="button" tabindex="0" ${bindBbHandler('click', function() { closeChatHistoryPopover(); openChatSession(chat.id); })} ${bindBbHandler('keydown', function(event) { handleClickableCardKey(event, () => { closeChatHistoryPopover(); openChatSession(chat.id); }); })}>
        <div class="chat-item-top">
          <div class="chat-item-title" title="${escapeAttr(baseTitle)} ${escapeAttr(_tr('(double-clic pour renommer)'))}" ${bindBbHandler('dblclick', function(event) { event.stopPropagation(); startChatTitleEdit(event, chat.id); })}>${escapeHtml(displayTitle)}</div>
          <button class="chat-item-del" title="Supprimer" ${bindBbHandler('click', function(event) { event.stopPropagation(); deleteChatSession(chat.id); })} aria-label="Supprimer">&times;</button>
        </div>
        <div class="chat-item-preview">${escapeHtml(preview)}</div>
        <div class="chat-item-meta">${escapeHtml(meta)}</div>
      </div>
    `;
  }).join('');
}

function startChatTitleEdit(event, chatId) {
  const titleEl = event.currentTarget;
  const session = (state.chatSessions || []).find((c) => c.id === chatId);
  if (!titleEl || !session) return;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'chat-item-title-input';
  input.value = session.title || '';
  input.maxLength = 120;
  input.setAttribute('aria-label', _tr('Renommer la conversation'));
  input.onclick = (e) => e.stopPropagation();
  input.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') input.blur();
    if (e.key === 'Escape') { input.value = session.title || ''; input.blur(); }
  };
  input.onblur = async () => {
    const next = input.value.trim();
    if (next && next !== session.title) {
      try {
        const r = await fetch('/api/chats/' + encodeURIComponent(chatId) + '/title', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: next }),
        });
        const d = await r.json();
        if (d?.ok) {
          session.title = d.title || next;
          if (state.activeChatId === chatId) state.activeChatTitle = session.title;
        } else {
          showToast(_tr('Renommage impossible.'), 2600, 'error');
        }
      } catch (e) {
        showToast(_tr('Renommage impossible.'), 2600, 'error');
      }
    }
    renderChatHistory();
  };
  titleEl.replaceWith(input);
  input.focus();
  input.select();
}

const DISCOVERY_RECENT_LIMIT = 5;

// Under Discovery in the sidebar: the most recent searches, one line each.
function _renderRecentDiscoverySearches() {
  const wrap = document.getElementById('sidebar-discovery-recent');
  if (!wrap) return;
  const searches = Array.isArray(state.discoveryHistory) ? state.discoveryHistory : [];
  document.getElementById('sidebar-discovery-history')?.toggleAttribute('hidden', !searches.length);
  wrap.innerHTML = searches.slice(0, DISCOVERY_RECENT_LIMIT).map((item) => {
    const title = item.title || _tr('Recherche sans titre');
    const active = item.id === state.activeDiscoveryHistoryId;
    const notebookIcon = item.notebook_id || item.mode === 'recommendations' || item.mode === 'notebook'
      ? `<svg class="nav-history-kind" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-label="${escapeAttr(_ti('discovery.fromNotebook'))}"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>`
      : '';
    return `
      <div class="nav-chat-item${active ? ' active' : ''}" role="button" tabindex="0"${active ? ' aria-current="true"' : ''} ${bindBbHandler('click', function() { openDiscoveryHistory(item.id); })} ${bindBbHandler('keydown', function(event) { handleClickableCardKey(event, () => openDiscoveryHistory(item.id)); })}>
        ${notebookIcon}<span class="chat-item-title" title="${escapeAttr(title)}">${escapeHtml(title)}</span>
        <button class="chat-item-del" type="button" title="Supprimer" ${bindBbHandler('click', function(event) { event.stopPropagation(); deleteDiscoveryHistory(item.id); })} aria-label="Supprimer">&times;</button>
      </div>`;
  }).join('');
}

function renderDiscoveryHistory() {
  _renderRecentDiscoverySearches();
  const list = document.getElementById('discovery-history-list');
  if (!list) return;
  const searches = Array.isArray(state.discoveryHistory) ? state.discoveryHistory : [];
  if (!searches.length) {
    list.innerHTML = '<div style="font-size:0.74em;color:var(--muted);padding:6px 2px;">' + _tr('Aucune recherche sauvegardee.') + '</div>';
    return;
  }
  // Grouped by date (today, this week, older) and filtered by the search box.
  const filter = String(state.discoveryHistoryFilter || '').trim().toLowerCase();
  const visible = filter
    ? searches.filter((item) => `${item.title || ''} ${item.input_preview || ''}`.toLowerCase().includes(filter))
    : searches;
  if (!visible.length) {
    list.innerHTML = `<div class="discovery-history-empty">${escapeHtml(_ti('discovery.historyNoMatch'))}</div>`;
    return;
  }
  let group = '';
  list.innerHTML = visible.map((item) => {
    const itemGroup = discoveryHistoryGroup(item.updated || item.created || '');
    const heading = itemGroup !== group
      ? `<div class="discovery-history-group">${escapeHtml(_ti(`discovery.group.${itemGroup}`))}</div>`
      : '';
    group = itemGroup;
    const activeClass = item.id === state.activeDiscoveryHistoryId ? ' active' : '';
    const when = _fmtChatDate(item.updated || item.created || '');
    const providerText = Array.isArray(item.providers_used) && item.providers_used.length
      ? item.providers_used.join(', ')
      : '';
    const resultCount = Number.isFinite(Number(item.result_count))
      ? _ti('discovery.resultCount', { count: item.result_count })
      : '';
    // The card shows only when and how many; the technical details stay in the tooltip.
    const meta = [when, resultCount].filter(Boolean).join(' · ');
    const details = [
      item.model ? `${_ti('nav.model')} ${item.model}` : '',
      _fmtDiscoveryDuration(item.elapsed_ms),
      providerText,
    ].filter(Boolean).join(' · ');
    return `${heading}
      <div class="chat-item${activeClass}" role="button" tabindex="0"${details ? ` title="${escapeAttr(details)}"` : ''} ${bindBbHandler('click', function() { openDiscoveryHistory(item.id); })} ${bindBbHandler('keydown', function(event) { handleClickableCardKey(event, () => openDiscoveryHistory(item.id)); })}>
        <div class="chat-item-top">
          <div class="chat-item-title">${escapeHtml(item.title || 'Recherche sans titre')}</div>
          <button class="chat-item-del" title="Supprimer" ${bindBbHandler('click', function(event) { event.stopPropagation(); deleteDiscoveryHistory(item.id); })} aria-label="Supprimer">&times;</button>
        </div>
        <div class="chat-item-preview">${escapeHtml(item.input_preview || (item.status_msg || _tr('Recherche sauvegardee')))}</div>
        <div class="chat-item-meta">${escapeHtml(meta || (item.ok ? _tr('Recherche terminee') : _tr('Recherche en erreur')))}</div>
      </div>
    `;
  }).join('');
}

function onDiscoveryHistoryFilterInput(event) {
  state.discoveryHistoryFilter = String(event?.target?.value || '');
  renderDiscoveryHistory();
}

async function loadChatHistory() {
  try {
    const d = await fetchAllPaginated('/api/chats', ['chats'], { limit: 250 });
    state.chatSessions = Array.isArray(d?.chats) ? d.chats : [];
  } catch (e) {
    state.chatSessions = [];
  }
  renderChatHistory();
}

async function loadDiscoveryHistory() {
  try {
    const d = await fetchAllPaginated('/api/discovery-history', ['searches'], { limit: 250 });
    state.discoveryHistory = Array.isArray(d?.searches) ? d.searches : [];
  } catch (e) {
    state.discoveryHistory = [];
  }
  renderDiscoveryHistory();
}

async function openDiscoveryHistory(searchId) {
  try {
    const r = await fetch(`/api/discovery-history/${encodeURIComponent(searchId)}`);
    if (!r.ok) throw new Error('Recherche introuvable');
    const d = await r.json();
    const search = d?.search || {};
    const payload = d?.response || {};
    state.activeDiscoveryHistoryId = String(search.id || searchId || '');
    state.discoveryCompareTo = null;

    const modeEl = document.getElementById('discovery-mode');
    if (modeEl && search.mode) modeEl.value = search.mode === 'recommendations' ? 'notebook' : search.mode;
    onDiscoveryModeChange();
    await loadDiscoveryNotebookOptions();

    const inputEl = document.getElementById('discovery-input');
    if (inputEl) inputEl.value = String(search.input_text || '');
    const nbEl = document.getElementById('discovery-notebook-select');
    if (nbEl) nbEl.value = String(search.notebook_id || '');
    onDiscoveryNotebookChange();
    const maxEl = document.getElementById('discovery-max-results');
    if (maxEl && search.max_results) maxEl.value = String(search.max_results);

    _renderDiscoveryResults(payload);
    setDiscoveryOpenedSearch(search);
    renderDiscoveryHistory();
    closeDiscoveryHistoryPopover();
    return true;
  } catch (e) {
    showToast(_tr('Erreur chargement historique discovery.'));
    return false;
  }
}

function normalizeDiscoverySession(session) {
  if (!session || typeof session !== 'object') return null;
  const historyId = String(session.history_id || '').trim();
  if (!historyId) return null;
  const resultCountRaw = Number(session.result_count);
  return {
    history_id: historyId,
    input_text: String(session.input_text || '').trim(),
    provider_used: String(session.provider_used || '').trim(),
    result_count: Number.isFinite(resultCountRaw) ? parseInt(resultCountRaw, 10) : 0,
  };
}

function normalizeExternalSearchConfirmation(confirmation) {
  if (!confirmation || typeof confirmation !== 'object') return null;
  const message = String(confirmation.message || '').trim();
  if (!message) return null;
  return {
    message,
    query: String(confirmation.query || '').trim(),
    msg: String(confirmation.msg || '').trim(),
    externalAllowed: confirmation.external_allowed !== false,
  };
}

async function confirmExternalSearchFromChat(confirmation, button = null, scope = 'external') {
  const normalized = normalizeExternalSearchConfirmation(confirmation);
  if (!normalized) return false;
  const selectedScope = scope === 'local' ? 'local' : 'external';
  if (button) {
    button.disabled = true;
    button.classList.add('is-loading');
  }
  try {
    for (let i = 0; i < 30 && state.streaming; i += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 80));
    }
    if (state.streaming) {
      showToast(_tr('Attendez la fin du message de confirmation.'), 2400);
      return false;
    }
    await sendMessage({
      messageOverride: normalized.message,
      appendUserBubble: false,
      privacyAckExternalSearch: selectedScope === 'external',
      externalSearchConfirmed: selectedScope === 'external',
      articleSearchScope: selectedScope,
      hintAction: 'article_search',
    });
    return true;
  } finally {
    if (button) {
      button.disabled = false;
      button.classList.remove('is-loading');
    }
  }
}

async function openDiscoverySessionFromChat(session, button = null) {
  const normalized = normalizeDiscoverySession(session);
  if (!normalized) {
    showToast(_tr("Cette session discovery n'est plus disponible."), 4000, 'error');
    return false;
  }
  if (button) {
    button.disabled = true;
    button.classList.add('is-loading');
  }
  try {
    const opened = await openDiscoveryHistory(normalized.history_id);
    if (!opened) {
      showToast(_tr('La recherche discovery liee a ce message est introuvable.'), 4000, 'error');
      return false;
    }
    switchTab('discovery');
    switchDiscoveryWorkspace('articles', { focus: false });
    return true;
  } finally {
    if (button) {
      button.disabled = false;
      button.classList.remove('is-loading');
    }
  }
}

async function deleteDiscoveryHistory(searchId) {
  const searches = Array.isArray(state.discoveryHistory) ? state.discoveryHistory : [];
  const idx = searches.findIndex((s) => s.id === searchId);
  if (idx === -1) return;
  const removed = searches[idx];
  const wasActive = state.activeDiscoveryHistoryId === searchId;
  searches.splice(idx, 1);
  if (wasActive) state.activeDiscoveryHistoryId = '';
  renderDiscoveryHistory();
  deleteWithUndo('discovery:' + searchId, {
    message: 'Recherche supprimée.',
    restore: () => {
      const list = Array.isArray(state.discoveryHistory) ? state.discoveryHistory : [];
      list.splice(Math.min(idx, list.length), 0, removed);
      if (wasActive) state.activeDiscoveryHistoryId = searchId;
      renderDiscoveryHistory();
    },
    commit: async () => {
      try {
        const r = await fetch(`/api/discovery-history/${encodeURIComponent(searchId)}`, { method: 'DELETE', keepalive: true });
        const d = await r.json();
        if (!d?.ok) throw new Error('Suppression impossible');
      } catch (e) {
        showToast(_tr('Erreur suppression historique discovery.'));
        loadDiscoveryHistory();
      }
    },
  });
}

// "All conversations": the full list with its filter, in a card beside the
// sidebar, level with the link. Escape and a click outside close it.
let _chatHistoryPopoverCleanup = null;

function _placeChatHistoryPopover(popover, anchor) {
  const link = anchor.getBoundingClientRect();
  const sidebar = document.getElementById('sidebar')?.getBoundingClientRect() || link;
  _onbPlacePopover(popover, { top: link.top, bottom: link.bottom, left: sidebar.left, right: sidebar.right });
}

function closeChatHistoryPopover() {
  const popover = document.getElementById('chat-history-popover');
  if (!popover || popover.hidden) return;
  popover.hidden = true;
  _chatHistoryPopoverCleanup?.();
  _chatHistoryPopoverCleanup = null;
  document.getElementById('chat-history-all-btn')?.setAttribute('aria-expanded', 'false');
}

function toggleChatHistory() {
  const popover = document.getElementById('chat-history-popover');
  const anchor = document.getElementById('chat-history-all-btn');
  if (!popover || !anchor) return;
  if (!popover.hidden) {
    closeChatHistoryPopover();
    anchor.focus();
    return;
  }
  // Out of the sidebar, whose overflow and stacking would clip the card.
  if (popover.parentElement !== document.body) document.body.appendChild(popover);
  popover.hidden = false;
  renderChatHistory();
  _placeChatHistoryPopover(popover, anchor);
  const reposition = () => _placeChatHistoryPopover(popover, anchor);
  const onKeydown = (event) => {
    if (event.key !== 'Escape') return;
    closeChatHistoryPopover();
    anchor.focus();
  };
  const onPointerDown = (event) => {
    if (!popover.contains(event.target) && !anchor.contains(event.target)) closeChatHistoryPopover();
  };
  window.addEventListener('resize', reposition, { passive: true });
  document.addEventListener('keydown', onKeydown);
  document.addEventListener('pointerdown', onPointerDown, true);
  _chatHistoryPopoverCleanup = () => {
    window.removeEventListener('resize', reposition);
    document.removeEventListener('keydown', onKeydown);
    document.removeEventListener('pointerdown', onPointerDown, true);
  };
  anchor.setAttribute('aria-expanded', 'true');
  document.getElementById('chat-history-search')?.focus();
}

let _discoveryHistoryPopoverCleanup = null;

function closeDiscoveryHistoryPopover() {
  const popover = document.getElementById('discovery-history-popover');
  if (!popover || popover.hidden) return;
  popover.hidden = true;
  _discoveryHistoryPopoverCleanup?.();
  _discoveryHistoryPopoverCleanup = null;
  document.getElementById('discovery-history-all-btn')?.setAttribute('aria-expanded', 'false');
}

function toggleDiscoveryHistory() {
  const popover = document.getElementById('discovery-history-popover');
  const anchor = document.getElementById('discovery-history-all-btn');
  if (!popover || !anchor) return;
  if (!popover.hidden) {
    closeDiscoveryHistoryPopover();
    anchor.focus();
    return;
  }
  if (popover.parentElement !== document.body) document.body.appendChild(popover);
  popover.hidden = false;
  renderDiscoveryHistory();
  const filterInput = document.getElementById('discovery-history-filter');
  if (filterInput && !filterInput.dataset.bound) {
    filterInput.dataset.bound = '1';
    filterInput.addEventListener('input', onDiscoveryHistoryFilterInput);
  }
  _placeChatHistoryPopover(popover, anchor);
  const reposition = () => _placeChatHistoryPopover(popover, anchor);
  const onKeydown = (event) => {
    if (event.key !== 'Escape') return;
    closeDiscoveryHistoryPopover();
    anchor.focus();
  };
  const onPointerDown = (event) => {
    if (!popover.contains(event.target) && !anchor.contains(event.target)) closeDiscoveryHistoryPopover();
  };
  window.addEventListener('resize', reposition, { passive: true });
  document.addEventListener('keydown', onKeydown);
  document.addEventListener('pointerdown', onPointerDown, true);
  _discoveryHistoryPopoverCleanup = () => {
    window.removeEventListener('resize', reposition);
    document.removeEventListener('keydown', onKeydown);
    document.removeEventListener('pointerdown', onPointerDown, true);
  };
  anchor.setAttribute('aria-expanded', 'true');
  (filterInput || popover.querySelector('.chat-item'))?.focus();
}

function startNewDiscoverySearch() {
  switchDiscoveryWorkspace('articles', { focus: false });
  state.activeDiscoveryHistoryId = '';
  state.discoveryCompareTo = null;
  setDiscoveryOpenedSearch(null);
  const modeEl = document.getElementById('discovery-mode');
  if (modeEl) modeEl.value = 'search';
  onDiscoveryModeChange();
  const inputEl = document.getElementById('discovery-input');
  if (inputEl) inputEl.value = '';
  const nbEl = document.getElementById('discovery-notebook-select');
  if (nbEl) nbEl.value = '';
  state.discoveryNotebookContext = null;
  syncDiscoveryNotebookPicker();
  updateDiscoveryNotebookContext();
  const maxEl = document.getElementById('discovery-max-results');
  if (maxEl) maxEl.value = '8';
  state.discoveryResults = [];
  setDiscoveryStatus('');
  renderDiscoveryWelcome();
  renderDiscoveryDebug(null);
  updateDiscoveryPrivacyUi();
  switchTab?.('discovery');
  document.getElementById('discovery-input')?.focus();
  renderDiscoveryHistory();
}

function _articleScopeFromMessages(messages) {
  const rows = Array.isArray(messages) ? messages : [];
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    const meta = rows[i]?.meta;
    if (!meta || !Object.prototype.hasOwnProperty.call(meta, 'article_path')) continue;
    if (meta.article_scope_once === true) return { path: '', title: '', once: false, paths: [], notebook: null };
    const notebookId = String(meta.article_scope_notebook_id || '').trim();
    return {
      path: String(meta.article_path || '').trim(),
      title: String(meta.article_title || '').trim(),
      once: false,
      paths: Array.isArray(meta.article_paths) ? meta.article_paths : [],
      notebook: notebookId ? { id: notebookId, title: String(meta.article_title || '').trim() } : null,
    };
  }
  return { path: '', title: '', once: false, paths: [], notebook: null };
}

function _chatArticleScopePaths(scope = state.chatArticleScope) {
  const paths = Array.isArray(scope?.paths) ? scope.paths : [];
  const single = String(scope?.path || '').trim();
  return paths.length ? paths : (single ? [single] : []);
}

function renderChatArticleScope() {
  updateChatOptionsCount();
  const banner = document.getElementById('chat-article-scope');
  const labelEl = document.getElementById('chat-article-scope-label');
  const titleEl = document.getElementById('chat-article-scope-title');
  const input = document.getElementById('msg-input');
  const paths = _chatArticleScopePaths();
  const path = paths.length === 1 ? paths[0] : '';
  const titles = Array.isArray(state.chatArticleScope?.titles) ? state.chatArticleScope.titles : [];
  const notebook = state.chatArticleScope?.notebook || null;
  const title = notebook
    ? _ti('chat.notebookScopeName', { title: notebook.title || _tr('Sans titre'), count: paths.length })
    : paths.length > 1
    ? _ti('chat.articleScopeCount', { count: paths.length })
    : String(state.chatArticleScope?.title || '').trim() || path.split(/[\\/]/).pop() || path;
  const once = Boolean(state.chatArticleScope?.once);
  if (banner) banner.hidden = !paths.length;
  if (labelEl) {
    labelEl.textContent = notebook && !once
      ? _ti('chat.notebookScopeLabel')
      : _tr(once ? 'Prochaine question limitée à' : 'Questions limitées à');
  }
  const notebookBtn = document.getElementById('chat-notebook-btn');
  if (notebookBtn) {
    notebookBtn.classList.toggle('is-active', Boolean(notebook && paths.length));
    notebookBtn.setAttribute('aria-pressed', notebook && paths.length ? 'true' : 'false');
  }
  if (titleEl) {
    titleEl.textContent = title;
    titleEl.title = paths.length > 1 ? (titles.length ? titles : paths).join('\n') : path;
  }
  if (input) {
    input.placeholder = notebook && paths.length
      ? _ti('chat.notebookInputPlaceholder')
      : paths.length > 1
      ? _ti('chat.articlesInputPlaceholder')
      : path
      ? _ti('chat.articleInputPlaceholder')
      : _ti('chat.inputPlaceholder');
  }
  document.querySelectorAll('.source-filter-btn').forEach((btn) => {
    const active = Boolean(path && once && String(btn.dataset.sourcePath || '') === path);
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function setChatArticleScope(articlePath, articleTitle = '', options = {}) {
  const paths = Array.isArray(options.paths)
    ? [...new Set(options.paths.map((value) => String(value || '').trim()).filter(Boolean))]
    : [];
  state.chatArticleScope = {
    path: paths.length > 1 ? '' : String(articlePath || paths[0] || '').trim(),
    title: String(articleTitle || '').trim(),
    once: Boolean(options.once),
    paths: paths.length > 1 ? paths : [],
    titles: paths.length > 1 && Array.isArray(options.titles) ? options.titles : [],
    notebook: options.notebook?.id
      ? { id: String(options.notebook.id), title: String(options.notebook.title || '').trim() }
      : null,
  };
  state.chatArticleScopeRevision += 1;
  renderChatArticleScope();
}

function setChatArticlesScope(articles, options = {}) {
  const items = (Array.isArray(articles) ? articles : []).filter((item) => String(item?.path || '').trim());
  if (items.length === 1) {
    setChatArticleScope(items[0].path, items[0].title || '', options);
    return;
  }
  setChatArticleScope('', '', {
    ...options,
    paths: items.map((item) => item.path),
    titles: items.map((item) => String(item.title || item.path)),
  });
}

function clearChatArticleScope() {
  setChatArticleScope('', '');
}

function restoreChatArticleScope(messages) {
  const scope = _articleScopeFromMessages(messages);
  setChatArticleScope(scope.path, scope.title, { once: scope.once, paths: scope.paths, notebook: scope.notebook });
}

// Limits the chat to the PDFs of a notebook. Text sources (typed or pasted
// by hand) have no library file and stay out.
async function openChatNotebookPicker() {
  const dialog = _discoveryNotebookDialog();
  const list = dialog.querySelector('#discovery-notebook-dialog-list');
  dialog.querySelector('#discovery-notebook-dialog-title').textContent = _ti('chat.pickNotebook');
  dialog.querySelector('[data-discovery-dialog-cancel]').textContent = _ti('notebooks.cancel');
  list.innerHTML = `<p class="discovery-notebook-dialog-empty">${escapeHtml(_ti('drafting.notebookLoading'))}</p>`;
  if (!dialog.open) dialog.showModal();
  let notebooks = [];
  try {
    const d = await fetchAllPaginated('/api/notebooks', ['notebooks'], { limit: 500 });
    notebooks = Array.isArray(d?.notebooks) ? d.notebooks : [];
  } catch (_) { notebooks = []; }
  if (!notebooks.length) {
    list.innerHTML = `<p class="discovery-notebook-dialog-empty">${escapeHtml(_ti('chat.noNotebookToUse'))}</p>`;
    return;
  }
  const activeId = String(state.chatArticleScope?.notebook?.id || '');
  list.innerHTML = notebooks.map((notebook) => `
    <button type="button" class="discovery-notebook-choice${String(notebook.id) === activeId ? ' is-current' : ''}" ${bindBbHandler('click', function() {
      void useNotebookInChat(notebook.id);
    })}>
      <strong>${escapeHtml(notebook.title || _tr('Sans titre'))}</strong>
      <span>${escapeHtml(_ti('notebooks.sourceCount', { count: Number(notebook.source_count) || 0 }))}</span>
    </button>`).join('');
}

async function useNotebookInChat(notebookId) {
  document.getElementById('discovery-notebook-dialog')?.close();
  let notebook = null;
  try {
    const response = await fetch('/api/notebooks/' + encodeURIComponent(notebookId), { cache: 'no-store' });
    if (response.ok) notebook = await response.json();
  } catch (_) { notebook = null; }
  if (!notebook) {
    showToast(_ti('chat.notebookLoadFailed'), 4000, 'error');
    return;
  }
  const title = String(notebook.title || '').trim() || _tr('Sans titre');
  const sources = Array.isArray(notebook.sources) ? notebook.sources : [];
  const seen = new Set();
  const articles = [];
  for (const source of sources) {
    const path = String(source?.source_path || '').trim();
    if (!path || seen.has(path)) continue;
    seen.add(path);
    articles.push({ path, title: String(source.title || path) });
  }
  if (!articles.length) {
    showToast(_ti('chat.notebookNoPdf', { title }), 6000, 'error');
    return;
  }
  // Matches ChatRequest.article_paths max_length on the server.
  const kept = articles.slice(0, 200);
  setChatArticleScope('', title, {
    paths: kept.map((item) => item.path),
    titles: kept.map((item) => item.title),
    notebook: { id: String(notebook.id || notebookId), title },
  });
  const textSources = sources.filter((source) => !String(source?.source_path || '').trim()).length;
  if (textSources || articles.length > kept.length) {
    showToast(_ti('chat.notebookScopePartial', { count: kept.length, skipped: textSources + articles.length - kept.length }), 6000);
  }
  document.getElementById('msg-input')?.focus();
}

function isLocalOllamaProvider() {
  return String(state.providerInfo?.provider || '').toLowerCase() === 'ollama';
}

function formatOllamaRuntimeStatus(runtime, modelName = '') {
  const wanted = String(modelName || '').trim();
  const running = Array.isArray(runtime?.running_models) ? runtime.running_models : [];

  if (!runtime || runtime.ok === false) {
    return 'Préparation de la réponse…';
  }
  if (runtime.model_loaded) {
    return 'Génération en cours…';
  }
  if (running.length > 0 && wanted) {
    return `Préparation du modèle ${wanted}…`;
  }
  if (running.length > 0) {
    return 'Préparation de la génération…';
  }
  if (wanted) {
    return `Chargement du modèle ${wanted}…`;
  }
  return 'Préparation de la réponse…';
}

async function fetchOllamaRuntimeStatus(modelName) {
  if (!isLocalOllamaProvider()) return null;
  try {
    const url = document.getElementById('ollama-url')?.value || 'http://localhost:11434';
    const r = await fetch(
      '/api/ollama-runtime?model=' + encodeURIComponent(modelName || '') +
      '&url=' + encodeURIComponent(url),
    );
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    return null;
  }
}

async function openChatSession(chatId, quiet = false) {
  const id = (chatId || '').trim();
  if (!id) return;
  try {
    const d = await fetchAllPaginated(
      '/api/chats/' + encodeURIComponent(id),
      ['messages'],
      { limit: 300, prepend: true },
    );
    state.chatViewRevision += 1;
    state.activeChatId = d?.chat?.id || id;
    state.activeChatTitle = d?.chat?.title || '';
    localStorage.setItem(LS_PREFIX + 'active-chat-id', state.activeChatId);
    restoreChatArticleScope(d?.messages || []);
    renderChatHistory();
    _renderChatMessages(d?.messages || []);
  } catch (e) {
    if (!quiet) showToast(_tr('Impossible de charger ce chat.'));
  }
}

async function deleteChatSession(chatId) {
  const id = (chatId || '').trim();
  if (!id) return;
  if (state.streaming && (state.activeStreamChatId === id || (!state.activeStreamChatId && state.activeChatId === id))) {
    await stopCurrentChat(true, 'chat_deleted');
  }
  const sessions = Array.isArray(state.chatSessions) ? state.chatSessions : [];
  const idx = sessions.findIndex((c) => c.id === id);
  if (idx === -1) return;
  const removed = sessions[idx];
  const wasActive = state.activeChatId === id;
  sessions.splice(idx, 1);
  renderChatHistory();
  if (wasActive) startNewChat();
  deleteWithUndo('chat:' + id, {
    message: 'Chat supprimé.',
    restore: () => {
      const list = Array.isArray(state.chatSessions) ? state.chatSessions : [];
      list.splice(Math.min(idx, list.length), 0, removed);
      renderChatHistory();
      if (wasActive) openChatSession(id, true);
    },
    commit: async () => {
      try {
        const r = await fetch('/api/chats/' + encodeURIComponent(id), { method: 'DELETE', keepalive: true });
        const d = await r.json();
        if (!d?.ok) throw new Error('Suppression impossible');
      } catch (e) {
        showToast(_tr('Erreur pendant la suppression du chat.'));
        loadChatHistory();
      }
    },
  });
}

function startNewChat(options = {}) {
  state.chatViewRevision += 1;
  state.activeChatId = '';
  state.activeChatTitle = '';
  if (!options.preserveArticleScope) clearChatArticleScope();
  resetChatSourceRegistry();
  localStorage.removeItem(LS_PREFIX + 'active-chat-id');
  clearChat();
  renderChatHistory();
  if (state.streaming) _setSendButtonStop();
  else _setSendButtonIdle();
  document.getElementById('msg-input')?.focus();
}

async function refreshActiveChatIfStillOpen(chatId, options = {}) {
  const id = String(chatId || '').trim();
  if (!id || state.activeChatId !== id) return;
  try {
    const d = await fetchAllPaginated(
      '/api/chats/' + encodeURIComponent(id),
      ['messages'],
      { limit: 300, prepend: true },
    );
    if (state.activeChatId !== id) return;
    if (state.activeChatId !== id) return;
    state.activeChatTitle = d?.chat?.title || state.activeChatTitle || '';
    if (options.restoreArticleScope !== false) restoreChatArticleScope(d?.messages || []);
    _renderChatMessages(d?.messages || []);
  } catch (e) {}
}

async function requestChatStreamAbort(requestId, model, reason = 'unknown') {
  const payload = {};
  const normalizedRequestId = String(requestId || '').trim();
  const normalizedModel = String(model || '').trim();
  if (normalizedRequestId) payload.request_id = normalizedRequestId;
  if (normalizedModel) payload.model = normalizedModel;
  if (!Object.keys(payload).length) return null;
  payload.reason = String(reason || 'unknown').trim().toLowerCase().slice(0, 40) || 'unknown';
  try {
    const response = await fetch('/api/stream/abort', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
      keepalive: true,
    });
    return await response.json();
  } catch (e) {
    return null;
  }
}

let _pageExitAbortSent = false;

function abortActiveStreamsOnPageExit() {
  const activeStreams = [];
  if (state.streaming) {
    activeStreams.push({
      request_id: String(state.activeStreamRequestId || '').trim(),
      model: String(state.activeStreamModel || document.getElementById('model-select')?.value || '').trim(),
      controller: state.chatAbortController,
    });
  }
  if (_nb.streaming) {
    activeStreams.push({
      request_id: String(_nb.activeStreamRequestId || '').trim(),
      model: String(_nb.activeStreamModel || optionalSelectedModel('notebook') || '').trim(),
      controller: _nb.chatAbortController,
    });
  }
  if (!activeStreams.length || _pageExitAbortSent) return;
  _pageExitAbortSent = true;

  const sent = new Set();
  activeStreams.forEach(({ request_id: requestId, model, controller }) => {
    const payload = { reason: 'page_exit' };
    if (requestId) payload.request_id = requestId;
    if (model) payload.model = model;
    _recordClientError(
      'chat.abort',
      'Active stream aborted because the page is closing.',
      '',
      requestId,
    );
    const key = JSON.stringify(payload);
    if (Object.keys(payload).length && !sent.has(key)) {
      sent.add(key);
      const body = JSON.stringify(payload);
      let beaconQueued = false;
      try {
        beaconQueued = navigator.sendBeacon(
          '/api/stream/abort',
          new Blob([body], { type: 'application/json' }),
        );
      } catch {}
      if (!beaconQueued) {
        fetch('/api/stream/abort', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body,
          keepalive: true,
        }).catch(() => null);
      }
    }
    try { controller?.abort(); } catch {}
  });
}

const _bibliobrainWindowSessionId = globalThis.crypto?.randomUUID?.()
  || `window-${Date.now()}-${Math.random().toString(16).slice(2)}`;
let _bibliobrainManagedWindow = false;
let _bibliobrainWindowRegistered = false;

async function registerBibliobrainWindow() {
  try {
    const response = await fetch('/api/app/window/open', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: _bibliobrainWindowSessionId}),
      keepalive: true,
    });
    if (!response.ok) return;
    const payload = await response.json();
    _bibliobrainManagedWindow = Boolean(payload?.managed_server && payload?.auto_shutdown);
    _bibliobrainWindowRegistered = _bibliobrainManagedWindow;
    document.documentElement.classList.toggle('managed-server', _bibliobrainManagedWindow);
  } catch {}
}

function closeBibliobrainWindowSession() {
  if (!_bibliobrainManagedWindow || !_bibliobrainWindowRegistered) return;
  _bibliobrainWindowRegistered = false;
  const body = JSON.stringify({session_id: _bibliobrainWindowSessionId});
  let queued = false;
  try {
    queued = navigator.sendBeacon(
      '/api/app/window/close',
      new Blob([body], {type: 'application/json'}),
    );
  } catch {}
  if (!queued) {
    fetch('/api/app/window/close', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body,
      keepalive: true,
    }).catch(() => null);
  }
}

function renderBibliobrainStoppedPage() {
  const title = _ti('app.stoppedTitle');
  const detail = _ti('app.stoppedDetail');
  document.title = title;
  document.body.innerHTML = `
    <main class="app-stopped-screen">
      <div class="app-stopped-icon" aria-hidden="true">✓</div>
      <h1>${title}</h1>
      <p>${detail}</p>
    </main>
  `;
}

async function shutdownBibliobrain() {
  if (!window.confirm(_ti('app.quitConfirm'))) return;
  document.querySelectorAll('#app-quit-btn, #app-quit-settings-btn').forEach((button) => {
    button.disabled = true;
  });
  try {
    const response = await fetch('/api/app/shutdown', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    _bibliobrainWindowRegistered = false;
    state.ragInitBusy = false;
    state.startupGuideRagRunning = false;
    renderBibliobrainStoppedPage();
    window.setTimeout(() => {
      try { window.close(); } catch {}
    }, 700);
  } catch {
    document.querySelectorAll('#app-quit-btn, #app-quit-settings-btn').forEach((button) => {
      button.disabled = false;
    });
    showToast(_ti('app.shutdownFailed'), 4500, 'error');
  }
}

async function stopCurrentChat(silent = false, reason = 'user_stop') {
  if (!state.streaming) return;
  const requestId = String(state.activeStreamRequestId || '').trim();
  const model = String(state.activeStreamModel || document.getElementById('model-select')?.value || '').trim();
  const normalizedReason = String(reason || 'user_stop').trim().toLowerCase().slice(0, 40) || 'user_stop';
  _recordClientError('chat.abort', normalizedReason, '', requestId);
  const abortPromise = requestChatStreamAbort(requestId, model, normalizedReason);
  const ctl = state.chatAbortController;
  if (abortPromise) {
    await Promise.race([
      abortPromise,
      new Promise((resolve) => window.setTimeout(() => resolve(null), 1200)),
    ]);
  }
  if (ctl) {
    try { ctl.abort(); } catch {}
  }
  state.chatAbortController = null;
  state.activeStreamRequestId = '';
  state.activeStreamModel = '';
  state.activeStreamChatId = '';
  state.streaming = false;
  _setSendButtonIdle();
  if (!silent) showToast(_tr('Génération interrompue.'));
}

async function sendMessage(options = {}) {
  if (state.streaming) {
    stopCurrentChat(false, 'user_stop');
    return;
  }
  await syncOllamaBaseUrl({ silent: true });

  const input = document.getElementById('msg-input');
  const hasMessageOverride = Object.prototype.hasOwnProperty.call(options || {}, 'messageOverride');
  const msg = hasMessageOverride
    ? String(options.messageOverride || '').trim()
    : input.value.trim();
  if (!msg) return;
  const hintAction = hasMessageOverride
    ? String(options.hintAction || '').trim().toLowerCase()
    : resolveChatHintAction(input, msg);
  const forceVisualAnalysis = Object.prototype.hasOwnProperty.call(
    options || {},
    'forceVisualAnalysis',
  )
    ? Boolean(options.forceVisualAnalysis)
    : Boolean(state.chatForceVisualOnce);

  const model = requireSelectedModel();
  const threshold = parseFloat(document.getElementById('threshold').value);
  const tags = parseFilterInput(document.getElementById('chat-tags-filter')?.value || '');
  const collections = parseFilterInput(document.getElementById('chat-collections-filter')?.value || '');
  const articleScope = {
    path: String(state.chatArticleScope?.path || '').trim(),
    title: String(state.chatArticleScope?.title || '').trim(),
    once: Boolean(state.chatArticleScope?.once),
    paths: _chatArticleScopePaths(),
    notebookId: String(state.chatArticleScope?.notebook?.id || ''),
  };
  const articleScopeRevisionAtStart = state.chatArticleScopeRevision;
  if (!model) return;

  const chatIdAtStart = state.activeChatId || '';
  const chatViewRevisionAtStart = state.chatViewRevision;
  let streamChatId = chatIdAtStart;
  let isNewConversation = false;

  if (!hasMessageOverride) {
    input.value = '';
    delete input.dataset.hintAction;
    delete input.dataset.hintText;
    input.style.height = 'auto';
  }
  state.streaming = true;
  _setSendButtonStop();
  updateChatVisualForceUi();
  window.dispatchEvent(new CustomEvent('bibliobrain:milestone', { detail: { name: 'question-asked' } }));

  if (options.appendUserBubble !== false) {
    _appendUserBubble(msg);
  }
  const bubble = _appendAssistantPlaceholder();
  bubble._ragQuery = msg;
  const msgs = document.getElementById('messages');
  let fullText = '';
  let lastStreamRenderAt = 0;
  let finalSources = [];
  let answerOrigin = '';
  let discoverySession = null;
  let externalSearchConfirmation = null;
  let receivedFirstToken = false;
  let receivedServerStatus = false;
  let runtimePollTimer = null;
  let scrollQueued = false;
  let hadError = false;
  const debugMetricsEnabled = isDebugMetricsEnabled();
  const debugTracker = debugMetricsEnabled ? createDebugMetricsTracker({ model }) : null;
  const shouldPollRuntime = isLocalOllamaProvider();

  const scheduleScroll = () => {
    if (scrollQueued || !isVisualUpdateVisible(msgs)) return;
    scrollQueued = true;
    window.requestAnimationFrame(() => {
      scrollQueued = false;
      if (isVisualUpdateVisible(msgs)) msgs.scrollTop = msgs.scrollHeight;
    });
  };

  const controller = new AbortController();
  state.chatAbortController = controller;
  state.activeStreamModel = model;
  state.activeStreamChatId = streamChatId;

  const pollRuntime = async () => {
    if (!shouldPollRuntime) return;
    if (receivedServerStatus) return;
    if (!state.streaming || receivedFirstToken) return;
    const runtime = await fetchOllamaRuntimeStatus(model);
    if (receivedServerStatus) return;
    if (!state.streaming || receivedFirstToken) return;
    const runtimeMsg = _tr(formatOllamaRuntimeStatus(runtime, model));
    if (bubble.classList.contains('status')) {
      bubble.innerHTML = `<span class="spinner"></span><span class="status-wait-text">${escapeHtml(runtimeMsg)}</span>`;
    }
  };

  try {
    if (shouldPollRuntime) {
      pollRuntime();
      runtimePollTimer = window.setInterval(pollRuntime, 1800);
    }

    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
      cache: 'no-store',
      body: JSON.stringify({
        message: msg,
        model,
        threshold,
        tags,
        collections,
        chat_id: chatIdAtStart || null,
        thinking: isThinkingEnabled(),
        force_thinking_small_models: isForceThinkingSmallModelsEnabled(),
        privacy_ack: Boolean(options.privacyAckExternalSearch),
        external_search_confirmed: Boolean(options.externalSearchConfirmed),
        article_search_scope: String(options.articleSearchScope || ''),
        article_path: articleScope.path,
        article_title: articleScope.title,
        article_paths: articleScope.paths.length > 1 ? articleScope.paths : [],
        article_scope_once: articleScope.once,
        article_scope_notebook_id: articleScope.notebookId,
        depth: getChatDepth(),
        hint_action: hintAction,
        force_visual_analysis: forceVisualAnalysis,
      }),
      signal: controller.signal,
    });
    if (response.ok && forceVisualAnalysis) {
      setChatForceVisual(false);
    }
    if (
      articleScope.once
      && state.chatArticleScopeRevision === articleScopeRevisionAtStart
    ) {
      clearChatArticleScope();
    }

    for await (const event of readSSE(response)) {
      if (event.type === 'stream_meta') {
        state.activeStreamRequestId = String(event.request_id || '');
      } else if (event.type === 'chat_meta' && event.chat) {
        streamChatId = event.chat.id || streamChatId;
        state.activeStreamChatId = streamChatId;
        isNewConversation = Boolean(event.created);
        if (state.chatViewRevision === chatViewRevisionAtStart) {
          state.activeChatId = streamChatId || state.activeChatId;
          state.activeChatTitle = event.chat.title || '';
          if (state.activeChatId) localStorage.setItem(LS_PREFIX + 'active-chat-id', state.activeChatId);
        }
        // The server creates the conversation before generation starts. Make it
        // reachable immediately, even if another chat has since been opened.
        const sessions = Array.isArray(state.chatSessions) ? state.chatSessions : [];
        const previous = sessions.find((chat) => chat.id === streamChatId);
        state.chatSessions = [
          { ...previous, ...event.chat },
          ...sessions.filter((chat) => chat.id !== streamChatId),
        ];
        renderChatHistory();
      } else if (event.type === 'status') {
        receivedServerStatus = true;
        if (runtimePollTimer) {
          clearInterval(runtimePollTimer);
          runtimePollTimer = null;
        }
        bubble.style.whiteSpace = '';
        bubble.className = 'bubble status';
        bubble.innerHTML = `<span class="spinner"></span><span class="status-wait-text">${escapeHtml(event.msg)}</span>`;
      } else if (event.type === 'tool_started') {
        const toolName = String(event.tool?.name || '').trim();
        bubble.dataset.activeTool = toolName;
        receivedServerStatus = true;
        if (!receivedFirstToken && toolName) {
          bubble.style.whiteSpace = '';
          bubble.className = 'bubble status';
          bubble.innerHTML = `<span class="spinner"></span><span class="status-wait-text">${escapeHtml(_ti('chat.toolRunning', { tool: toolName }))}</span>`;
        }
      } else if (event.type === 'tool_result') {
        const toolName = String(event.tool?.name || bubble.dataset.activeTool || '').trim();
        bubble.dataset.lastTool = toolName;
        bubble.dataset.toolOk = event.ok === false ? '0' : '1';
        delete bubble.dataset.activeTool;
        if (event.ok === false && toolName) {
          showToast(_ti('chat.toolFailed', { tool: toolName }), 4200, 'error');
        }
      } else if (event.type === 'tool_confirmation') {
        const confirmationMessage = String(event.confirmation?.message || '').trim();
        if (!receivedFirstToken && confirmationMessage) {
          receivedServerStatus = true;
          bubble.style.whiteSpace = '';
          bubble.className = 'bubble status';
          bubble.innerHTML = `<span class="status-wait-text">${escapeHtml(confirmationMessage)}</span>`;
        }
      } else if (event.type === 'token') {
        receivedFirstToken = true;
        if (runtimePollTimer) {
          clearInterval(runtimePollTimer);
          runtimePollTimer = null;
        }
        const piece = event.text || '';
        if (!piece) continue;
        fullText += piece;
        bubble.dataset.copyText = stripThinkingMarkup(fullText);
        debugTracker?.update(piece);
        bubble.className = 'bubble bot';
        // Throttle : reconstruire le DOM à chaque token est trop coûteux.
        // Le rendu final (sources/done/erreur) repasse toujours derrière.
        const nowTs = performance.now();
        if (nowTs - lastStreamRenderAt >= streamRenderIntervalMs()) {
          lastStreamRenderAt = nowTs;
          renderRichAssistantOutput(bubble, fullText, {
            streaming: true,
            appendCursor: true,
            normalizeCitations: false,
            emptyText: 'Réflexion en cours…',
            discoverySession,
            externalSearchConfirmation,
            debugMetrics: withAnswerOrigin(debugTracker?.snapshot(), answerOrigin),
          });
          scheduleScroll();
        }
      } else if (event.type === 'answer_replace') {
        receivedFirstToken = true;
        if (runtimePollTimer) {
          clearInterval(runtimePollTimer);
          runtimePollTimer = null;
        }
        fullText = String(event.text || '');
        bubble.dataset.copyText = stripThinkingMarkup(fullText);
        debugTracker?.replace(fullText);
        bubble.className = 'bubble bot';
        renderRichAssistantOutput(bubble, fullText, {
          streaming: true,
          appendCursor: true,
          normalizeCitations: false,
          sources: finalSources,
          hideSourcesSection: true,
          emptyText: _tr('Réponse en cours de validation…'),
          discoverySession,
          externalSearchConfirmation,
          debugMetrics: withAnswerOrigin(debugTracker?.snapshot(), answerOrigin),
        });
        scheduleScroll();
      } else if (event.type === 'discovery_session') {
        discoverySession = normalizeDiscoverySession(event.data);
        if (fullText) {
          bubble.className = 'bubble bot';
          renderRichAssistantOutput(bubble, fullText, {
            normalizeCitations: true,
            sources: finalSources,
            showAllSources: shouldShowAllAssistantSources(answerOrigin),
            discoverySession,
            externalSearchConfirmation,
            debugMetrics: withAnswerOrigin(debugTracker?.snapshot(), answerOrigin),
          });
        }
      } else if (event.type === 'external_search_confirmation') {
        receivedServerStatus = true;
        receivedFirstToken = true;
        if (runtimePollTimer) {
          clearInterval(runtimePollTimer);
          runtimePollTimer = null;
        }
        externalSearchConfirmation = normalizeExternalSearchConfirmation(event);
        fullText = String(event.msg || '').trim();
        bubble.dataset.copyText = stripThinkingMarkup(fullText);
        bubble.className = 'bubble bot';
        renderRichAssistantOutput(bubble, fullText, {
          normalizeCitations: true,
          sources: finalSources,
          showAllSources: shouldShowAllAssistantSources(answerOrigin),
          discoverySession,
          externalSearchConfirmation,
          debugMetrics: withAnswerOrigin(debugTracker?.snapshot(), answerOrigin),
        });
        scheduleScroll();
      } else if (event.type === 'sources') {
        finalSources = normalizeChatSourcesForRender(Array.isArray(event.data) ? event.data : []);
        answerOrigin = String(event.answer_origin || answerOrigin || '').trim();
        bubble._answerOrigin = answerOrigin;
        if (typeof rememberLastRagRetrieval === 'function') {
          rememberLastRagRetrieval(msg, finalSources, answerOrigin);
        }
        const visibleAnswer = normalizeAssistantCitations(stripThinkingMarkup(fullText), finalSources);
        bubble.dataset.copyText = visibleAnswer;
        renderRichAssistantOutput(bubble, fullText, {
          normalizeCitations: true,
          sources: finalSources,
          showAllSources: shouldShowAllAssistantSources(answerOrigin),
          discoverySession,
          externalSearchConfirmation,
          debugMetrics: withAnswerOrigin(debugTracker?.snapshot(), answerOrigin),
        });
      } else if (event.type === 'citation_anchors') {
        const anchorsMap = event.data && typeof event.data === 'object' ? event.data : {};
        finalSources = normalizeChatSourcesForRender(mergeCitationAnchorsIntoSources(finalSources, anchorsMap));
      } else if (event.type === 'pipeline_metrics') {
        if (typeof debugTracker !== 'undefined' && debugTracker && typeof debugTracker.addPipelineMetric === 'function') {
          debugTracker.addPipelineMetric(event.stage, event.data);
          if (fullText) {
            renderRichAssistantOutput(bubble, fullText, {
              normalizeCitations: true,
              sources: finalSources,
              showAllSources: shouldShowAllAssistantSources(answerOrigin),
              discoverySession,
              externalSearchConfirmation,
              debugMetrics: withAnswerOrigin(debugTracker.snapshot(), answerOrigin),
            });
          }
        }
      } else if (event.type === 'error') {
        hadError = true;
        bubble.style.whiteSpace = '';
        bubble.className = 'bubble bot';
        bubble.dataset.copyText = String(event.msg || 'Erreur');
        bubble.innerHTML = `<span style="color:var(--red)">&#9888; ${escapeHtml(event.msg || 'Erreur')}</span>`;
        _upsertChatCopyButton(bubble);
        if (event.remote_fallback) {
          showToast(_tr('LLM distant inaccessible - basculement automatique sur Ollama local.'), 6000);
          loadProviderInfo().then(() => loadModels({ silent: true }));
        }
      }
    }
  } catch (e) {
    bubble.style.whiteSpace = '';
    if (controller.signal.aborted) {
      bubble.className = 'bubble bot';
      if (fullText) {
        bubble.dataset.copyText = stripThinkingMarkup(`${fullText}\n\n_Réponse interrompue._`);
        renderRichAssistantOutput(
          bubble,
          `${fullText}\n\n_Réponse interrompue._`,
          {
            normalizeCitations: true,
            sources: finalSources,
            showAllSources: shouldShowAllAssistantSources(answerOrigin),
            discoverySession,
            externalSearchConfirmation,
            debugMetrics: withAnswerOrigin(debugTracker?.snapshot(), answerOrigin),
          },
        );
      }
      else {
        bubble.dataset.copyText = 'Génération interrompue.';
        bubble.innerHTML = '<span style="color:var(--muted)">' + _tr('Génération interrompue.') + '</span>';
        _upsertChatCopyButton(bubble);
      }
    } else {
      hadError = true;
      bubble.className = 'bubble bot';
      const msg = await explainChatStreamError(e);
      bubble.dataset.copyText = `Erreur : ${msg}`;
      bubble.innerHTML = `<span style="color:var(--red)">Erreur : ${escapeHtml(msg)}</span>`;
      _upsertChatCopyButton(bubble);
    }
  } finally {
    if (runtimePollTimer) {
      clearInterval(runtimePollTimer);
      runtimePollTimer = null;
    }
    state.chatAbortController = null;
    state.activeStreamRequestId = '';
    state.activeStreamModel = '';
    state.activeStreamChatId = '';
    state.streaming = false;
    _setSendButtonIdle();
    updateChatVisualForceUi();
    await loadChatHistory();
    await refreshActiveChatIfStillOpen(streamChatId, {
      restoreArticleScope: state.chatArticleScopeRevision === articleScopeRevisionAtStart,
    });
    if (typeof refreshRerankerStatusPill === 'function') {
      refreshRerankerStatusPill();
    }
  }

  if (fullText && !hadError) {
    bubble.dataset.copyText = normalizeAssistantCitations(stripThinkingMarkup(fullText), finalSources);
    renderRichAssistantOutput(bubble, fullText, {
      normalizeCitations: true,
      sources: finalSources,
      showAllSources: shouldShowAllAssistantSources(answerOrigin),
      discoverySession,
      externalSearchConfirmation,
      debugMetrics: withAnswerOrigin(debugTracker?.snapshot(), answerOrigin),
    });
    if (isNewConversation && streamChatId) {
      _autotitleChat(streamChatId, model);
    }
  }
  // The tutorial waits for this to point at the answer's citations.
  window.dispatchEvent(new CustomEvent('bibliobrain:milestone', { detail: { name: 'answer-ready', ok: Boolean(fullText && !hadError), bubble } }));
}

// Génère un titre court via le LLM après le premier échange d'une
// nouvelle conversation, puis rafraîchit l'historique.
async function _autotitleChat(chatId, model) {
  try {
    const r = await fetch('/api/chats/' + encodeURIComponent(chatId) + '/autotitle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: model || '' }),
    });
    const d = await r.json();
    if (!d?.ok || !d.title) return;
    const session = (state.chatSessions || []).find((c) => c.id === chatId);
    if (session) session.title = d.title;
    if (state.activeChatId === chatId) state.activeChatTitle = d.title;
    renderChatHistory();
  } catch (e) { /* titre heuristique conservé */ }
}
