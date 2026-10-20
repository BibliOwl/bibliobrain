// Real DOM checks for Word / LaTeX interop in the Rédaction editor.
// Uses installed Playwright (Edge); all APIs are mocked.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');

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
    // Playwright tries the most recently registered route first.
    await page.route('**/api/**', route => route.fulfill({ json: { ok: true, exists: false } }));
    await page.route('**/api/drafting-resolve-citations', async route => {
      const { keys } = JSON.parse(route.request().postData() || '{}');
      const records = {};
      (keys || []).forEach(key => {
        if (key === 'curie2024' || key === 'ABCD1234') {
          records[key] = { citation_id: 'curie2024', title: 'Quench studies', author: 'Curie', year: '2024',
            bibtex: '@article{curie2024,\n  title = {Quench studies}\n}' };
        }
      });
      await route.fulfill({ json: { ok: true, records } });
    });
    await page.route('**/api/drafting-word-citations', async route => {
      const { keys } = JSON.parse(route.request().postData() || '{}');
      const items = {};
      if ((keys || []).includes('smith_2020')) {
        items.smith_2020 = { uris: ['http://zotero.org/users/local/AbCdEf/items/SMIT2020'], itemData: { type: 'book', title: 'Library Smith' } };
      }
      await route.fulfill({ json: { ok: true, items } });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(() => {
      window.LS_PREFIX = 'interop-regression-';
      window._tr = text => text;
      window.escapeHtml = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.escapeAttr = window.escapeHtml;
      window.showToast = (msg) => { window.lastToast = msg; };
      window.copyTextWithFallback = async text => { window.copiedText = text; return true; };
    });
    for (const file of ['static/js/app.js', 'static/js/event-bindings.js', 'static/js/app/drafting.js', 'static/js/app/drafting-interop.js']) {
      await page.addScriptTag({ path: path.join(root, file) });
    }
    // Real clipboard HTML from Word 16 with Zotero fields, with and without a
    // field separator (the displayed citation then follows field-end).
    const fixtures = {
      separator: fs.readFileSync(path.join(root, 'tests/fixtures/word-zotero-separator.html'), 'utf8'),
      noSeparator: fs.readFileSync(path.join(root, 'tests/fixtures/word-zotero-no-separator.html'), 'utf8'),
    };
    const results = await page.evaluate(async (fixtures) => {
      const editor = _draftingGetEditor();
      const citeOpen = String.fromCharCode(0xE020);
      const citeClose = String.fromCharCode(0xE021);
      const htmlOf = fragment => { const div = document.createElement('div'); div.appendChild(fragment); return div.innerHTML; };
      const record = (id, extra = {}) => _draftingBuildCitationRecord({ citation_id: id, title: `Title ${id}`, author: 'Doe', year: '2020', ...extra });

      // Word: headings, Word list paragraphs with Ignore markers, bold via
      // style, Google Docs' non-bold <b> wrapper, and no styles or scripts.
      const wordHtml = `<html><head><style>p{color:red}</style></head><body><!--StartFragment-->
        <h1 style="color:white">Intro <span style="font-weight:normal">title</span></h1>
        <p class="MsoNormal" style="color:#fff">Plain <b>bold</b> and <span style="font-style:italic">italic</span>&nbsp;text.<script>window.pwned=1</script></p>
        <p class="MsoListParagraphCxSpFirst" style="mso-list:l0 level1 lfo1"><span style="mso-list:Ignore">1.<span>&nbsp;&nbsp;</span></span>First</p>
        <p class="MsoListParagraphCxSpLast" style="mso-list:l0 level1 lfo1"><span style="mso-list:Ignore">2.<span>&nbsp;</span></span>Second</p>
        <b style="font-weight:normal" id="docs-internal-guid-1"><p><span style="font-weight:700">Docs bold</span> normal</p></b>
        <ul><li><p>Item <em>one</em></p></li><li>Item two</li></ul>
        <!--EndFragment--></body></html>`;
      const wordOut = htmlOf(_draftingSanitizeRichHtml(wordHtml));
      const wordStructure = wordOut === '<h1>Intro title</h1>'
        + '<p>Plain <strong>bold</strong> and <em>italic</em> text.</p>'
        + '<ol><li>First</li><li>Second</li></ol>'
        + '<p><strong>Docs bold</strong> normal</p>'
        + '<ul><li>Item <em>one</em></li><li>Item two</li></ul>';
      const noScriptRan = window.pwned === undefined && !/style|script/i.test(wordOut);

      // Several blocks pasted mid-paragraph split it around them.
      editor.innerHTML = '<p>Hello world</p>';
      let range = document.createRange();
      range.setStart(editor.firstChild.firstChild, 6);
      range.collapse(true);
      _draftingInsertRichFragment(_draftingSanitizeRichHtml('<h2>Head</h2><p>Body</p>'), range);
      const splitsParagraph = editor.innerHTML === '<p>Hello </p><h2>Head</h2><p>Body</p><p>world</p>';

      // One paragraph flows into the current one and keeps its formatting.
      editor.innerHTML = '<p>Hello world</p>';
      range = document.createRange();
      range.setStart(editor.firstChild.firstChild, 6);
      range.collapse(true);
      _draftingInsertRichFragment(_draftingSanitizeRichHtml('<p>big <b>bold</b> </p>'), range);
      const inlinePaste = editor.innerHTML === '<p>Hello big <strong>bold</strong>world</p>';

      // LaTeX → editor structure, with citations collected.
      const latex = String.raw`\documentclass{article}
\begin{document}
\section{Résultats}
% a comment that must vanish
Les aimants \emph{résistifs} atteignent 50~T \textbf{en continu} \cite{curie2024,unknown_key}.
Il vaut 10\% de moins --- selon \citet[p.~3]{curie2024}, voir \ref{fig:a}.

\begin{itemize}
  \item Premier point \'etudi\'e
  \item Second {\em point}
\end{itemize}
\begin{figure}\includegraphics{x.png}\caption{Hidden}\end{figure}
\subsection*{Conclusion}
Fin.\footnote{Note}
\end{document}`;
      const looksLatex = _draftingLooksLikeLatex(latex) && !_draftingLooksLikeLatex('Plain text 50% off {not latex}');
      const parsed = _draftingLatexToHtml(latex);
      const latexStructure = parsed.html === '<h1>Résultats</h1>'
        + '<p>Les aimants <em>résistifs</em> atteignent 50 T <strong>en continu</strong> <bb-cite data-keys="curie2024,unknown_key"></bb-cite>. Il vaut 10% de moins — selon <bb-cite data-keys="curie2024"></bb-cite>, voir [fig:a].</p>'
        + '<ul><li>Premier point étudié</li><li>Second <em>point</em></li></ul>'
        + '<h2>Conclusion</h2>'
        + '<p>Fin. (Note)</p>';
      const latexKeys = [...parsed.keys].join(',') === 'curie2024,unknown_key';

      // Paste LaTeX: known keys resolve from the library, unknown ones stay cited.
      editor.innerHTML = '';
      _draftingSyncFromEditor(false);
      await _draftingPasteLatex(String.raw`\section{Titre} Texte \cite{curie2024,unknown_key}.`, null);
      const tokens = [...editor.querySelectorAll('.drafting-citation-token')];
      const latexPaste = editor.querySelector('h1')?.textContent === 'Titre'
        && tokens.length === 2
        && tokens[0].dataset.title === 'Quench studies'
        && tokens[1].dataset.citationId === 'unknown_key'
        && !tokens[0].style.getPropertyValue('--drafting-confidence-color')
        && state.drafting.citationOrder.join(',') === 'curie2024,unknown_key'
        && editor.textContent.includes('Texte [1,2].');
      const latexPasteDebug = { html: editor.innerHTML, order: state.drafting.citationOrder, toast: window.lastToast };

      // Editor → Markdown (labels) and LaTeX-marker content.
      editor.innerHTML = '';
      editor.appendChild(_draftingSanitizeRichHtml('<h2>Methods</h2><p>A <b>strong <i>claim</i></b> here</p><ol><li>one</li><li>two <em>x</em></li></ol><ul><li>dot</li></ul>'));
      editor.querySelector('p').appendChild(_draftingCreateCitationToken(record('smith_2020', { bibtex: '@article{smith_2020_bib,\n title={x}\n}' }), 1));
      editor.querySelector('p').appendChild(_draftingCreateCitationToken(record('lee:2021'), 2));
      _draftingSyncFromEditor(false);
      const markdown = _draftingSerializeMarkdown(editor);
      const markdownOk = markdown === '## Methods\n\nA **strong *claim*** here[1,2]\n\n1. one\n2. two *x*\n\n- dot';
      const latexMarkdown = _draftingSerializeMarkdown(editor, { citations: 'latex' });
      const latexMarkersOk = latexMarkdown.includes(`here${citeOpen}smith_2020_bib${citeClose}${citeOpen}lee:2021${citeClose}`);

      // Legacy drafts: loose text with newlines stays paragraphs.
      const legacy = document.createElement('div');
      legacy.innerHTML = 'First line\nsecond line\n\nNext para<div>typed div</div>';
      const legacyOk = _draftingSerializeMarkdown(legacy) === 'First line\nsecond line\n\nNext para\n\ntyped div';

      // Copy for Word: clean HTML, labels, bibliography; pasting it back
      // restores live tokens and drops the bibliography block.
      const payload = _draftingBuildClipboardPayload(editor, { withBibliography: true });
      const copyHtmlOk = !/style=|class="drafting-citation-link"|<a /.test(payload.html)
        && payload.html.includes('<h2>Methods</h2>')
        && payload.html.includes('data-drafting-bibliography')
        && /<span class="drafting-citation-token" data-citation-id="smith_2020"[^>]*>\[1<\/span>/.test(payload.html);
      const copyTextOk = payload.text.startsWith('Methods') && payload.text.includes('Bibliographie\n[1] ');
      const reparsed = _draftingSanitizeRichHtml(payload.html);
      const roundTrip = reparsed.querySelectorAll('.drafting-citation-token .drafting-citation-link').length === 2
        && !htmlOf(reparsed).includes('Bibliographie');

      // Copy for Word (Zotero): adjacent tokens form one field with both
      // items; library items carry their URI, the others their editor
      // metadata; the Word importer reads the result back.
      const fieldOf = html => {
        const code = /ADDIN ZOTERO_ITEM CSL_CITATION (.*?) <span style='mso-element:field-separator'>/s.exec(html)?.[1] || '';
        try {
          return JSON.parse(code.replaceAll('&quot;', '"').replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&'));
        } catch (e) { return null; }
      };
      const word = _draftingBuildWordZoteroPayload(editor, new Map([['smith_2020', {
        uris: ['http://zotero.org/users/local/AbCdEf/items/SMIT2020'], itemData: { type: 'book', title: 'Library Smith' } }]]));
      const field = fieldOf(word.html);
      const wordFieldOk = word.fields === 1
        && field?.properties.plainCitation === '[1,2]'
        && field.citationItems.length === 2
        && field.citationItems[0].uris[0] === 'http://zotero.org/users/local/AbCdEf/items/SMIT2020'
        && field.citationItems[0].itemData.title === 'Library Smith'
        && field.citationItems[1].uris.length === 0
        && field.citationItems[1].itemData.title === 'Title lee:2021'
        && field.citationItems[1].itemData.author[0].literal === 'Doe'
        && word.html.includes("<![endif]-->[1,2]<!--[if supportFields]><span style='mso-element:field-end'>")
        && !word.html.includes('drafting-citation-token')
        && word.html.includes('<h2>Methods</h2>')
        && word.text.includes('here[1,2]');
      const wordStats = {};
      const wordBack = _draftingSanitizeRichHtml(word.html, { stats: wordStats });
      const wordRoundTrip = wordStats.fieldCitations === 2
        && [...wordBack.querySelectorAll('.drafting-citation-token')].map(t => t.dataset.citationId).join(',') === 'SMIT2020,csl-2'
        && !wordBack.textContent.includes('ADDIN') && !wordBack.textContent.includes('[1,2]');
      window._draftingWriteRichClipboard = async (html, text) => { window.wordClip = { html, text }; return true; };
      const wordRange = document.createRange();
      wordRange.selectNodeContents(editor.querySelector('p'));
      const wordCopied = await copyDraftingSelectionForWord(wordRange);
      const copiedField = fieldOf(window.wordClip?.html || '');
      const wordCopyOk = wordCopied === true
        && copiedField?.citationItems[0].uris[0] === 'http://zotero.org/users/local/AbCdEf/items/SMIT2020'
        && copiedField.citationItems[1].uris.length === 0
        && !window.wordClip.html.includes('Methods')
        && window.lastToast.startsWith('Copié pour Word') && window.lastToast.includes('pas liées');

      // BibTeX file: stored entry kept, generated entry for the other, no unresolved keys.
      const bib = _draftingBuildBibtexFile([
        record('smith_2020', { bibtex: '@article{smith_2020_bib,\n title={x}\n}' }),
        record('lee:2021', { journal: 'J {x}' }),
        record('ghost', { support_status: 'unresolved' }),
      ]);
      const bibOk = bib.startsWith('@article{smith_2020_bib,')
        && bib.includes('@misc{lee:2021,\n  author = {Doe},\n  title = {Title lee:2021},\n  journal = {J x},\n  year = {2020}\n}')
        && !bib.includes('ghost');

      // Clear with undo: nothing saved until the toast expires.
      let undo = null;
      window.deleteWithUndo = (key, options) => { undo = options; };
      editor.innerHTML = '<p>Keep me</p>';
      _draftingSyncFromEditor(false);
      clearDraftingEditor();
      const clearedNow = editor.innerHTML === '' && undo?.message === 'Texte effacé.';
      undo.restore();
      const undoRestores = editor.innerHTML === '<p>Keep me</p>';

      // Toolbar: bold and heading on a selection.
      editor.innerHTML = '<p>alpha beta</p>';
      editor.focus();
      range = document.createRange();
      range.setStart(editor.firstChild.firstChild, 0);
      range.setEnd(editor.firstChild.firstChild, 5);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
      _draftingApplyFormat('bold');
      const boldApplied = /<b>alpha<\/b>/.test(editor.innerHTML);
      _draftingApplyFormat('block', 'h2');
      const headingApplied = editor.firstElementChild?.tagName === 'H2';
      _draftingApplyFormat('ul');
      const listApplied = Boolean(editor.querySelector('ul li'));

      // Word + Zotero: fields become tokens, their displayed text disappears,
      // library items win over the metadata stored in the document.
      const libraryRecord = record('curie2024', { title: 'Library title' });
      const tokenIds = fragment => [...fragment.querySelectorAll('.drafting-citation-token')].map(t => t.dataset.citationId).join(',');
      const sepStats = {};
      const sep = _draftingSanitizeRichHtml(fixtures.separator, { fieldCitations: new Map([['ABCD1234', libraryRecord]]), stats: sepStats });
      const sepText = sep.textContent;
      const docToken = sep.querySelectorAll('.drafting-citation-token')[1];
      const zoteroSeparator = tokenIds(sep) === 'curie2024,WXYZ9876'
        && sep.firstChild.outerHTML === '<h1>Introduction</h1>'
        && sep.querySelector('p strong')?.textContent === 'mechanical stress'
        && /stress \[1\]\[1\]\. Next sentence\./.test(sepText)
        && !sepText.includes('(Curie') && !sepText.includes('ADDIN') && !sepText.includes('Quench studies.')
        && sepStats.fieldCitations === 2 && sepStats.fromDocument === 1
        && sep.querySelectorAll('.drafting-citation-token')[0].dataset.title === 'Library title'
        && docToken.dataset.title === 'Resistive magnets, a long title that forces Word to wrap the field code line'
        && docToken.dataset.authorsDisplay === 'Doe, John and Roe, Jane'
        && docToken.dataset.year === '2021';
      const noSep = _draftingSanitizeRichHtml(fixtures.noSeparator);
      const noSepText = noSep.textContent;
      const zoteroNoSeparator = tokenIds(noSep) === 'ABCD1234,WXYZ9876'
        && /stress \[1\]\[1\]\. Next sentence\./.test(noSepText)
        && !noSepText.includes('(Curie')
        // Without a separator the bibliography's extent is unknown: kept.
        && noSepText.includes('Curie, M. (2024). Quench studies.');
      editor.innerHTML = '';
      _draftingSyncFromEditor(false);
      window.lastToast = '';
      await _draftingPasteWordCitations(fixtures.separator, '', null);
      const zoteroPaste = state.drafting.citationOrder.join(',') === 'curie2024,WXYZ9876'
        && editor.textContent.includes('stress [1,2]. Next sentence.')
        && document.getElementById('drafting-bibliography').textContent.includes('Quench studies')
        && window.lastToast.startsWith('2 citations Zotero importées. 1 absente');
      _draftingInsertTransferContent({ html: fixtures.separator, text: 'plain' }, null);
      await new Promise(resolve => setTimeout(resolve, 200));
      const zoteroRoutedFromPaste = editor.querySelectorAll('h1').length === 4;
      const zoteroDebug = { sep: htmlOf(sep).slice(0, 600), noSepText, toast: window.lastToast, order: state.drafting.citationOrder };

      return { zoteroSeparator, zoteroNoSeparator, zoteroPaste, zoteroRoutedFromPaste,
        wordStructure, noScriptRan, splitsParagraph, inlinePaste, looksLatex, latexStructure, latexKeys, latexPaste,
        markdownOk, latexMarkersOk, legacyOk, copyHtmlOk, copyTextOk, roundTrip, wordFieldOk, wordRoundTrip, wordCopyOk, bibOk, clearedNow, undoRestores,
        boldApplied, headingApplied, listApplied,
        debug: { zoteroDebug, latexPasteDebug, wordZotero: { html: word.html, toast: window.lastToast, clip: window.wordClip?.html }, wordOut, parsed: parsed.html, markdown, latexMarkdown, payload: payload.html, bib, editor: editor.innerHTML } };
    }, fixtures);
    assert.deepEqual(errors, []);
    const { debug, ...checks } = results;
    for (const [name, passed] of Object.entries(checks)) {
      assert.equal(passed, true, `${name}\n${JSON.stringify(debug, null, 2)}`);
    }
    console.log(`${Object.keys(checks).length} interop browser checks passed`);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
