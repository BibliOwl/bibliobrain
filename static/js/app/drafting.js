async function copyDraftingEditor(button = null) {
  const editor = _draftingGetEditor();
  const text = _draftingExtractCopyText(editor);
  if (!text) {
    showToast(_tr('Aucun texte à copier.'));
    return;
  }
  // Rich HTML for Word (headings, bold, lists, bibliography) plus plain text.
  const payload = _draftingBuildClipboardPayload(editor, { withBibliography: true });
  const ok = await _draftingWriteRichClipboard(payload.html, payload.text);
  if (!ok) {
    showToast(_tr('Copie impossible.'), 3600, 'error');
    return;
  }
  if (button) {
    button.classList.add('is-copied');
    button.textContent = 'Copié';
    clearTimeout(button._copiedTimer);
    button._copiedTimer = setTimeout(() => {
      button.classList.remove('is-copied');
      button.textContent = 'Copier';
    }, 1400);
  }
  showToast(_tr('Texte copié.'), 2200, 'success');
}

function _draftingGetExportMenuNodes() {
  return {
    root: document.getElementById('drafting-export-menu'),
    toggle: document.getElementById('drafting-export-toggle'),
    popover: document.getElementById('drafting-export-popover'),
  };
}

function closeDraftingExportMenu() {
  const { toggle, popover } = _draftingGetExportMenuNodes();
  if (toggle) toggle.setAttribute('aria-expanded', 'false');
  if (popover) popover.hidden = true;
}

function toggleDraftingExportMenu(event) {
  event?.preventDefault?.();
  event?.stopPropagation?.();
  const { toggle, popover } = _draftingGetExportMenuNodes();
  if (!toggle || !popover) return;
  const willOpen = popover.hidden;
  closeDraftingExportMenu();
  if (!willOpen) return;
  toggle.setAttribute('aria-expanded', 'true');
  popover.hidden = false;
}

function _draftingCollectBibliographyRecords() {
  return state.drafting.citationOrder
    .map((citationId) => state.drafting.citationIndex.get(citationId))
    .filter(Boolean)
    .map((record) => ({ ...record }));
}

function _draftingSuggestExportTitle(text) {
  const lines = String(text || '').split(/\r?\n/);
  for (const rawLine of lines) {
    const line = rawLine.replace(/\s+/g, ' ').trim();
    if (!line) continue;
    return line.replace(/\s*\[[A-Za-z]?\d+(?:,\s*[A-Za-z]?\d+)*\][\s.,;:!?-]*$/g, '').slice(0, 120).trim();
  }
  return '';
}

async function exportDraftingDocument(format = 'markdown') {
  const requested = String(format || '').toLowerCase();
  closeDraftingExportMenu();
  if (requested === 'bibtex') {
    downloadDraftingBibtex();
    return;
  }
  const exportFormat = requested === 'latex' ? 'latex' : 'markdown';
  const editor = _draftingGetEditor();
  const text = _draftingExtractCopyText(editor);
  if (!text) {
    showToast(_tr('Ajoutez du texte avant export.'));
    return;
  }
  if (_draftingGetPreviewTokens().length) {
    showToast(_tr('Validez ou retirez les citations prévisualisées avant export.'), 3600, 'info');
    return;
  }
  if (_draftingFindArgumentPreview()) {
    showToast(_tr('drafting.argument.previewBeforeExport'), 3600, 'info');
    return;
  }

  const toggle = document.getElementById('drafting-export-toggle');
  const optionButtons = Array.from(document.querySelectorAll('#drafting-export-popover .drafting-export-option'));
  const previousLabel = toggle?.textContent || 'Exporter';
  if (toggle) {
    toggle.disabled = true;
    toggle.textContent = _tr('Export…');
  }
  optionButtons.forEach((button) => { button.disabled = true; });

  try {
    const response = await fetch('/api/drafting-export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        // Markdown keeps headings, lists, bold and italic; LaTeX citations
        // travel as key markers so the server can write \cite{key}.
        content: _draftingSerializeMarkdown(editor, { citations: exportFormat === 'latex' ? 'latex' : 'label' }),
        export_format: exportFormat,
        title: _draftingSuggestExportTitle(text),
        bibliography: _draftingCollectBibliographyRecords()
          .map((record) => ({ ...record, latex_key: _draftingLatexKey(record) })),
      }),
    });
    const payload = await response.json();
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.detail || payload?.msg || `HTTP ${response.status}`);
    }
    const exportFile = normalizeChatExportFile(payload?.export_file);
    if (!exportFile) {
      throw new Error('Fichier exporté invalide.');
    }
    const link = document.createElement('a');
    link.href = exportFile.url;
    link.download = exportFile.filename;
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    link.remove();
    showToast(payload?.msg || _tr(`Export ${exportFormat === 'latex' ? 'LaTeX' : 'Markdown'} généré.`), 3400, 'success');
  } catch (e) {
    showToast(_tr('Export impossible: ') + (e?.message || _tr('erreur inconnue')), 4200, 'error');
  } finally {
    if (toggle) {
      toggle.disabled = false;
      toggle.textContent = previousLabel;
    }
    optionButtons.forEach((button) => { button.disabled = false; });
  }
}

function _draftingNormalizeCitationId(value) {
  return String(value || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function _draftingMakeDomId(prefix, value) {
  const safe = String(value || '').trim().replace(/[^A-Za-z0-9._:-]+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
  return `${prefix}${safe ? `-${safe}` : ''}`;
}

function _draftingGetEditor() {
  return document.getElementById('drafting-editor');
}

function _draftingGetResultPanel() {
  return document.getElementById('drafting-result');
}

// A late response must land in its own panel even if another review was
// started meanwhile, so renderers pass their view explicitly.
function _draftingGetReviewPanel(view = state.drafting.reviewView || 'proofread') {
  return document.getElementById(`drafting-${view}-result`);
}

function _draftingGetBibliographyList() {
  return document.getElementById('drafting-bibliography');
}

function _draftingGetBibliographyEmpty() {
  return document.getElementById('drafting-bibliography-empty');
}

function _draftingSyncNotebookSearchControls() {
  const select = document.getElementById('drafting-notebook-select');
  const input = document.getElementById('drafting-notebook-query');
  const button = document.getElementById('drafting-notebook-search-btn');
  const hasNotebooks = Array.isArray(state.drafting.notebookOptions)
    && state.drafting.notebookOptions.length > 0;
  const disabled = state.drafting.notebookOptionsLoading || !hasNotebooks;
  if (select) select.disabled = state.drafting.reviewBusy || disabled;
  if (input) input.disabled = state.drafting.reviewBusy || disabled;
  if (button && state.drafting.reviewActiveAction !== 'drafting-notebook-search-btn') {
    button.disabled = state.drafting.reviewBusy || disabled;
  }
}

function _draftingPopulateNotebookOptions(notebooks = []) {
  const select = document.getElementById('drafting-notebook-select');
  const list = Array.isArray(notebooks) ? notebooks : [];
  state.drafting.notebookOptions = list;
  state.drafting.notebookOptionsLoaded = true;
  // "Consult a notebook" is a fourth job, offered only when notebooks exist.
  const card = document.getElementById('drafting-notebook-card');
  if (card) card.hidden = !list.length;
  if (!select) return;

  const previous = String(select.value || '');
  select.replaceChildren();
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = list.length
    ? _ti('drafting.notebookChoose')
    : _ti('drafting.notebookNone');
  select.appendChild(placeholder);
  list.forEach((notebook) => {
    const option = document.createElement('option');
    option.value = String(notebook?.id || '');
    const count = Math.max(0, Number(notebook?.source_count) || 0);
    const sourceLabel = `${count} source${count === 1 ? '' : 's'}`;
    option.textContent = `${String(notebook?.title || _tr('Sans titre'))} · ${sourceLabel}`;
    select.appendChild(option);
  });
  if (previous && list.some((notebook) => String(notebook?.id || '') === previous)) {
    select.value = previous;
  } else if (list.length === 1) {
    select.value = String(list[0]?.id || '');
  }
  _draftingSyncNotebookSearchControls();
}

async function _draftingLoadNotebookOptions(force = false) {
  if (state.drafting.notebookOptionsLoading) return;
  if (state.drafting.notebookOptionsLoaded && !force) {
    _draftingSyncNotebookSearchControls();
    return;
  }
  const select = document.getElementById('drafting-notebook-select');
  state.drafting.notebookOptionsLoading = true;
  if (select) {
    select.replaceChildren();
    const loadingOption = document.createElement('option');
    loadingOption.value = '';
    loadingOption.textContent = _ti('drafting.notebookLoading');
    select.appendChild(loadingOption);
  }
  _draftingSyncNotebookSearchControls();
  try {
    const response = await fetch('/api/notebooks', { cache: 'no-store' });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload?.detail || `HTTP ${response.status}`);
    _draftingPopulateNotebookOptions(payload?.notebooks);
  } catch (error) {
    state.drafting.notebookOptions = [];
    state.drafting.notebookOptionsLoaded = false;
    if (select) {
      select.replaceChildren();
      const errorOption = document.createElement('option');
      errorOption.value = '';
      errorOption.textContent = _tr('Erreur chargement carnets');
      select.appendChild(errorOption);
    }
  } finally {
    state.drafting.notebookOptionsLoading = false;
    _draftingSyncNotebookSearchControls();
  }
}

const DRAFTING_VIEWS = ['citations', 'proofread', 'arguments', 'notebook', 'history'];

function _draftingViewCounts() {
  return {
    citations: (state.lastDraftingSuggestions || []).length,
    proofread: (typeof draftingChanges === 'undefined' ? [] : draftingChanges.issues)
      .filter((issue) => !issue.resolved).length,
    arguments: (state.drafting.lastArgumentSuggestions || []).length,
  };
}

function _draftingRefreshViewTabs() {
  const counts = _draftingViewCounts();
  document.querySelectorAll('[data-drafting-count]').forEach((badge) => {
    const count = counts[badge.dataset.draftingCount] || 0;
    badge.textContent = count ? String(count) : '';
  });
  const notebookTab = document.getElementById('drafting-view-tab-notebook');
  if (notebookTab) {
    notebookTab.hidden = !state.drafting.notebookViewUsed && state.drafting.activeView !== 'notebook';
  }
}

// Result tabs only change what is shown; each action owns its own view.
function setDraftingView(view) {
  const active = DRAFTING_VIEWS.includes(view) ? view : 'citations';
  state.drafting.activeView = active;
  if (active === 'notebook') state.drafting.notebookViewUsed = true;
  DRAFTING_VIEWS.forEach((name) => {
    const isActive = name === active;
    const tab = document.getElementById(`drafting-view-tab-${name}`);
    const panel = document.getElementById(`drafting-view-${name}`);
    if (tab) {
      tab.classList.toggle('is-active', isActive);
      tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
      tab.tabIndex = isActive ? 0 : -1;
    }
    if (panel) {
      panel.classList.toggle('is-active', isActive);
      panel.hidden = !isActive;
    }
  });
  _draftingRefreshViewTabs();
  if (active === 'history') loadDraftingHistory();
}

function _draftingStartReviewView(view) {
  state.drafting.reviewView = view;
  setDraftingView(view);
}

function _draftingGetSelectionText() {
  const editor = _draftingGetEditor();
  const selection = window.getSelection();
  if (!editor || !selection || selection.rangeCount === 0 || selection.isCollapsed) return '';
  const anchor = selection.anchorNode;
  const focus = selection.focusNode;
  if (!anchor || !focus || !editor.contains(anchor) || !editor.contains(focus)) return '';
  return _draftingExtractPlainText(selection.getRangeAt(0).cloneContents());
}

function _draftingRangePointsIntoEditor(range) {
  const editor = _draftingGetEditor();
  if (!editor || !range) return false;
  const start = range.startContainer;
  const end = range.endContainer;
  return Boolean(start && end && editor.contains(start) && editor.contains(end));
}

function _draftingStoreSelection() {
  const editor = _draftingGetEditor();
  const selection = window.getSelection();
  if (!editor || !selection || selection.rangeCount === 0) return;
  const range = selection.getRangeAt(0).cloneRange();
  if (!_draftingRangePointsIntoEditor(range)) return;
  state.drafting.selectionRange = range;
  _draftingScheduleScopeUpdate();
}

function _draftingRestoreSelection() {
  const editor = _draftingGetEditor();
  const selection = window.getSelection();
  const range = state.drafting.selectionRange;
  if (!editor || !selection || !range || !_draftingRangePointsIntoEditor(range)) return false;
  selection.removeAllRanges();
  selection.addRange(range);
  editor.focus();
  return true;
}

// Invisible separator standing for a citation token; the server skips the
// sentences that carry one (they are already cited).
const DRAFTING_CITED_MARKER = '\u2063';
// Draft text sent around a selection or a retried sentence: the server keeps
// its nearest sentences as context ("Ces courants…" needs the one before).
const DRAFTING_CONTEXT_CHARS = 600;

function _draftingIncludesCitedSentences() {
  return Boolean(document.getElementById('drafting-include-cited')?.checked);
}

function _draftingPassageContext(passage) {
  const editor = _draftingGetEditor();
  const text = String(passage || '').trim();
  const documentText = editor && text ? _draftingExtractPlainText(editor) : '';
  const start = documentText.indexOf(text);
  if (!text || start < 0) return { before: '', after: '' };
  const end = start + text.length;
  return {
    before: documentText.slice(Math.max(0, start - DRAFTING_CONTEXT_CHARS), start).trim(),
    after: documentText.slice(end, end + DRAFTING_CONTEXT_CHARS).trim(),
  };
}

function _draftingGetQueryText(options = {}) {
  const selection = _draftingGetSelectionText();
  if (selection) return selection;
  const storedRange = state.drafting.selectionRange;
  if (storedRange && _draftingRangePointsIntoEditor(storedRange) && !storedRange.collapsed) {
    const storedSelection = _draftingExtractPlainText(storedRange.cloneContents());
    if (storedSelection) return storedSelection;
  }
  const editor = _draftingGetEditor();
  if (!editor) return '';
  if (options.markCitations) {
    // Only for the whole document: an explicit selection is analysed in full.
    return _draftingNormalizeSerializedText(
      _draftingSerializeCopyNode(editor, { isRoot: true, citationMarker: DRAFTING_CITED_MARKER }),
    );
  }
  return _draftingExtractPlainText(editor);
}

function _draftingExtractPlainText(root) {
  if (!root) return '';
  return _draftingNormalizeSerializedText(
    _draftingSerializeCopyNode(root, { isRoot: true, excludeCitations: true }),
  );
}

function _draftingGetCitationTokenCopyLabel(token) {
  if (!token) return '';
  const linkLabel = String(token.querySelector('.drafting-citation-link')?.textContent || '').replace(/\u00a0/g, ' ');
  if (linkLabel.trim()) return linkLabel;
  return String(token.textContent || '').replace(/\u00a0/g, ' ').trim();
}

function _draftingSerializeCopyNode(node, options = {}) {
  if (!node) return '';
  if (node.nodeType === Node.TEXT_NODE) {
    return String(node.nodeValue || '').replace(/\u00a0/g, ' ');
  }
  if (node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
    const childOptions = { ...options, isRoot: false };
    return Array.from(node.childNodes)
      .map((child) => _draftingSerializeCopyNode(child, childOptions))
      .join('');
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return '';

  const element = /** @type {HTMLElement} */ (node);
  if (element.classList?.contains('drafting-argument-preview')) return '';
  if (element.classList?.contains('drafting-citation-token')) {
    // Unconfirmed previews are not part of the text yet.
    if (element.dataset?.preview === '1') return '';
    if (options.citationMarker) return options.citationMarker;
    if (options.excludeCitations) return '';
    return _draftingGetCitationTokenCopyLabel(element);
  }
  if (element.tagName === 'BR') return '\n';

  const isRoot = Boolean(options.isRoot);
  const isBlock = /^(ADDRESS|ARTICLE|ASIDE|BLOCKQUOTE|DIV|DL|FIELDSET|FIGCAPTION|FIGURE|FOOTER|FORM|H[1-6]|HEADER|HR|LI|MAIN|NAV|OL|P|PRE|SECTION|TABLE|TD|TH|TR|UL)$/.test(element.tagName);
  const childOptions = { ...options, isRoot: false };
  const text = Array.from(element.childNodes)
    .map((child) => _draftingSerializeCopyNode(child, childOptions))
    .join('');

  if (!isRoot && isBlock) {
    const normalized = text.trim();
    return normalized ? `${normalized}\n\n` : '';
  }
  return text;
}

function _draftingNormalizeSerializedText(value) {
  return String(value || '')
    .replace(/\r\n/g, '\n')
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n[ \t]+/g, '\n')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function _draftingExtractCopyText(root) {
  if (!root) return '';
  return _draftingNormalizeSerializedText(
    _draftingSerializeCopyNode(root, { isRoot: true }),
  );
}

function _draftingGetDraftStorageKey() {
  return LS_PREFIX + 'drafting-document-html';
}

function _draftingGetDraftStartNumberKey() {
  return LS_PREFIX + 'drafting-citation-start-number';
}

function _draftingGetDraftUpdatedAtKey() {
  return LS_PREFIX + 'drafting-document-updated-at';
}

let _draftingRemoteSaveTimer = null;

function _draftingScheduleRemoteSave(html, citationStartNumber) {
  if (_draftingRemoteSaveTimer) window.clearTimeout(_draftingRemoteSaveTimer);
  _draftingRemoteSaveTimer = window.setTimeout(async () => {
    _draftingRemoteSaveTimer = null;
    try {
      const response = await fetch('/api/drafting-document', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          html: String(html || ''),
          citation_start_number: _draftingNormalizeStartNumber(citationStartNumber),
        }),
        keepalive: true,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
    } catch (error) {
      console.warn('Draft persistence deferred; the browser copy remains available.', error);
    }
  }, 250);
}

function _draftingNormalizeStartNumber(value) {
  const parsed = parseInt(String(value || '').trim(), 10);
  return Number.isFinite(parsed) && parsed >= 1 ? parsed : 1;
}

function _draftingGetStartNumber() {
  const inputValue = document.getElementById('drafting-start-number')?.value;
  if (inputValue !== undefined) return _draftingNormalizeStartNumber(inputValue);
  return _draftingNormalizeStartNumber(state.drafting.citationStartNumber || 1);
}

function _draftingGetDisplayNumberFromIndex(index) {
  return _draftingGetStartNumber() + Math.max(0, Number(index) || 0);
}

function _draftingSetStartNumber(value, persist = true) {
  const next = _draftingNormalizeStartNumber(value);
  state.drafting.citationStartNumber = next;
  const input = document.getElementById('drafting-start-number');
  if (input && input.value !== String(next)) input.value = String(next);
  if (persist) localStorage.setItem(_draftingGetDraftStartNumberKey(), String(next));
  return next;
}

function _draftingApplyStartNumberChange(value) {
  _draftingSetStartNumber(value, true);
  _draftingRenumberRenderedCitations(false);
  _draftingSaveDocument();
}

function _draftingRenumberRenderedCitations(persist = true) {
  const editor = _draftingGetEditor();
  if (!editor) return;
  const tokens = Array.from(editor.querySelectorAll('.drafting-citation-token[data-citation-id]'));
  const citationIndex = new Map();
  const citationOrder = [];
  const numberById = new Map();

  tokens.forEach((token) => {
    if (token.dataset.preview === '1') return;
    const record = _draftingGetCitationRecordFromToken(token);
    if (!record) return;
    citationIndex.set(record.citation_id, record);
    if (!numberById.has(record.citation_id)) {
      citationOrder.push(record.citation_id);
      numberById.set(record.citation_id, _draftingGetDisplayNumberFromIndex(citationOrder.length - 1));
    }
  });

  tokens.forEach((token) => {
    if (token.dataset.preview === '1') return;
    const citationId = String(token?.dataset?.citationId || '').trim();
    const number = numberById.get(citationId) || _draftingGetStartNumber();
    token.dataset.citationNumber = String(number);
  });

  state.drafting.citationIndex = citationIndex;
  state.drafting.citationOrder = citationOrder;
  _draftingRenderCitationLabels(editor);
  _draftingUpdateCounters();
  _draftingRenderBibliography();
  _draftingUpdatePreviewState();
  _draftingRefreshSuggestionActionButtons();
  _draftingUpdateBulkPreviewButton();
  if (persist) _draftingSaveDocument();
}

function _draftingSaveDocument() {
  const editor = _draftingGetEditor();
  if (!editor) return;
  // An unconfirmed argument is not part of the draft yet.
  const html = _draftingHtmlWithoutArgumentPreview(editor) || '';
  const citationStartNumber = _draftingGetStartNumber();
  localStorage.setItem(_draftingGetDraftStorageKey(), html);
  localStorage.setItem(_draftingGetDraftStartNumberKey(), String(citationStartNumber));
  localStorage.setItem(_draftingGetDraftUpdatedAtKey(), String(Date.now()));
  _draftingScheduleRemoteSave(html, citationStartNumber);
}

async function _draftingLoadDocument() {
  const editor = _draftingGetEditor();
  if (!editor) return;
  const localHtml = localStorage.getItem(_draftingGetDraftStorageKey()) || '';
  const localUpdatedAt = Number(localStorage.getItem(_draftingGetDraftUpdatedAtKey()) || 0);
  let restoredFromServer = false;
  try {
    const response = await fetch('/api/drafting-document');
    const payload = await response.json();
    if (response.ok && payload?.ok && payload?.exists && payload.document) {
      const remoteHtml = String(payload.document.html || '');
      const remoteStartNumber = _draftingNormalizeStartNumber(
        payload.document.citation_start_number,
      );
      const remoteUpdatedAt = Date.parse(String(payload.document.updated_at || '')) || 0;
      if (!localHtml || !localUpdatedAt || remoteUpdatedAt >= localUpdatedAt) {
        editor.innerHTML = remoteHtml;
        _draftingSetStartNumber(remoteStartNumber, false);
        localStorage.setItem(_draftingGetDraftStorageKey(), remoteHtml);
        localStorage.setItem(_draftingGetDraftStartNumberKey(), String(remoteStartNumber));
        localStorage.setItem(_draftingGetDraftUpdatedAtKey(), String(remoteUpdatedAt || Date.now()));
        restoredFromServer = true;
      }
    }
  } catch (error) {
    console.warn('Server draft unavailable; using the browser copy.', error);
  }
  if (!restoredFromServer && localHtml) {
    editor.innerHTML = localHtml;
    _draftingSaveDocument();
  }
  editor.querySelectorAll('.drafting-citation-token.is-preview, .drafting-argument-preview').forEach((node) => node.remove());
}

function _draftingEstimateTokens(text) {
  return Math.max(0, Math.ceil(String(text || '').length / 3.2));
}

function _draftingProofreadPlan(text) {
  const inputTokens = _draftingEstimateTokens(text);
  const configuredContext = Number(state.appSettings?.ollama_context_length || 0);
  const contextTokens = configuredContext > 0 ? configuredContext : 4096;
  const targetTokens = Math.min(2200, Math.max(700, Math.floor((contextTokens * 0.8 - 1100) / 2)));
  return {
    inputTokens,
    targetTokens,
    sectionCount: Math.max(1, Math.ceil(inputTokens / targetTokens)),
  };
}

function _draftingUpdateCounters() {
  const editor = _draftingGetEditor();
  const citationCountEl = document.getElementById('drafting-citation-count');
  const wordCountEl = document.getElementById('drafting-word-count');
  const tokenEstimateEl = document.getElementById('drafting-token-estimate');
  const text = String(editor?.innerText || editor?.textContent || '').replace(/\s+/g, ' ').trim();
  const words = text ? text.split(' ').filter(Boolean).length : 0;
  const citationCount = state.drafting.citationOrder.length;
  if (citationCountEl) citationCountEl.textContent = `${citationCount} citation${citationCount > 1 ? 's' : ''}`;
  if (wordCountEl) wordCountEl.textContent = `${words} mot${words > 1 ? 's' : ''}`;
  if (tokenEstimateEl) {
    const plan = _draftingProofreadPlan(text);
    tokenEstimateEl.textContent = `≈ ${plan.inputTokens.toLocaleString()} token${plan.inputTokens > 1 ? 's' : ''}`;
    tokenEstimateEl.classList.toggle('is-warning', plan.sectionCount > 1);
    tokenEstimateEl.title = plan.sectionCount > 1
      ? `Le mode Writing traitera ce texte en environ ${plan.sectionCount} sections.`
      : 'Estimation locale du nombre de tokens.';
  }
}

function _draftingBuildCitationRecord(source) {
  const citationId = String(source?.citation_id || source?.citation_key || '').trim();
  const anchorId = String(source?.anchor_id || _draftingMakeDomId('draft-ref', citationId)).trim();
  const sentenceIndexRaw = source?.sentence_index;
  const sentenceIndex = Number.isFinite(Number(sentenceIndexRaw)) && sentenceIndexRaw !== null && sentenceIndexRaw !== ''
    ? Number(sentenceIndexRaw)
    : null;
  return {
    citation_id: citationId,
    anchor_id: anchorId,
    title: String(source?.title || '').trim(),
    author: String(source?.author || '').trim(),
    authors_display: String(source?.authors_display || source?.author || '').trim(),
    year: String(source?.year || '').trim(),
    collection: String(source?.collection || '').trim(),
    label: String(source?.label || '').trim(),
    journal: String(source?.journal || '').trim(),
    doi: String(source?.doi || '').trim(),
    apa: String(source?.apa || '').trim(),
    bibtex: String(source?.bibtex || '').trim(),
    rationale: String(source?.rationale || '').trim(),
    support_status: String(source?.support_status || '').trim(),
    // No score (external or imported sources) must not become a 0 % score.
    confidence: _draftingNormalizeConfidence(source?.confidence) === null ? null : Number(source.confidence),
    base_confidence: Number.isFinite(Number(source?.base_confidence)) ? Number(source?.base_confidence) : null,
    authority_bonus: Number.isFinite(Number(source?.authority_bonus)) ? Number(source?.authority_bonus) : null,
    citation_count: Number.isFinite(Number(source?.citation_count)) ? Number(source?.citation_count) : 0,
    influential_citation_count: Number.isFinite(Number(source?.influential_citation_count)) ? Number(source?.influential_citation_count) : 0,
    citation_authority_score: Number.isFinite(Number(source?.citation_authority_score)) ? Number(source?.citation_authority_score) : 0,
    evidence_quote: String(source?.evidence_quote || '').trim(),
    evidence_chunk_uid: String(source?.evidence_chunk_uid || '').trim(),
    rel_path: String(source?.rel_path || '').trim(),
    page: String(source?.page ?? '').trim(),
    support_explanation: String(source?.support_explanation || source?.rationale || '').trim(),
    source_pipeline: String(source?.source_pipeline || '').trim(),
    // Sources found outside the library ('external' pipeline) only.
    external_url: String(source?.external_url || '').trim(),
    open_access_pdf: String(source?.open_access_pdf || '').trim(),
    summary: String(source?.summary || '').trim(),
    sentence_index: sentenceIndex,
    sentence_text: String(source?.sentence_text || '').trim(),
    // 'document' when sentence_index counts from the start of the editor,
    // 'selection' when it counts from the start of the analysed passage.
    query_scope: source?.query_scope === 'document' ? 'document' : 'selection',
  };
}

function _draftingGetCitationRecordFromToken(token) {
  const citationId = String(token?.dataset?.citationId || '').trim();
  if (!citationId) return null;
  const existing = state.drafting.citationIndex.get(citationId) || {};
  const sentenceIndexAttr = token?.dataset?.sentenceIndex;
  const sentenceIndex = sentenceIndexAttr !== undefined && sentenceIndexAttr !== ''
    ? Number(sentenceIndexAttr)
    : (existing.sentence_index ?? null);
  return {
    citation_id: citationId,
    anchor_id: String(token?.dataset?.anchorId || existing.anchor_id || _draftingMakeDomId('draft-ref', citationId)).trim(),
    title: String(token?.dataset?.title || existing.title || '').trim(),
    author: String(token?.dataset?.author || existing.author || '').trim(),
    authors_display: String(token?.dataset?.authorsDisplay || existing.authors_display || existing.author || '').trim(),
    year: String(token?.dataset?.year || existing.year || '').trim(),
    collection: String(token?.dataset?.collection || existing.collection || '').trim(),
    label: String(token?.dataset?.label || existing.label || '').trim(),
    journal: String(token?.dataset?.journal || existing.journal || '').trim(),
    doi: String(token?.dataset?.doi || existing.doi || '').trim(),
    apa: String(token?.dataset?.apa || existing.apa || '').trim(),
    bibtex: String(token?.dataset?.bibtex || existing.bibtex || '').trim(),
    rationale: String(token?.dataset?.rationale || existing.rationale || '').trim(),
    support_status: String(token?.dataset?.supportStatus || existing.support_status || '').trim(),
    confidence: _draftingNormalizeConfidence(token?.dataset?.confidence || existing.confidence) === null
      ? null
      : Number(token?.dataset?.confidence || existing.confidence),
    base_confidence: Number.isFinite(Number(token?.dataset?.baseConfidence || existing.base_confidence))
      ? Number(token?.dataset?.baseConfidence || existing.base_confidence)
      : null,
    authority_bonus: Number.isFinite(Number(token?.dataset?.authorityBonus || existing.authority_bonus))
      ? Number(token?.dataset?.authorityBonus || existing.authority_bonus)
      : null,
    citation_count: Number.isFinite(Number(token?.dataset?.citationCount || existing.citation_count))
      ? Number(token?.dataset?.citationCount || existing.citation_count)
      : 0,
    influential_citation_count: Number.isFinite(Number(token?.dataset?.influentialCitationCount || existing.influential_citation_count))
      ? Number(token?.dataset?.influentialCitationCount || existing.influential_citation_count)
      : 0,
    citation_authority_score: Number.isFinite(Number(token?.dataset?.citationAuthorityScore || existing.citation_authority_score))
      ? Number(token?.dataset?.citationAuthorityScore || existing.citation_authority_score)
      : 0,
    evidence_quote: String(token?.dataset?.evidenceQuote || existing.evidence_quote || '').trim(),
    evidence_chunk_uid: String(token?.dataset?.evidenceChunkUid || existing.evidence_chunk_uid || '').trim(),
    rel_path: String(token?.dataset?.relPath || existing.rel_path || '').trim(),
    page: String(token?.dataset?.page || existing.page || '').trim(),
    support_explanation: String(token?.dataset?.supportExplanation || existing.support_explanation || existing.rationale || '').trim(),
    source_pipeline: String(token?.dataset?.sourcePipeline || existing.source_pipeline || '').trim(),
    sentence_index: Number.isFinite(sentenceIndex) ? sentenceIndex : null,
    sentence_text: String(token?.dataset?.sentenceText || existing.sentence_text || '').trim(),
  };
}

function _draftingNormalizeConfidence(value) {
  // No score (imported citations) is not a 0 % score.
  if (value === null || value === undefined || value === '') return null;
  const raw = Number(value);
  if (!Number.isFinite(raw)) return null;
  const ratio = raw > 1 ? raw / 100 : raw;
  return Math.max(0, Math.min(1, ratio));
}

function _draftingFormatConfidencePercent(value) {
  const ratio = _draftingNormalizeConfidence(value);
  return ratio === null ? null : Math.round(ratio * 100);
}

function _draftingConfidenceCssVars(value) {
  const ratio = _draftingNormalizeConfidence(value);
  if (ratio === null) return null;
  const tone = Math.max(0, Math.min(1, (ratio - 0.5) / 0.5));
  const hue = Math.round(8 + tone * 134);
  const secondaryHue = Math.min(176, hue + 28);
  return {
    '--drafting-confidence-color': `hsl(${hue} 100% 68%)`,
    '--drafting-confidence-border': `hsla(${hue}, 96%, 60%, 0.86)`,
    '--drafting-confidence-soft': `hsla(${hue}, 96%, 50%, 0.34)`,
    '--drafting-confidence-softer': `hsla(${secondaryHue}, 96%, 48%, 0.18)`,
    '--drafting-confidence-hover': `hsla(${hue}, 96%, 50%, 0.46)`,
  };
}

function _draftingApplyConfidenceStyle(node, value) {
  if (!node) return;
  const vars = _draftingConfidenceCssVars(value);
  [
    '--drafting-confidence-color',
    '--drafting-confidence-border',
    '--drafting-confidence-soft',
    '--drafting-confidence-softer',
    '--drafting-confidence-hover',
  ].forEach((name) => node.style.removeProperty(name));
  if (!vars) return;
  Object.entries(vars).forEach(([name, color]) => node.style.setProperty(name, color));
}

function _draftingConfidenceStyleAttr(value) {
  const vars = _draftingConfidenceCssVars(value);
  if (!vars) return '';
  const css = Object.entries(vars).map(([name, color]) => `${name}: ${color}`).join('; ');
  return ` style="${escapeAttr(css)}"`;
}

function _draftingCreateCitationToken(record, number) {
  const token = document.createElement('span');
  token.className = 'drafting-citation-token';
  token.contentEditable = 'false';
  token.dataset.citationId = record.citation_id;
  token.dataset.anchorId = record.anchor_id;
  token.dataset.title = record.title;
  token.dataset.author = record.author;
  token.dataset.authorsDisplay = record.authors_display;
  token.dataset.year = record.year;
  token.dataset.collection = record.collection;
  token.dataset.label = record.label;
  token.dataset.journal = record.journal;
  token.dataset.doi = record.doi;
  token.dataset.apa = record.apa;
  token.dataset.bibtex = record.bibtex;
  token.dataset.rationale = record.rationale;
  token.dataset.supportStatus = record.support_status || '';
  token.dataset.confidence = record.confidence === null || record.confidence === undefined ? '' : String(record.confidence);
  token.dataset.baseConfidence = record.base_confidence === null || record.base_confidence === undefined ? '' : String(record.base_confidence);
  token.dataset.authorityBonus = record.authority_bonus === null || record.authority_bonus === undefined ? '' : String(record.authority_bonus);
  token.dataset.citationCount = String(record.citation_count || 0);
  token.dataset.influentialCitationCount = String(record.influential_citation_count || 0);
  token.dataset.citationAuthorityScore = String(record.citation_authority_score || 0);
  token.dataset.evidenceQuote = record.evidence_quote || '';
  token.dataset.evidenceChunkUid = record.evidence_chunk_uid || '';
  token.dataset.relPath = record.rel_path || '';
  token.dataset.page = record.page || '';
  token.dataset.supportExplanation = record.support_explanation || '';
  token.dataset.sourcePipeline = record.source_pipeline || '';
  token.dataset.sentenceText = record.sentence_text || '';
  token.dataset.sentenceIndex = record.sentence_index === null || record.sentence_index === undefined
    ? ''
    : String(record.sentence_index);
  token.dataset.citationNumber = String(number);
  token.title = record.apa || record.label || record.title || record.citation_id;

  const link = document.createElement('a');
  link.className = 'drafting-citation-link';
  link.href = `#draft-ref-${number}`;
  link.textContent = `[${number}]`;
  link.title = token.title;
  token.appendChild(link);
  _draftingApplyConfidenceStyle(token, record.confidence);
  return token;
}

function _draftingFindAdjacentCitationToken(token, direction = 'next') {
  if (!token) return null;
  let cursor = direction === 'previous' ? token.previousSibling : token.nextSibling;
  while (cursor) {
    if (cursor.nodeType === Node.TEXT_NODE) {
      if (/\S/.test(cursor.nodeValue || '')) return null;
      cursor = direction === 'previous' ? cursor.previousSibling : cursor.nextSibling;
      continue;
    }
    if (cursor.nodeType !== Node.ELEMENT_NODE) return null;
    if (!cursor.classList?.contains('drafting-citation-token')) return null;
    if (cursor.dataset.preview === '1') return null;
    return cursor;
  }
  return null;
}

function _draftingTightenCitationWhitespace(editor) {
  if (!editor) return;
  const tokens = Array.from(editor.querySelectorAll('.drafting-citation-token[data-citation-id]'));
  tokens.forEach((token) => {
    if (token.dataset.preview === '1') return;
    let cursor = token.nextSibling;
    const whitespaceNodes = [];
    while (cursor && cursor.nodeType === Node.TEXT_NODE && !/\S/.test(cursor.nodeValue || '')) {
      whitespaceNodes.push(cursor);
      cursor = cursor.nextSibling;
    }
    if (
      whitespaceNodes.length
      && cursor?.nodeType === Node.ELEMENT_NODE
      && cursor.classList?.contains('drafting-citation-token')
      && cursor.dataset.preview !== '1'
    ) {
      whitespaceNodes.forEach((node) => { node.nodeValue = ''; });
    }
  });
}

function _draftingPreviewShortLabel(record) {
  // "Fazilleau, Philippe" and "Philippe Fazilleau et al." both give "Fazilleau".
  // Accepts a citation record or a token dataset (camelCase keys).
  const rawAuthor = String(record?.author || record?.authors_display || record?.authorsDisplay || '').trim();
  let author = rawAuthor.split(/\s*;\s*|\s+and\s+|\s+et\s+/i)[0] || '';
  author = author.includes(',') ? author.split(',')[0] : author.split(/\s+/).pop();
  const base = [String(author || '').trim(), String(record?.year || '').trim()].filter(Boolean).join(' ')
    || String(record?.label || record?.title || record?.citation_id || record?.citationId || '').trim();
  return base.length > 32 ? `${base.slice(0, 31)}…` : base;
}

function _draftingEnsurePreviewControls(token) {
  let link = token.querySelector('.drafting-citation-link');
  if (!link) {
    token.textContent = '';
    link = document.createElement('a');
    link.className = 'drafting-citation-link';
    token.appendChild(link);
  }
  link.href = '#drafting-editor';
  link.textContent = `[${_draftingPreviewShortLabel(token.dataset)} ?]`;
  if (token.querySelector('.drafting-preview-controls')) return;
  const controls = document.createElement('span');
  controls.className = 'drafting-preview-controls';
  [
    ['accept', '✓', _tr('Valider cette citation')],
    ['reject', '✕', _tr('Retirer cet aperçu')],
  ].forEach(([action, symbol, label]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `drafting-preview-control is-${action}`;
    button.dataset.previewAction = action;
    button.textContent = symbol;
    button.title = label;
    button.setAttribute('aria-label', label);
    controls.appendChild(button);
  });
  token.appendChild(controls);
}

function _draftingRenderCitationLabels(editor) {
  if (!editor) return;
  const tokens = Array.from(editor.querySelectorAll('.drafting-citation-token[data-citation-id]'));
  tokens.forEach((token) => {
    const link = token.querySelector('a');
    if (token.dataset.preview === '1') {
      token.classList.add('is-preview');
      token.classList.remove('has-prev', 'has-next');
      _draftingApplyConfidenceStyle(token, token.dataset.confidence);
      _draftingEnsurePreviewControls(token);
      return;
    }
    const citationId = String(token?.dataset?.citationId || '').trim();
    const number = token.dataset.citationNumber || '1';
    const hasPrev = Boolean(_draftingFindAdjacentCitationToken(token, 'previous'));
    const hasNext = Boolean(_draftingFindAdjacentCitationToken(token, 'next'));
    // Grouped references read "[4,5,6]": no padding between the members.
    token.classList.toggle('has-prev', hasPrev);
    token.classList.toggle('has-next', hasNext);
    const label = `${hasPrev ? ',' : '['}${number}${hasNext ? '' : ']'}`;
    token.title = token.dataset.apa || token.dataset.label || token.dataset.title || citationId;
    if (link) {
      link.href = `#draft-ref-${number}`;
      link.textContent = label;
      link.title = token.title;
    } else {
      token.textContent = label;
    }
  });
}

function _draftingFindPreviewToken(citationId = '') {
  const editor = _draftingGetEditor();
  if (!editor) return null;
  return editor.querySelector(`.drafting-citation-token.is-preview[data-citation-id="${CSS.escape(String(citationId || '').trim())}"]`);
}

function _draftingGetPreviewTokens() {
  const editor = _draftingGetEditor();
  if (!editor) return [];
  return Array.from(editor.querySelectorAll('.drafting-citation-token.is-preview[data-citation-id]'));
}

function _draftingHasPreviewToken(citationId = '') {
  return Boolean(_draftingFindPreviewToken(citationId));
}

// Previews at or above this confidence can be confirmed in bulk.
const DRAFTING_CONFIDENT_THRESHOLD = 0.7;

function _draftingSuggestionStatus(citationId) {
  if (_draftingHasPreviewToken(citationId)) return 'previewed';
  if (_draftingHasPersistedCitation(citationId)) return 'applied';
  return 'idle';
}

function _draftingPrimaryActionLabel(status) {
  if (status === 'previewed') return _tr('Valider');
  if (status === 'applied') return _tr('Voir dans le texte');
  return _tr('Placer dans le texte');
}

function _draftingCitationDisplayNumber(citationId) {
  const normalized = _draftingNormalizeCitationId(citationId);
  const index = state.drafting.citationOrder.findIndex((id) => _draftingNormalizeCitationId(id) === normalized);
  return index >= 0 ? _draftingGetDisplayNumberFromIndex(index) : null;
}

function _draftingApplySuggestionCardState(item) {
  const citationId = String(item.dataset.citationId || '').trim();
  const status = _draftingSuggestionStatus(citationId);
  item.classList.toggle('is-pending', status === 'previewed');
  item.classList.toggle('is-applied', status === 'applied');
  const primary = item.querySelector('[data-drafting-action="preview"]');
  if (primary) primary.textContent = _draftingPrimaryActionLabel(status);
  const reject = item.querySelector('[data-drafting-action="reject-preview"]');
  if (reject) reject.hidden = status !== 'previewed';
  const applied = item.querySelector('.drafting-item-applied');
  if (applied) {
    const number = _draftingCitationDisplayNumber(citationId);
    applied.hidden = status !== 'applied';
    applied.textContent = number ? `✓ ${_tr('Dans le texte')} [${number}]` : `✓ ${_tr('Dans le texte')}`;
  }
}

function _draftingRefreshSuggestionActionButtons() {
  const container = _draftingGetResultPanel();
  if (!container) return;
  container.querySelectorAll('.drafting-item[data-citation-id]').forEach(_draftingApplySuggestionCardState);
}

function _draftingIsConfidentPreview(token) {
  return (_draftingNormalizeConfidence(token?.dataset?.confidence) ?? 0) >= DRAFTING_CONFIDENT_THRESHOLD;
}

function _draftingUpdateBulkPreviewButton() {
  const suggestions = state.lastDraftingSuggestions || [];
  const previews = _draftingGetPreviewTokens();
  const unplaced = suggestions.filter((item) => _draftingSuggestionStatus(item.citation_id) === 'idle').length;
  const confident = previews.filter(_draftingIsConfidentPreview).length;
  const bar = document.getElementById('drafting-bulk-actions');
  if (bar) bar.hidden = !unplaced && !previews.length;

  const placeAll = document.getElementById('drafting-preview-all-btn');
  if (placeAll) {
    placeAll.hidden = !unplaced;
    placeAll.disabled = !unplaced;
    placeAll.textContent = `${_tr('Tout placer')} (${unplaced})`;
    placeAll.title = _tr('Placer un aperçu de chaque suggestion à la fin de sa phrase');
  }
  const applyConfident = document.getElementById('drafting-apply-confident-btn');
  if (applyConfident) {
    applyConfident.hidden = !confident;
    applyConfident.textContent = `${_tr('Valider ≥ 70 %')} (${confident})`;
    applyConfident.title = _tr('Valider les aperçus dont la confiance atteint au moins 70 %');
  }
  _draftingUpdatePreviewCleanupButton();
}

function _draftingUpdatePreviewCleanupButton() {
  const btn = document.getElementById('drafting-clear-previews-btn');
  if (!btn) return;
  const count = _draftingGetPreviewTokens().length;
  btn.hidden = count === 0;
  btn.disabled = count === 0;
  btn.textContent = `${_tr('Rejeter les aperçus')} (${count})`;
  btn.title = _tr('Retirer du texte les citations prévisualisées qui ne sont pas encore validées');
}

// Removes a citation token and the space that was added around it.
function _draftingRemoveTokenCleanly(token) {
  if (!token?.parentNode) return;
  const parent = token.parentNode;
  const prev = token.previousSibling;
  const next = token.nextSibling;
  token.remove();
  if (prev?.nodeType === Node.TEXT_NODE && next?.nodeType === Node.TEXT_NODE
    && /\s$/.test(prev.nodeValue || '') && /^(\s|[.,;:!?…)\]»])/.test(next.nodeValue || '')) {
    prev.nodeValue = prev.nodeValue.replace(/\s+$/, '');
  }
  // Merges the split text back; live ranges (the stored selection) follow.
  parent.normalize();
}

function _draftingRemovePreviewToken(citationId = '') {
  const editor = _draftingGetEditor();
  if (!editor) return null;
  const trimmed = String(citationId || '').trim();
  const token = trimmed
    ? _draftingFindPreviewToken(trimmed)
    : editor.querySelector('.drafting-citation-token.is-preview');
  _draftingRemoveTokenCleanly(token);
  if (!trimmed || state.drafting.previewCitationId === trimmed) {
    state.drafting.previewCitationId = '';
    state.drafting.previewTokenId = '';
  }
  return token;
}

function _draftingRemoveAllPreviewTokens() {
  _draftingGetPreviewTokens().forEach(_draftingRemoveTokenCleanly);
  state.drafting.previewCitationId = '';
  state.drafting.previewTokenId = '';
  _draftingRefreshSuggestionActionButtons();
  _draftingUpdateBulkPreviewButton();
}

function rejectDraftingPreview(citationId) {
  if (!_draftingRemovePreviewToken(citationId)) return;
  _draftingSyncFromEditor();
  showToast(_tr('Aperçu retiré du texte.'), 2200, 'info');
}

function _draftingCommitPreviewToken(record) {
  const preview = _draftingFindPreviewToken(record.citation_id);
  if (!preview) return null;
  preview.classList.remove('is-preview');
  preview.dataset.preview = '0';
  preview.removeAttribute('data-preview-citation-id');
  const existingNumber = state.drafting.citationOrder.findIndex((id) => _draftingNormalizeCitationId(id) === _draftingNormalizeCitationId(record.citation_id));
  const number = existingNumber >= 0
    ? _draftingGetDisplayNumberFromIndex(existingNumber)
    : _draftingGetDisplayNumberFromIndex(state.drafting.citationOrder.length);
  const token = _draftingCreateCitationToken(record, number);
  preview.replaceWith(token);
  state.drafting.previewCitationId = '';
  state.drafting.previewTokenId = '';
  window.dispatchEvent(new CustomEvent('bibliobrain:milestone', { detail: { name: 'citation-inserted' } }));
  return token;
}

function _draftingCommitPreviewCitation(record, editor = _draftingGetEditor()) {
  const token = _draftingCommitPreviewToken(record);
  if (!token) {
    showToast(_tr('Aperçu de citation introuvable.'));
    return false;
  }
  _draftingSyncFromEditor();
  if (editor) editor.focus();
  showToast(_tr('Citation validée dans le texte.'), 2600, 'success');
  return true;
}

function _draftingResolveEventElementTarget(target) {
  if (!target) return null;
  if (target.nodeType === Node.ELEMENT_NODE) return target;
  return target.parentElement || null;
}

function _draftingFindTokenFromTarget(target, editor = _draftingGetEditor()) {
  const el = _draftingResolveEventElementTarget(target);
  if (!el || !editor) return null;
  const token = el.closest?.('.drafting-citation-token[data-citation-id]') || null;
  return token && editor.contains(token) ? token : null;
}

function _draftingActivatePreviewToken(previewToken, editor = _draftingGetEditor()) {
  if (!previewToken || !editor || !editor.contains(previewToken)) return false;
  const record = _draftingGetCitationRecordFromToken(previewToken);
  if (!record) {
    showToast(_tr('Aperçu de citation introuvable.'));
    return false;
  }
  return _draftingCommitPreviewCitation(record, editor);
}

function _draftingFlash(node) {
  if (!node) return;
  node.classList.remove('is-flashing');
  void node.offsetWidth;
  node.classList.add('is-flashing');
  setTimeout(() => node.classList.remove('is-flashing'), 1400);
}

function _draftingRevealSuggestionCard(citationId) {
  const container = _draftingGetResultPanel();
  const card = container?.querySelector(`.drafting-item[data-citation-id="${CSS.escape(String(citationId || ''))}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  _draftingFlash(card);
}

function _draftingPreviewCitation(record) {
  const editor = _draftingGetEditor();
  if (!editor) return false;
  if (_draftingHasPersistedCitation(record.citation_id) && !_draftingFindPreviewToken(record.citation_id)) {
    return false;
  }
  const hasSentenceIndex = record.sentence_index !== null && record.sentence_index !== undefined && record.sentence_index !== '';
  const existingPreview = _draftingFindPreviewToken(record.citation_id);
  if (existingPreview) {
    existingPreview.dataset.title = record.title;
    existingPreview.dataset.author = record.author;
    existingPreview.dataset.year = record.year;
    existingPreview.dataset.collection = record.collection;
    existingPreview.dataset.label = record.label;
    existingPreview.dataset.apa = record.apa;
    existingPreview.dataset.bibtex = record.bibtex;
    existingPreview.dataset.rationale = record.rationale;
    existingPreview.dataset.supportStatus = record.support_status || '';
    existingPreview.dataset.confidence = record.confidence === null || record.confidence === undefined ? '' : String(record.confidence);
    existingPreview.dataset.evidenceQuote = record.evidence_quote || '';
    existingPreview.dataset.evidenceChunkUid = record.evidence_chunk_uid || '';
    existingPreview.dataset.relPath = record.rel_path || '';
    existingPreview.dataset.page = record.page || '';
    existingPreview.dataset.supportExplanation = record.support_explanation || '';
    existingPreview.dataset.sourcePipeline = record.source_pipeline || '';
    existingPreview.dataset.sentenceText = record.sentence_text || '';
    existingPreview.dataset.sentenceIndex = record.sentence_index === null || record.sentence_index === undefined
      ? ''
      : String(record.sentence_index);
    _draftingApplyConfidenceStyle(existingPreview, record.confidence);
    _draftingLastPlacement = 'sentence';
    state.drafting.previewCitationId = record.citation_id;
    _draftingSetPendingSuggestion(record.citation_id);
    return true;
  }

  const preview = _draftingCreatePreviewToken(record);

  // The server numbers sentences within the analysed text: the index only
  // locates the sentence in the editor when the whole document was sent.
  const sentenceIndex = record.query_scope === 'document' ? record.sentence_index : null;
  const sentenceRange = (record.sentence_text || hasSentenceIndex)
    ? _draftingFindSentenceEndRange(editor, record.sentence_text, sentenceIndex)
    : null;
  if (sentenceRange) _draftingMovePastCitationTokens(sentenceRange);
  const targetRange = sentenceRange || state.drafting.selectionRange;
  _draftingLastPlacement = sentenceRange ? 'sentence' : 'cursor';

  const ok = _draftingInsertNodeAtSelection(preview, targetRange);
  if (!ok) return false;
  state.drafting.previewCitationId = record.citation_id;
  state.drafting.previewTokenId = record.citation_id;
  _draftingSetPendingSuggestion(record.citation_id);
  return true;
}

// Several sources for one sentence keep the order in which they were placed.
function _draftingMovePastCitationTokens(range) {
  const node = range.startContainer;
  if (node.nodeType !== Node.TEXT_NODE || /\S/.test(String(node.nodeValue || '').slice(range.startOffset))) return;
  let cursor = node.nextSibling;
  let lastToken = null;
  while (cursor) {
    if (cursor.nodeType === Node.TEXT_NODE && !/\S/.test(cursor.nodeValue || '')) {
      cursor = cursor.nextSibling;
      continue;
    }
    if (!cursor.classList?.contains('drafting-citation-token')) break;
    lastToken = cursor;
    cursor = cursor.nextSibling;
  }
  if (lastToken) {
    range.setStartAfter(lastToken);
    range.collapse(true);
  }
}

function _draftingFindSentenceEndRange(editor, sentenceText, sentenceIndex = null) {
  if (!editor) return null;
  const target = String(sentenceText || '').replace(/\s+/g, ' ').trim();
  const hasIndex = sentenceIndex !== null && sentenceIndex !== undefined && sentenceIndex !== '';
  const indexValue = hasIndex ? Number(sentenceIndex) : NaN;
  if ((!target || target.length < 6) && !(Number.isInteger(indexValue) && indexValue >= 0)) return null;

  const segments = [];
  let combined = '';
  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, null);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node.nodeType === Node.ELEMENT_NODE) {
      if (/^(BR|P|DIV|LI|H[1-6])$/.test(node.tagName) && combined && !/\s$/.test(combined)) combined += ' ';
      continue;
    }
    const parent = node.parentElement;
    if (parent && parent.closest('.drafting-citation-token')) continue;
    const text = node.nodeValue || '';
    segments.push({ node, start: combined.length, length: text.length });
    combined += text;
  }
  if (!segments.length) return null;

  const sentenceRanges = [];
  const sentenceRe = /[^.!?\n\r]+(?:[.!?]+[)\]»"'’”]*|$)/g;
  let sentenceMatch;
  while ((sentenceMatch = sentenceRe.exec(combined)) !== null) {
    const rawSentence = String(sentenceMatch[0] || '').replace(/\s+/g, ' ').trim();
    if (!rawSentence || rawSentence.length < 4) continue;
    sentenceRanges.push({
      start: sentenceMatch.index,
      end: sentenceMatch.index + sentenceMatch[0].length,
    });
  }

  // Construit une version normalisée (whitespace réduit à un espace) avec un mapping
  // index normalisé -> position dans la chaîne originale.
  let normalized = '';
  const origPos = [];
  let lastWs = false;
  for (let i = 0; i < combined.length; i++) {
    const ch = combined[i];
    const isWs = /\s/.test(ch);
    if (isWs) {
      if (!lastWs) {
        normalized += ' ';
        origPos.push(i);
        lastWs = true;
      }
    } else {
      normalized += ch;
      origPos.push(i);
      lastWs = false;
    }
  }

  const lookupEnd = (needle) => {
    if (!needle) return -1;
    let idx = normalized.indexOf(needle);
    if (idx < 0) {
      const lower = normalized.toLowerCase();
      idx = lower.indexOf(needle.toLowerCase());
    }
    if (idx < 0) return -1;
    return idx + needle.length;
  };

  const clampOriginalIndex = (value) => Math.max(0, Math.min(combined.length, Number(value) || 0));
  const normBoundaryToOriginalIndex = (value) => {
    const index = Number(value);
    if (!Number.isFinite(index)) return -1;
    if (index <= 0) return 0;
    if (index >= origPos.length) return combined.length;
    return origPos[index - 1] !== undefined ? origPos[index - 1] + 1 : combined.length;
  };
  const moveBeforeSentencePunctuation = (value) => {
    let index = clampOriginalIndex(value);
    while (index > 0 && /\s/.test(combined[index - 1])) index -= 1;
    const prefix = combined.slice(0, index);
    const tail = prefix.match(/[.!?…]+[)\]»"'’”]*$/);
    if (tail && tail[0]) index = Math.max(0, index - tail[0].length);
    return index;
  };
  const rangeFromOriginalIndex = (value) => {
    const originalIndex = clampOriginalIndex(value);
    for (const seg of segments) {
      if (originalIndex <= seg.start + seg.length) {
        const offset = Math.max(0, Math.min(seg.length, originalIndex - seg.start));
        const range = document.createRange();
        range.setStart(seg.node, offset);
        range.collapse(true);
        return range;
      }
    }
    const last = segments[segments.length - 1];
    const range = document.createRange();
    range.setStart(last.node, last.length);
    range.collapse(true);
    return range;
  };

  // The sentence text is authoritative; the index is only a fallback because
  // the server and this regex do not always split sentences the same way.
  let endNorm = target.length >= 6 ? lookupEnd(target) : -1;
  if (endNorm < 0 && target.length >= 6) {
    const stripped = target.replace(/[.!?…»"'’”)\]]+\s*$/, '').trim();
    if (stripped && stripped.length >= 6 && stripped !== target) {
      endNorm = lookupEnd(stripped);
    }
  }
  if (endNorm < 0 || endNorm > origPos.length) {
    if (Number.isInteger(indexValue) && indexValue >= 0 && sentenceRanges[indexValue]) {
      return rangeFromOriginalIndex(moveBeforeSentencePunctuation(sentenceRanges[indexValue].end));
    }
    return null;
  }

  let endOriginal = normBoundaryToOriginalIndex(endNorm);
  while (endOriginal < combined.length && /[.!?…»"'’”)\]]/.test(combined[endOriginal])) {
    endOriginal += 1;
  }
  return rangeFromOriginalIndex(moveBeforeSentencePunctuation(endOriginal));
}

function _draftingCreatePreviewToken(record) {
  const token = _draftingCreateCitationToken(record, '…');
  token.classList.add('is-preview');
  token.dataset.preview = '1';
  token.dataset.previewCitationId = record.citation_id;
  token.dataset.authorsDisplay = record.authors_display;
  token.dataset.journal = record.journal;
  token.dataset.doi = record.doi;
  _draftingApplyConfidenceStyle(token, record.confidence);
  const confidence = _draftingFormatConfidencePercent(record.confidence);
  token.title = [
    record.apa || record.label || record.title || record.citation_id,
    confidence !== null ? `${confidence} % ${_tr('confiance')}` : '',
    _tr('✓ valide, ✕ retire'),
  ].filter(Boolean).join(' · ');
  // Clicks are handled by the editor's delegated listeners, which also
  // cover previews restored from a saved draft.
  _draftingEnsurePreviewControls(token);
  const link = token.querySelector('.drafting-citation-link');
  if (link) link.title = token.title;
  return token;
}

function _draftingIsWordChar(value) {
  return /[0-9A-Za-zÀ-ÿ]/.test(String(value || ''));
}

function _draftingAdjustRangeOutOfWord(range) {
  if (!range || !range.collapsed) return range;
  const node = range.startContainer;
  const offset = range.startOffset;
  if (!node || node.nodeType !== Node.TEXT_NODE) return range;
  const text = String(node.nodeValue || '');
  if (!text) return range;
  const prevChar = offset > 0 ? text[offset - 1] : '';
  const nextChar = offset < text.length ? text[offset] : '';
  if (/\s/.test(prevChar) && _draftingIsWordChar(nextChar)) {
    let left = offset - 1;
    while (left >= 0 && /\s/.test(text[left])) left -= 1;
    if (left >= 0 && /[.!?…:;)\]»"'’”]/.test(text[left])) {
      range.setStart(node, left + 1);
      range.collapse(true);
      return range;
    }
  }
  if (!_draftingIsWordChar(prevChar) || !_draftingIsWordChar(nextChar)) return range;
  let nextOffset = offset;
  while (nextOffset < text.length && _draftingIsWordChar(text[nextOffset])) {
    nextOffset += 1;
  }
  range.setStart(node, nextOffset);
  range.collapse(true);
  return range;
}

function _draftingInsertNodeAtSelection(node, rangeOverride = null) {
  const editor = _draftingGetEditor();
  if (!editor || !node) return false;

  const selection = window.getSelection();
  if (!selection) return false;
  let range = null;
  if (rangeOverride && _draftingRangePointsIntoEditor(rangeOverride)) {
    range = rangeOverride.cloneRange();
  } else if (selection.rangeCount > 0) {
    const candidate = selection.getRangeAt(0);
    if (editor.contains(candidate.commonAncestorContainer)) range = candidate;
  }
  if (!range && state.drafting.selectionRange && _draftingRangePointsIntoEditor(state.drafting.selectionRange)) {
    range = state.drafting.selectionRange.cloneRange();
  }
  if (!range) {
    range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(false);
  }
  _draftingAdjustRangeOutOfWord(range);

  const rangeNode = range.startContainer;
  const rangeOffset = range.startOffset;
  const inlineText = rangeNode?.nodeType === Node.TEXT_NODE ? String(rangeNode.nodeValue || '') : '';
  // Between two nodes (e.g. right after a citation token), look at the
  // neighbours; a neighbouring token needs no separating space.
  const neighbourChar = (child, atEnd) => {
    if (!child) return '';
    if (child.nodeType === Node.TEXT_NODE) {
      const value = String(child.nodeValue || '');
      return atEnd ? value.slice(-1) : value.charAt(0);
    }
    return child.classList?.contains('drafting-citation-token') ? (atEnd ? '[' : ']') : '';
  };
  // At either end of a text node, the neighbour is in the adjacent node;
  // empty text nodes are skipped.
  const nonEmpty = (cursor, forward) => {
    while (cursor?.nodeType === Node.TEXT_NODE && !cursor.nodeValue) {
      cursor = forward ? cursor.nextSibling : cursor.previousSibling;
    }
    return cursor;
  };
  const isText = rangeNode?.nodeType === Node.TEXT_NODE;
  const prevChar = isText && rangeOffset > 0
    ? inlineText[rangeOffset - 1]
    : neighbourChar(nonEmpty(isText ? rangeNode.previousSibling : rangeNode?.childNodes?.[rangeOffset - 1], false), true);
  const nextChar = isText && rangeOffset < inlineText.length
    ? inlineText[rangeOffset]
    : neighbourChar(nonEmpty(isText ? rangeNode.nextSibling : rangeNode?.childNodes?.[rangeOffset], true), false);
  const nextIsSentencePunctuation = /[.!?…:;,)\]»"'’”]/.test(nextChar);
  const needsLeadingSpace = Boolean(prevChar && !/\s/.test(prevChar) && !/[[({«"“‘]/.test(prevChar));
  const needsTrailingSpace = !nextIsSentencePunctuation && !(nextChar && /\s/.test(nextChar));

  range.deleteContents();
  const fragment = document.createDocumentFragment();
  if (needsLeadingSpace) fragment.appendChild(document.createTextNode(' '));
  fragment.appendChild(node);
  if (needsTrailingSpace) fragment.appendChild(document.createTextNode(' '));
  const lastInserted = fragment.lastChild;
  range.insertNode(fragment);
  if (lastInserted) {
    range.setStartAfter(lastInserted);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    state.drafting.selectionRange = range.cloneRange();
  }
  return true;
}

function _draftingSyncFromEditor(persist = true) {
  const editor = _draftingGetEditor();
  if (!editor) return;

  const tokens = Array.from(editor.querySelectorAll('.drafting-citation-token[data-citation-id]'));
  const citationIndex = new Map();
  const citationOrder = [];
  const numberById = new Map();

  tokens.forEach((token) => {
    if (token.dataset.preview === '1') return;
    const record = _draftingGetCitationRecordFromToken(token);
    if (!record) return;
    citationIndex.set(record.citation_id, record);
    if (!numberById.has(record.citation_id)) {
      citationOrder.push(record.citation_id);
      numberById.set(record.citation_id, _draftingGetDisplayNumberFromIndex(citationOrder.length - 1));
    }
  });

  _draftingTightenCitationWhitespace(editor);
  tokens.forEach((token) => {
    if (token.dataset.preview === '1') return;
    const citationId = String(token?.dataset?.citationId || '').trim();
    const number = numberById.get(citationId) || _draftingGetStartNumber();
    token.dataset.citationNumber = String(number);
  });
  _draftingRenderCitationLabels(editor);

  state.drafting.citationIndex = citationIndex;
  state.drafting.citationOrder = citationOrder;
  _draftingUpdateCounters();
  _draftingRenderBibliography();
  _draftingUpdatePreviewState();
  _draftingRefreshSuggestionActionButtons();
  _draftingUpdateBulkPreviewButton();
  _draftingRefreshArgumentCards();
  _draftingScheduleScopeUpdate();
  if (persist) _draftingSaveDocument();
}

function _draftingUpdatePreviewState() {
  const editor = _draftingGetEditor();
  if (!editor) return;

  const preview = editor.querySelector('.drafting-citation-token.is-preview');
  if (!preview) {
    state.drafting.previewCitationId = '';
    state.drafting.previewTokenId = '';
    return;
  }

  const previewId = String(preview.dataset?.citationId || preview.dataset?.previewCitationId || '').trim();
  if (previewId) {
    state.drafting.previewCitationId = previewId;
    state.drafting.previewTokenId = previewId;
  }
}

function _draftingRenderBibliography() {
  const list = _draftingGetBibliographyList();
  const empty = _draftingGetBibliographyEmpty();
  if (!list || !empty) return;

  const entries = state.drafting.citationOrder
    .map((citationId, index) => {
      const record = state.drafting.citationIndex.get(citationId);
      if (!record) return null;
      return { record, number: _draftingGetDisplayNumberFromIndex(index) };
    })
    .filter(Boolean);

  if (!entries.length) {
    empty.style.display = '';
    list.removeAttribute('start');
    list.innerHTML = '';
    return;
  }

  empty.style.display = 'none';
  list.start = entries[0]?.number || _draftingGetStartNumber();
  list.innerHTML = entries.map(({ record, number }) => {
    const summaryParts = [record.author, record.year, record.collection].filter(Boolean);
    const summary = summaryParts.join(' · ');
    const ieee = _draftingFormatBibliographyEntry(record, number).replace(/^\[\d+\]\s*/, '');
    const display = ieee || record.apa || record.label || record.title || record.citation_id || 'Référence sans titre';
    return `
      <li id="draft-ref-${number}" value="${number}">
        <div class="drafting-bibliography-entry" data-citation-id="${escapeHtml(record.citation_id)}">
          <div class="drafting-bibliography-text">${escapeHtml(display)}</div>
          ${summary ? `<div class="drafting-item-meta">${escapeHtml(summary)}</div>` : ''}
          <div class="drafting-item-actions">
            <button class="citation-btn" type="button" data-drafting-action="copy" data-drafting-format="apa">Copier APA</button>
            <button class="citation-btn" type="button" data-drafting-action="copy" data-drafting-format="bibtex">Copier BibTeX</button>
          </div>
        </div>
      </li>
    `;
  }).join('');
}

function _draftingSuggestionExclusions() {
  const exclusions = new Set();
  state.drafting.citationOrder.forEach((citationId) => {
    const normalized = _draftingNormalizeCitationId(citationId);
    if (normalized) exclusions.add(normalized);
  });
  (state.lastDraftingSuggestions || []).forEach((suggestion) => {
    const normalized = _draftingNormalizeCitationId(suggestion?.citation_id || suggestion?.citation_key);
    if (normalized) exclusions.add(normalized);
  });
  return Array.from(exclusions);
}

function _draftingSetPendingSuggestion(citationId) {
  state.drafting.pendingSuggestionId = _draftingNormalizeCitationId(citationId);
  return state.drafting.pendingSuggestionId;
}

function _draftingHasPersistedCitation(citationId) {
  const normalized = _draftingNormalizeCitationId(citationId);
  if (!normalized) return false;
  return Array.from(state.drafting.citationIndex.keys()).some(
    (existingId) => _draftingNormalizeCitationId(existingId) === normalized,
  );
}

function removeDraftingSuggestion(citationId) {
  const normalized = _draftingNormalizeCitationId(citationId);
  if (!normalized) return;
  const nextSuggestions = (state.lastDraftingSuggestions || []).filter((item) => {
    const itemId = item?.citation_id || item?.citation_key;
    return _draftingNormalizeCitationId(itemId) !== normalized;
  });
  if (nextSuggestions.length === (state.lastDraftingSuggestions || []).length) return;
  state.lastDraftingSuggestions = nextSuggestions;
  if (state.drafting.pendingSuggestionId === normalized) state.drafting.pendingSuggestionId = '';
  if (_draftingHasPreviewToken(citationId)) {
    _draftingRemovePreviewToken(citationId);
    _draftingSyncFromEditor(false);
  }
  _draftingRenderCitationsPanel();
  showToast(_tr('Suggestion supprimée.'), 2200, 'success');
}

function _draftingSetButtonBusy(isBusy) {
  const btn = document.getElementById('drafting-btn');
  if (btn) {
    // A data-i18n key would make the translator put "Suggérer" back.
    btn.removeAttribute('data-i18n');
    btn.disabled = false;
    btn.classList.toggle('is-stop', Boolean(isBusy));
    btn.textContent = isBusy ? _tr('Stop') : _ti('drafting.run');
    btn.title = isBusy ? _tr('Interrompre la recherche de citations') : _tr('Suggérer des citations');
    btn.setAttribute('aria-pressed', isBusy ? 'true' : 'false');
  }
  _draftingUpdateBulkPreviewButton();
}

// ---- Analysis scope (selection or whole document) ----

let _draftingScopeFrame = 0;

function _draftingGetQueryScope() {
  if (_draftingGetSelectionText()) return 'selection';
  const storedRange = state.drafting.selectionRange;
  if (storedRange && _draftingRangePointsIntoEditor(storedRange) && !storedRange.collapsed
    && _draftingExtractPlainText(storedRange.cloneContents())) {
    return 'selection';
  }
  return 'document';
}

function _draftingCountCitableSentences(text) {
  // Mirrors the server: sentences carrying a citation marker or fewer than
  // four words are not analysed.
  return (String(text || '').match(/[^.!?\n\r]+(?:[.!?]+|$)/g) || [])
    .filter((sentence) => !sentence.includes(DRAFTING_CITED_MARKER))
    .filter((sentence) => sentence.trim().split(/\s+/).filter(Boolean).length >= 4)
    .length;
}

function _draftingUpdateScopeChip() {
  _draftingScopeFrame = 0;
  const chip = document.getElementById('drafting-scope-chip');
  if (!chip) return;
  const scope = _draftingGetQueryScope();
  const includeCited = _draftingIncludesCitedSentences();
  const count = _draftingCountCitableSentences(_draftingGetQueryText({ markCitations: !includeCited }));
  const label = chip.querySelector('.drafting-scope-label');
  const reset = chip.querySelector('[data-drafting-action="use-document"]');
  chip.classList.toggle('is-selection', scope === 'selection');
  const sentences = `${count} ${_tr(count > 1 ? 'phrases' : 'phrase')}`;
  if (label) {
    label.textContent = scope === 'selection'
      ? `${_tr('Sélection')} · ${sentences}`
      : `${_tr('Document')} · ${sentences}${includeCited ? '' : ` ${_tr('sans citation')}`}`;
  }
  chip.title = scope === 'selection'
    ? _tr('Seul le passage sélectionné sera analysé.')
    : _tr(includeCited
      ? 'Tout le document sera analysé, y compris les phrases déjà citées.'
      : 'Tout le document sera analysé ; les phrases déjà citées sont ignorées.');
  if (reset) reset.hidden = scope !== 'selection';
}

function _draftingScheduleScopeUpdate() {
  // Debounced: selectionchange fires continuously while dragging.
  if (_draftingScopeFrame) return;
  _draftingScopeFrame = setTimeout(_draftingUpdateScopeChip, 80);
}

function useDraftingWholeDocument() {
  state.drafting.selectionRange = null;
  window.getSelection()?.removeAllRanges();
  if (typeof updateDraftingSelectionToolbar === 'function') updateDraftingSelectionToolbar();
  _draftingUpdateScopeChip();
}

// ---- Editor interactions with citation tokens ----

function _draftingOnEditorPointerDown(event) {
  const editor = _draftingGetEditor();
  const el = _draftingResolveEventElementTarget(event?.target);
  // Keep the caret in place: the preview controls act on click.
  if (editor && el?.closest?.('[data-preview-action],[data-argument-preview-action]') && editor.contains(el)) event.preventDefault();
}

function _draftingOnEditorClick(event) {
  const editor = _draftingGetEditor();
  if (!editor) return;
  const el = _draftingResolveEventElementTarget(event?.target);
  const argumentPreview = el?.closest?.('.drafting-argument-preview');
  if (argumentPreview && editor.contains(argumentPreview)) {
    event.preventDefault();
    event.stopPropagation();
    const action = el.closest('[data-argument-preview-action]')?.dataset.argumentPreviewAction;
    if (action === 'accept') confirmDraftingArgumentPreview();
    else if (action === 'reject') removeDraftingArgumentPreview();
    else _draftingRevealArgumentCard(argumentPreview.dataset.argumentIndex);
    return;
  }
  const token = _draftingFindTokenFromTarget(event?.target, editor);
  if (!token) {
    _draftingCloseCitationPopover();
    _draftingStoreSelection();
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  const citationId = String(token.dataset.citationId || '');
  if (token.dataset.preview === '1') {
    const action = el?.closest?.('[data-preview-action]')?.dataset.previewAction;
    if (action === 'accept') _draftingActivatePreviewToken(token, editor);
    else if (action === 'reject') rejectDraftingPreview(citationId);
    else _draftingRevealSuggestionCard(citationId);
    return;
  }
  _draftingOpenCitationPopover(token);
}

function _draftingOnDelegatedAction(event) {
  const target = event?.target;
  if (!(target instanceof Element)) return;
  const button = target.closest('[data-drafting-action]');
  if (!button || button.disabled) return;
  const action = String(button.dataset.draftingAction || '');
  button.closest('details.drafting-item-more')?.removeAttribute('open');
  const sentence = _draftingGroupSentences[Number(button.dataset.sentenceRef)];
  if (action === 'preview') {
    const index = Number(button.dataset.draftingIndex);
    if (Number.isInteger(index) && index >= 0) previewDraftingCitation(index);
    return;
  }
  if (action === 'origin') {
    const index = Number(button.dataset.draftingIndex);
    if (Number.isInteger(index) && index >= 0) runDraftingOriginCheck(index);
    return;
  }
  if (action.startsWith('origin-')) { _draftingOnOriginAction(action, button); return; }
  if (action === 'locate-sentence') { _draftingRevealSentence(sentence); return; }
  if (action === 'retry-sentence') { if (sentence) retryDraftingSentences([sentence]); return; }
  if (action === 'search-external') { if (sentence) searchDraftingExternalCitations(sentence); return; }
  if (action === 'retry-failed') { retryDraftingSentences(_draftingFailedSentences()); return; }
  if (action === 'rerun') { runDraftingAssistant(); return; }
  if (action === 'use-document') { useDraftingWholeDocument(); return; }
  if (action === 'apply-confident') { applyConfidentDraftingPreviews(); return; }
  if (action.startsWith('history-') || action.startsWith('version-')) { _draftingOnHistoryAction(action, button); return; }

  const citationRoot = button.closest('[data-citation-id]');
  const citationId = String(citationRoot?.dataset.citationId || '');
  if (!citationId) return;
  if (action === 'copy') {
    copyDraftingCitationById(citationId, String(button.dataset.draftingFormat || 'apa'));
  } else if (action === 'remove') {
    removeDraftingSuggestion(citationId);
  } else if (action === 'reject-preview') {
    rejectDraftingPreview(citationId);
  } else if (action === 'read' || action === 'library') {
    const normalized = _draftingNormalizeCitationId(citationId);
    const record = (state.lastDraftingSuggestions || [])
      .find((entry) => _draftingNormalizeCitationId(entry.citation_id) === normalized);
    _draftingOpenCitationSource(record, action, button);
  }
}

// Opens the cited article: in the reader at the evidence page ('read'),
// or selected in the library ('library').
function _draftingOpenCitationSource(record, mode, returnFocusEl = null) {
  const path = String(record?.rel_path || '').trim();
  if (!path) {
    showToast(_tr('Article non trouvé dans la bibliothèque.'));
    return;
  }
  if (mode === 'library') {
    goToSource(path);
    return;
  }
  // PDF pages are indexed from 0; other formats use chapter or slide numbers.
  const page = Number.parseInt(record.page, 10);
  const pdfPage = /\.pdf$/i.test(path) && Number.isInteger(page) && page >= 0 ? page + 1 : null;
  openArticleViewer(path, record.title || record.label || path, returnFocusEl, pdfPage);
}

function _draftingOnDetailsToggle(event) {
  const details = event.target;
  if (!(details instanceof HTMLDetailsElement)) return;
  // Origin blocks are redrawn as the chain grows: keep their sections as the user left them.
  const originPart = details.classList.contains('drafting-origin-others')
    ? 'others'
    : (details.classList.contains('drafting-origin-passage') ? 'passage' : '');
  if (originPart) {
    const entry = _draftingOriginEntry(String(details.closest('[data-origin-key]')?.dataset.originKey || ''));
    if (entry) entry.open = { ...(entry.open || {}), [originPart]: details.open };
    return;
  }
  if (!details.classList.contains('drafting-item-details')) return;
  const id = _draftingNormalizeCitationId(details.closest('[data-citation-id]')?.dataset.citationId);
  if (!id) return;
  if (details.open) state.drafting.openSuggestionDetails.add(id);
  else state.drafting.openSuggestionDetails.delete(id);
}

function _draftingOnDocumentPointerDown(event) {
  const target = event?.target;
  document.querySelectorAll('.drafting-item-more[open]').forEach((menu) => {
    if (!(target instanceof Node) || !menu.contains(target)) menu.removeAttribute('open');
  });
  const popover = document.getElementById('drafting-citation-popover');
  if (popover && !popover.hidden && target instanceof Node && !popover.contains(target)
    && !_draftingFindTokenFromTarget(target)) {
    _draftingCloseCitationPopover();
  }
}

function _draftingBindCitationActions() {
  // Results can be rendered before asynchronous workspace startup completes.
  // Registering the same listener again is idempotent in the DOM.
  const panel = document.getElementById('drafting-panel-assistant') || _draftingGetResultPanel();
  panel?.addEventListener('click', _draftingOnDelegatedAction);
  panel?.addEventListener('toggle', _draftingOnDetailsToggle, true);
  _draftingGetBibliographyList()?.addEventListener('click', _draftingOnDelegatedAction);
  const editor = _draftingGetEditor();
  editor?.addEventListener('pointerdown', _draftingOnEditorPointerDown);
  editor?.addEventListener('click', _draftingOnEditorClick);
  editor?.addEventListener('scroll', _draftingCloseCitationPopover, { passive: true });
  document.addEventListener('pointerdown', _draftingOnDocumentPointerDown);
  document.addEventListener('keydown', _draftingOnPopoverKeydown);
  window.addEventListener('resize', _draftingCloseCitationPopover);
}

// ---- Popover for confirmed citations ----

let _draftingPopoverToken = null;

function _draftingGetCitationPopover() {
  let popover = document.getElementById('drafting-citation-popover');
  if (popover) return popover;
  popover = document.createElement('div');
  popover.id = 'drafting-citation-popover';
  popover.className = 'drafting-citation-popover';
  popover.setAttribute('role', 'dialog');
  popover.hidden = true;
  popover.addEventListener('click', (event) => {
    const button = event.target instanceof Element ? event.target.closest('[data-popover-action]') : null;
    if (button) _draftingRunPopoverAction(button.dataset.popoverAction);
  });
  document.body.appendChild(popover);
  return popover;
}

function _draftingOpenCitationPopover(token) {
  const record = _draftingGetCitationRecordFromToken(token);
  if (!record) return;
  const popover = _draftingGetCitationPopover();
  const number = token.dataset.citationNumber || '';
  const quote = String(record.evidence_quote || '').trim();
  const meta = [record.journal || record.collection, record.year].filter(Boolean).join(' · ');
  popover.setAttribute('aria-label', `${_tr('Référence')} [${number}]`);
  popover.innerHTML = `
    <div class="drafting-popover-head">
      <span class="drafting-popover-number">[${escapeHtml(number)}]</span>
      <strong>${escapeHtml(record.label || record.author || record.citation_id)}</strong>
    </div>
    ${record.title ? `<div class="drafting-popover-title">${escapeHtml(record.title)}</div>` : ''}
    ${meta ? `<div class="drafting-popover-meta">${escapeHtml(meta)}</div>` : ''}
    ${quote ? `<blockquote class="drafting-popover-quote">« ${escapeHtml(quote)} »</blockquote>` : ''}
    <div class="drafting-popover-actions">
      ${record.rel_path ? `<button type="button" class="citation-btn" data-popover-action="read">${escapeHtml(_tr('Lire'))}</button>` : ''}
      <button type="button" class="citation-btn" data-popover-action="bibliography">${escapeHtml(_tr('Voir dans la bibliographie'))}</button>
      <button type="button" class="citation-btn" data-popover-action="copy">${escapeHtml(_tr('Copier APA'))}</button>
      <button type="button" class="citation-btn drafting-delete-btn" data-popover-action="remove">${escapeHtml(_tr('Retirer du texte'))}</button>
    </div>`;
  _draftingPopoverToken = token;
  token.classList.add('is-selected');
  popover.hidden = false;
  const rect = token.getBoundingClientRect();
  const width = popover.offsetWidth;
  const height = popover.offsetHeight;
  const left = Math.max(12, Math.min(window.innerWidth - width - 12, rect.left + rect.width / 2 - width / 2));
  const below = rect.bottom + 8;
  const top = below + height > window.innerHeight - 12 ? Math.max(12, rect.top - height - 8) : below;
  popover.style.left = `${Math.round(left)}px`;
  popover.style.top = `${Math.round(top)}px`;
}

function _draftingCloseCitationPopover() {
  const popover = document.getElementById('drafting-citation-popover');
  if (popover) popover.hidden = true;
  _draftingPopoverToken?.classList.remove('is-selected');
  _draftingPopoverToken = null;
}

function _draftingOnPopoverKeydown(event) {
  if (event.key === 'Escape' && _draftingPopoverToken) _draftingCloseCitationPopover();
}

function _draftingRunPopoverAction(action) {
  const token = _draftingPopoverToken;
  if (!token?.isConnected) {
    _draftingCloseCitationPopover();
    return;
  }
  const citationId = String(token.dataset.citationId || '');
  const number = token.dataset.citationNumber || '';
  if (action === 'copy') {
    copyDraftingCitationById(citationId, 'apa');
  } else if (action === 'read') {
    const record = _draftingGetCitationRecordFromToken(token);
    _draftingCloseCitationPopover();
    _draftingOpenCitationSource(record, 'read', token);
  } else if (action === 'bibliography') {
    _draftingCloseCitationPopover();
    revealDraftingBibliographyEntry(number);
  } else if (action === 'remove') {
    _draftingCloseCitationPopover();
    _draftingRemoveTokenCleanly(token);
    _draftingSyncFromEditor();
    showToast(_tr('Citation retirée du texte.'), 2400, 'success');
  }
}

function revealDraftingBibliographyEntry(number) {
  const shell = document.getElementById('drafting-bibliography-shell');
  if (shell && !shell.open) shell.open = true;
  const entry = document.getElementById(`draft-ref-${number}`);
  if (!entry) return;
  entry.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  _draftingFlash(entry);
}

// ---- Citation results: one group per sentence ----

// Sentence texts of the last render, referenced by data-sentence-ref.
let _draftingGroupSentences = [];
let _draftingRunTimer = null;

const DRAFTING_SENTENCE_REASONS = {
  none: 'Aucune source assez solide',
  error: 'Vérification interrompue ou expirée',
  running: 'Analyse en cours…',
};

function _draftingSentenceKey(text) {
  return String(text || '').replace(/\s+/g, ' ').trim().toLowerCase();
}

function _draftingTruncate(text, max = 160) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function _draftingUpsertSentenceLog(sentenceText, patch) {
  const key = _draftingSentenceKey(sentenceText);
  if (!key) return null;
  const log = state.drafting.sentenceLog;
  let entry = log.find((item) => item.key === key);
  if (!entry) {
    entry = { key, text: String(sentenceText).trim(), status: 'running', count: 0, reason: '' };
    log.push(entry);
  }
  Object.assign(entry, patch);
  return entry;
}

function _draftingSentenceStatusFromEvent(event) {
  const count = Array.isArray(event?.suggestions) ? event.suggestions.length : 0;
  if (event?.skipped_cited) return 'cited';
  if (event?.skipped_short) return 'short';
  if (count) return 'found';
  if (event?.validation_status === 'validation_error') return 'error';
  return 'none';
}

function _draftingFailedSentences() {
  return state.drafting.sentenceLog.filter((item) => item.status === 'error').map((item) => item.text);
}

function _draftingRenderSuggestionCard(s, idx) {
  const pipelineRaw = String(s.source_pipeline || '').trim();
  const isExternal = pipelineRaw === 'external';
  const pipelineLabel = pipelineRaw === 'wiki+rag'
    ? 'Wiki + RAG'
    : (pipelineRaw === 'rag_fallback' ? _tr('RAG de secours') : (isExternal ? _tr('Hors bibliothèque') : _tr('Preuve indexée')));
  const citationCount = Number.isFinite(Number(s.citation_count)) ? Math.max(0, Math.round(Number(s.citation_count))) : 0;
  const influentialCount = Number.isFinite(Number(s.influential_citation_count)) ? Math.max(0, Math.round(Number(s.influential_citation_count))) : 0;
  const authorityBonus = Number.isFinite(Number(s.authority_bonus)) ? Math.max(0, Math.round(Number(s.authority_bonus) * 100)) : 0;
  const supportText = String(s.support_explanation || s.rationale || '').trim();
  const evidenceQuote = String(s.evidence_quote || '').trim();
  const confidencePercent = _draftingFormatConfidencePercent(s.confidence);
  const confidenceStyle = _draftingConfidenceStyleAttr(s.confidence);
  const citationId = _draftingNormalizeCitationId(s.citation_id);
  const detailsOpen = state.drafting.openSuggestionDetails.has(citationId) ? ' open' : '';
  const bibliometrics = citationCount > 0
    ? `<span class="drafting-item-confidence" title="${escapeAttr(authorityBonus ? `${_tr('Bonus bibliométrique plafonné')} : +${authorityBonus} pt(s).` : _tr('Signal bibliométrique disponible, sans bonus notable.'))}">${escapeHtml(String(citationCount))} ${escapeHtml(_tr(citationCount > 1 ? 'citations' : 'citation'))}${influentialCount > 0 ? ` · ${escapeHtml(String(influentialCount))} ${escapeHtml(_tr(influentialCount > 1 ? 'influentes' : 'influente'))}` : ''}</span>`
    : '';
  const externalSummary = isExternal ? String(s.summary || '').trim() : '';
  const hasDetails = evidenceQuote || supportText || externalSummary;
  return `
    <article class="drafting-item${isExternal ? ' is-external' : ''}" data-citation-id="${escapeHtml(s.citation_id)}">
      <div class="drafting-item-head">
        <div class="drafting-item-heading">
          <div class="drafting-item-title">${escapeHtml(s.label || `${s.author || _tr('Auteur inconnu')} (${s.year || 'n.d.'})`)}</div>
          <div class="drafting-item-meta">${escapeHtml((s.title || _tr('Sans titre')) + (s.collection ? ' · ' + s.collection : '') + (isExternal && s.journal ? ' · ' + s.journal : ''))}</div>
        </div>
        ${confidencePercent !== null ? `<span class="drafting-item-confidence is-score" title="${escapeAttr(_tr('Confiance de la vérification'))}"${confidenceStyle}>${escapeHtml(_tr('indice de confiance'))} ${confidencePercent}%</span>` : ''}
        ${isExternal ? `<span class="drafting-item-external-badge" title="${escapeAttr(_tr('Trouvé par une recherche externe : pertinence estimée sur le titre et le résumé, pas vérifiée dans le texte intégral.'))}">${escapeHtml(_tr('Hors bibliothèque'))}</span>` : ''}
      </div>
      ${hasDetails ? `
      <details class="drafting-item-details"${detailsOpen}>
        <summary class="disclosure">${escapeHtml(_tr('Pourquoi cette source ?'))}</summary>
        ${evidenceQuote ? `<blockquote class="drafting-item-evidence">« ${escapeHtml(evidenceQuote)} »</blockquote>` : ''}
        ${supportText ? `<div class="drafting-item-rationale">${escapeHtml(supportText)}</div>` : ''}
        ${externalSummary ? `<div class="drafting-item-rationale">${escapeHtml(_tr('Résumé'))} : ${escapeHtml(externalSummary)}</div>` : ''}
        ${isExternal ? `<div class="drafting-item-rationale is-warning">${escapeHtml(_tr('Non vérifiée dans le texte intégral : lisez l’article avant de le citer.'))}</div>` : ''}
        <div class="drafting-item-support">
          <span class="drafting-item-source-badge">${escapeHtml(pipelineLabel)}</span>
          ${bibliometrics}
        </div>
      </details>` : ''}
      <div class="drafting-item-actions">
        <span class="drafting-item-applied" hidden></span>
        <button class="citation-btn drafting-preview-btn" type="button" data-drafting-action="preview" data-drafting-index="${idx}">${escapeHtml(_tr('Placer dans le texte'))}</button>
        ${s.rel_path ? `<button class="citation-btn" type="button" data-drafting-action="read" title="${escapeAttr(_tr('Ouvrir l’article à la page de l’extrait'))}">${escapeHtml(_tr('Lire'))}</button>` : ''}
        ${s.rel_path && !isExternal ? `<button class="citation-btn" type="button" data-drafting-action="origin" data-drafting-index="${idx}" title="${escapeAttr(_tr('Cet article attribue-t-il cette idée à une source plus ancienne ?'))}">${escapeHtml(_tr('Source d’origine ?'))}</button>` : ''}
        ${isExternal && (s.external_url || s.open_access_pdf) ? `<a class="citation-btn" href="${escapeAttr(s.external_url || s.open_access_pdf)}" target="_blank" rel="noopener">${escapeHtml(_tr('Voir chez l’éditeur'))}</a>` : ''}
        <button class="citation-btn" type="button" data-drafting-action="reject-preview" hidden>${escapeHtml(_tr('Retirer l’aperçu'))}</button>
        <details class="drafting-item-more">
          <summary class="citation-btn" title="${escapeAttr(_tr('Plus d’actions'))}" aria-label="${escapeAttr(_tr('Plus d’actions'))}">⋯</summary>
          <div class="drafting-item-menu" role="menu">
            ${s.rel_path ? `<button type="button" role="menuitem" data-drafting-action="library">${escapeHtml(_tr('Voir dans la bibliothèque'))}</button>` : ''}
            <button type="button" role="menuitem" data-drafting-action="copy" data-drafting-format="apa">${escapeHtml(_tr('Copier APA'))}</button>
            <button type="button" role="menuitem" data-drafting-action="copy" data-drafting-format="bibtex">${escapeHtml(_tr('Copier BibTeX'))}</button>
            <button type="button" role="menuitem" class="is-danger" data-drafting-action="remove">${escapeHtml(_tr('Supprimer la suggestion'))}</button>
          </div>
        </details>
      </div>
      ${s.rel_path && !isExternal ? `<div class="drafting-origin-slot" data-origin-key="${escapeAttr(_draftingOriginKey(s))}">${_draftingRenderOrigin(_draftingOriginKey(s))}</div>` : ''}
    </article>`;
}

// ---- Source d'origine ----
// Does the cited article credit the idea to an earlier source? The check
// reads the article's own passage and bibliography (local only); the model
// only labels which citations of the passage carry the idea.

function _draftingOriginKey(suggestion) {
  return `${_draftingNormalizeCitationId(suggestion?.citation_id)}::${_draftingSentenceKey(suggestion?.sentence_text)}`;
}

function _draftingOriginEntry(key) {
  return state.drafting.origins.get(key) || null;
}

function _draftingOriginHeading(result) {
  const notes = Array.isArray(result.notes) ? result.notes : [];
  if (result.verdict === 'attributes_elsewhere') return _tr('Cet article attribue cette idée à une source antérieure :');
  if (result.verdict === 'no_attribution') return _tr('Dans ce passage, l’article ne cite aucune autre source pour cette idée.');
  if (result.verdict === 'unchecked') {
    return notes.includes('model_failed')
      ? _tr('Citations du passage (la vérification par le modèle a échoué) :')
      : _tr('Citations du passage (non vérifiées : aucun modèle choisi) :');
  }
  if (notes.includes('passage_not_found')) return _tr('Passage introuvable dans le texte de l’article.');
  if (notes.includes('reference_list_not_found')) return _tr('Bibliographie introuvable dans le PDF de l’article.');
  return _tr('Les citations du passage ne correspondent à aucune entrée de la bibliographie.');
}

// Following the chain: each library source credited for the idea can be
// checked in turn, its result nested under it. Sources outside the library
// can only be identified (Crossref, with web access on).
const DRAFTING_ORIGIN_MAX_DEPTH = 4;

function _draftingOriginChildKey(key, relPath) {
  return `${key}>${relPath}`;
}

function _draftingOriginRefYear(ref, identified) {
  const year = String(ref?.library?.year || identified?.work?.year || '').trim();
  if (year) return year;
  const match = String(ref?.text || '').match(/\b(1[5-9]|20)\d{2}\b/);
  return match ? match[0] : '';
}

function _draftingOriginRefLabel(ref, identified) {
  if (ref?.library?.label) return ref.library.label;
  const work = identified?.work;
  if (work?.authors_display) return `${work.authors_display} (${work.year || 'n.d.'})`;
  return _draftingTruncate(String(ref?.text || '').replace(/^\s*\[?\d{1,3}\]?\.?\s*/, ''), 80);
}

// Oldest source credited for the idea anywhere in the chain explored so far.
function _draftingOriginEarliest(key) {
  const entry = _draftingOriginEntry(key);
  const result = entry?.result;
  if (!result || result.verdict !== 'attributes_elsewhere') return null;
  let best = null;
  (result.citations || []).forEach((citation, citationIndex) => {
    if (citation.attribution !== 'for_this_idea') return;
    (citation.references || []).forEach((ref, refIndex) => {
      const identified = entry.identified?.[`${citationIndex}:${refIndex}`];
      const candidates = [{ label: _draftingOriginRefLabel(ref, identified), year: _draftingOriginRefYear(ref, identified) }];
      if (ref.library?.rel_path) {
        const deeper = _draftingOriginEarliest(_draftingOriginChildKey(key, ref.library.rel_path));
        if (deeper) candidates.push(deeper);
      }
      candidates.forEach((candidate) => {
        if (!candidate.year) return;
        if (!best || Number(candidate.year) < Number(best.year)) best = candidate;
      });
    });
  });
  return best;
}

function _draftingRenderOriginWork(identified) {
  if (!identified) return '';
  if (identified.status === 'loading') {
    return `<div class="drafting-origin-work is-muted"><span class="spinner"></span> ${escapeHtml(_tr('Identification…'))}</div>`;
  }
  if (identified.status === 'error') {
    return `<div class="drafting-origin-work is-warning">${escapeHtml(identified.message || _tr('Identification impossible.'))}</div>`;
  }
  const work = identified.work;
  if (!work) return `<div class="drafting-origin-work is-muted">${escapeHtml(_tr('Référence non identifiée dans Crossref.'))}</div>`;
  const parts = [work.title, work.journal, work.year].filter(Boolean).map((value) => escapeHtml(String(value)));
  if (Number(work.cited_by_count) > 0) parts.push(escapeHtml(`${_tr('cité')} ${work.cited_by_count} ${_tr('fois')}`));
  const link = work.url
    ? ` · <a href="${escapeAttr(work.url)}" target="_blank" rel="noopener">${escapeHtml(_tr('Voir chez l’éditeur'))}</a>`
    : '';
  return `<div class="drafting-origin-work">${parts.join(' · ')}${link}</div>`;
}

function _draftingRenderOriginRef(key, entry, citation, citationIndex) {
  const markers = (citation.markers || []).join(', ');
  const references = Array.isArray(citation.references) ? citation.references : [];
  if (!references.length) {
    return `<li class="drafting-origin-ref"><span class="drafting-origin-marker">${escapeHtml(markers)}</span> <span class="drafting-origin-text is-muted">${escapeHtml(_tr('Entrée introuvable dans la bibliographie.'))}</span></li>`;
  }
  const webAllowed = Boolean(state.appSettings?.allow_external_api_requests);
  const credited = citation.attribution !== 'for_another_point';
  return references.map((ref, refIndex) => {
    const data = `data-origin-citation="${citationIndex}" data-origin-ref="${refIndex}"`;
    // A numbered entry shows its own number once: "[5] E. Taliot…", not "[5,6] [5] E. Taliot…".
    const numbered = Number.isInteger(ref.number);
    const label = numbered ? `[${ref.number}]` : markers;
    const text = numbered ? String(ref.text || '').replace(/^\s*\[?\d{1,3}\]?\.?\s*/, '') : String(ref.text || '');
    const relPath = String(ref.library?.rel_path || '');
    const identified = entry.identified?.[`${citationIndex}:${refIndex}`];
    let actions;
    let nested = '';
    if (relPath) {
      const childKey = _draftingOriginChildKey(key, relPath);
      const canGoDeeper = credited
        && (entry.depth || 0) + 1 < DRAFTING_ORIGIN_MAX_DEPTH
        && !(entry.ancestors || []).includes(relPath)
        && !_draftingOriginEntry(childKey);
      actions = `
        ${canGoDeeper ? `<button type="button" class="drafting-mini-btn is-primary" data-drafting-action="origin-deeper" ${data} title="${escapeAttr(_tr('Chercher à qui cet article attribue l’idée'))}">${escapeHtml(_tr('Remonter plus loin'))}</button>` : ''}
        <button type="button" class="drafting-mini-btn" data-drafting-action="origin-read" ${data}>${escapeHtml(_tr('Lire'))}</button>
        <button type="button" class="drafting-mini-btn" data-drafting-action="origin-library" ${data}>${escapeHtml(_tr('Voir dans la bibliothèque'))}</button>`;
      if (_draftingOriginEntry(childKey)) {
        nested = `<div class="drafting-origin-slot is-nested" data-origin-key="${escapeAttr(childKey)}">${_draftingRenderOrigin(childKey)}</div>`;
      }
    } else {
      actions = `
        <span class="drafting-origin-badge">${escapeHtml(_tr('Hors bibliothèque'))}</span>
        ${webAllowed && !identified ? `<button type="button" class="drafting-mini-btn" data-drafting-action="origin-identify" ${data} title="${escapeAttr(_tr('Rechercher cette référence dans Crossref (seule la référence est envoyée)'))}">${escapeHtml(_tr('Identifier'))}</button>` : ''}`;
    }
    return `
      <li class="drafting-origin-ref">
        <span class="drafting-origin-marker">${escapeHtml(label)}</span>
        <span class="drafting-origin-text">${escapeHtml(_draftingTruncate(text, 260))}</span>
        ${_draftingRenderOriginWork(identified)}
        <span class="drafting-origin-actions">
          ${actions}
          <button type="button" class="drafting-mini-btn" data-drafting-action="origin-copy" ${data}>${escapeHtml(_tr('Copier la référence'))}</button>
        </span>
        ${nested}
      </li>`;
  }).join('');
}

function _draftingRenderOrigin(key) {
  const entry = _draftingOriginEntry(key);
  if (!entry) return '';
  if (entry.status === 'loading') {
    return `<div class="drafting-origin is-loading" role="status"><span class="spinner"></span> ${escapeHtml(_tr('Lecture de l’article et de sa bibliographie…'))}</div>`;
  }
  if (entry.status === 'error') {
    return `<div class="drafting-origin is-error" role="status">${escapeHtml(entry.message || _tr('La recherche de la source d’origine a échoué.'))}</div>`;
  }
  const result = entry.result || {};
  const citations = (Array.isArray(result.citations) ? result.citations : []).map((citation, index) => ({ citation, index }));
  const credited = citations.filter(({ citation }) => citation.attribution === 'for_this_idea');
  const shown = result.verdict === 'attributes_elsewhere' ? credited : (result.verdict === 'unchecked' ? citations : []);
  const others = citations.filter((item) => !shown.includes(item));
  const notes = [];
  if (result.verdict === 'no_attribution') {
    notes.push(_tr('Cela ne prouve pas que l’article soit à l’origine de l’idée.'));
    if (result.self_claim) notes.push(_tr('Le passage présente l’idée comme un apport de l’article.'));
  }
  if (result.model_reliability === 'low' && ['attributes_elsewhere', 'no_attribution'].includes(result.verdict)) {
    notes.push(_tr('Petit modèle : attribution indicative, vérifiez le passage.'));
  }
  if (result.passage && result.passage.located_by !== 'quote') {
    notes.push(_tr('Passage retrouvé automatiquement : vérifiez qu’il énonce bien l’idée.'));
  }
  const outside = shown.some(({ citation }) => (citation.references || []).some((ref) => !ref.library?.rel_path));
  if (outside && !state.appSettings?.allow_external_api_requests) {
    notes.push(_tr('Activez l’accès web dans les Réglages pour identifier les sources hors bibliothèque.'));
  }
  const earliest = (entry.depth || 0) === 0 ? _draftingOriginEarliest(key) : null;
  const explored = earliest && [...state.drafting.origins.keys()].some((other) => other.startsWith(`${key}>`));
  const passageOpen = entry.open?.passage ? ' open' : '';
  const passage = result.passage?.text
    ? `<details class="drafting-origin-passage"${passageOpen}>
        <summary class="disclosure">${escapeHtml(_tr('Passage de l’article'))}${result.passage.page ? ` · ${escapeHtml(String(result.passage.page))}` : ''}</summary>
        <blockquote class="drafting-item-evidence">« ${escapeHtml(result.passage.text)} »</blockquote>
      </details>`
    : '';
  // With no credited source, the passage's citations are the user's best check
  // against a missed attribution: show them open.
  const othersOpen = (entry.open?.others ?? result.verdict === 'no_attribution') ? ' open' : '';
  const otherList = others.length
    ? `<details class="drafting-origin-others"${othersOpen}>
        <summary class="disclosure">${escapeHtml(_tr('Autres citations du passage'))} (${others.length})</summary>
        <ul class="drafting-origin-list">${others.map(({ citation, index }) => _draftingRenderOriginRef(key, entry, citation, index)).join('')}</ul>
      </details>`
    : '';
  return `
    <div class="drafting-origin" data-verdict="${escapeAttr(result.verdict || '')}">
      ${explored ? `<div class="drafting-origin-earliest">${escapeHtml(_tr('Plus ancienne source créditée trouvée'))} : <strong>${escapeHtml(earliest.label)}</strong></div>` : ''}
      <div class="drafting-origin-heading">${escapeHtml(_draftingOriginHeading(result))}</div>
      ${shown.length ? `<ul class="drafting-origin-list">${shown.map(({ citation, index }) => _draftingRenderOriginRef(key, entry, citation, index)).join('')}</ul>` : ''}
      ${notes.map((note) => `<div class="drafting-item-rationale">${escapeHtml(note)}</div>`).join('')}
      ${otherList}
      ${passage}
    </div>`;
}

function _draftingRefreshOrigin(key) {
  document.querySelectorAll('.drafting-origin-slot[data-origin-key]').forEach((slot) => {
    if (slot.dataset.originKey === key) slot.innerHTML = _draftingRenderOrigin(key);
  });
}

async function runDraftingOriginCheck(index) {
  const record = (state.lastDraftingSuggestions || [])[index];
  if (!record?.rel_path) {
    showToast(_tr('Article non trouvé dans la bibliothèque.'));
    return;
  }
  const key = _draftingOriginKey(record);
  if (_draftingOriginEntry(key)?.status === 'loading') return;
  await _draftingRunOriginTrace(key, {
    sentence: String(record.sentence_text || '').trim(),
    relPath: record.rel_path,
    evidenceQuote: String(record.evidence_quote || '').trim(),
    depth: 0,
    ancestors: [record.rel_path],
  }, key);
}

// ``refreshKey`` is the slot to redraw while loading: the entry itself at the
// root, its parent for a nested step (the child slot only exists in it).
async function _draftingRunOriginTrace(key, request, refreshKey) {
  const base = {
    sentence: request.sentence,
    depth: request.depth,
    ancestors: request.ancestors,
    identified: {},
  };
  state.drafting.origins.set(key, { ...base, status: 'loading' });
  _draftingRefreshOrigin(refreshKey);
  try {
    const response = await fetch('/api/drafting-origin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify({
        sentence: request.sentence,
        rel_path: request.relPath,
        evidence_quote: request.evidenceQuote || '',
        model: optionalSelectedModel('drafting') || null,
      }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.msg || _tr('La recherche de la source d’origine a échoué.'));
    }
    state.drafting.origins.set(key, { ...base, status: 'done', result: payload });
  } catch (error) {
    state.drafting.origins.set(key, { ...base, status: 'error', message: String(error?.message || '') });
  }
  _draftingRefreshOrigin(refreshKey);
}

async function _draftingIdentifyOriginRef(key, entry, refId, ref) {
  entry.identified = { ...(entry.identified || {}), [refId]: { status: 'loading' } };
  _draftingRefreshOrigin(key);
  let identified;
  try {
    const response = await fetch('/api/drafting-origin-identify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      body: JSON.stringify({ reference: String(ref.text || '') }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || payload.ok === false) throw new Error(payload.msg || _tr('Identification impossible.'));
    identified = { status: 'done', work: payload.work || null };
  } catch (error) {
    identified = { status: 'error', message: String(error?.message || '') };
  }
  entry.identified = { ...(entry.identified || {}), [refId]: identified };
  _draftingRefreshOrigin(_draftingOriginRootKey(key));
}

function _draftingOriginRootKey(key) {
  return String(key || '').split('>')[0];
}

async function _draftingOnOriginAction(action, button) {
  const key = String(button.closest('[data-origin-key]')?.dataset.originKey || '');
  const entry = _draftingOriginEntry(key);
  const citationIndex = Number(button.dataset.originCitation);
  const refIndex = Number(button.dataset.originRef);
  const citation = entry?.result?.citations?.[citationIndex];
  const ref = citation?.references?.[refIndex];
  if (!ref) return;
  if (action === 'origin-copy') {
    const ok = await copyTextWithFallback(ref.text || '');
    showToast(ok ? _tr('Référence copiée.') : _tr('Copie impossible.'), ok ? 2500 : 3600, ok ? 'success' : 'error');
    return;
  }
  if (action === 'origin-identify') {
    _draftingIdentifyOriginRef(key, entry, `${citationIndex}:${refIndex}`, ref);
    return;
  }
  const library = ref.library || {};
  if (action === 'origin-deeper') {
    if (!library.rel_path) return;
    const childKey = _draftingOriginChildKey(key, library.rel_path);
    await _draftingRunOriginTrace(childKey, {
      sentence: entry.sentence,
      relPath: library.rel_path,
      evidenceQuote: '',
      depth: (entry.depth || 0) + 1,
      ancestors: [...(entry.ancestors || []), library.rel_path],
    }, key);
    // The root summary ("oldest credited source") depends on every level.
    _draftingRefreshOrigin(_draftingOriginRootKey(key));
    return;
  }
  _draftingOpenCitationSource(
    { rel_path: library.rel_path, title: library.title || library.label, page: '' },
    action === 'origin-library' ? 'library' : 'read',
    button,
  );
}

function _draftingRenderRunStatus() {
  const run = state.drafting.citationRun;
  if (!run) return '';
  if (run.running) {
    const total = Number(run.total || 0);
    const current = Math.min(total || 1, Number(run.processed || 0) + 1);
    const percent = total ? Math.round((Number(run.processed || 0) / total) * 100) : 0;
    const heading = total
      ? `${_tr('Phrase')} ${current}/${total}`
      : _tr('Préparation de l’analyse…');
    return `
      <div class="drafting-run-status is-running" role="status" aria-live="polite">
        <div class="drafting-run-line">
          <span class="spinner"></span>
          <strong>${escapeHtml(heading)}</strong>
          <span class="drafting-run-count">${escapeHtml(`${run.accepted || 0} ${_tr(Number(run.accepted) > 1 ? 'sources trouvées' : 'source trouvée')}`)}</span>
          <span class="drafting-run-elapsed"></span>
        </div>
        <div class="drafting-run-bar" aria-hidden="true"><span style="width:${percent}%"></span></div>
        ${run.currentSentence ? `<div class="drafting-run-current">« ${escapeHtml(_draftingTruncate(run.currentSentence, 180))} »</div>` : ''}
        <div class="drafting-run-hint" hidden>${escapeHtml(_tr('Le modèle vérifie chaque source contre la phrase : comptez jusqu’à 1–2 min par phrase avec un modèle local.'))}</div>
      </div>`;
  }
  const failed = _draftingFailedSentences().length;
  const found = state.drafting.sentenceLog.filter((item) => item.status === 'found').length;
  const analysed = state.drafting.sentenceLog.filter((item) => ['found', 'none', 'error'].includes(item.status)).length;
  let summary;
  let tone = 'is-done';
  if (run.error) {
    summary = `${_tr('Erreur')} : ${run.error}`;
    tone = 'is-error';
  } else if (run.stopped) {
    summary = _tr('Recherche interrompue.');
    tone = 'is-warning';
  } else {
    summary = `${_tr('Analyse terminée')} · ${run.accepted || 0} ${_tr(Number(run.accepted) > 1 ? 'sources' : 'source')} ${_tr('pour')} ${found}/${analysed} ${_tr(analysed > 1 ? 'phrases' : 'phrase')}`;
  }
  const actions = [];
  if (failed && !run.error) {
    tone = 'is-warning';
    actions.push(`<button type="button" class="drafting-mini-btn is-primary" data-drafting-action="retry-failed">${escapeHtml(`${_tr('Réessayer les phrases en échec')} (${failed})`)}</button>`);
  }
  if (run.error || run.stopped) {
    actions.push(`<button type="button" class="drafting-mini-btn is-primary" data-drafting-action="rerun">${escapeHtml(_tr('Relancer'))}</button>`);
  }
  return `
    <div class="drafting-run-status ${tone}" role="status">
      <div class="drafting-run-line"><strong>${escapeHtml(summary)}</strong></div>
      ${failed && !run.error ? `<div class="drafting-run-note">${escapeHtml(_tr('Certaines phrases n’ont pas pu être vérifiées (délai du modèle dépassé ou réponse invalide).'))}</div>` : ''}
      ${actions.length ? `<div class="drafting-run-actions">${actions.join('')}</div>` : ''}
    </div>`;
}

function _draftingRenderEmptyState(skippedCited) {
  const run = state.drafting.citationRun;
  if (run?.running) {
    return `<div class="drafting-empty-note">${escapeHtml(_tr('Les sources vérifiées s’afficheront ici, phrase par phrase.'))}</div>`;
  }
  if (!run) {
    return `
      <div class="drafting-empty-state">
        <strong>${escapeHtml(_tr('Trouver des sources pour votre texte'))}</strong>
        <ol>
          <li>${escapeHtml(_tr('Sélectionnez un passage, ou gardez tout le document.'))}</li>
          <li>${escapeHtml(_tr('Cliquez sur Citations : chaque phrase est comparée à votre bibliothèque.'))}</li>
          <li>${escapeHtml(_tr('Placez une source dans le texte, puis validez-la avec ✓.'))}</li>
        </ol>
      </div>`;
  }
  if (run.error || run.stopped) return '';
  const status = run.finalStatus || '';
  let title = _tr('Aucune source trouvée.');
  let tips = [];
  if (run.serverMessage && ['rag_unavailable', 'empty_paragraph'].includes(status)) {
    title = run.serverMessage;
    if (status === 'rag_unavailable') tips = [_tr('Ouvrez la Bibliothèque pour lancer ou terminer l’indexation.')];
  } else if (skippedCited && !state.drafting.sentenceLog.some((item) => ['found', 'none', 'error'].includes(item.status))) {
    title = _tr('Toutes les phrases analysées ont déjà une citation.');
    tips = [
      _tr('Sélectionnez un passage pour réanalyser des phrases déjà citées.'),
      _tr('Ou cochez « Inclure les phrases déjà citées » dans Options des citations.'),
    ];
  } else if (status === 'no_candidates') {
    tips = [
      _tr('Aucun passage de votre bibliothèque ne correspond à ces phrases.'),
      _tr('Précisez l’indice de recherche (auteurs, mots-clés, sujet).'),
      _tr('Vérifiez que les PDF concernés sont bien indexés.'),
      _tr('« Rechercher plus de citations » sous une phrase cherche des articles hors de votre bibliothèque.'),
    ];
  } else if (status === 'validation_error') {
    tips = [
      _tr('La vérification par le modèle a échoué ou expiré.'),
      _tr('Réessayez ; si le problème persiste, choisissez un modèle plus rapide.'),
    ];
  } else {
    tips = [
      _tr('Des passages proches existent, mais aucun ne soutient assez clairement ces phrases.'),
      _tr('Formulez la phrase de façon plus factuelle, ou sélectionnez un passage plus large.'),
      _tr('Ajoutez un indice de recherche (auteurs, mots-clés).'),
      _tr('« Rechercher plus de citations » sous une phrase cherche des articles hors de votre bibliothèque.'),
    ];
  }
  return `
    <div class="drafting-empty-state">
      <strong>${escapeHtml(title)}</strong>
      ${tips.length ? `<ul>${tips.map((tip) => `<li>${escapeHtml(tip)}</li>`).join('')}</ul>` : ''}
    </div>`;
}

function _draftingRenderCitationsPanel() {
  const container = _draftingGetResultPanel();
  if (!container) return;
  _draftingBindCitationActions();
  _draftingRefreshViewTabs();
  container.removeAttribute('data-i18n');
  const suggestions = state.lastDraftingSuggestions || [];

  // Groups follow the analysis order; suggestions without a log entry
  // (restored or rendered directly) are appended in their own order.
  const groups = [];
  const byKey = new Map();
  const ensureGroup = (key, text, entry = null) => {
    if (!byKey.has(key)) {
      const group = { key, text, entry, items: [] };
      byKey.set(key, group);
      groups.push(group);
    }
    return byKey.get(key);
  };
  state.drafting.sentenceLog.forEach((entry) => {
    if (['found', 'none', 'error', 'running'].includes(entry.status)) ensureGroup(entry.key, entry.text, entry);
  });
  suggestions.forEach((s, idx) => {
    const key = _draftingSentenceKey(s.sentence_text) || '__other__';
    ensureGroup(key, String(s.sentence_text || '').trim()).items.push({ s, idx });
  });

  _draftingGroupSentences = [];
  const running = Boolean(state.drafting.citationRun?.running);
  const groupsHtml = groups.map((group) => {
    const ref = _draftingGroupSentences.push(group.text) - 1;
    const sentenceButton = group.text
      ? `<button type="button" class="drafting-group-sentence" data-drafting-action="locate-sentence" data-sentence-ref="${ref}" title="${escapeAttr(_tr('Afficher la phrase dans le texte'))}">« ${escapeHtml(_draftingTruncate(group.text, 220))} »</button>`
      : `<span class="drafting-group-sentence is-static">${escapeHtml(_tr('Autres suggestions'))}</span>`;
    const external = group.text && group.entry?.status !== 'running'
      ? _draftingRenderExternalSearch(group.key, ref, running)
      : '';
    if (group.items.length) {
      return `
        <section class="drafting-group">
          <div class="drafting-group-head">
            <span class="drafting-group-badge is-found" title="${escapeAttr(_tr('Sources trouvées pour cette phrase'))}">${group.items.length}</span>
            ${sentenceButton}
          </div>
          ${group.items.map(({ s, idx }) => _draftingRenderSuggestionCard(s, idx)).join('')}
          ${external}
        </section>`;
    }
    const status = group.entry?.status || 'none';
    const icon = status === 'error' ? '⚠' : (status === 'running' ? '' : '✕');
    const retry = status === 'none' || status === 'error'
      ? `<button type="button" class="drafting-mini-btn" data-drafting-action="retry-sentence" data-sentence-ref="${ref}"${running ? ' disabled' : ''}>${escapeHtml(_tr('Réessayer'))}</button>`
      : '';
    return `
      <div class="drafting-log-row is-${escapeAttr(status)}">
        <span class="drafting-group-badge is-${escapeAttr(status)}" aria-hidden="true">${status === 'running' ? '<span class="spinner"></span>' : icon}</span>
        <div class="drafting-log-body">
          ${sentenceButton}
          <span class="drafting-log-reason">${escapeHtml(_tr(DRAFTING_SENTENCE_REASONS[status] || DRAFTING_SENTENCE_REASONS.none))}</span>
          ${external}
        </div>
        ${retry}
      </div>`;
  }).join('');

  const skippedCited = state.drafting.sentenceLog.filter((item) => item.status === 'cited').length;
  const skippedShort = state.drafting.sentenceLog.filter((item) => item.status === 'short').length;
  const skipped = [
    skippedCited ? `${skippedCited} ${_tr(skippedCited > 1 ? 'phrases déjà citées' : 'phrase déjà citée')}` : '',
    skippedShort ? `${skippedShort} ${_tr(skippedShort > 1 ? 'phrases trop courtes' : 'phrase trop courte')}` : '',
  ].filter(Boolean).join(' · ');

  container.innerHTML = `
    ${_draftingRenderRunStatus()}
    ${suggestions.length ? '' : _draftingRenderEmptyState(skippedCited)}
    ${groupsHtml}
    ${skipped ? `<div class="drafting-skipped-note">${escapeHtml(`${_tr('Ignorées')} : ${skipped}`)}</div>` : ''}`;

  _draftingRefreshSuggestionActionButtons();
  _draftingUpdateBulkPreviewButton();
  _draftingUpdateRunTimer();
}

function _renderDraftingSuggestions(payload, append = false) {
  const incoming = Array.isArray(payload?.suggestions) ? payload.suggestions : [];
  const normalized = incoming
    .map((item) => _draftingBuildCitationRecord(item))
    .filter((item) => item.citation_id);
  const existing = append ? (state.lastDraftingSuggestions || []) : [];
  const merged = new Map();
  existing.forEach((item) => merged.set(_draftingNormalizeCitationId(item.citation_id), item));
  normalized.forEach((item) => merged.set(_draftingNormalizeCitationId(item.citation_id), item));
  state.lastDraftingSuggestions = Array.from(merged.values());
  const assistantText = String(payload?.assistant_text || '').trim();
  if (assistantText) state.drafting.lastAssistantText = assistantText;
  _draftingRenderCitationsPanel();
}

function _draftingUpdateRunTimer() {
  const run = state.drafting.citationRun;
  if (!run?.running) {
    if (_draftingRunTimer) clearInterval(_draftingRunTimer);
    _draftingRunTimer = null;
    return;
  }
  const tick = () => {
    const container = _draftingGetResultPanel();
    const current = state.drafting.citationRun;
    if (!container || !current?.running) {
      _draftingUpdateRunTimer();
      return;
    }
    const seconds = Math.max(0, Math.round((Date.now() - (current.sentenceStartedAt || current.startedAt)) / 1000));
    const elapsed = container.querySelector('.drafting-run-elapsed');
    if (elapsed) elapsed.textContent = seconds >= 3 ? `· ${seconds} s` : '';
    const hint = container.querySelector('.drafting-run-hint');
    if (hint) hint.hidden = seconds < 20;
  };
  tick();
  if (!_draftingRunTimer) _draftingRunTimer = setInterval(tick, 1000);
}

// Full range of a sentence in the editor, or null when it was edited away.
function _draftingFindSentenceRange(editor, sentenceText) {
  const end = _draftingFindSentenceEndRange(editor, sentenceText, null);
  if (!end) return null;
  const target = String(sentenceText || '').replace(/\s+/g, ' ').trim().replace(/[.!?…»"'’”)\]]+$/, '');
  const probe = target.slice(0, 48);
  const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT, null);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (node.parentElement?.closest('.drafting-citation-token')) continue;
    const index = String(node.nodeValue || '').replace(/\s/g, ' ').indexOf(probe);
    if (index < 0) continue;
    const range = document.createRange();
    range.setStart(node, index);
    range.setEnd(end.startContainer, end.startOffset);
    if (!range.collapsed) return range;
  }
  const fallback = end.cloneRange();
  fallback.setStart(end.startContainer, Math.max(0, end.startOffset - Math.min(end.startOffset, target.length)));
  return fallback;
}

function _draftingScrollRangeIntoView(range) {
  const editor = _draftingGetEditor();
  const rect = range.getBoundingClientRect();
  if (!editor || !rect.height) return;
  const box = editor.getBoundingClientRect();
  if (rect.top >= box.top && rect.bottom <= box.bottom) return;
  editor.scrollBy({ top: rect.top - box.top - editor.clientHeight / 3, behavior: 'smooth' });
}

function _draftingRevealSentence(sentenceText) {
  const editor = _draftingGetEditor();
  if (!editor || !sentenceText) return;
  const range = _draftingFindSentenceRange(editor, sentenceText);
  if (!range) {
    showToast(_tr('Phrase introuvable : elle a sans doute été modifiée depuis l’analyse.'), 3200, 'info');
    return;
  }
  _draftingScrollRangeIntoView(range);
  // A highlight, not a selection: selecting would change the analysis scope.
  if (window.CSS?.highlights && typeof Highlight === 'function') {
    CSS.highlights.set('drafting-sentence', new Highlight(range));
    clearTimeout(_draftingRevealSentence.timer);
    _draftingRevealSentence.timer = setTimeout(() => CSS.highlights.delete('drafting-sentence'), 2600);
  }
}

async function copyDraftingCitationById(citationId, format) {
  const normalized = _draftingNormalizeCitationId(citationId);
  const persisted = Array.from(state.drafting.citationIndex.values());
  const suggestions = state.lastDraftingSuggestions || [];
  const item = persisted.find((entry) => _draftingNormalizeCitationId(entry.citation_id) === normalized)
    || suggestions.find((entry) => _draftingNormalizeCitationId(entry.citation_id) === normalized);
  if (!item) {
    showToast(_tr('Citation introuvable.'));
    return;
  }
  const value = format === 'bibtex' ? item.bibtex : item.apa;
  if (!value) {
    showToast(_tr('Citation vide.'));
    return;
  }
  const ok = await copyTextWithFallback(value);
  showToast(ok
    ? _tr(format === 'bibtex' ? 'BibTeX copié.' : 'Citation APA copiée.')
    : _tr('Copie impossible.'), ok ? 2500 : 3600, ok ? 'success' : 'error');
}

function _draftingFormatBibliographyEntry(record, number) {
  const displayNumber = _draftingNormalizeStartNumber(number);
  const authors = _draftingFormatIeeeAuthors(record);
  const title = String(record?.title || record?.label || _tr('Sans titre')).trim();
  const journal = String(record?.journal || '').trim();
  const collection = String(record?.collection || '').trim();
  const year = String(record?.year || '').trim();
  const doi = String(record?.doi || '').trim();

  const parts = [];
  if (authors) parts.push(authors);
  parts.push(`"${title},"`);
  if (journal) {
    parts.push(year ? `${journal}, ${year}` : journal);
  } else if (collection && year) {
    parts.push(`${collection}, ${year}`);
  } else if (year) {
    parts.push(year);
  } else if (collection) {
    parts.push(collection);
  }
  let line = `[${displayNumber}] ` + parts.filter(Boolean).join(', ');
  if (doi) line += `, doi: ${doi}`;
  if (!/[.!?]$/.test(line)) line += '.';
  return line;
}

function _draftingFormatIeeeAuthors(record) {
  const raw = String(record?.authors_display || record?.author || '').trim();
  if (!raw) return _tr('Auteur inconnu');
  if (/\bet\s+al\.?$/i.test(raw)) {
    const head = raw.replace(/\s*,?\s*et\s+al\.?$/i, '').trim();
    const firstAuthor = _draftingSplitAuthors(head)[0] || head;
    return `${_draftingIeeeName(firstAuthor)} et al.`;
  }
  const authors = _draftingSplitAuthors(raw)
    .map(_draftingIeeeName)
    .filter(Boolean);
  if (!authors.length) return raw;
  if (authors.length === 1) return authors[0];
  if (authors.length === 2) return `${authors[0]} and ${authors[1]}`;
  return authors.slice(0, -1).join(', ') + ', and ' + authors[authors.length - 1];
}

function _draftingSplitAuthors(raw) {
  const value = String(raw || '').trim();
  if (!value) return [];
  if (/\s+and\s+/i.test(value)) {
    return value.split(/\s*;\s*|\s+and\s+/i).map((s) => s.trim()).filter(Boolean);
  }
  // Cas Zotero "Last, First, Last, First" → on regroupe par paires.
  const tokens = value.split(/\s*,\s*/).filter(Boolean);
  const looksLikePairs = tokens.length >= 2
    && tokens.length % 2 === 0
    && /^[A-Za-zÀ-ÿ'’-]+$/.test(tokens[0])
    && /^[A-Z]/.test(tokens[1]);
  if (looksLikePairs) {
    const pairs = [];
    for (let i = 0; i + 1 < tokens.length; i += 2) {
      pairs.push(`${tokens[i]}, ${tokens[i + 1]}`);
    }
    return pairs;
  }
  return tokens;
}

function _draftingIeeeName(raw) {
  const trimmed = String(raw || '').trim().replace(/\s+/g, ' ');
  if (!trimmed) return '';
  if (trimmed.includes(',')) {
    const [last, ...rest] = trimmed.split(/\s*,\s*/);
    const firsts = rest.join(' ').split(/\s+/).filter(Boolean);
    const initials = firsts.map((part) => part.charAt(0).toUpperCase() + '.').join(' ');
    return [initials, last].filter(Boolean).join(' ').trim();
  }
  const parts = trimmed.split(/\s+/);
  if (parts.length === 1) return parts[0];
  const last = parts[parts.length - 1];
  const initials = parts.slice(0, -1).map((p) => p.charAt(0).toUpperCase() + '.').join(' ');
  return [initials, last].filter(Boolean).join(' ').trim();
}

async function copyDraftingBibliography() {
  const entries = state.drafting.citationOrder
    .map((citationId, index) => ({ record: state.drafting.citationIndex.get(citationId), number: _draftingGetDisplayNumberFromIndex(index) }))
    .filter((entry) => Boolean(entry.record));
  if (!entries.length) {
    showToast(_tr('Aucune bibliographie à copier.'));
    return;
  }
  const text = entries.map(({ record, number }) => _draftingFormatBibliographyEntry(record, number)).join('\n');
  const ok = await copyTextWithFallback(text);
  showToast(ok ? _tr('Bibliographie copiée.') : _tr('Copie impossible.'), ok ? 2500 : 3600, ok ? 'success' : 'error');
}

function stopDraftingAssistantSearch() {
  const controller = state.drafting.searchAbortController;
  if (controller) controller.abort();
  state.drafting.searchAbortController = null;
  state.drafting.searching = false;
  _draftingFinishRun({ stopped: true });
  _draftingSetButtonBusy(false);
  showToast(_tr('Recherche interrompue.'), 2800, 'info');
}

function _draftingFinishRun(patch = {}) {
  const run = state.drafting.citationRun;
  if (run) Object.assign(run, { running: false }, patch);
  // Sentences still marked as running were never analysed.
  state.drafting.sentenceLog = state.drafting.sentenceLog.filter((item) => item.status !== 'running');
  _draftingRenderCitationsPanel();
}

// Where the last preview landed: 'sentence', 'cursor' or ''.
let _draftingLastPlacement = '';

function _draftingRevealCitationToken(token) {
  if (!token) return;
  const range = document.createRange();
  range.selectNode(token);
  _draftingScrollRangeIntoView(range);
  _draftingFlash(token);
}

function previewDraftingCitation(index) {
  const suggestions = state.lastDraftingSuggestions || [];
  const item = suggestions[index];
  if (!item) {
    showToast(_tr('Citation introuvable.'));
    return;
  }
  const record = _draftingBuildCitationRecord(item);
  const existingPreview = _draftingFindPreviewToken(record.citation_id);
  if (existingPreview) {
    _draftingCommitPreviewCitation(record);
    return;
  }
  if (_draftingHasPersistedCitation(record.citation_id)) {
    const editor = _draftingGetEditor();
    const token = editor?.querySelector(`.drafting-citation-token[data-citation-id="${CSS.escape(record.citation_id)}"]:not(.is-preview)`);
    _draftingRevealCitationToken(token);
    return;
  }
  _draftingSetPendingSuggestion(record.citation_id);
  if (_draftingPreviewCitation(record)) {
    _draftingSyncFromEditor();
    _draftingRevealCitationToken(_draftingFindPreviewToken(record.citation_id));
    if (_draftingLastPlacement === 'cursor' && record.sentence_text) {
      showToast(_tr('Phrase d’origine introuvable : aperçu placé au curseur.'), 3400, 'info');
    }
  } else {
    showToast(_tr('Impossible de placer l\'aperçu.'));
  }
}

function applyAllPreviewedDraftingCitations(filter = null) {
  const editor = _draftingGetEditor();
  if (!editor) return;
  const previewTokens = _draftingGetPreviewTokens().filter((token) => !filter || filter(token));
  if (!previewTokens.length) {
    showToast(_tr('Aucun aperçu à appliquer.'));
    return;
  }
  const records = previewTokens
    .map((token) => _draftingGetCitationRecordFromToken(token))
    .filter(Boolean);
  if (!records.length) {
    showToast(_tr('Aucun aperçu exploitable à appliquer.'));
    return;
  }
  if (records.length > 1) _draftingSnapshotVersion('before_citations');
  records.forEach((record) => {
    _draftingCommitPreviewToken(record);
  });
  _draftingSyncFromEditor();
  editor.focus();
  showToast(`${records.length} ${_tr(records.length > 1 ? 'citations validées.' : 'citation validée.')}`, 2800, 'success');
}

function applyConfidentDraftingPreviews() {
  applyAllPreviewedDraftingCitations(_draftingIsConfidentPreview);
}

function previewAllDraftingCitations() {
  const suggestions = state.lastDraftingSuggestions || [];
  if (!suggestions.length) {
    showToast(_tr('Aucune suggestion à prévisualiser.'));
    return;
  }
  let inserted = 0;
  let atCursor = 0;
  suggestions.forEach((item) => {
    const record = _draftingBuildCitationRecord(item);
    if (!record.citation_id || _draftingSuggestionStatus(record.citation_id) !== 'idle') return;
    if (_draftingPreviewCitation(record)) {
      inserted += 1;
      if (_draftingLastPlacement === 'cursor') atCursor += 1;
    }
  });
  _draftingSyncFromEditor();
  if (!inserted) {
    showToast(_tr('Toutes les suggestions sont déjà placées.'), 2600, 'info');
    return;
  }
  const message = `${inserted} ${_tr(inserted > 1 ? 'aperçus placés. Validez-les avec ✓ ou en bloc.' : 'aperçu placé. Validez-le avec ✓.')}`;
  showToast(atCursor ? `${message} ${atCursor} ${_tr('au curseur (phrase introuvable).')}` : message, 3400, 'info');
}

function clearDraftingPreviews() {
  const count = _draftingGetPreviewTokens().length;
  if (!count) {
    _draftingUpdatePreviewCleanupButton();
    return;
  }
  _draftingRemoveAllPreviewTokens();
  _draftingSyncFromEditor();
  showToast(`${count} ${_tr(count > 1 ? 'aperçus retirés.' : 'aperçu retiré.')}`, 2600, 'success');
}

// Clears at once and offers "Annuler"; the empty draft is only saved when the
// toast expires, so an accidental click loses nothing.
function clearDraftingEditor() {
  const editor = _draftingGetEditor();
  if (!editor) return;
  if (!_draftingNodeHasContent(editor)) {
    showToast(_tr('Le texte est déjà vide.'));
    return;
  }
  const snapshot = editor.innerHTML;
  _draftingSnapshotVersion('before_clear');
  editor.innerHTML = '';
  state.drafting.selectionRange = null;
  state.drafting.previewCitationId = '';
  state.drafting.previewTokenId = '';
  state.drafting.pendingSuggestionId = '';
  _draftingSyncFromEditor(false);
  const restore = () => {
    if (_draftingNodeHasContent(editor)) {
      showToast(_tr('Du texte a été saisi entre-temps : restauration annulée.'), 3600, 'info');
      return;
    }
    editor.innerHTML = snapshot;
    _draftingSyncFromEditor();
  };
  if (typeof deleteWithUndo !== 'function') {
    _draftingSaveDocument();
    return;
  }
  deleteWithUndo('drafting-clear', {
    message: 'Texte effacé.',
    restore,
    commit: () => _draftingSaveDocument(),
    duration: 8000,
  });
}

function _draftingOnEditorInput() {
  _draftingStoreSelection();
  _draftingSyncFromEditor();
}

function _draftingOnEditorPaste(event) {
  const clipboardData = event.clipboardData;
  const text = String(clipboardData?.getData('text/plain') || '');
  const html = String(clipboardData?.getData('text/html') || '');
  if (!text && !html) {
    if (_clipboardHasImage(clipboardData)) {
      event.preventDefault();
      _notifyImagePasteBlocked();
    }
    return;
  }
  event.preventDefault();
  const editor = _draftingGetEditor();
  const selection = window.getSelection();
  if (!editor || !selection) return;
  let range = null;
  if (selection.rangeCount > 0) {
    const candidate = selection.getRangeAt(0);
    if (editor.contains(candidate.commonAncestorContainer)) range = candidate;
  }
  _draftingInsertTransferContent({ text, html }, range);
}

const DRAFTING_INTERNAL_DRAG_TYPE = 'application/x-bibliobrain-drafting';

function _draftingRangeFromPoint(editor, x, y) {
  let range = null;
  if (document.caretRangeFromPoint) {
    range = document.caretRangeFromPoint(x, y);
  } else if (document.caretPositionFromPoint) {
    const position = document.caretPositionFromPoint(x, y);
    if (position) {
      range = document.createRange();
      range.setStart(position.offsetNode, position.offset);
      range.collapse(true);
    }
  }
  if (!range || !editor.contains(range.startContainer)) return null;
  const container = range.startContainer;
  const element = container.nodeType === Node.ELEMENT_NODE ? container : container.parentElement;
  // Never drop text inside a citation token.
  const locked = element?.closest('.drafting-citation-token,[contenteditable="false"]');
  if (locked && locked !== editor && editor.contains(locked)) {
    range.setStartAfter(locked);
    range.collapse(true);
  }
  return range;
}

function _draftingOnEditorDrop(event) {
  // Moving text or citations inside the editor keeps the native behaviour.
  const dataTransfer = event.dataTransfer;
  if (Array.from(dataTransfer?.types || []).includes(DRAFTING_INTERNAL_DRAG_TYPE)) return;
  const text = String(dataTransfer?.getData('text/plain') || '');
  const html = String(dataTransfer?.getData('text/html') || '');
  event.preventDefault();
  if (!text && !html) {
    if (_clipboardHasImage(dataTransfer)) _notifyImagePasteBlocked();
    return;
  }
  const editor = _draftingGetEditor();
  if (!editor) return;
  editor.focus();
  _draftingInsertTransferContent({ text, html }, _draftingRangeFromPoint(editor, event.clientX, event.clientY));
}

function _draftingInsertPlainText(text, range) {
  const editor = _draftingGetEditor();
  const selection = window.getSelection();
  if (!editor || !selection) return;
  if (!range) {
    range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(false);
  }
  range.deleteContents();
  const textNode = document.createTextNode(text);
  range.insertNode(textNode);
  range.setStartAfter(textNode);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);
  state.drafting.selectionRange = range.cloneRange();
  _draftingSyncFromEditor();
}

async function initDraftingWorkspace() {
  _draftingBindCitationActions();
  const editor = _draftingGetEditor();
  const startNumberInput = document.getElementById('drafting-start-number');
  if (!editor || editor.dataset.initialized === '1') return;
  editor.dataset.initialized = '1';
  const savedStartNumber = parseInt(localStorage.getItem(_draftingGetDraftStartNumberKey()) || '', 10);
  _draftingSetStartNumber(Number.isFinite(savedStartNumber) ? savedStartNumber : (state.drafting.citationStartNumber || 1), false);

  // Bound before the draft request so a paste or drop during loading is already plain text.
  editor.addEventListener('paste', _draftingOnEditorPaste);
  editor.addEventListener('dragstart', (event) => {
    event.dataTransfer?.setData(DRAFTING_INTERNAL_DRAG_TYPE, '1');
  });
  editor.addEventListener('drop', _draftingOnEditorDrop);
  initDraftingInterop();
  await _draftingLoadDocument();
  editor.addEventListener('input', _draftingOnEditorInput);
  editor.addEventListener('keyup', _draftingUpdateCounters);
  editor.addEventListener('mouseup', () => {
    _draftingStoreSelection();
    _draftingUpdateCounters();
  });
  editor.addEventListener('focus', _draftingUpdateCounters);
  if (startNumberInput) {
    startNumberInput.addEventListener('input', (event) => {
      _draftingApplyStartNumberChange(event.target?.value);
    });
    startNumberInput.addEventListener('keyup', (event) => {
      _draftingApplyStartNumberChange(event.target?.value);
    });
    startNumberInput.addEventListener('change', (event) => {
      _draftingApplyStartNumberChange(event.target?.value);
    });
    startNumberInput.addEventListener('blur', (event) => {
      _draftingApplyStartNumberChange(event.target?.value);
    });
  }
  const includeCitedInput = document.getElementById('drafting-include-cited');
  if (includeCitedInput) {
    includeCitedInput.checked = localStorage.getItem(LS_PREFIX + 'drafting-include-cited') === '1';
    includeCitedInput.addEventListener('change', () => {
      localStorage.setItem(LS_PREFIX + 'drafting-include-cited', includeCitedInput.checked ? '1' : '0');
      _draftingUpdateScopeChip();
    });
  }
  const notebookQuery = document.getElementById('drafting-notebook-query');
  if (notebookQuery) {
    notebookQuery.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
      event.preventDefault();
      runDraftingNotebookSearch();
    });
  }
  document.addEventListener('click', (event) => {
    const { root, popover } = _draftingGetExportMenuNodes();
    if (!root || !popover || popover.hidden) return;
    const target = event?.target;
    if (target instanceof Node && root.contains(target)) return;
    closeDraftingExportMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeDraftingExportMenu();
  });
  document.addEventListener('selectionchange', _draftingStoreSelection);
  void _draftingLoadNotebookOptions();
  // The ⋯ menus of the editor and the assistant; history opens under them.
  document.addEventListener('click', (event) => {
    const show = event.target.closest?.('[data-drafting-show]');
    if (show) setDraftingView(show.dataset.draftingShow);
    document.querySelectorAll('.drafting-editor-more[open], .drafting-assistant-more[open]').forEach((menu) => {
      if (!event.composedPath().includes(menu) || event.target.closest?.('.drafting-menu-item')) menu.removeAttribute('open');
    });
  });
  document.querySelector('.drafting-view-tabs')?.addEventListener('keydown', (event) => {
    const keys = { ArrowLeft: -1, ArrowRight: 1, Home: -Infinity, End: Infinity };
    if (!(event.key in keys)) return;
    const tabs = [...event.currentTarget.querySelectorAll('[role="tab"]:not([hidden])')];
    const current = tabs.indexOf(document.activeElement);
    if (current < 0) return;
    event.preventDefault();
    const step = keys[event.key];
    const next = Number.isFinite(step)
      ? tabs[(current + step + tabs.length) % tabs.length]
      : tabs[step < 0 ? 0 : tabs.length - 1];
    setDraftingView(next.dataset.draftingView);
    next.focus();
  });
  setDraftingView(state.drafting.activeView || 'citations');
  _draftingSyncFromEditor(false);
  _draftingRenderCitationsPanel();
  _draftingUpdateScopeChip();
}

function _draftingSetReviewBusy(isBusy, activeId = '') {
  state.drafting.reviewBusy = Boolean(isBusy);
  state.drafting.reviewActiveAction = isBusy ? String(activeId || '') : '';
  ['drafting-proofread-btn', 'drafting-arguments-btn', 'drafting-notebook-search-btn'].forEach((id) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.dataset.originalText = btn.dataset.originalText || btn.textContent;
    const isActive = Boolean(isBusy) && id === activeId;
    btn.classList.toggle('is-stop', isActive);
    btn.disabled = Boolean(isBusy) && !isActive;
    if (isActive) {
      btn.textContent = 'Stop';
      btn.title = _tr('Stop generation');
    } else {
      btn.textContent = btn.dataset.originalText || btn.textContent;
      btn.title = btn.textContent;
      if (!isBusy && btn.dataset.originalText) {
        btn.textContent = btn.dataset.originalText;
      }
      if (!isBusy) {
        delete btn.dataset.originalText;
      }
    }
    if (!isBusy && btn.dataset.originalText) {
      delete btn.dataset.originalText;
    }
  });
  _draftingSyncNotebookSearchControls();
}

async function stopDraftingReviewAction() {
  const controller = state.drafting.reviewAbortController;
  const requestId = String(state.drafting.reviewAbortRequestId || '').trim();
  const model = String(
    state.drafting.reviewAbortModel || optionalSelectedModel('drafting') || '',
  ).trim();
  const abortPromise = requestId
    ? requestChatStreamAbort(requestId, model, 'drafting_stop')
    : null;
  if (abortPromise) {
    await Promise.race([
      abortPromise,
      new Promise((resolve) => window.setTimeout(() => resolve(null), 1200)),
    ]);
  }
  if (controller) {
    try { controller.abort(); } catch {}
  }
  state.drafting.reviewAbortController = null;
  state.drafting.reviewAbortRequestId = '';
  state.drafting.reviewAbortModel = '';
  state.drafting.reviewBusy = false;
  state.drafting.reviewActiveAction = '';
  _draftingSetReviewBusy(false);
  _renderDraftingReviewError(_tr('Génération interrompue.'));
  showToast(_tr('Génération interrompue.'), 2600, 'info');
}

function _draftingReviewRequestText() {
  const text = _draftingGetQueryText();
  if (!text) showToast(_tr('Add text or select a passage.'));
  return text;
}

function _renderDraftingProofreadProgress(event = {}) {
  const container = _draftingGetReviewPanel('proofread');
  if (!container) return;
  const percent = Math.max(0, Math.min(100, Number(event.percent) || 0));
  const message = String(event.msg || _tr('Proofreading the draft…'));
  container.innerHTML = `
    <div class="drafting-loading-note" style="display:block;width:100%">
      <div style="display:flex;align-items:center;gap:8px">
        <span class="spinner"></span>
        <span style="flex:1">${escapeHtml(message)}</span>
        <strong style="font-variant-numeric:tabular-nums">${Math.round(percent)}%</strong>
      </div>
      <div role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(percent)}"
           style="height:5px;margin-top:9px;border-radius:999px;background:var(--glass-2);overflow:hidden">
        <div style="height:100%;width:${percent}%;background:linear-gradient(90deg,var(--indigo),var(--cyan));transition:width .2s ease"></div>
      </div>
    </div>
  `;
}

function _draftingNotebookSearchMessage(question, passage) {
  const safeQuestion = String(question || '').trim().slice(0, 320);
  const safePassage = String(passage || '').trim().slice(0, 1200);
  const english = window.getLanguage?.() === 'en';
  if (safeQuestion && safePassage) {
    return english
      ? `Use only this notebook to help with the draft. Answer the question "${safeQuestion}" in relation to this passage:\n\n${safePassage}`
      : `Utilise uniquement ce carnet pour aider à la rédaction. Réponds à la question « ${safeQuestion} » en lien avec ce passage :\n\n${safePassage}`;
  }
  if (safeQuestion) return safeQuestion;
  return english
    ? `Find information in this notebook that can substantiate, clarify, or qualify the following passage. Give a concise, directly reusable answer with inline citations:\n\n${safePassage}`
    : `Recherche dans ce carnet les informations qui peuvent étayer, préciser ou nuancer le passage suivant. Donne une réponse concise, directement exploitable, avec des citations inline :\n\n${safePassage}`;
}

function _renderDraftingNotebookSearch(answer, sources = [], notebookTitle = '', streaming = false) {
  const container = _draftingGetReviewPanel('notebook');
  if (!container) return;
  const title = String(notebookTitle || '').trim();
  container.innerHTML = `
    <div class="drafting-review-text">
      <div class="drafting-review-text-head">
        <span>${escapeHtml(_ti('drafting.notebookResult'))}${title ? ` · ${escapeHtml(title)}` : ''}</span>
        ${streaming ? '' : `<button class="citation-btn" type="button" ${bindBbHandler('click', function() { copyDraftingNotebookAnswer(); })}>${escapeHtml(_ti('drafting.notebookCopy'))}</button>`}
      </div>
      <div id="drafting-notebook-answer"></div>
    </div>
  `;
  const answerRoot = document.getElementById('drafting-notebook-answer');
  renderRichAssistantOutput(answerRoot, answer || '', {
    streaming,
    appendCursor: streaming,
    normalizeCitations: false,
    sources,
    hideSourcesSection: false,
    emptyText: _tr('Recherche dans le carnet…'),
  });
}

async function copyDraftingNotebookAnswer() {
  const text = String(splitThinkingContent(state.drafting.lastNotebookAnswer || '').answer || '').trim();
  if (!text) {
    showToast(_tr('Aucune réponse à copier.'));
    return;
  }
  const ok = await copyTextWithFallback(text);
  showToast(
    ok ? _tr('Réponse copiée.') : _tr('Copie impossible.'),
    ok ? 2400 : 3600,
    ok ? 'success' : 'error',
  );
}

async function runDraftingNotebookSearch() {
  if (state.drafting.reviewBusy) {
    if (state.drafting.reviewActiveAction === 'drafting-notebook-search-btn') {
      stopDraftingReviewAction();
    }
    return;
  }
  _draftingStartReviewView('notebook');
  if (!state.drafting.notebookOptionsLoaded) {
    await _draftingLoadNotebookOptions();
  }
  const model = optionalSelectedModel('drafting');
  if (!model) {
    showToast(_tr('Sélectionnez un modèle pour interroger le carnet.'));
    return;
  }
  const select = document.getElementById('drafting-notebook-select');
  const notebookId = String(select?.value || '').trim();
  if (!notebookId) {
    showToast(_tr('Choisissez un carnet.'));
    select?.focus();
    return;
  }
  const question = String(document.getElementById('drafting-notebook-query')?.value || '').trim();
  const passage = _draftingGetQueryText();
  if (!question && !passage) {
    showToast(_tr('Ajoutez une question ou sélectionnez un passage.'));
    document.getElementById('drafting-notebook-query')?.focus();
    return;
  }

  const controller = new AbortController();
  const container = _draftingGetReviewPanel('notebook');
  state.drafting.reviewAbortController = controller;
  state.drafting.reviewAbortRequestId = '';
  state.drafting.reviewAbortModel = model;
  _draftingSetReviewBusy(true, 'drafting-notebook-search-btn');
  if (container) {
    container.innerHTML = '<div class="drafting-loading-note"><span class="spinner"></span><span>' + _tr('Recherche dans le carnet…') + '</span></div>';
    container.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  try {
    const notebookResponse = await fetch(`/api/notebooks/${encodeURIComponent(notebookId)}`, {
      cache: 'no-store',
      signal: controller.signal,
    });
    const notebook = await notebookResponse.json();
    if (!notebookResponse.ok) {
      throw new Error(notebook?.detail || `HTTP ${notebookResponse.status}`);
    }
    const sources = Array.isArray(notebook?.sources) ? notebook.sources : [];
    const notebookTitle = String(notebook?.title || select?.selectedOptions?.[0]?.textContent || '');
    const message = _draftingNotebookSearchMessage(question, passage);
    await syncOllamaBaseUrl({ silent: true });
    const response = await fetch(`/api/notebooks/${encodeURIComponent(notebookId)}/chat`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
      cache: 'no-store',
      signal: controller.signal,
      body: JSON.stringify({
        message,
        model,
        append_to_notes: false,
        persist_history: false,
        read_only: true,
        thinking: isThinkingEnabled(),
        force_thinking_small_models: isForceThinkingSmallModelsEnabled(),
      }),
    });
    let fullText = '';
    let lastRenderAt = 0;
    for await (const event of readSSE(response)) {
      if (event.type === 'stream_meta' && event.request_id) {
        state.drafting.reviewAbortRequestId = String(event.request_id);
      } else if (event.type === 'token') {
        fullText += String(event.text || '');
        const now = performance.now();
        if (now - lastRenderAt >= streamRenderIntervalMs()) {
          lastRenderAt = now;
          _renderDraftingNotebookSearch(fullText, sources, notebookTitle, true);
        }
      } else if (event.type === 'error') {
        throw new Error(event.msg || 'unknown error');
      }
    }
    if (!fullText.trim()) throw new Error(_tr('Le carnet n’a retourné aucune réponse.'));
    state.drafting.lastNotebookAnswer = fullText;
    state.drafting.lastNotebookSources = sources;
    state.drafting.lastNotebookTitle = notebookTitle;
    _renderDraftingNotebookSearch(fullText, sources, notebookTitle, false);
  } catch (error) {
    if (error?.name === 'AbortError') return;
    _renderDraftingReviewError(_tr('Recherche dans le carnet impossible : ') + (error?.message || _tr('erreur inconnue')));
  } finally {
    if (state.drafting.reviewAbortController === controller) {
      state.drafting.reviewAbortController = null;
      state.drafting.reviewAbortRequestId = '';
      state.drafting.reviewAbortModel = '';
      _draftingSetReviewBusy(false);
    }
  }
}

async function runDraftingProofread() {
  if (state.drafting.reviewBusy) {
    if (state.drafting.reviewActiveAction === 'drafting-proofread-btn') stopDraftingReviewAction();
    return;
  }
  _draftingStartReviewView('proofread');
  const model = optionalSelectedModel('drafting');
  if (!model) {
    showToast(_tr('Select a local model to proofread the text.'));
    return;
  }
  const text = _draftingReviewRequestText();
  if (!text) return;
  captureDraftingChangeSource();
  setDraftingFocus(false);
  const proofreadPlan = _draftingProofreadPlan(text);
  if (proofreadPlan.sectionCount > 1) {
    showToast(
      `Texte long : relecture automatique en environ ${proofreadPlan.sectionCount} sections.`,
      4200,
      'info',
    );
  }

  const container = _draftingGetReviewPanel('proofread');
  const controller = new AbortController();
  const requestId = (
    window.crypto?.randomUUID?.() ||
    `drafting-proofread-${Date.now()}-${Math.random().toString(16).slice(2)}`
  );
  state.drafting.reviewAbortController = controller;
  state.drafting.reviewAbortRequestId = requestId;
  state.drafting.reviewAbortModel = model;
  _draftingSetReviewBusy(true, 'drafting-proofread-btn');
  if (container) {
    _renderDraftingProofreadProgress({ percent: 1, msg: _tr('Proofreading the draft…') });
    container.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  try {
    const r = await fetch('/api/drafting-proofread', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
      cache: 'no-store',
      signal: controller.signal,
      body: JSON.stringify({ text, model, request_id: requestId }),
    });
    let receivedResult = false;
    for await (const event of readSSE(r)) {
      if (event.type === 'stream_meta' && event.request_id) {
        state.drafting.reviewAbortRequestId = String(event.request_id);
      } else if (event.type === 'progress') {
        _renderDraftingProofreadProgress(event);
      } else if (event.type === 'result') {
        receivedResult = true;
        _renderDraftingProofread(event.payload || {});
        _draftingSaveHistoryRun(_draftingNewHistoryId(), {
          kind: 'proofread',
          model,
          excerpt: text,
          result_count: draftingChanges.issues.length,
          payload: event.payload || {},
        });
      } else if (event.type === 'error') {
        throw new Error(event.msg || 'unknown error');
      }
    }
    if (!receivedResult) throw new Error('No proofreading result returned.');
  } catch (e) {
    if (e?.name === 'AbortError') return;
    _renderDraftingReviewError(_tr('La relecture a échoué : ') + (e?.message || _tr('erreur inconnue')));
  } finally {
    if (state.drafting.reviewAbortController === controller) {
      state.drafting.reviewAbortController = null;
      state.drafting.reviewAbortRequestId = '';
      state.drafting.reviewAbortModel = '';
      _draftingSetReviewBusy(false);
    }
  }
}

function _draftingArgumentRequestId(prefix = 'drafting-arguments') {
  return window.crypto?.randomUUID?.() || `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function _draftingSelectedArgumentKind() {
  const value = String(document.getElementById('drafting-argument-kind')?.value || 'any');
  return ['any', 'mechanism', 'counter_argument', 'limitation', 'context', 'implication'].includes(value)
    ? value : 'any';
}

function _draftingArgumentScopeLabel(scope = 'document') {
  return _tr(scope === 'selection'
    ? 'drafting.argument.scope.selection'
    : 'drafting.argument.scope.document');
}

function _draftingHttpErrorText(body, status) {
  const clean = String(body || '').replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  return clean ? clean.slice(0, 500) : `HTTP ${status}`;
}

async function _requestDraftingArgumentSuggestions({ text, model, argumentKind, maxSuggestions, requestId, signal }) {
  const response = await fetch('/api/drafting-argument-suggestions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    signal,
    body: JSON.stringify({
      text,
      model,
      argument_kind: argumentKind,
      max_suggestions: maxSuggestions,
      request_id: requestId,
    }),
  });
  const contentType = String(response.headers.get('content-type') || '').toLowerCase();
  if (!contentType.includes('application/json')) {
    throw new Error(_draftingHttpErrorText(await response.text(), response.status));
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new Error(_tr('Le serveur a renvoyé une réponse JSON illisible.'));
  }
  return { response, payload };
}

function _draftingStartArgumentProgress(container, scope) {
  if (!container) return () => {};
  const stages = [
    ['drafting.argument.progress.sources', 0],
    ['drafting.argument.progress.generation', 2400],
    ['drafting.argument.progress.verification', 6500],
  ];
  const renderStage = (key) => {
    container.innerHTML = `
      <div class="drafting-loading-note">
        <span class="spinner"></span>
        <span><strong>${escapeHtml(_draftingArgumentScopeLabel(scope))}</strong><br>${escapeHtml(_tr(key))}</span>
      </div>`;
  };
  renderStage(stages[0][0]);
  const timers = stages.slice(1).map(([key, delay]) => setTimeout(() => renderStage(key), delay));
  return () => timers.forEach((timer) => clearTimeout(timer));
}

async function runDraftingArgumentSuggestions() {
  if (state.drafting.reviewBusy) {
    if (state.drafting.reviewActiveAction === 'drafting-arguments-btn') stopDraftingReviewAction();
    return;
  }
  _draftingStartReviewView('arguments');
  const model = optionalSelectedModel('drafting');
  if (!model) {
    showToast(_tr('Sélectionnez un modèle pour rechercher des arguments sourcés.'));
    return;
  }
  const scope = _draftingGetQueryScope();
  const text = _draftingReviewRequestText();
  if (!text) return;
  const argumentKind = _draftingSelectedArgumentKind();

  const container = _draftingGetReviewPanel('arguments');
  const controller = new AbortController();
  const requestId = _draftingArgumentRequestId();
  state.drafting.lastArgumentRequest = { text, scope, argumentKind, model };
  _draftingResetArgumentInsertions();
  state.drafting.reviewAbortController = controller;
  state.drafting.reviewAbortRequestId = requestId;
  state.drafting.reviewAbortModel = model;
  _draftingSetReviewBusy(true, 'drafting-arguments-btn');
  const stopProgress = _draftingStartArgumentProgress(container, scope);
  container?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  try {
    const { response, payload } = await _requestDraftingArgumentSuggestions({
      text,
      model,
      argumentKind,
      maxSuggestions: 4,
      requestId,
      signal: controller.signal,
    });
    if (!response.ok || payload?.ok === false) {
      _renderDraftingArgumentSuggestions(payload || {
        ok: false,
        status: 'error',
        summary: `HTTP ${response.status}`,
      });
      return;
    }
    _renderDraftingArgumentSuggestions(payload);
    state.drafting.argumentHistoryId = _draftingNewHistoryId();
    _draftingRecordArgumentRun();
  } catch (e) {
    if (e?.name === 'AbortError') return;
    _renderDraftingReviewError(
      _tr('L’analyse des arguments a échoué : ') + (e?.message || _tr('erreur inconnue')),
    );
  } finally {
    stopProgress();
    if (state.drafting.reviewAbortController === controller) {
      state.drafting.reviewAbortController = null;
      state.drafting.reviewAbortRequestId = '';
      state.drafting.reviewAbortModel = '';
      _draftingSetReviewBusy(false);
    }
  }
}

function _draftingArgumentSourceIdentity(source = {}) {
  return [source.chunk_uid, source.citation_id, source.citation_key, source.rel_path, source.page, source.quote]
    .map((value) => String(value || '').trim()).join('|');
}

async function regenerateDraftingArgumentSuggestion(index) {
  if (state.drafting.reviewBusy) return;
  const item = _draftingGetArgumentSuggestion(index);
  const context = state.drafting.lastArgumentRequest;
  if (!item || !context?.text || !context?.model) return;

  const allowedKinds = ['mechanism', 'counter_argument', 'limitation', 'context', 'implication'];
  const argumentKind = allowedKinds.includes(String(item.kind))
    ? String(item.kind)
    : (allowedKinds.includes(context.argumentKind) ? context.argumentKind : 'any');
  const controller = new AbortController();
  const requestId = _draftingArgumentRequestId('drafting-argument-regenerate');
  const button = document.querySelector(`[data-argument-regenerate-index="${Number(index)}"]`);
  if (button) {
    button.disabled = true;
    button.dataset.originalText = button.textContent;
    button.textContent = _tr('drafting.argument.regenerating');
  }
  state.drafting.reviewAbortController = controller;
  state.drafting.reviewAbortRequestId = requestId;
  state.drafting.reviewAbortModel = context.model;
  _draftingSetReviewBusy(true, 'drafting-arguments-btn');
  try {
    const { response, payload } = await _requestDraftingArgumentSuggestions({
      text: context.text,
      model: context.model,
      argumentKind,
      maxSuggestions: 1,
      requestId,
      signal: controller.signal,
    });
    const replacement = Array.isArray(payload?.suggestions) ? payload.suggestions[0] : null;
    if (!response.ok || payload?.ok === false || !replacement) {
      throw new Error(payload?.summary || `HTTP ${response.status}`);
    }

    const existingSources = [...(state.drafting.lastArgumentSources || [])];
    const sourceByIdentity = new Map(existingSources.map((source) => [_draftingArgumentSourceIdentity(source), source]));
    const idMap = new Map();
    (Array.isArray(payload.sources) ? payload.sources : []).forEach((source, sourceIndex) => {
      const identity = _draftingArgumentSourceIdentity(source);
      const existing = sourceByIdentity.get(identity);
      if (existing) {
        idMap.set(String(source.source_id || ''), String(existing.source_id || ''));
        return;
      }
      const nextId = `R${Date.now()}-${sourceIndex + 1}`;
      const nextSource = { ...source, source_id: nextId };
      idMap.set(String(source.source_id || ''), nextId);
      existingSources.push(nextSource);
      sourceByIdentity.set(identity, nextSource);
    });
    const remapId = (id) => idMap.get(String(id || '')) || String(id || '');
    const nextSuggestion = {
      ...replacement,
      source_ids: (Array.isArray(replacement.source_ids) ? replacement.source_ids : []).map(remapId),
      evidence: (Array.isArray(replacement.evidence) ? replacement.evidence : [])
        .map((entry) => ({ ...entry, source_id: remapId(entry.source_id) })),
    };
    const nextSuggestions = [...state.drafting.lastArgumentSuggestions];
    nextSuggestions[Number(index)] = nextSuggestion;
    const previousPayload = state.drafting.lastArgumentPayload || {};
    _renderDraftingArgumentSuggestions({
      ...previousPayload,
      ok: true,
      status: 'success',
      suggestions: nextSuggestions,
      sources: existingSources,
      model: payload.model || previousPayload.model,
    });
    _draftingRecordArgumentRun();
  } catch (error) {
    if (error?.name !== 'AbortError') {
      showToast(_tr('L’analyse des arguments a échoué : ') + (error?.message || _tr('erreur inconnue')), 4200, 'error');
    }
  } finally {
    if (state.drafting.reviewAbortController === controller) {
      state.drafting.reviewAbortController = null;
      state.drafting.reviewAbortRequestId = '';
      state.drafting.reviewAbortModel = '';
      _draftingSetReviewBusy(false);
    }
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = button.dataset.originalText || _tr('drafting.argument.regenerate');
      delete button.dataset.originalText;
    }
  }
}

function _renderDraftingReviewError(message) {
  const container = _draftingGetReviewPanel();
  if (!container) return;
  container.innerHTML = `<div class="drafting-assistant-note">${escapeHtml(message)}</div>`;
}

async function copyDraftingProofreadText() {
  const text = String(state.drafting.lastProofreadText || '').trim();
  if (!text) {
    showToast(_tr('No corrected text to copy.'));
    return;
  }
  const ok = await copyTextWithFallback(text);
  showToast(ok ? _tr('Corrected text copied.') : _tr('Copy failed.'), ok ? 2500 : 3600, ok ? 'success' : 'error');
}

function _draftingArgumentKindLabel(kind = '') {
  const labels = {
    fact: 'drafting.argument.kind.fact',
    context: 'drafting.argument.kind.context',
    mechanism: 'drafting.argument.kind.mechanism',
    comparison: 'drafting.argument.kind.comparison',
    limitation: 'drafting.argument.kind.limitation',
    counter_argument: 'drafting.argument.kind.counterArgument',
    implication: 'drafting.argument.kind.implication',
    definition: 'drafting.argument.kind.definition',
    claim_link: 'drafting.argument.kind.claimLink',
  };
  return _tr(labels[String(kind || '').trim()] || labels.claim_link);
}

function _draftingArgumentStatusLabel(status = '') {
  const labels = {
    supported: 'drafting.argument.status.supported',
    weak_evidence: 'drafting.argument.status.needsChecking',
    too_general: 'drafting.argument.status.tooGeneral',
    unsupported: 'drafting.argument.status.unsupported',
  };
  return _tr(labels[String(status || '').trim()] || labels.weak_evidence);
}

function _draftingGetArgumentSuggestion(index) {
  const numeric = Number(index);
  if (!Number.isInteger(numeric) || numeric < 0) return null;
  return state.drafting.lastArgumentSuggestions[numeric] || null;
}

function _draftingGetArgumentVariantText(item, variant = 'sentence') {
  if (!item) return '';
  const insertText = item.insert_text && typeof item.insert_text === 'object' ? item.insert_text : {};
  const key = ['sentence', 'paragraph', 'note'].includes(String(variant)) ? String(variant) : 'sentence';
  return String(insertText[key] || item.suggestion || '').trim();
}

function _draftingArgumentSources(item) {
  if (!item || item.validation_status !== 'supported') return [];
  const ids = new Set(Array.isArray(item.source_ids) ? item.source_ids : []);
  const available = state.drafting.lastArgumentSources || [];
  if (![...ids].every(id => available.some(source => source.source_id === id && (source.citation_id || source.citation_key)))) return [];
  const seen = new Set();
  return available.filter(source => {
    const id = source.citation_id || source.citation_key;
    if (!ids.has(source.source_id) || !id || seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

async function copyDraftingArgumentText(index, variant = 'sentence') {
  const item = _draftingGetArgumentSuggestion(index);
  const text = _draftingGetArgumentVariantText(item, variant);
  const sources = _draftingArgumentSources(item);
  if (!text || !sources.length) {
    showToast(_tr('No argument text to copy.'));
    return;
  }
  const references = sources.map(source => source.apa || [source.author, source.year, source.title, source.doi].filter(Boolean).join('. '));
  const ok = await copyTextWithFallback(`${text}\n\nSources :\n${references.join('\n')}`);
  showToast(ok ? _tr('Argument text copied.') : _tr('Copy failed.'), ok ? 2400 : 3600, ok ? 'success' : 'error');
}

// ---- Argument insertion: a preview in the text, then validation ----
// The preview is a non-editable span that is never saved, copied or exported;
// validating replaces it with the text and its citation tokens.

function _draftingFindArgumentPreview() {
  return _draftingGetEditor()?.querySelector('.drafting-argument-preview') || null;
}

function _draftingHtmlWithoutArgumentPreview(editor = _draftingGetEditor()) {
  if (!editor) return '';
  if (!editor.querySelector('.drafting-argument-preview')) return editor.innerHTML;
  const clone = editor.cloneNode(true);
  clone.querySelectorAll('.drafting-argument-preview').forEach((node) => node.remove());
  return clone.innerHTML;
}

function _draftingArgumentCitationRecords(item, sources) {
  return sources.map((source) => {
    const evidence = (item.evidence || []).find(entry => entry.source_id === source.source_id);
    return _draftingBuildCitationRecord({
      ...source,
      evidence_quote: evidence?.quote || item.evidence_quote || '',
      evidence_chunk_uid: source.chunk_uid || '',
      support_status: 'supported',
      support_explanation: item.suggestion,
      source_pipeline: 'rag',
    });
  });
}

function _draftingCreateArgumentPreview(index, variant, text, records) {
  const preview = document.createElement('span');
  preview.className = 'drafting-argument-preview';
  preview.contentEditable = 'false';
  preview.dataset.preview = '1';
  preview.dataset.argumentIndex = String(index);
  preview.dataset.argumentVariant = variant;
  preview.dataset.argumentText = text;
  preview.title = _tr('drafting.argument.previewHint');
  const body = document.createElement('span');
  body.className = 'drafting-argument-preview-text';
  body.textContent = text;
  preview.appendChild(body);
  records.forEach((record) => {
    const cite = document.createElement('span');
    cite.className = 'drafting-argument-preview-cite';
    cite.textContent = `[${_draftingPreviewShortLabel(record)}]`;
    preview.appendChild(cite);
  });
  const controls = document.createElement('span');
  controls.className = 'drafting-preview-controls';
  [
    ['accept', '✓', _tr('drafting.argument.previewAccept')],
    ['reject', '✕', _tr('drafting.argument.previewReject')],
  ].forEach(([action, symbol, label]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `drafting-preview-control is-${action}`;
    button.dataset.argumentPreviewAction = action;
    button.textContent = symbol;
    button.title = label;
    button.setAttribute('aria-label', label);
    controls.appendChild(button);
  });
  preview.appendChild(controls);
  return preview;
}

function removeDraftingArgumentPreview() {
  const preview = _draftingFindArgumentPreview();
  if (!preview) return;
  _draftingRemoveTokenCleanly(preview);
  _draftingSyncFromEditor();
}

function insertDraftingArgumentText(index, variant = 'sentence') {
  const item = _draftingGetArgumentSuggestion(index);
  const text = _draftingGetArgumentVariantText(item, variant);
  const sources = _draftingArgumentSources(item);
  if (!text || !sources.length) {
    showToast(_tr('No argument text to insert.'));
    return;
  }
  // One argument preview at a time: a new one takes the place of the old one,
  // unless the writer has since put the caret elsewhere in the text.
  const editor = _draftingGetEditor();
  const previous = _draftingFindArgumentPreview();
  const selection = window.getSelection();
  const selectedRange = selection?.rangeCount ? selection.getRangeAt(0) : null;
  let anchor = _draftingRangePointsIntoEditor(selectedRange) ? selectedRange : state.drafting.selectionRange;
  if (previous && !_draftingRangePointsIntoEditor(selectedRange)) {
    anchor = document.createRange();
    anchor.setStartBefore(previous);
    anchor.collapse(true);
  }
  // An argument is an addition: preserve the passage selected for analysis.
  const insertionRange = _draftingRangePointsIntoEditor(anchor) ? anchor.cloneRange() : null;
  insertionRange?.collapse(false);
  if (previous) _draftingRemoveTokenCleanly(previous);
  const preview = _draftingCreateArgumentPreview(index, variant, text, _draftingArgumentCitationRecords(item, sources));
  if (!_draftingInsertNodeAtSelection(preview, insertionRange)) {
    showToast(_tr('Insertion failed.'));
    return;
  }
  _draftingSyncFromEditor();
  if (editor) editor.focus();
  preview.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  _draftingFlash(preview);
}

function confirmDraftingArgumentPreview() {
  const preview = _draftingFindArgumentPreview();
  const editor = _draftingGetEditor();
  if (!preview || !editor) return;
  const index = Number(preview.dataset.argumentIndex);
  const variant = String(preview.dataset.argumentVariant || 'sentence');
  const item = _draftingGetArgumentSuggestion(index);
  const text = _draftingGetArgumentVariantText(item, variant);
  const sources = _draftingArgumentSources(item);
  // The idea may have been regenerated since the preview was placed.
  if (!text || !sources.length || text !== preview.dataset.argumentText) {
    removeDraftingArgumentPreview();
    showToast(_tr('drafting.argument.previewStale'), 3600, 'info');
    return;
  }
  const before = _draftingHtmlWithoutArgumentPreview(editor);
  // One version per analysis, taken before its first insertion.
  if (!state.drafting.argumentVersionSaved) {
    state.drafting.argumentVersionSaved = true;
    _draftingSnapshotVersion('before_arguments');
  }
  const fragment = document.createDocumentFragment();
  fragment.appendChild(document.createTextNode(text + ' '));
  _draftingArgumentCitationRecords(item, sources)
    .forEach((record) => fragment.appendChild(_draftingCreateCitationToken(record, 1)));
  const lastNode = fragment.lastChild;
  preview.replaceWith(fragment);
  const caret = document.createRange();
  caret.setStartAfter(lastNode);
  caret.collapse(true);
  state.drafting.selectionRange = caret;
  item.inserted_variant = variant;
  _draftingSyncFromEditor();
  state.drafting.argumentUndo = { before, after: editor.innerHTML, index };
  _draftingRecordArgumentRun();
  _draftingRefreshArgumentCards();
  showToast(_tr('Text inserted in the draft.'), 2400, 'success');
}

function undoDraftingArgumentInsertion() {
  const undo = state.drafting.argumentUndo;
  const editor = _draftingGetEditor();
  state.drafting.argumentUndo = null;
  if (!undo || !editor || editor.innerHTML !== undo.after) {
    _draftingRefreshArgumentCards();
    showToast(_tr('drafting.argument.undoStale'), 4200, 'info');
    return;
  }
  editor.innerHTML = undo.before;
  state.drafting.selectionRange = null;
  const item = _draftingGetArgumentSuggestion(undo.index);
  if (item) delete item.inserted_variant;
  _draftingSyncFromEditor();
  _draftingRecordArgumentRun();
  _draftingRefreshArgumentCards();
  showToast(_tr('drafting.argument.undone'), 2400, 'success');
}

// Each analysis takes its own version and has its own undo.
function _draftingResetArgumentInsertions() {
  state.drafting.argumentUndo = null;
  state.drafting.argumentVersionSaved = false;
}

function _draftingRevealArgumentCard(index) {
  setDraftingView('arguments');
  const card = _draftingGetReviewPanel('arguments')?.querySelector(`[data-argument-card="${Number(index)}"]`);
  if (!card) return;
  card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  _draftingFlash(card);
}

// Card state follows the editor: a preview can also be deleted with the keyboard.
function _draftingRefreshArgumentCards() {
  const container = _draftingGetReviewPanel('arguments');
  if (!container) return;
  const preview = _draftingFindArgumentPreview();
  const previewIndex = preview ? Number(preview.dataset.argumentIndex) : -1;
  container.querySelectorAll('[data-argument-card]').forEach((card) => {
    const index = Number(card.dataset.argumentCard);
    const pending = index === previewIndex;
    card.classList.toggle('is-pending', pending);
    const row = card.querySelector('[data-argument-preview-row]');
    if (row) row.hidden = !pending;
    const inserted = card.querySelector('[data-argument-inserted]');
    const variant = _draftingGetArgumentSuggestion(index)?.inserted_variant;
    if (inserted) {
      inserted.hidden = !variant;
      inserted.textContent = variant ? _tr(`drafting.argument.inserted.${variant}`) : '';
    }
  });
  const undo = container.querySelector('[data-argument-undo]');
  const editor = _draftingGetEditor();
  if (undo) undo.hidden = !(state.drafting.argumentUndo && editor?.innerHTML === state.drafting.argumentUndo.after);
}

function _renderDraftingProofread(payload) {
  const container = _draftingGetReviewPanel('proofread');
  if (!container) return;
  const corrected = String(payload?.corrected_text || '').trim();
  state.drafting.lastProofreadText = corrected;
  const issues = Array.isArray(payload?.issues) ? payload.issues : [];
  draftingChanges.issues = issues.filter(issue => issue.original && typeof issue.suggestion === 'string' && issue.original !== issue.suggestion)
    .map(issue => ({ ...issue }));
  const sectionCount = Math.max(1, Number(payload?.section_count) || 1);
  const sectionNote = sectionCount > 1 ? ` · ${sectionCount} sections réassemblées` : '';
  container.innerHTML = `
    <div class="drafting-assistant-note">${escapeHtml(payload?.summary || _tr('Relecture terminée.'))}${escapeHtml(sectionNote)}</div>
    <div id="drafting-change-list"></div>
    ${!draftingChanges.issues.length ? `<p>${escapeHtml(workspaceText('noChanges'))}</p>` : ''}
    ${corrected ? `
      <details class="drafting-review-text">
        <summary class="disclosure">${escapeHtml(workspaceText('correctedVersion'))}</summary>
        <div class="drafting-review-text-head">
          <span>${escapeHtml(workspaceText('correctedVersionHint'))}</span>
          <button class="citation-btn" type="button" ${bindBbHandler('click', function() { copyDraftingProofreadText(); })}>${escapeHtml(workspaceText('copy'))}</button>
        </div>
        <pre>${escapeHtml(corrected)}</pre>
      </details>
    ` : ''}
  `;
  renderDraftingChangeCards();
}

const _DRAFTING_CLAIM_STATUS_LABELS = {
  supported: 'drafting.argument.claim.supported',
  needs_source: 'drafting.argument.claim.needsSource',
  too_vague: 'drafting.argument.claim.tooVague',
  contradicted: 'drafting.argument.claim.contradicted',
};

const _DRAFTING_ARGUMENT_RESULT_LABELS = {
  success: 'drafting.argument.result.success',
  no_sources: 'drafting.argument.result.noSources',
  generation_invalid: 'drafting.argument.result.generationInvalid',
  verification_rejected: 'drafting.argument.result.verificationRejected',
  verification_failed: 'drafting.argument.result.verificationFailed',
  empty: 'drafting.argument.result.empty',
  model_incompatible: 'drafting.argument.result.modelIncompatible',
  invalid_request: 'drafting.argument.result.invalidRequest',
  cancelled: 'drafting.argument.result.cancelled',
  error: 'drafting.argument.result.error',
};

function _renderDraftingArgumentSuggestions(payload) {
  const container = _draftingGetReviewPanel('arguments');
  if (!container) return;
  const sources = Array.isArray(payload?.sources) ? payload.sources : [];
  const sourceById = new Map(sources.map((source) => [String(source.source_id || ''), source]));
  const suggestions = Array.isArray(payload?.suggestions) ? payload.suggestions : [];
  const reviewCandidates = Array.isArray(payload?.review_candidates) ? payload.review_candidates : [];
  const claims = Array.isArray(payload?.claims) ? payload.claims : [];
  state.drafting.lastArgumentSuggestions = suggestions;
  state.drafting.lastArgumentSources = sources;
  state.drafting.lastArgumentPayload = payload;
  // A preview whose idea changed (new analysis, regeneration) is stale.
  const preview = _draftingFindArgumentPreview();
  if (preview && _draftingGetArgumentVariantText(suggestions[Number(preview.dataset.argumentIndex)], preview.dataset.argumentVariant) !== preview.dataset.argumentText) {
    removeDraftingArgumentPreview();
  }
  _draftingRefreshViewTabs();

  const claimsHtml = claims.length ? `
    <div class="drafting-claims-map">
      <div class="drafting-review-text-head">
        <span>${escapeHtml(_tr('drafting.argument.claimsTitle'))}</span>
      </div>
      ${claims.map((claim) => {
        const claimSources = (Array.isArray(claim.source_ids) ? claim.source_ids : [])
          .map((id) => sourceById.get(String(id)))
          .filter(Boolean)
          .map((source) => [source.source_id, source.author, source.year].filter(Boolean).join(' · '))
          .join(', ');
        const status = String(claim.support_status || 'needs_source');
        return `
          <div class="drafting-claim-row is-${escapeAttr(status)}">
            <span class="drafting-item-source-badge">${escapeHtml(_tr(_DRAFTING_CLAIM_STATUS_LABELS[status] || _DRAFTING_CLAIM_STATUS_LABELS.needs_source))}</span>
            <span class="drafting-claim-text">${escapeHtml(claim.claim || '')}</span>
            ${claimSources ? `<span class="drafting-item-confidence">${escapeHtml(claimSources)}</span>` : ''}
            ${claim.note ? `<span class="drafting-claim-note">${escapeHtml(claim.note)}</span>` : ''}
          </div>
        `;
      }).join('')}
    </div>
  ` : '';

  const suggestionsHtml = suggestions.map((item, index) => {
    const linkedSources = (Array.isArray(item.source_ids) ? item.source_ids : [])
      .map((id) => sourceById.get(String(id)))
      .filter(Boolean);
    const sourceBadges = linkedSources.map((source) => {
      const bits = [source.source_id, source.author, source.year].filter(Boolean).join(' · ');
      return `<span class="drafting-item-confidence">${escapeHtml(bits || source.source_id || _tr('drafting.argument.source'))}</span>`;
    }).join('');
    const sourceBlocks = linkedSources.map((source) => `
      <blockquote class="drafting-item-evidence">
        <strong>${escapeHtml(source.source_id || '')}</strong>
        ${escapeHtml(source.title || _tr('drafting.argument.untitledSource'))}
        ${source.page ? `, p. ${escapeHtml(source.page)}` : ''}
        <br>${escapeHtml(source.quote || '')}
      </blockquote>
    `).join('');
    const evidenceLabel = item.evidence_status === 'verified'
      ? _tr('drafting.argument.evidenceVerified')
      : '';
    const kindLabel = _draftingArgumentKindLabel(item.kind);
    const statusLabel = _draftingArgumentStatusLabel(item.validation_status);
    const variants = item.insert_text && typeof item.insert_text === 'object' ? item.insert_text : {};
    const paragraph = String(variants.paragraph || '').trim();
    const note = String(variants.note || '').trim();
    return `
      <div class="drafting-item" data-argument-card="${index}">
        <div class="drafting-item-title">${escapeHtml(item.title || _tr('drafting.argument.possibleArgument'))}</div>
        <div class="drafting-item-support">
          <span class="drafting-item-source-badge is-inserted" data-argument-inserted hidden></span>
          <span class="drafting-item-source-badge">${escapeHtml(kindLabel)}</span>
          <span class="drafting-item-confidence">${escapeHtml(statusLabel)}</span>
          ${evidenceLabel ? `<span class="drafting-item-source-badge">${escapeHtml(evidenceLabel)}</span>` : ''}
          ${sourceBadges}
        </div>
        ${item.draft_gap ? `<div class="drafting-item-sentence"><span class="drafting-item-sentence-tag">${escapeHtml(_tr('drafting.argument.gap'))}</span><span class="drafting-item-sentence-text">${escapeHtml(item.draft_gap)}</span></div>` : ''}
        <div class="drafting-item-rationale">${escapeHtml(item.suggestion || '')}</div>
        ${item.insertion_hint ? `<div class="drafting-item-sentence"><span class="drafting-item-sentence-tag">${escapeHtml(_tr('drafting.argument.insertion'))}</span><span class="drafting-item-sentence-text">${escapeHtml(item.insertion_hint)}</span></div>` : ''}
        ${paragraph ? `
          <div class="drafting-review-text drafting-argument-variant">
            <div class="drafting-review-text-head">
              <span>${escapeHtml(_tr('drafting.argument.paragraph'))}</span>
              <button class="citation-btn" type="button" ${bindBbHandler('click', function() { copyDraftingArgumentText(index, 'paragraph'); })}>${escapeHtml(_tr('drafting.argument.copy'))}</button>
            </div>
            <pre>${escapeHtml(paragraph)}</pre>
          </div>
        ` : ''}
        ${note ? `<div class="drafting-item-sentence"><span class="drafting-item-sentence-tag">${escapeHtml(_tr('drafting.argument.note'))}</span><span class="drafting-item-sentence-text">${escapeHtml(note)}</span></div>` : ''}
        <div class="drafting-item-actions drafting-argument-preview-row" data-argument-preview-row hidden>
          <span class="drafting-argument-preview-label">${escapeHtml(_tr('drafting.argument.previewPlaced'))}</span>
          <button class="citation-btn drafting-preview-btn" type="button" ${bindBbHandler('click', function() { confirmDraftingArgumentPreview(); })}>${escapeHtml(_tr('drafting.argument.previewAccept'))}</button>
          <button class="citation-btn" type="button" ${bindBbHandler('click', function() { removeDraftingArgumentPreview(); })}>${escapeHtml(_tr('drafting.argument.previewReject'))}</button>
        </div>
        <div class="drafting-item-actions">
          <button class="citation-btn drafting-preview-btn" type="button" ${bindBbHandler('click', function() { insertDraftingArgumentText(index, 'sentence'); })}>${escapeHtml(_tr('drafting.argument.insertSentence'))}</button>
          <button class="citation-btn" type="button" ${bindBbHandler('click', function() { insertDraftingArgumentText(index, 'paragraph'); })}>${escapeHtml(_tr('drafting.argument.insertParagraph'))}</button>
          <button class="citation-btn" type="button" title="${escapeAttr(_tr('drafting.argument.insertNoteHelp'))}" ${bindBbHandler('click', function() { insertDraftingArgumentText(index, 'note'); })}>${escapeHtml(_tr('drafting.argument.insertNote'))}</button>
          <button class="citation-btn" type="button" ${bindBbHandler('click', function() { copyDraftingArgumentText(index, 'sentence'); })}>${escapeHtml(_tr('drafting.argument.copySentence'))}</button>
          <button class="citation-btn" type="button" data-argument-regenerate-index="${index}" ${bindBbHandler('click', function() { regenerateDraftingArgumentSuggestion(index); })}>${escapeHtml(_tr('drafting.argument.regenerate'))}</button>
        </div>
        ${item.evidence_quote ? `<blockquote class="drafting-item-evidence">« ${escapeHtml(item.evidence_quote)} »</blockquote>` : ''}
        ${sourceBlocks}
      </div>
    `;
  }).join('');

  const reviewCandidatesHtml = reviewCandidates.length ? `
    <div class="drafting-claims-map drafting-review-candidates">
      <div class="drafting-review-text-head">
        <span>${escapeHtml(_tr('drafting.argument.reviewTitle'))}</span>
      </div>
      <div class="drafting-assistant-note">${escapeHtml(_tr('drafting.argument.reviewHelp'))}</div>
      ${reviewCandidates.map((item) => {
        const linkedSources = (Array.isArray(item.source_ids) ? item.source_ids : [])
          .map((id) => sourceById.get(String(id)))
          .filter(Boolean);
        const sourceBadges = linkedSources.map((source) => {
          const bits = [source.source_id, source.author, source.year].filter(Boolean).join(' · ');
          return `<span class="drafting-item-confidence">${escapeHtml(bits || source.source_id || _tr('drafting.argument.source'))}</span>`;
        }).join('');
        const evidenceBlocks = (Array.isArray(item.evidence) ? item.evidence : []).map((entry) => `
          <blockquote class="drafting-item-evidence">
            <strong>${escapeHtml(entry.source_id || '')}</strong><br>
            ${escapeHtml(entry.quote || '')}
          </blockquote>
        `).join('');
        return `
          <div class="drafting-item drafting-item-review-only">
            <div class="drafting-item-title">${escapeHtml(item.title || _tr('drafting.argument.idea'))}</div>
            <div class="drafting-item-support">
              <span class="drafting-item-source-badge">${escapeHtml(_draftingArgumentKindLabel(item.kind))}</span>
              <span class="drafting-item-confidence">${escapeHtml(_draftingArgumentStatusLabel(item.validation_status))}</span>
              ${sourceBadges}
            </div>
            <div class="drafting-item-rationale">${escapeHtml(item.suggestion || '')}</div>
            <div class="drafting-item-sentence">
              <span class="drafting-item-sentence-tag">${escapeHtml(_tr('drafting.argument.whyCheck'))}</span>
              <span class="drafting-item-sentence-text">${escapeHtml(item.rejection_reason || _tr('drafting.argument.notConfirmed'))}</span>
            </div>
            ${evidenceBlocks}
          </div>
        `;
      }).join('')}
    </div>
  ` : '';

  const resultStatus = String(payload?.status || (payload?.ok === false ? 'error' : 'success'));
  const resultLabel = _DRAFTING_ARGUMENT_RESULT_LABELS[resultStatus] || _DRAFTING_ARGUMENT_RESULT_LABELS.error;
  const modelInfo = payload?.model && typeof payload.model === 'object' ? payload.model : {};
  let modelNotice = '';
  if (modelInfo.fallback_used) {
    const requested = String(modelInfo.requested || '').trim();
    const used = String(modelInfo.used || '').trim();
    const verifier = String(modelInfo.verification_model || used).trim();
    const detail = verifier && verifier !== used
      ? `${used || requested} · vérification : ${verifier}`
      : `${used || verifier}`;
    modelNotice = `<div class="drafting-assistant-note">${escapeHtml(
      _tr(`Fallback automatique utilisé${requested ? ` à la place de ${requested}` : ''} : ${detail}.`),
    )}</div>`;
  }

  container.innerHTML = `
    <div class="drafting-assistant-note">
      <span class="drafting-item-source-badge">${escapeHtml(_tr(resultLabel))}</span>
      ${escapeHtml(payload?.summary || _tr('Analyse de l’argumentation terminée.'))}
    </div>
    <div class="drafting-assistant-note">${escapeHtml(_draftingArgumentScopeLabel(state.drafting.lastArgumentRequest?.scope))}</div>
    ${modelNotice}
    <div class="drafting-item-actions" data-argument-undo hidden>
      <button class="citation-btn" type="button" ${bindBbHandler('click', function() { undoDraftingArgumentInsertion(); })}>${escapeHtml(_tr('drafting.argument.undo'))}</button>
    </div>
    ${claimsHtml}
    ${suggestionsHtml}
    ${reviewCandidatesHtml}
    ${!suggestionsHtml && !reviewCandidatesHtml ? `<div style="color:var(--muted)">${escapeHtml(_tr('Aucune idée argumentative suffisamment étayée.'))}</div>` : ''}
  `;
  _draftingRefreshArgumentCards();
}

// ---- Sources outside the library ----
// A sentence can be searched with the external article search (same backend
// as the Search tab). Its results become ordinary suggestions tagged
// 'external': they are placed and validated like the others, but they were
// judged on title and abstract only, so they carry no verification score and
// are never "confident" previews.

const DRAFTING_EXTERNAL_MAX_RESULTS = 5;

function _draftingResetExternalSearches() {
  state.drafting.externalSearchController?.abort();
  state.drafting.externalSearchController = null;
  state.drafting.externalSearches.clear();
}

function _draftingRenderExternalSearch(key, ref, analysisRunning) {
  const search = state.drafting.externalSearches.get(key);
  const searching = search?.status === 'running';
  const otherSearching = Boolean(state.drafting.externalSearchController) && !searching;
  const label = searching ? _tr('Arrêter la recherche') : _tr('Rechercher plus de citations');
  const title = searching ? label : _tr('Chercher des articles hors de votre bibliothèque (sources externes)');
  const status = search?.message
    ? `<span class="drafting-external-status is-${escapeAttr(search.status)}" role="status">${searching ? '<span class="spinner"></span>' : ''}${escapeHtml(search.message)}</span>`
    : '';
  return `
    <div class="drafting-external-row">
      <button type="button" class="drafting-external-btn" data-drafting-action="search-external" data-sentence-ref="${ref}"${analysisRunning || otherSearching ? ' disabled' : ''} title="${escapeAttr(title)}">${escapeHtml(label)}</button>
      ${status}
    </div>`;
}

function _draftingAsciiSlug(value) {
  return String(value || '').normalize('NFD').replace(/\p{M}/gu, '').toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function _draftingLastName(name) {
  const value = String(name || '').trim();
  return value.includes(',') ? value.split(',')[0].trim() : value.split(/\s+/).pop() || '';
}

function _draftingSafeUrl(value) {
  const url = String(value || '').trim();
  return /^https?:\/\//i.test(url) ? url : '';
}

// Same paper: same DOI, or same title when there is no DOI.
function _draftingExternalIdentity(record) {
  const doi = String(record?.doi || '').trim().toLowerCase();
  return doi ? `doi:${doi}` : `title:${_draftingAsciiSlug(record?.title)}`;
}

// BibTeX-style key (dupont2021magnetic), suffixed when another paper uses it.
function _draftingExternalCitationKey(item, authors) {
  const titleWord = String(item.title || '').split(/\s+/).map(_draftingAsciiSlug).find((word) => word.length > 3) || '';
  const base = `${_draftingAsciiSlug(_draftingLastName(authors[0])) || 'anon'}${_draftingAsciiSlug(item.year) || 'nd'}${titleWord}`;
  const identity = _draftingExternalIdentity(item);
  const records = [...state.drafting.citationIndex.values(), ...(state.lastDraftingSuggestions || [])];
  for (let n = 0; n < 26; n += 1) {
    const key = n ? `${base}${String.fromCharCode(97 + n)}` : base;
    const clash = records.find((record) => _draftingNormalizeCitationId(record.citation_id) === key);
    if (!clash || _draftingExternalIdentity(clash) === identity) return key;
  }
  return `${base}${Date.now().toString(36)}`;
}

function _draftingApaAuthors(authors) {
  const names = authors.slice(0, 20).map((name) => {
    const last = _draftingLastName(name);
    const firsts = name.includes(',') ? name.split(',').slice(1).join(' ') : name.split(/\s+/).slice(0, -1).join(' ');
    const initials = firsts.split(/[\s-]+/).filter(Boolean).map((part) => `${part.charAt(0).toUpperCase()}.`).join(' ');
    return initials ? `${last}, ${initials}` : last;
  });
  if (names.length <= 1) return names[0] || '';
  return `${names.slice(0, -1).join(', ')}, & ${names[names.length - 1]}`;
}

// Turns an article-discovery result into a drafting suggestion for `sentence`.
function _draftingExternalSuggestion(item, sentence) {
  const title = String(item?.title || '').replace(/\s+/g, ' ').trim();
  if (!title) return null;
  const listed = Array.isArray(item.authors) && item.authors.length ? item.authors : String(item.authors_line || '').split(',');
  const authors = listed.map((name) => String(name || '').replace(/\s+/g, ' ').trim()).filter((name) => name && name !== 'Auteur inconnu');
  const year = String(item.year || '').trim();
  const doi = String(item.doi || '').trim();
  const venue = String(item.venue || '').trim();
  const key = _draftingExternalCitationKey(item, authors);
  const lastNames = authors.map(_draftingLastName);
  const authorLabel = lastNames.length > 2
    ? `${lastNames[0]} et al.`
    : lastNames.join(' & ');
  const apaAuthors = _draftingApaAuthors(authors);
  const sentenceEnd = (text) => (/[.!?]$/.test(text) ? text : `${text}.`);
  const apa = `${apaAuthors ? `${apaAuthors} ` : ''}(${year || 'n.d.'}). ${sentenceEnd(title)}${venue ? ` ${sentenceEnd(venue)}` : ''}${doi ? ` https://doi.org/${doi}` : ''}`;
  const url = doi
    ? `https://doi.org/${doi}`
    : _draftingSafeUrl(item.publisher_url || item.external_url || item.semantic_scholar_url);
  const field = (name, value) => (value ? `  ${name} = {${String(value).replace(/[{}]/g, '')}},` : '');
  const bibtex = [
    `@article{${key},`,
    field('title', title),
    field('author', authors.join(' and ')),
    field('journal', venue),
    field('year', year),
    field('doi', doi),
    doi ? '' : field('url', url),
    '}',
  ].filter(Boolean).join('\n');
  const rationale = String(item.llm_rationale || '').trim();
  const hasAbstract = item.abstract_available !== false;
  return {
    citation_id: key,
    title,
    author: authors[0] || '',
    authors_display: authors.join(' and '),
    year,
    journal: venue,
    doi,
    label: `${authorLabel || _tr('Auteur inconnu')} (${year || 'n.d.'})`,
    apa,
    bibtex,
    rationale,
    support_explanation: rationale,
    summary: hasAbstract ? String(item.short_summary || item.abstract_preview || '').trim() : '',
    citation_count: item.citation_count,
    influential_citation_count: item.influential_citation_count,
    confidence: null,
    source_pipeline: 'external',
    external_url: url,
    open_access_pdf: _draftingSafeUrl(item.open_access_pdf),
    sentence_text: sentence,
    sentence_index: null,
    query_scope: 'selection',
  };
}

async function searchDraftingExternalCitations(sentenceText) {
  const sentence = String(sentenceText || '').trim();
  const key = _draftingSentenceKey(sentence);
  if (!key) return;
  if (state.drafting.externalSearches.get(key)?.status === 'running') {
    state.drafting.externalSearchController?.abort();
    return;
  }
  if (state.drafting.externalSearchController || state.drafting.searching) return;
  if (!window.isExternalDiscoveryEnabled?.()) {
    showToast(_tr('Cette recherche interroge des sources externes (OpenAlex, Semantic Scholar…) : autorisez d’abord les API externes dans les réglages.'), 5600, 'info');
    window.openExternalApiSettings?.();
    return;
  }
  const model = optionalSelectedModel('drafting');
  if (!model) {
    showToast(_tr('Sélectionnez un modèle valide pour la génération locale.'));
    return;
  }

  const controller = new AbortController();
  state.drafting.externalSearchController = controller;
  const search = { status: 'running', message: _tr('Recherche d’articles hors bibliothèque…') };
  state.drafting.externalSearches.set(key, search);
  _draftingRenderCitationsPanel();

  const hint = (document.getElementById('drafting-user-hint')?.value || '').trim().slice(0, 240);
  let payload = null;
  try {
    const r = await fetch('/api/article-discovery/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        mode: 'search',
        input_text: hint ? `${sentence}\n\n${hint}` : sentence,
        model,
        max_results: DRAFTING_EXTERNAL_MAX_RESULTS,
        include_unpaywall: true,
        unpaywall_email: (document.getElementById('unpaywall-email')?.value || '').trim() || null,
        query_planner: Boolean(state.appSettings?.discovery_query_planner),
        privacy_ack: true,
      }),
      signal: controller.signal,
    });
    if (!r.ok || !String(r.headers.get('content-type') || '').includes('text/event-stream')) {
      payload = await r.json().catch(() => null);
    } else {
      for await (const event of readSSE(r)) {
        if (controller.signal.aborted) break;
        if (event?.type === 'progress' && event.msg) {
          search.message = _tr(String(event.msg));
          _draftingRenderCitationsPanel();
        } else if (event?.type === 'done') {
          payload = event.payload || null;
        }
      }
    }
    if (controller.signal.aborted) {
      Object.assign(search, { status: 'stopped', message: _tr('Recherche interrompue.') });
    } else if (!payload?.ok) {
      const message = String(payload?.msg || '');
      Object.assign(search, {
        status: 'error',
        message: message && message !== 'article_discovery_failed' ? message : _tr('La recherche externe a échoué. Réessayez.'),
      });
    } else {
      const known = new Set([...state.drafting.citationIndex.values(), ...(state.lastDraftingSuggestions || [])]
        .map(_draftingExternalIdentity));
      const found = (Array.isArray(payload.results) ? payload.results : [])
        .slice(0, DRAFTING_EXTERNAL_MAX_RESULTS)
        .map((item) => _draftingExternalSuggestion(item, sentence))
        .filter(Boolean);
      const added = found.filter((item) => !known.has(_draftingExternalIdentity(item)));
      if (added.length) {
        Object.assign(search, {
          status: 'done',
          message: `${added.length} ${_tr(added.length > 1 ? 'articles trouvés hors bibliothèque, à vérifier avant de citer.' : 'article trouvé hors bibliothèque, à vérifier avant de citer.')}`,
        });
        _renderDraftingSuggestions({ suggestions: added }, true);
        const run = state.drafting.citationRun || {};
        _draftingRecordCitationRun(run.model || model, run.excerpt || sentence);
      } else {
        Object.assign(search, {
          status: 'empty',
          message: _tr(found.length ? 'Les articles trouvés sont déjà proposés ou cités.' : 'Aucun article externe pertinent trouvé.'),
        });
      }
    }
  } catch (e) {
    Object.assign(search, controller.signal.aborted || e?.name === 'AbortError'
      ? { status: 'stopped', message: _tr('Recherche interrompue.') }
      : { status: 'error', message: _tr('Connexion au serveur Bibliobrain perdue pendant la recherche.') });
  } finally {
    if (state.drafting.externalSearchController === controller) state.drafting.externalSearchController = null;
    _draftingRenderCitationsPanel();
  }
}

function retryDraftingSentences(sentences = []) {
  const texts = sentences.map((text) => String(text || '').trim()).filter(Boolean);
  if (!texts.length || state.drafting.searching) return;
  // Sentences are sent on their own: their indexes no longer match the editor.
  runDraftingAssistant(true, { paragraph: texts.join(' '), scope: 'selection' });
}

async function runDraftingAssistant(appendMore = false, options = {}) {
  if (state.drafting.searching) {
    stopDraftingAssistantSearch();
    return;
  }
  setDraftingView('citations');

  const model = optionalSelectedModel('drafting');
  if (!appendMore) {
    _draftingRemoveAllPreviewTokens();
    _draftingSyncFromEditor(false);
  }
  const scope = options.scope || _draftingGetQueryScope();
  // Without markers the server analyses every sentence, cited or not.
  const paragraph = options.paragraph || _draftingGetQueryText({ markCitations: !_draftingIncludesCitedSentences() });
  if (!paragraph.replaceAll(DRAFTING_CITED_MARKER, '').trim()) {
    showToast(_tr('Ajoutez un paragraphe ou sélectionnez un passage.'));
    return;
  }

  const controller = new AbortController();
  state.drafting.searchAbortController = controller;
  state.drafting.searching = true;
  // Retries and "more" append to the analysis already in the panel.
  if (!appendMore || !state.drafting.citationHistoryId) state.drafting.citationHistoryId = _draftingNewHistoryId();
  const historyExcerpt = paragraph.replaceAll(DRAFTING_CITED_MARKER, '').trim();
  if (!appendMore) {
    _draftingResetExternalSearches();
    state.lastDraftingSuggestions = [];
    state.drafting.sentenceLog = [];
    state.drafting.openSuggestionDetails.clear();
    state.drafting.origins.clear();
  }
  const run = {
    running: true,
    scope,
    processed: 0,
    total: 0,
    accepted: 0,
    currentSentence: '',
    startedAt: Date.now(),
    sentenceStartedAt: Date.now(),
    // Kept so later external searches update the same history entry.
    model,
    excerpt: historyExcerpt,
  };
  state.drafting.citationRun = run;
  _draftingSetButtonBusy(true);
  _draftingRenderCitationsPanel();

  const userHint = (document.getElementById('drafting-user-hint')?.value || '').trim().slice(0, 240);
  const context = scope === 'selection' ? _draftingPassageContext(historyExcerpt) : { before: '', after: '' };
  const tagScope = (items) => (Array.isArray(items) ? items : []).map((item) => ({ ...item, query_scope: scope }));

  try {
    const r = await fetch('/api/drafting-suggestions/stream', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        paragraph,
        model,
        max_suggestions: 40,
        exclude_citation_keys: _draftingSuggestionExclusions(),
        user_hint: userHint || null,
        context_before: context.before,
        context_after: context.after,
      }),
      signal: controller.signal,
    });
    if (!r.ok || !r.body) {
      throw new Error(`HTTP ${r.status}`);
    }
    for await (const event of readSSE(r)) {
      if (controller.signal.aborted) break;
      const type = String(event?.type || '');
      if (Number.isFinite(Number(event?.total_sentences))) run.total = Number(event.total_sentences);
      if (Number.isFinite(Number(event?.processed_sentences))) run.processed = Number(event.processed_sentences);
      if (type === 'start') {
        _draftingRenderCitationsPanel();
      } else if (type === 'progress') {
        run.currentSentence = String(event.sentence_text || '');
        run.sentenceStartedAt = Date.now();
        _draftingUpsertSentenceLog(event.sentence_text, { status: 'running', count: 0 });
        _draftingRenderCitationsPanel();
      } else if (type === 'sentence_result') {
        const suggestions = tagScope(event.suggestions);
        run.accepted += suggestions.length;
        _draftingUpsertSentenceLog(event.sentence_text, {
          status: _draftingSentenceStatusFromEvent(event),
          count: suggestions.length,
          reason: String(event.validation_status || ''),
        });
        _renderDraftingSuggestions({ suggestions }, true);
      } else if (type === 'error') {
        throw new Error(event.msg || 'Erreur inconnue');
      } else if (type === 'done') {
        run.finalStatus = String(event.validation_status || '');
        run.serverMessage = String(event.assistant_text || '');
        if (Array.isArray(event.suggestions) && event.suggestions.length) {
          _renderDraftingSuggestions({ suggestions: tagScope(event.suggestions) }, true);
        }
      }
    }
    if (!controller.signal.aborted) _draftingFinishRun();
    _draftingSyncFromEditor(false);
  } catch(e) {
    if (controller.signal.aborted || e?.name === 'AbortError') return;
    _draftingFinishRun({ error: e?.message || _tr('inconnue') });
    showToast(_tr('Erreur de suggestion: ') + (e?.message || _tr('inconnue')), 4200, 'error');
  }
  finally {
    if (state.drafting.searchAbortController === controller) {
      state.drafting.searchAbortController = null;
      state.drafting.searching = false;
      _draftingSetButtonBusy(false);
      _draftingRenderCitationsPanel();
    }
    _draftingRecordCitationRun(model, historyExcerpt);
  }
}

// ---- History: past analyses and draft versions ----

const DRAFTING_HISTORY_KINDS = {
  citations: { label: 'drafting.viewCitations', count: 'drafting.history.count.citations' },
  proofread: { label: 'drafting.viewProofread', count: 'drafting.history.count.proofread' },
  arguments: { label: 'drafting.viewArguments', count: 'drafting.history.count.arguments' },
};

const DRAFTING_VERSION_REASONS = {
  auto: 'drafting.history.reason.auto',
  before_proofread: 'drafting.history.reason.beforeProofread',
  before_citations: 'drafting.history.reason.beforeCitations',
  before_arguments: 'drafting.history.reason.beforeArguments',
  before_clear: 'drafting.history.reason.beforeClear',
  before_restore: 'drafting.history.reason.beforeRestore',
};

function _draftingNewHistoryId() {
  return window.crypto?.randomUUID?.() || `run-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function _draftingHistoryDate(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const locale = typeof window.getLanguage === 'function' && window.getLanguage() === 'en' ? 'en-GB' : 'fr-FR';
  return date.toLocaleString(locale, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

// History is a convenience: a failed write never interrupts the analysis.
function _draftingSaveHistoryRun(runId, entry) {
  if (!runId) return;
  fetch(`/api/drafting-history/runs/${encodeURIComponent(runId)}`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(entry),
  })
    .then((response) => { if (response.ok && state.drafting.activeView === 'history') loadDraftingHistory(); })
    .catch((error) => console.warn('Drafting history unavailable.', error));
}

function _draftingRecordCitationRun(model, excerpt) {
  const suggestions = state.lastDraftingSuggestions || [];
  const sentenceLog = state.drafting.sentenceLog.filter((entry) => entry.status !== 'running');
  if (!suggestions.length && !sentenceLog.length) return;
  const run = state.drafting.citationRun || {};
  _draftingSaveHistoryRun(state.drafting.citationHistoryId, {
    kind: 'citations',
    model: model || '',
    excerpt,
    result_count: suggestions.length,
    payload: {
      suggestions,
      sentence_log: sentenceLog,
      run: {
        scope: run.scope,
        total: run.total,
        processed: run.processed,
        accepted: run.accepted,
        finalStatus: run.finalStatus,
        serverMessage: run.serverMessage,
        error: run.error,
        stopped: run.stopped,
        model: model || '',
        excerpt,
      },
    },
  });
}

function _draftingRecordArgumentRun() {
  const payload = state.drafting.lastArgumentPayload || {};
  const request = state.drafting.lastArgumentRequest || {};
  const suggestions = Array.isArray(payload.suggestions) ? payload.suggestions : [];
  const candidates = Array.isArray(payload.review_candidates) ? payload.review_candidates : [];
  if (!suggestions.length && !candidates.length) return;
  _draftingSaveHistoryRun(state.drafting.argumentHistoryId, {
    kind: 'arguments',
    model: request.model || payload.model || '',
    excerpt: request.text || '',
    result_count: suggestions.length,
    payload: { request, result: payload },
  });
}

function _draftingCurrentHtml() {
  const editor = _draftingGetEditor();
  if (!editor) return '';
  const clone = editor.cloneNode(true);
  clone.querySelectorAll('.drafting-citation-token.is-preview, .drafting-argument-preview').forEach((node) => node.remove());
  return clone.innerHTML;
}

// Snapshot taken before a bulk change; the server skips empty or unchanged drafts.
function _draftingSnapshotVersion(reason, html = _draftingCurrentHtml()) {
  return fetch('/api/drafting-history/versions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ html, citation_start_number: _draftingGetStartNumber(), reason }),
  })
    .then((response) => { if (response.ok && state.drafting.activeView === 'history') loadDraftingHistory(); })
    .catch((error) => console.warn('Draft version not saved.', error));
}

function _draftingHistoryBusy() {
  if (!state.drafting.searching && !state.drafting.reviewBusy) return false;
  showToast(_tr('drafting.history.busy'), 3200, 'info');
  return true;
}

function _draftingRenderHistory(runs = [], versions = []) {
  const container = document.getElementById('drafting-history-result');
  if (!container) return;
  const deleteLabel = escapeAttr(_tr('drafting.history.delete'));
  const runsHtml = runs.length ? runs.map((run) => {
    const kind = DRAFTING_HISTORY_KINDS[run.kind] || DRAFTING_HISTORY_KINDS.citations;
    const meta = [_draftingHistoryDate(run.updated_at), _ti(kind.count, { count: run.result_count }), run.model]
      .filter(Boolean).join(' · ');
    return `
      <li class="drafting-history-row" data-history-run="${escapeAttr(run.id)}">
        <span class="drafting-history-kind is-${escapeAttr(run.kind)}">${escapeHtml(_tr(kind.label))}</span>
        <div class="drafting-history-body">
          ${run.excerpt ? `<span class="drafting-history-excerpt" data-i18n-skip>« ${escapeHtml(run.excerpt)} »</span>` : ''}
          <span class="drafting-history-meta" data-i18n-skip>${escapeHtml(meta)}</span>
        </div>
        <div class="drafting-history-actions">
          <button type="button" class="drafting-mini-btn is-primary" data-drafting-action="history-open">${escapeHtml(_tr('drafting.history.reopen'))}</button>
          <button type="button" class="drafting-mini-btn drafting-history-delete" data-drafting-action="history-delete" title="${deleteLabel}" aria-label="${deleteLabel}">×</button>
        </div>
      </li>`;
  }).join('') : `<li class="drafting-history-empty">${escapeHtml(_tr('drafting.history.noRuns'))}</li>`;
  const versionsHtml = versions.length ? versions.map((version) => {
    const meta = [
      _draftingHistoryDate(version.created_at),
      _tr(DRAFTING_VERSION_REASONS[version.reason] || DRAFTING_VERSION_REASONS.auto),
      _ti('drafting.history.words', { count: version.word_count }),
    ].join(' · ');
    return `
      <li class="drafting-history-row" data-history-version="${escapeAttr(String(version.id))}">
        <div class="drafting-history-body">
          ${version.preview ? `<span class="drafting-history-excerpt" data-i18n-skip>${escapeHtml(version.preview)}</span>` : ''}
          <span class="drafting-history-meta" data-i18n-skip>${escapeHtml(meta)}</span>
        </div>
        <div class="drafting-history-actions">
          <button type="button" class="drafting-mini-btn" data-drafting-action="version-restore">${escapeHtml(_tr('drafting.history.restore'))}</button>
          <button type="button" class="drafting-mini-btn drafting-history-delete" data-drafting-action="version-delete" title="${deleteLabel}" aria-label="${deleteLabel}">×</button>
        </div>
      </li>`;
  }).join('') : `<li class="drafting-history-empty">${escapeHtml(_tr('drafting.history.noVersions'))}</li>`;
  container.innerHTML = `
    <section class="drafting-history-section">
      <div class="drafting-history-head"><strong>${escapeHtml(_tr('drafting.history.runsTitle'))}</strong></div>
      <ul class="drafting-history-list">${runsHtml}</ul>
    </section>
    <section class="drafting-history-section">
      <div class="drafting-history-head">
        <strong>${escapeHtml(_tr('drafting.history.versionsTitle'))}</strong>
        <span class="drafting-history-hint">${escapeHtml(_tr('drafting.history.versionsHint'))}</span>
      </div>
      <ul class="drafting-history-list">${versionsHtml}</ul>
    </section>
    ${runs.length || versions.length ? `<button type="button" class="drafting-mini-btn is-danger-soft drafting-history-clear" data-drafting-action="history-clear">${escapeHtml(_tr('drafting.history.clear'))}</button>` : ''}`;
}

async function loadDraftingHistory() {
  const container = document.getElementById('drafting-history-result');
  if (!container) return;
  try {
    const response = await fetch('/api/drafting-history', { cache: 'no-store' });
    const payload = await response.json();
    if (!response.ok || !payload?.ok) throw new Error(`HTTP ${response.status}`);
    _draftingRenderHistory(payload.runs || [], payload.versions || []);
  } catch (error) {
    container.innerHTML = `<div class="drafting-assistant-note">${escapeHtml(_tr('drafting.history.loadFailed'))}</div>`;
  }
}

async function _draftingFetchHistoryItem(url, field) {
  const response = await fetch(url, { cache: 'no-store' });
  const payload = await response.json().catch(() => null);
  if (!response.ok || !payload?.[field]) throw new Error(payload?.message || `HTTP ${response.status}`);
  return payload[field];
}

async function openDraftingHistoryRun(runId) {
  if (_draftingHistoryBusy()) return;
  let run;
  try {
    run = await _draftingFetchHistoryItem(`/api/drafting-history/runs/${encodeURIComponent(runId)}`, 'run');
  } catch (error) {
    showToast(_tr('drafting.history.loadFailed'), 3600, 'error');
    return;
  }
  const data = run.payload || {};
  if (run.kind === 'citations') {
    _draftingResetExternalSearches();
    _draftingRemoveAllPreviewTokens();
    _draftingSyncFromEditor(false);
    state.lastDraftingSuggestions = [];
    state.drafting.openSuggestionDetails.clear();
    state.drafting.origins.clear();
    state.drafting.sentenceLog = Array.isArray(data.sentence_log) ? data.sentence_log : [];
    state.drafting.citationRun = { ...(data.run || {}), running: false };
    // Retrying failed sentences adds to this analysis instead of a new one.
    state.drafting.citationHistoryId = run.id;
    setDraftingView('citations');
    _renderDraftingSuggestions({ suggestions: Array.isArray(data.suggestions) ? data.suggestions : [] });
  } else if (run.kind === 'proofread') {
    // Reopened corrections are matched against the whole draft.
    const selection = state.drafting.selectionRange;
    state.drafting.selectionRange = null;
    captureDraftingChangeSource();
    state.drafting.selectionRange = selection;
    _draftingStartReviewView('proofread');
    _renderDraftingProofread(data);
  } else if (run.kind === 'arguments') {
    state.drafting.lastArgumentRequest = data.request || null;
    state.drafting.argumentHistoryId = run.id;
    _draftingResetArgumentInsertions();
    _draftingStartReviewView('arguments');
    _renderDraftingArgumentSuggestions(data.result || {});
  }
  showToast(_tr('drafting.history.reopened'), 2400, 'success');
}

async function restoreDraftingVersion(versionId) {
  if (_draftingHistoryBusy()) return;
  const editor = _draftingGetEditor();
  if (!editor) return;
  let version;
  try {
    version = await _draftingFetchHistoryItem(`/api/drafting-history/versions/${encodeURIComponent(versionId)}`, 'version');
  } catch (error) {
    showToast(_tr('drafting.history.loadFailed'), 3600, 'error');
    return;
  }
  // The text being replaced stays restorable.
  await _draftingSnapshotVersion('before_restore');
  editor.innerHTML = String(version.html || '');
  editor.querySelectorAll('.drafting-citation-token.is-preview, .drafting-argument-preview').forEach((node) => node.remove());
  state.drafting.selectionRange = null;
  state.drafting.previewCitationId = '';
  state.drafting.previewTokenId = '';
  state.drafting.pendingSuggestionId = '';
  _draftingSetStartNumber(version.citation_start_number, true);
  _draftingSyncFromEditor();
  _draftingRenderCitationsPanel();
  showToast(_tr('drafting.history.versionRestored'), 4200, 'success');
  loadDraftingHistory();
}

async function _draftingDeleteHistoryItem(url) {
  try {
    await fetch(url, { method: 'DELETE' });
  } catch (error) {
    showToast(_tr('drafting.history.loadFailed'), 3600, 'error');
  }
  loadDraftingHistory();
}

function _draftingOnHistoryAction(action, button) {
  const row = button.closest('.drafting-history-row');
  const runId = row?.dataset.historyRun || '';
  const versionId = row?.dataset.historyVersion || '';
  if (action === 'history-open' && runId) {
    openDraftingHistoryRun(runId);
  } else if (action === 'history-delete' && runId) {
    _draftingDeleteHistoryItem(`/api/drafting-history/runs/${encodeURIComponent(runId)}`);
  } else if (action === 'version-restore' && versionId) {
    restoreDraftingVersion(versionId);
  } else if (action === 'version-delete' && versionId) {
    _draftingDeleteHistoryItem(`/api/drafting-history/versions/${encodeURIComponent(versionId)}`);
  } else if (action === 'history-clear' && window.confirm(_tr('drafting.history.clearConfirm'))) {
    _draftingDeleteHistoryItem('/api/drafting-history');
  }
}
