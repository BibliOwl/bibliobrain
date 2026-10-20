// Real DOM regression tests. Uses installed Playwright; all APIs are mocked.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const vm = require('node:vm');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');

// English text of every i18n key, read from the ENTRIES table of i18n.js.
function englishLabels() {
  const source = fs.readFileSync(path.join(root, 'static/js/i18n.js'), 'utf8');
  const start = source.indexOf('{', source.indexOf('const ENTRIES = {'));
  let depth = 0;
  let end = start;
  for (let i = start; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '"' || ch === "'" || ch === '`') {
      for (i += 1; i < source.length && source[i] !== ch; i += 1) {
        if (source[i] === '\\') i += 1;
      }
      continue;
    }
    if (ch === '{') depth += 1;
    if (ch === '}' && --depth === 0) {
      end = i + 1;
      break;
    }
  }
  const entries = vm.runInNewContext(`(${source.slice(start, end)})`);
  return Object.fromEntries(Object.entries(entries).map(([key, entry]) => [key, entry.en]));
}

async function main() {
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8')
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
  const server = http.createServer((req, res) => {
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(html);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', route => route.fulfill({ json: { ok: true, exists: false } }));
    // In-memory stand-in for the drafting history endpoints.
    const historyRuns = new Map();
    const historyVersions = [];
    await page.route('**/api/drafting-history**', route => {
      const request = route.request();
      const { pathname } = new URL(request.url());
      const method = request.method();
      const tail = decodeURIComponent(pathname.split('/').pop());
      if (pathname.startsWith('/api/drafting-history/runs/')) {
        if (method === 'PUT') historyRuns.set(tail, { ...historyRuns.get(tail), ...request.postDataJSON(), id: tail, updated_at: new Date().toISOString() });
        const run = historyRuns.get(tail);
        return route.fulfill(run ? { json: { ok: true, run } } : { status: 404, json: { ok: false } });
      }
      if (pathname === '/api/drafting-history/versions' && method === 'POST') {
        historyVersions.unshift({ id: historyVersions.length + 1, ...request.postDataJSON(), created_at: new Date().toISOString(), word_count: 2, preview: '' });
        return route.fulfill({ json: { ok: true } });
      }
      if (pathname.startsWith('/api/drafting-history/versions/')) {
        const version = historyVersions.find(item => String(item.id) === tail);
        return route.fulfill(version ? { json: { ok: true, version } } : { status: 404, json: { ok: false } });
      }
      return route.fulfill({ json: { ok: true, runs: [...historyRuns.values()].map(({ payload, ...run }) => run), versions: historyVersions } });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate((labels) => {
      window.LS_PREFIX = 'citation-regression-';
      // app.js's _tr goes through BibliobrainI18n: i18n keys get their
      // English text, any other text is left unchanged.
      window.BibliobrainI18n = { translateRaw: text => labels[text] || text };
      window.escapeHtml = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.escapeAttr = window.escapeHtml;
      window.showToast = () => {};
      window.copyTextWithFallback = async text => { window.copiedText = text; return true; };
    }, englishLabels());
    for (const file of ['static/js/app.js', 'static/js/event-bindings.js', 'static/js/app/drafting.js']) {
      await page.addScriptTag({ path: path.join(root, file) });
    }
    const results = await page.evaluate(async () => {
      // Deliberately skip initDraftingWorkspace: network startup may not reach it.
      const editor = _draftingGetEditor();
      editor.innerHTML = '<p>Une expérience RMN autour de 50 T en champ continu constituerait<br>donc une capacité expérimentale inédite.</p>';
      const item = {
        citation_id: 'source-1', title: 'Test source', label: 'Author (2024)',
        author: 'Author', year: '2024', apa: 'Author (2024). Test source.',
        sentence_text: 'Une expérience RMN autour de 50 T en champ continu constituerait donc une capacité expérimentale inédite.',
        sentence_index: 0, confidence: 0.9,
      };
      const render = () => _renderDraftingSuggestions({ suggestions: [item] });
      const click = action => document.querySelector(`[data-drafting-action="${action}"]`).click();
      render();
      click('preview');
      const previewWorksBeforeStartup = _draftingGetPreviewTokens().length === 1;
      const previewLabel = _draftingGetPreviewTokens()[0]?.querySelector('.drafting-citation-link')?.textContent || '';
      const correctPlacement = previewLabel === '[Author 2024 ?]'
        && editor.textContent.indexOf('inédite') < editor.textContent.indexOf(previewLabel);
      render(); render(); // Listener must not fire twice after repeated rendering.
      click('preview');
      const applyWorks = !_draftingGetPreviewTokens().length && state.drafting.citationOrder.length === 1;
      click('copy');
      const copyWorks = window.copiedText === item.apa;
      click('remove');
      const removeKeepsAcceptedCitation = state.lastDraftingSuggestions.length === 0 && state.drafting.citationOrder.length === 1;
      editor.innerHTML = '<p>' + item.sentence_text + '</p>';
      _draftingSyncFromEditor(false);
      render(); click('preview'); click('remove');
      const removeCleansPreview = !_draftingGetPreviewTokens().length && state.lastDraftingSuggestions.length === 0;
      render(); click('preview'); applyAllPreviewedDraftingCitations();
      const bulkStillWorks = !_draftingGetPreviewTokens().length && state.drafting.citationOrder.length === 1;

      // A proposed source can be read at the evidence page (0-based in the
      // index) or shown in the library; without a path there is no button.
      window.openArticleViewer = (...args) => { window.openedArticle = args; };
      window.goToSource = sourcePath => { window.librarySource = sourcePath; };
      state.lastDraftingSuggestions = [];
      _renderDraftingSuggestions({ suggestions: [{ ...item, citation_id: 'readable', rel_path: 'Coll/paper.pdf', page: 4 }] });
      click('read');
      const readOpensEvidencePage = window.openedArticle?.[0] === 'Coll/paper.pdf'
        && window.openedArticle[1] === 'Test source' && window.openedArticle[3] === 5;
      click('library');
      const libraryShowsSource = window.librarySource === 'Coll/paper.pdf';
      state.lastDraftingSuggestions = [];
      _renderDraftingSuggestions({ suggestions: [item] });
      const noPathNoReadButton = !document.querySelector('[data-drafting-action="read"], [data-drafting-action="library"]');
      editor.innerHTML = '<p>' + item.sentence_text + '</p>';
      _draftingSyncFromEditor(false);
      state.lastDraftingSuggestions = [];
      _renderDraftingSuggestions({ suggestions: [{ ...item, citation_id: 'placed', rel_path: 'notes.docx', page: 2 }] });
      click('preview'); applyAllPreviewedDraftingCitations();
      window.openedArticle = null;
      _draftingOpenCitationPopover(editor.querySelector('.drafting-citation-token'));
      document.querySelector('#drafting-citation-popover [data-popover-action="read"]').click();
      const popoverReadsPlacedCitation = window.openedArticle?.[0] === 'notes.docx' && window.openedArticle[3] === null;

      // Indexes from a selection count from the selection, not the editor:
      // the sentence text must win.
      editor.innerHTML = '<p>First sentence of the draft here. Second sentence of the draft here. Mechanical stresses limit high field magnets.</p>';
      _draftingSyncFromEditor(false);
      state.lastDraftingSuggestions = [];
      const target = { ...item, citation_id: 'source-2', sentence_text: 'Mechanical stresses limit high field magnets.', sentence_index: 0, query_scope: 'selection' };
      _renderDraftingSuggestions({ suggestions: [target] });
      const beforePreview = editor.innerHTML;
      click('preview');
      const text = editor.textContent;
      const selectionIndexIgnored = text.indexOf('[Author 2024 ?]') > text.indexOf('magnets')
        && text.indexOf('[Author 2024 ?]') > text.indexOf('Second sentence');
      editor.querySelector('[data-preview-action="reject"]').click();
      const rejectRestoresText = editor.innerHTML === beforePreview && state.lastDraftingSuggestions.length === 1;
      click('preview');
      editor.querySelector('[data-preview-action="accept"]').click();
      const acceptControlCommits = !_draftingGetPreviewTokens().length && state.drafting.citationOrder.includes('source-2');

      // Several sources for one sentence are grouped under it.
      state.lastDraftingSuggestions = [];
      const sibling = { ...target, citation_id: 'source-3', label: 'Other (2023)' };
      _renderDraftingSuggestions({ suggestions: [target, sibling] });
      const groups = document.querySelectorAll('#drafting-result .drafting-group');
      const groupedBySentence = groups.length === 1 && groups[0].querySelectorAll('.drafting-item').length === 2
        && Boolean(groups[0].querySelector('.drafting-item.is-applied'));

      // Rejecting then placing again leaves no stray space, and several
      // sources for one sentence keep their order.
      editor.innerHTML = '<p>Intro sentence here for testing. Mechanical stresses limit high field magnets. Quench studies follow.</p>';
      _draftingSyncFromEditor(false);
      state.lastDraftingSuggestions = [];
      _renderDraftingSuggestions({ suggestions: [{ ...target, citation_id: 'b1', author: 'Bortot', year: '2021' }, { ...target, citation_id: 'z1', author: 'Zhou', year: '2024' }] });
      const cycleStart = editor.innerHTML;
      previewDraftingCitation(0); previewDraftingCitation(1);
      const keepsOrder = editor.textContent.indexOf('[Bortot 2021 ?]') < editor.textContent.indexOf('[Zhou 2024 ?]');
      rejectDraftingPreview('z1'); rejectDraftingPreview('b1');
      const cycleRestores = editor.innerHTML === cycleStart;
      previewDraftingCitation(0); previewDraftingCitation(1); applyAllPreviewedDraftingCitations();
      const cycleCommitsCleanly = editor.textContent.includes('magnets [1,2]. Quench');

      _draftingSetButtonBusy(true);
      const stopLabel = document.getElementById('drafting-btn').textContent === 'Stop'
        && !document.getElementById('drafting-btn').hasAttribute('data-i18n');
      _draftingSetButtonBusy(false);
      editor.innerHTML = '<p>Original selected passage.</p>';
      _draftingSyncFromEditor(false);
      const range = document.createRange();
      range.selectNodeContents(editor.firstChild);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
      state.drafting.selectionRange = range.cloneRange();
      const source = { source_id: 'S1', citation_id: 'Curie2025', title: 'Evidence paper', author: 'Curie', year: '2025',
        apa: 'Curie (2025). Evidence paper.', quote: 'The apparatus has a charging delay.', chunk_uid: 'paper:3' };
      const idea = { validation_status: 'supported', evidence_status: 'verified', source_ids: ['S1'], suggestion: 'Mention the charging delay.',
        evidence: [{source_id: 'S1', quote: source.quote}], evidence_quote: source.quote,
        insert_text: {sentence: 'The apparatus has a charging delay.', paragraph: 'A charging delay limits operation.', note: 'Discuss the delay.'} };
      const renderArguments = () => _renderDraftingArgumentSuggestions({ suggestions: [idea], sources: [source] });
      const clickArgument = label => [...document.querySelectorAll('#drafting-arguments-result button')].find(b => b.textContent === label).click();
      // A proofread started meanwhile must not receive the argument cards.
      state.drafting.reviewView = 'proofread';
      const proofreadBefore = document.getElementById('drafting-proofread-result').innerHTML;
      renderArguments(); renderArguments();
      const argumentsKeepTheirPanel = document.getElementById('drafting-proofread-result').innerHTML === proofreadBefore;
      const verifiedEvidenceIsShown = document.getElementById('drafting-arguments-result').textContent.includes('Evidence verified');
      state.drafting.argumentHistoryId = 'argument-run-1';
      // History requests are recorded here, away from the history test below.
      const historyVersions = [];
      const historyRuns = new Map();
      const pageFetch = window.fetch;
      window.fetch = (url, options = {}) => {
        if (!String(url).startsWith('/api/drafting-history')) return pageFetch(url, options);
        const body = options.body ? JSON.parse(options.body) : null;
        if (String(url) === '/api/drafting-history/versions') historyVersions.push(body);
        else historyRuns.set(decodeURIComponent(String(url).split('/').pop()), body);
        return Promise.resolve(new Response('{"ok":true}', { headers: { 'Content-Type': 'application/json' } }));
      };
      const savedDraft = () => localStorage.getItem(_draftingGetDraftStorageKey()) || '';
      // Placing an argument shows a preview only: not saved, cited, copied or versioned.
      clickArgument('Place sentence');
      const preview = editor.querySelector('.drafting-argument-preview');
      const argumentPreviewShown = Boolean(preview)
        && preview.textContent.includes(idea.insert_text.sentence) && preview.textContent.includes('[Curie 2025]')
        && !editor.querySelector('.drafting-citation-token') && state.drafting.citationOrder.length === 0
        && !savedDraft().includes('drafting-argument-preview')
        && !_draftingExtractCopyText(editor).includes(idea.insert_text.sentence)
        && document.querySelector('[data-argument-card="0"]').classList.contains('is-pending');
      // Placing another variant replaces the preview; removing it restores the text.
      clickArgument('Place paragraph');
      const previewReplaced = editor.querySelectorAll('.drafting-argument-preview').length === 1
        && editor.textContent.includes(idea.insert_text.paragraph) && !editor.textContent.includes(idea.insert_text.sentence);
      clickArgument('Remove preview');
      const previewRemoved = !editor.querySelector('.drafting-argument-preview') && editor.textContent.trim() === 'Original selected passage.'
        && !document.querySelector('[data-argument-card="0"]').classList.contains('is-pending');
      clickArgument('Place sentence');
      editor.querySelector('.drafting-argument-preview [data-argument-preview-action="accept"]').click();
      await new Promise(resolve => setTimeout(resolve, 120));
      const argumentPreservesSelection = editor.textContent.startsWith('Original selected passage.');
      const argumentInsertsReference = !editor.querySelector('.drafting-argument-preview')
        && editor.querySelectorAll('.drafting-citation-token').length === 1
        && state.drafting.citationOrder[0] === 'Curie2025'
        && document.getElementById('drafting-bibliography').textContent.includes('Evidence paper');
      // History: a version of the text before the addition, and the run marks the idea inserted.
      const argumentVersion = historyVersions.find(version => version.reason === 'before_arguments');
      const argumentVersioned = Boolean(argumentVersion) && !argumentVersion.html.includes('Curie')
        && argumentVersion.html.includes('Original selected passage.');
      const argumentRunMarked = historyRuns.get('argument-run-1')?.payload?.result?.suggestions?.[0]?.inserted_variant === 'sentence'
        && document.querySelector('[data-argument-inserted]').textContent === 'Sentence inserted';
      clickArgument('Undo last insertion');
      await new Promise(resolve => setTimeout(resolve, 120));
      const argumentUndone = editor.textContent.trim() === 'Original selected passage.' && state.drafting.citationOrder.length === 0
        && !historyRuns.get('argument-run-1')?.payload?.result?.suggestions?.[0]?.inserted_variant
        && document.querySelector('[data-argument-undo]').hidden;
      clickArgument('Place sentence');
      clickArgument('Confirm insertion');
      const versionsAfterSecondInsert = historyVersions.filter(version => version.reason === 'before_arguments').length;
      clickArgument('Copy sentence');
      const argumentCopyIncludesReference = window.copiedText.includes(source.apa) && window.copiedText.startsWith(idea.insert_text.sentence);
      clickArgument('Place paragraph');
      clickArgument('Confirm insertion');
      clickArgument('Add as working note');
      clickArgument('Confirm insertion');
      const argumentVariantsKeepReference = editor.querySelectorAll('.drafting-citation-token').length === 3 && state.drafting.citationOrder.length === 1
        && versionsAfterSecondInsert === 1;
      window.fetch = pageFetch;
      const beforeUnverified = editor.innerHTML;
      state.drafting.lastArgumentSuggestions[0].validation_status = 'weak_evidence';
      insertDraftingArgumentText(0);
      const unverifiedCannotInsert = editor.innerHTML === beforeUnverified;
      state.drafting.lastArgumentSuggestions[0].validation_status = 'supported';
      state.drafting.lastArgumentSuggestions[0].source_ids.push('S-missing');
      insertDraftingArgumentText(0);
      const missingSourceCannotInsert = editor.innerHTML === beforeUnverified;
      const rejectedIdea = { ...idea, validation_status: 'weak_evidence', rejection_reason: 'The excerpt is only contextual.' };
      _renderDraftingArgumentSuggestions({
        ok: true,
        status: 'verification_rejected',
        suggestions: [],
        review_candidates: [rejectedIdea],
        sources: [source],
      });
      const rejectedCard = document.querySelector('.drafting-item-review-only');
      const rejectedHasNoActions = rejectedCard
        && rejectedCard.querySelectorAll('button').length === 0
        && rejectedCard.textContent.includes(rejectedIdea.rejection_reason);
      const rejectedExcludedFromInsertState = state.drafting.lastArgumentSuggestions.length === 0;
      return { previewWorksBeforeStartup, correctPlacement, applyWorks, copyWorks, removeKeepsAcceptedCitation, removeCleansPreview, bulkStillWorks,
        readOpensEvidencePage, libraryShowsSource, noPathNoReadButton, popoverReadsPlacedCitation,
        selectionIndexIgnored, rejectRestoresText, acceptControlCommits, groupedBySentence, keepsOrder, cycleRestores, cycleCommitsCleanly, stopLabel,
        argumentPreservesSelection, argumentInsertsReference, argumentCopyIncludesReference, argumentVariantsKeepReference, unverifiedCannotInsert,
        missingSourceCannotInsert, rejectedHasNoActions, rejectedExcludedFromInsertState, verifiedEvidenceIsShown,
        argumentsKeepTheirPanel, argumentPreviewShown, previewReplaced, previewRemoved, argumentVersioned, argumentRunMarked, argumentUndone };
    });
    assert.deepEqual(errors, []);
    for (const [name, passed] of Object.entries(results)) assert.equal(passed, true, name);

    const historyResults = await page.evaluate(async () => {
      const settle = () => new Promise(resolve => setTimeout(resolve, 120));
      const editor = _draftingGetEditor();
      const sentence = 'Mechanical stresses limit high field magnets.';
      editor.innerHTML = `<p>${sentence}</p>`;
      _draftingSyncFromEditor(false);
      window.optionalSelectedModel = () => 'mistral';
      const found = { citation_id: 'hist-1', title: 'Stress paper', label: 'Stress (2020)', author: 'Stress', year: '2020',
        apa: 'Stress (2020).', sentence_text: sentence, sentence_index: 0, confidence: 0.8 };
      window.readSSE = async function* () {
        yield { type: 'start', total_sentences: 1, processed_sentences: 0 };
        yield { type: 'sentence_result', sentence_text: sentence, suggestions: [found], validation_status: 'ok', processed_sentences: 1 };
        yield { type: 'done', validation_status: 'ok' };
      };
      state.lastDraftingSuggestions = [];
      await runDraftingAssistant();
      await settle();

      // Another analysis replaced the panel; the first one can be reopened.
      state.lastDraftingSuggestions = [];
      state.drafting.sentenceLog = [];
      _draftingRenderCitationsPanel();
      setDraftingView('history');
      await settle();
      const rows = document.querySelectorAll('#drafting-history-result [data-history-run]');
      const citationRunListed = rows.length === 1 && rows[0].textContent.includes(sentence);
      rows[0].querySelector('[data-drafting-action="history-open"]').click();
      await settle();
      const citationRunReopened = state.drafting.activeView === 'citations'
        && state.lastDraftingSuggestions.length === 1
        && state.lastDraftingSuggestions[0].citation_id === 'hist-1'
        && document.querySelector('#drafting-result [data-drafting-action="preview"]') !== null;

      // Clearing the text keeps a version; restoring brings it back and
      // first saves what it replaces.
      editor.innerHTML = '<p>Text worth keeping.</p>';
      _draftingSyncFromEditor(false);
      window._draftingNodeHasContent = node => Boolean(node.textContent.trim()); // From drafting-interop.js.
      clearDraftingEditor();
      await settle();
      setDraftingView('history');
      await settle();
      document.querySelector('#drafting-history-result [data-drafting-action="version-restore"]').click();
      await settle();
      const versionRestored = editor.textContent.includes('Text worth keeping.');
      return { citationRunListed, citationRunReopened, versionRestored };
    });
    assert.deepEqual(errors, []);
    for (const [name, passed] of Object.entries(historyResults)) assert.equal(passed, true, name);
    // Validating two previews at once earlier in this test also kept a version.
    assert.deepEqual(historyVersions.map(version => version.reason), ['before_restore', 'before_clear', 'before_citations']);

    // "Rechercher plus de citations": external article search for one sentence.
    const discoveryRequests = [];
    await page.route('**/api/article-discovery/stream', route => {
      discoveryRequests.push(route.request().postDataJSON());
      return route.fulfill({ contentType: 'text/event-stream', body: 'data: {}\n\n' });
    });
    const externalResults = await page.evaluate(async () => {
      const settle = () => new Promise(resolve => setTimeout(resolve, 60));
      const editor = _draftingGetEditor();
      const sentence = 'Superconducting magnets above 30 T need new conductors.';
      editor.innerHTML = `<p>${sentence}</p>`;
      _draftingSyncFromEditor(false);
      _draftingResetExternalSearches();
      state.lastDraftingSuggestions = [];
      state.drafting.sentenceLog = [];
      state.drafting.citationRun = { running: false, model: 'mistral', excerpt: sentence };
      _draftingUpsertSentenceLog(sentence, { status: 'none' });
      _draftingRenderCitationsPanel();
      const searchButton = () => document.querySelector('#drafting-result [data-drafting-action="search-external"]');
      const buttonOnEmptySentence = searchButton()?.textContent === 'Rechercher plus de citations';

      // External APIs not allowed: nothing is sent, the settings open.
      window.isExternalDiscoveryEnabled = () => false;
      window.openExternalApiSettings = () => { window.externalSettingsOpened = true; };
      searchButton().click();
      await settle();
      const blockedWithoutConsent = window.externalSettingsOpened === true && !state.drafting.externalSearchController;

      window.isExternalDiscoveryEnabled = () => true;
      const paper = {
        title: 'Magnetic high-field conductors', authors: ['Jean Dupont', 'Marie Curie', 'Ada Lovelace'], year: 2021,
        doi: '10.1000/xyz', venue: 'Supercond. Sci. Technol.', llm_rationale: 'Discusses conductors for 30 T magnets.',
        short_summary: 'A review of HTS conductors.', open_access_pdf: 'https://example.org/paper.pdf', citation_count: 12,
      };
      window.readSSE = async function* () {
        yield { type: 'progress', msg: 'Interrogation des sources…' };
        yield { type: 'done', payload: { ok: true, results: [paper, { ...paper, title: 'javascript: bad', doi: '', open_access_pdf: 'javascript:alert(1)' }] } };
      };
      searchButton().click();
      await settle();
      const suggestion = state.lastDraftingSuggestions[0];
      const card = document.querySelector('#drafting-result .drafting-item.is-external');
      const externalListed = state.lastDraftingSuggestions.length === 2
        && suggestion.citation_id === 'dupont2021magnetic'
        && suggestion.source_pipeline === 'external'
        && suggestion.confidence === null
        && suggestion.label === 'Dupont et al. (2021)'
        && Boolean(card?.querySelector('.drafting-item-external-badge'))
        && !card.querySelector('.is-score')
        && card.querySelector('a.citation-btn')?.getAttribute('href') === 'https://doi.org/10.1000/xyz';
      // The unsafe PDF link is dropped: only the first source gets a publisher link.
      const externalLinks = [...document.querySelectorAll('#drafting-result .drafting-item.is-external a.citation-btn')];
      const unsafeUrlDropped = state.lastDraftingSuggestions[1].open_access_pdf === ''
        && externalLinks.length === 1
        && !externalLinks.some(link => link.getAttribute('href').startsWith('javascript'));
      const statusShown = document.querySelector('.drafting-external-status.is-done')?.textContent.startsWith('2 ');

      // Placed like any suggestion, but never validated as a "confident" preview.
      previewDraftingCitation(0);
      const placedAfterSentence = editor.textContent.indexOf('[Dupont 2021 ?]') > editor.textContent.indexOf('conductors');
      applyConfidentDraftingPreviews();
      const notConfident = _draftingGetPreviewTokens().length === 1;
      previewDraftingCitation(0);
      const committed = state.drafting.citationOrder.includes('dupont2021magnetic')
        && state.drafting.citationIndex.get('dupont2021magnetic').confidence === null
        && document.getElementById('drafting-bibliography').textContent.includes('doi: 10.1000/xyz');
      await copyDraftingCitationById('dupont2021magnetic', 'bibtex');
      const bibtexUsable = window.copiedText.startsWith('@article{dupont2021magnetic,')
        && window.copiedText.includes('doi = {10.1000/xyz}')
        && window.copiedText.includes('author = {Jean Dupont and Marie Curie and Ada Lovelace}');

      // Searching again does not duplicate what is already suggested or cited.
      searchButton().click();
      await settle();
      const noDuplicates = state.lastDraftingSuggestions.length === 2
        && document.querySelector('.drafting-external-status.is-empty') !== null;

      // No download from the Writing tab: the reader goes to the publisher.
      const publisherLinkOnly = !document.querySelector('[data-drafting-action="external-add"]')
        && externalLinks[0].textContent.trim() === 'Voir chez l’éditeur';
      return { buttonOnEmptySentence, blockedWithoutConsent, externalListed, unsafeUrlDropped, statusShown, placedAfterSentence,
        notConfident, committed, bibtexUsable, noDuplicates, publisherLinkOnly };
    });
    assert.deepEqual(errors, []);
    for (const [name, passed] of Object.entries(externalResults)) assert.equal(passed, true, name);
    assert.equal(discoveryRequests.length, 2);
    assert.equal(discoveryRequests[0].mode, 'search');
    assert.equal(discoveryRequests[0].privacy_ack, true);
    assert.ok(discoveryRequests[0].input_text.startsWith('Superconducting magnets above 30 T'));
    console.log(`${Object.keys(results).length + Object.keys(historyResults).length + Object.keys(externalResults).length} citation browser checks passed`);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
