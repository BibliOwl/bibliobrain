/* Drafting interop: moving text between the Rédaction editor and Word,
 * web pages or LaTeX without losing its structure.
 *
 * The editor keeps a small subset of HTML: paragraphs, h1-h3, ul/ol, bold,
 * italic, line breaks and citation tokens. Everything pasted is reduced to
 * that subset; everything copied or exported is written from it. */

// Private-use markers around a citation key in LaTeX exports; the server
// turns runs of them into \cite{key1,key2}.
const DRAFTING_CITE_OPEN = '\uE020';
const DRAFTING_CITE_CLOSE = '\uE021';

const DRAFTING_BLOCK_TAG_RE = /^(ADDRESS|ARTICLE|ASIDE|BLOCKQUOTE|DD|DIV|DL|DT|FIELDSET|FIGCAPTION|FIGURE|FOOTER|FORM|H[1-6]|HEADER|HR|LI|MAIN|NAV|OL|P|PRE|SECTION|TABLE|TBODY|TD|TFOOT|TH|THEAD|TR|UL)$/;
const DRAFTING_DROP_TAGS = new Set([
  'AUDIO', 'BUTTON', 'CANVAS', 'EMBED', 'HEAD', 'IFRAME', 'IMG', 'INPUT', 'LINK', 'META', 'NOSCRIPT',
  'OBJECT', 'PICTURE', 'SCRIPT', 'SELECT', 'STYLE', 'SVG', 'TEMPLATE', 'TEXTAREA', 'TITLE', 'VIDEO',
]);

function _draftingLatexKey(record) {
  const fromBibtex = /^\s*@\w+\s*[{(]\s*([^,\s]+)\s*,/.exec(String(record?.bibtex || ''));
  const raw = fromBibtex ? fromBibtex[1] : String(record?.citation_id || record?.citation_key || '');
  return raw.replace(/[^A-Za-z0-9:_\-.]+/g, '').slice(0, 200);
}

// Bold / italic carried by an element: true, false (explicitly reset, as in
// Google Docs' <b style="font-weight:normal"> wrapper) or null (inherited).
function _draftingInlineFormat(element) {
  const tag = String(element?.tagName || '').toUpperCase();
  const style = String(element?.getAttribute?.('style') || '');
  let bold = tag === 'B' || tag === 'STRONG' ? true : null;
  let italic = tag === 'I' || tag === 'EM' || tag === 'CITE' ? true : null;
  // Anchored so Word's mso-bidi-font-weight (right-to-left text only) is ignored.
  const weight = /(?:^|[;\s])font-weight\s*:\s*([^;"']+)/i.exec(style)?.[1]?.trim().toLowerCase();
  if (weight) bold = weight === 'bold' || weight === 'bolder' || Number(weight) >= 600;
  const fontStyle = /(?:^|[;\s])font-style\s*:\s*([^;"']+)/i.exec(style)?.[1]?.trim().toLowerCase();
  if (fontStyle) italic = fontStyle === 'italic' || fontStyle === 'oblique';
  return { bold, italic };
}

function _draftingNodeHasContent(node) {
  if (!node) return false;
  if (String(node.textContent || '').replace(/\u00a0/g, ' ').trim()) return true;
  return Boolean(node.querySelector?.('.drafting-citation-token'));
}

function _draftingTrimBlock(block) {
  while (block.lastChild && block.lastChild.nodeName === 'BR') block.lastChild.remove();
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
  const texts = [];
  while (walker.nextNode()) texts.push(walker.currentNode);
  if (!texts.length) return;
  texts[0].nodeValue = texts[0].nodeValue.replace(/^\s+/, '');
  const last = texts[texts.length - 1];
  last.nodeValue = last.nodeValue.replace(/\s+$/, '');
}

/* ── Word citation fields (Zotero, Mendeley) ────────────────────────────
 * Word's clipboard HTML keeps each field in <!--[if supportFields]-->
 * comments: field-begin, the code ("ADDIN ZOTERO_ITEM CSL_CITATION {json}"),
 * an optional field-separator, the displayed result, then field-end. When
 * there is no separator the displayed text follows field-end instead. */

const DRAFTING_FIELD_BEGIN = String.fromCharCode(0xE030);
const DRAFTING_FIELD_SEPARATOR = String.fromCharCode(0xE031);
const DRAFTING_FIELD_END = String.fromCharCode(0xE032);
const DRAFTING_FIELD_EVENT_RE = new RegExp(`(${DRAFTING_FIELD_BEGIN}|${DRAFTING_FIELD_SEPARATOR}|${DRAFTING_FIELD_END})`);
const DRAFTING_WORD_CITATION_RE = /ADDIN\s+(?:ZOTERO_ITEM|CSL_CITATION)\b/;
// Item URIs: users/<id>/items/KEY, users/local/<id>/items/KEY, groups/<id>/items/KEY.
const DRAFTING_ZOTERO_URI_RE = /zotero\.org\/(?:users|groups)\/(?:local\/)?[^\s"&/]+\/items\/([A-Za-z0-9]+)/;
const DRAFTING_WORD_BIBLIOGRAPHY_RE =/ADDIN\s+(?:ZOTERO_BIBL|Mendeley\s+Bibliography)\b|CSL_BIBLIOGRAPHY/;

// A Word field comment → sequence of begin / separator / end markers and
// code text, in order.
function _draftingFieldCommentEvents(data) {
  const raw = String(data || '');
  if (!/mso-element\s*:\s*field-/i.test(raw)) return [];
  const markers = { begin: DRAFTING_FIELD_BEGIN, separator: DRAFTING_FIELD_SEPARATOR, end: DRAFTING_FIELD_END };
  const marked = raw.replace(
    /<span[^>]*mso-element\s*:\s*field-(begin|separator|end)[^>]*>/gi,
    (match, kind) => markers[kind.toLowerCase()],
  );
  const text = new DOMParser().parseFromString(marked, 'text/html').body.textContent || '';
  return text.split(DRAFTING_FIELD_EVENT_RE).filter((part) => part !== '');
}

// "ADDIN ZOTERO_ITEM CSL_CITATION {json}" → cited items and displayed text.
function _draftingParseCslCitation(code) {
  // Word wraps long lines of the field code by replacing a space with a newline.
  const flat = String(code || '').replace(/\r?\n/g, ' ');
  const start = flat.indexOf('{');
  const end = flat.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  let payload = null;
  try {
    payload = JSON.parse(flat.slice(start, end + 1));
  } catch (e) {
    return null;
  }
  const items = (Array.isArray(payload?.citationItems) ? payload.citationItems : []).map((item) => {
    const uris = [].concat(item?.uris || [], item?.uri || []);
    const key = uris.map((uri) => DRAFTING_ZOTERO_URI_RE.exec(String(uri))?.[1])
      .find(Boolean) || '';
    return { key, data: item?.itemData && typeof item.itemData === 'object' ? item.itemData : {} };
  }).filter((item) => item.key || item.data.title);
  const shown = String(payload?.properties?.plainCitation || payload?.properties?.formattedCitation || '');
  return { items, shown: shown.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim() };
}

// Citation record from the CSL data Word keeps in the field, for items that
// are not in the local library.
function _draftingRecordFromCslItem(key, data) {
  const authors = Array.isArray(data?.author) ? data.author : [];
  const names = authors
    .map((author) => author?.literal || [author?.family, author?.given].filter(Boolean).join(', '))
    .filter(Boolean);
  const issued = data?.issued || {};
  const year = String(issued['date-parts']?.[0]?.[0] ?? '').trim()
    || (/\d{4}/.exec(String(issued.raw || issued.literal || ''))?.[0] || '');
  const family = String(authors[0]?.family || authors[0]?.literal || '').trim();
  return _draftingBuildCitationRecord({
    citation_id: key || `csl-${data?.id ?? 'item'}`,
    title: data?.title,
    author: names[0] || '',
    authors_display: names.join(' and '),
    year,
    journal: data?.['container-title'],
    doi: data?.DOI,
    label: `${family || 'Source'} (${year || 'n.d.'})`,
    support_status: 'imported',
  });
}

function _draftingZoteroKeysFromHtml(html) {
  const keys = new Set();
  const pattern = new RegExp(DRAFTING_ZOTERO_URI_RE.source, 'g');
  let match;
  while ((match = pattern.exec(String(html || '')))) keys.add(match[1]);
  return Array.from(keys);
}

function _draftingSanitizeRichHtml(html, options = {}) {
  let doc = null;
  try {
    // DOMParser documents are inert: no script runs and no image loads.
    doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
  } catch (e) {
    return document.createDocumentFragment();
  }
  return _draftingSanitizeNode(doc.body, options);
}

// Reduce any DOM (a pasted page, a Word document, the editor itself) to the
// editor's subset. options.tokens: 'rebuild' recreates live citation tokens,
// 'export' writes a plain "[n]" span that keeps the data attributes.
// options.citations: Map of LaTeX key -> citation record for <bb-cite>.
// options.fieldCitations: Map of Zotero item key -> library record for Word
// citation fields; options.stats receives how many were found.
function _draftingSanitizeNode(root, options = {}) {
  const out = document.createDocumentFragment();
  const exportTokens = options.tokens === 'export';
  const citations = options.citations instanceof Map ? options.citations : new Map();
  const fieldCitations = options.fieldCitations instanceof Map ? options.fieldCitations : new Map();
  const stats = options.stats && typeof options.stats === 'object' ? options.stats : {};
  stats.fieldCitations = 0;
  stats.fromDocument = 0;
  let block = null;
  let list = null;
  // Open Word fields; a citation or bibliography field in its result phase
  // hides its displayed text, which the tokens replace.
  const fields = [];
  let pendingShownText = '';

  const finishBlock = () => {
    if (!block) return;
    _draftingTrimBlock(block);
    if (!_draftingNodeHasContent(block)) block.remove();
    block = null;
  };
  const startBlock = (tag) => {
    finishBlock();
    const element = document.createElement(tag);
    if (tag === 'li') {
      if (!list) {
        list = document.createElement('ul');
        out.appendChild(list);
      }
      list.appendChild(element);
    } else {
      list = null;
      out.appendChild(element);
    }
    block = element;
    return element;
  };
  const startList = (ordered) => {
    finishBlock();
    const tag = ordered ? 'ol' : 'ul';
    if (!list || list.tagName.toLowerCase() !== tag) {
      list = document.createElement(tag);
      out.appendChild(list);
    }
  };
  const ensureBlock = () => block || startBlock('p');
  const reuse = (parent, tag) => {
    const last = parent.lastChild;
    if (last && last.nodeType === Node.ELEMENT_NODE && last.tagName.toLowerCase() === tag) return last;
    const element = document.createElement(tag);
    parent.appendChild(element);
    return element;
  };
  const endsWithSpace = (element) => {
    if (element.lastChild?.nodeName === 'BR') return true;
    const text = String(element.textContent || '');
    return !text || /\s$/.test(text);
  };
  const appendText = (raw, fmt) => {
    let text = String(raw || '').replace(/\u200b/g, '').replace(/\s+/g, ' ');
    if (!text) return;
    if (pendingShownText) {
      // Field without separator: its displayed citation follows field-end.
      const trimmed = text.replace(/^ /, '');
      if (pendingShownText.startsWith(trimmed)) {
        pendingShownText = pendingShownText.slice(trimmed.length).replace(/^ /, '');
        return;
      }
      text = trimmed.startsWith(pendingShownText) ? trimmed.slice(pendingShownText.length) : text;
      pendingShownText = '';
      if (!text) return;
    }
    if (text[0] === ' ' && (!block || endsWithSpace(block))) text = text.slice(1);
    if (!text) return;
    let parent = ensureBlock();
    if (fmt.bold && !fmt.inHeading) parent = reuse(parent, 'strong');
    if (fmt.italic) parent = reuse(parent, 'em');
    parent.appendChild(document.createTextNode(text));
  };
  const appendToken = (element) => {
    if (element.dataset?.preview === '1' || !element.dataset?.citationId) return;
    if (exportTokens) {
      const span = document.createElement('span');
      span.className = 'drafting-citation-token';
      Array.from(element.attributes).forEach((attr) => {
        if (attr.name.startsWith('data-')) span.setAttribute(attr.name, attr.value);
      });
      span.textContent = _draftingGetCitationTokenCopyLabel(element);
      ensureBlock().appendChild(span);
      return;
    }
    const record = _draftingBuildCitationRecord(_draftingGetCitationRecordFromToken(element));
    ensureBlock().appendChild(_draftingCreateCitationToken(record, Number(element.dataset.citationNumber) || 1));
  };
  const appendCitationKeys = (raw) => {
    String(raw || '').split(',').map((key) => key.trim()).filter(Boolean).forEach((key) => {
      const record = citations.get(key)
        || _draftingBuildCitationRecord({ citation_id: key, label: key, support_status: 'unresolved' });
      ensureBlock().appendChild(_draftingCreateCitationToken(record, 1));
    });
  };
  const appendFieldCitation = (code) => {
    const parsed = _draftingParseCslCitation(code);
    if (!parsed?.items.length) return null;
    parsed.items.forEach(({ key, data }) => {
      let record = key ? fieldCitations.get(key) : null;
      if (!record) {
        record = _draftingRecordFromCslItem(key, data);
        stats.fromDocument += 1;
      }
      stats.fieldCitations += 1;
      ensureBlock().appendChild(_draftingCreateCitationToken(record, 1));
    });
    return parsed;
  };
  // A field's code is complete at its separator (or its end when it has
  // none): classify it then, and emit citation tokens.
  const closeFieldCode = (field, hasSeparator) => {
    const code = field.code.replace(/\s+/g, ' ');
    if (DRAFTING_WORD_CITATION_RE.test(code)) {
      const parsed = appendFieldCitation(field.code);
      if (parsed) {
        field.kind = 'citation';
        if (!hasSeparator) pendingShownText = parsed.shown;
      }
    } else if (DRAFTING_WORD_BIBLIOGRAPHY_RE.test(code) && hasSeparator) {
      // Our bibliography replaces Word's; without a separator its extent is unknown.
      field.kind = 'bibliography';
    }
    field.phase = 'result';
  };
  const onComment = (data) => {
    _draftingFieldCommentEvents(data).forEach((event) => {
      if (event === DRAFTING_FIELD_BEGIN) {
        fields.push({ code: '', phase: 'code', kind: '' });
        return;
      }
      const field = fields[fields.length - 1];
      if (!field) return;
      if (event === DRAFTING_FIELD_SEPARATOR || event === DRAFTING_FIELD_END) {
        if (field.phase === 'code') closeFieldCode(field, event === DRAFTING_FIELD_SEPARATOR);
        if (event === DRAFTING_FIELD_END) fields.pop();
      } else if (field.phase === 'code') {
        field.code += event;
      }
    });
  };
  const hidingFieldResult = () => fields.some((field) => field.phase === 'result' && field.kind);

  const walk = (node, fmt) => {
    if (node.nodeType === Node.COMMENT_NODE) {
      onComment(node.nodeValue);
      return;
    }
    if (hidingFieldResult()) {
      // Still walk the children: the field-end comment may be nested.
      if (node.nodeType === Node.ELEMENT_NODE) Array.from(node.childNodes).forEach((child) => walk(child, fmt));
      return;
    }
    if (node.nodeType === Node.TEXT_NODE) {
      appendText(node.nodeValue, fmt);
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const element = /** @type {HTMLElement} */ (node);
    const tag = element.tagName.toUpperCase();
    const style = String(element.getAttribute('style') || '');
    if (DRAFTING_DROP_TAGS.has(tag) || element.hasAttribute('data-drafting-bibliography')) return;
    if (element.classList.contains('drafting-preview-controls') || element.classList.contains('drafting-argument-preview')) return;
    // Word writes list bullets as text inside mso-list:Ignore spans.
    if (/mso-list\s*:\s*ignore/i.test(style) || /display\s*:\s*none/i.test(style)) return;
    if (element.classList.contains('drafting-citation-token')) {
      appendToken(element);
      return;
    }
    if (tag === 'BB-CITE') {
      appendCitationKeys(element.getAttribute('data-keys'));
      return;
    }
    if (tag === 'BR') {
      ensureBlock().appendChild(document.createElement('br'));
      return;
    }

    const own = _draftingInlineFormat(element);
    const next = {
      ...fmt,
      bold: own.bold === null ? fmt.bold : own.bold,
      italic: own.italic === null ? fmt.italic : own.italic,
    };
    const walkChildren = (childFmt = next) => {
      Array.from(element.childNodes).forEach((child) => walk(child, childFmt));
    };

    const heading = /^H([1-6])$/.exec(tag);
    const wordTitle = tag === 'P' && /\bMsoTitle\b/.test(element.className);
    if ((heading || wordTitle) && !fmt.inHeading && !fmt.inItem) {
      startBlock(`h${wordTitle ? 1 : Math.min(3, Number(heading[1]))}`);
      walkChildren({ ...next, inHeading: true });
      finishBlock();
      return;
    }
    if ((tag === 'UL' || tag === 'OL') && !fmt.inHeading) {
      startList(tag === 'OL');
      walkChildren({ ...next, inItem: false });
      finishBlock();
      return;
    }
    const wordListItem = tag === 'P' && /mso-list\s*:/i.test(style);
    if ((tag === 'LI' || wordListItem) && !fmt.inHeading && !fmt.inItem) {
      if (wordListItem) {
        const marker = Array.from(element.querySelectorAll('span'))
          .find((span) => /mso-list\s*:\s*ignore/i.test(span.getAttribute('style') || ''));
        startList(/^\(?[0-9A-Za-z]{1,4}[.)]/.test(String(marker?.textContent || '').trim()));
      } else if (!list) {
        startList(element.parentElement?.tagName === 'OL');
      }
      startBlock('li');
      walkChildren({ ...next, inItem: true });
      finishBlock();
      return;
    }
    if (tag === 'TD' || tag === 'TH') {
      walkChildren();
      appendText(' ', next);
      return;
    }
    if (DRAFTING_BLOCK_TAG_RE.test(tag)) {
      if (fmt.inHeading || fmt.inItem) {
        // Paragraphs inside a list item or a heading stay on its line.
        if (block && _draftingNodeHasContent(block)) appendText(' ', next);
        walkChildren();
        return;
      }
      finishBlock();
      walkChildren();
      finishBlock();
      return;
    }
    walkChildren();
  };

  Array.from(root?.childNodes || []).forEach((child) => {
    walk(child, { bold: false, italic: false, inHeading: false, inItem: false });
  });
  finishBlock();
  out.querySelectorAll('ul, ol').forEach((element) => {
    if (!element.querySelector('li')) element.remove();
  });
  out.normalize();
  return out;
}

// Insert sanitized blocks at the caret. A single paragraph flows into the
// current one; several blocks split the current block around them.
function _draftingInsertRichFragment(fragment, range) {
  const editor = _draftingGetEditor();
  const selection = window.getSelection();
  if (!editor || !selection || !fragment) return;
  if (!range || !_draftingRangePointsIntoEditor(range)) {
    range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(false);
  }
  range.deleteContents();
  const blocks = Array.from(fragment.childNodes);
  if (!blocks.length) return;

  if (blocks.length === 1 && blocks[0].nodeName === 'P') {
    const children = Array.from(blocks[0].childNodes);
    const inline = document.createDocumentFragment();
    children.forEach((child) => inline.appendChild(child));
    range.insertNode(inline);
    const last = children[children.length - 1];
    if (last) range.setStartAfter(last);
    range.collapse(true);
  } else {
    const lastBlock = blocks[blocks.length - 1];
    let top = range.startContainer;
    while (top && top !== editor && top.parentNode !== editor) top = top.parentNode;
    if (!top || top === editor || top.nodeType !== Node.ELEMENT_NODE) {
      range.insertNode(fragment);
    } else {
      const tail = document.createRange();
      tail.setStart(range.startContainer, range.startOffset);
      tail.setEnd(top, top.childNodes.length);
      const tailBlock = top.cloneNode(false);
      tailBlock.appendChild(tail.extractContents());
      top.after(fragment);
      if (_draftingNodeHasContent(tailBlock)) lastBlock.after(tailBlock);
      if (!_draftingNodeHasContent(top)) top.remove();
    }
    range = document.createRange();
    range.selectNodeContents(lastBlock);
    range.collapse(false);
  }
  selection.removeAllRanges();
  selection.addRange(range);
  state.drafting.selectionRange = range.cloneRange();
  _draftingSyncFromEditor();
}

// Entry point for paste and drop: LaTeX source first, then rich HTML, then
// plain text as before.
function _draftingInsertTransferContent({ text = '', html = '' }, range) {
  if (text && _draftingLooksLikeLatex(text)) {
    void _draftingPasteLatex(text, range);
    return;
  }
  if (html && DRAFTING_WORD_CITATION_RE.test(html)) {
    void _draftingPasteWordCitations(html, text, range);
    return;
  }
  if (html) {
    const fragment = _draftingSanitizeRichHtml(html);
    if (_draftingNodeHasContent(fragment)) {
      _draftingInsertRichFragment(fragment, range);
      return;
    }
  }
  if (text) _draftingInsertPlainText(text, range);
}

// Word text with Zotero / Mendeley citation fields: items found in the local
// library get their library record, the others keep the metadata stored in
// the Word document.
async function _draftingPasteWordCitations(html, text, range) {
  const keys = _draftingZoteroKeysFromHtml(html);
  const known = keys.length ? await _draftingResolveCitationKeys(keys) : new Map();
  const stats = {};
  const fragment = _draftingSanitizeRichHtml(html, { fieldCitations: known, stats });
  if (!_draftingNodeHasContent(fragment)) {
    if (text) _draftingInsertPlainText(text, range);
    return;
  }
  _draftingInsertRichFragment(fragment, range);
  const count = stats.fieldCitations || 0;
  if (!count) return;
  let message = `${count} ${_tr(count > 1 ? 'citations Zotero importées.' : 'citation Zotero importée.')}`;
  if (stats.fromDocument) {
    message += ` ${stats.fromDocument} ${_tr(stats.fromDocument > 1
      ? 'absentes de la bibliothèque : métadonnées reprises du document Word.'
      : 'absente de la bibliothèque : métadonnées reprises du document Word.')}`;
  }
  showToast(message, stats.fromDocument ? 5200 : 3200, stats.fromDocument ? 'info' : 'success');
}

/* ── LaTeX → editor ─────────────────────────────────────────────────── */

const DRAFTING_LATEX_HINT_RE = /\\(?:(?:sub){0,2}section|chapter)\*?\s*[[{]|\\(?:[Cc]ite[a-z]*|[Pp]arencite|[Tt]extcite|[Aa]utocite|[Ff]ootcite)\*?\s*[[{]|\\(?:emph|textbf|textit)\s*\{|\\begin\s*\{|\\item\b/;
const DRAFTING_LATEX_CITE_RE = /^(?:[Cc]ite[a-z]*|[Pp]arencite|[Tt]extcite|[Aa]utocite|[Ff]ootcite|[Ss]upercite|[Ss]martcite)$/;
const DRAFTING_LATEX_DROP_ARGS = {
  addbibresource: 1, addcontentsline: 3, bibliography: 1, bibliographystyle: 1, caption: 1, hspace: 1,
  includegraphics: 1, index: 1, label: 1, nocite: 1, pagestyle: 1, setlength: 2, thispagestyle: 1, vspace: 1,
};
const DRAFTING_LATEX_TEXT = {
  LaTeX: 'LaTeX', TeX: 'TeX', aa: 'å', ae: 'æ', copyright: '©', degree: '°', dots: '…', euro: '€',
  fg: ' »', guillemotleft: '«', guillemotright: '»', i: 'ı', ldots: '…', linebreak: '\n', newline: '\n',
  o: 'ø', oe: 'œ', og: '« ', ss: 'ß', textasciitilde: '~', textbackslash: '\\', textbar: '|',
  textdegree: '°', textellipsis: '…', textemdash: '—', textendash: '–', textgreater: '>', textless: '<',
  textquoteleft: '‘', textquoteright: '’',
};
const DRAFTING_LATEX_ACCENTS = {
  "'": '\u0301', '`': '\u0300', '^': '\u0302', '"': '\u0308', '~': '\u0303', '=': '\u0304', '.': '\u0307',
  H: '\u030B', c: '\u0327', k: '\u0328', r: '\u030A', u: '\u0306', v: '\u030C',
};

function _draftingLooksLikeLatex(text) {
  return DRAFTING_LATEX_HINT_RE.test(String(text || ''));
}

function _draftingEscapeHtmlText(value) {
  return String(value || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// LaTeX inline markup → HTML of the editor subset. Collects cited keys.
function _draftingLatexInlineToHtml(source, keys) {
  const s = String(source || '');
  let i = 0;
  let out = '';
  const esc = _draftingEscapeHtmlText;
  const inner = (raw) => _draftingLatexInlineToHtml(raw, keys);
  const skipSpaces = () => { while (i < s.length && /[ \t\n]/.test(s[i])) i += 1; };
  const readGroup = () => {
    skipSpaces();
    if (s[i] !== '{') return null;
    const start = i;
    let depth = 0;
    for (; i < s.length; i += 1) {
      if (s[i] === '\\') { i += 1; continue; }
      if (s[i] === '{') depth += 1;
      else if (s[i] === '}') {
        depth -= 1;
        if (depth === 0) {
          i += 1;
          return s.slice(start + 1, i - 1);
        }
      }
    }
    return s.slice(start + 1);
  };
  const readOptional = () => {
    const saved = i;
    skipSpaces();
    if (s[i] !== '[') {
      i = saved;
      return null;
    }
    const end = s.indexOf(']', i);
    if (end < 0) {
      i = saved;
      return null;
    }
    const value = s.slice(i + 1, end);
    i = end + 1;
    return value;
  };
  const applyAccent = (mark) => {
    let base = '';
    if (s[i] === '{') base = readGroup() || '';
    else if (s[i] === '\\' && s[i + 1] === 'i' && !/[A-Za-z]/.test(s[i + 2] || '')) { base = 'i'; i += 2; }
    else if (i < s.length) { base = s[i]; i += 1; }
    base = base.replace(/^\\i$/, 'i');
    if (!base) return;
    out += esc((base[0] + DRAFTING_LATEX_ACCENTS[mark] + base.slice(1)).normalize('NFC'));
  };
  const verbatimUntil = (closer, openLength) => {
    const end = s.indexOf(closer, i + openLength);
    if (end < 0) {
      out += esc(s.slice(i, i + openLength));
      i += openLength;
      return;
    }
    out += esc(s.slice(i, end + closer.length));
    i = end + closer.length;
  };
  const command = () => {
    i += 1;
    if (i >= s.length) return;
    const ch = s[i];
    if (!/[A-Za-z]/.test(ch)) {
      if (ch === '(' || ch === '[') {
        i -= 1;
        verbatimUntil(ch === '(' ? '\\)' : '\\]', 2);
        return;
      }
      i += 1;
      if (ch === '\\') {
        readOptional();
        out += '<br>';
      } else if (DRAFTING_LATEX_ACCENTS[ch]) {
        applyAccent(ch);
      } else if ('%&_#${}'.includes(ch)) {
        out += esc(ch);
      } else if (' ,;:\n'.includes(ch)) {
        out += ' ';
      } else if (ch !== '-' && ch !== '/' && ch !== '!') {
        out += esc(ch);
      }
      return;
    }
    let name = '';
    while (i < s.length && /[A-Za-z]/.test(s[i])) { name += s[i]; i += 1; }
    if (s[i] === '*') i += 1;
    if (name.length === 1 && DRAFTING_LATEX_ACCENTS[name]) {
      skipSpaces();
      applyAccent(name);
      return;
    }
    if (DRAFTING_LATEX_CITE_RE.test(name)) {
      readOptional();
      readOptional();
      const raw = readGroup();
      const cited = String(raw || '').split(',').map((key) => key.trim()).filter(Boolean);
      cited.forEach((key) => keys.add(key));
      if (cited.length) out += `<bb-cite data-keys="${esc(cited.join(','))}"></bb-cite>`;
      return;
    }
    if (Object.prototype.hasOwnProperty.call(DRAFTING_LATEX_DROP_ARGS, name)) {
      readOptional();
      for (let n = 0; n < DRAFTING_LATEX_DROP_ARGS[name]; n += 1) readGroup();
      return;
    }
    if (['emph', 'textit', 'textsl'].includes(name)) {
      out += `<em>${inner(readGroup())}</em>`;
      return;
    }
    if (name === 'textbf') {
      out += `<strong>${inner(readGroup())}</strong>`;
      return;
    }
    if (name === 'paragraph' || name === 'subparagraph') {
      readOptional();
      out += `<strong>${inner(readGroup())}</strong> `;
      return;
    }
    if (name === 'footnote') {
      readOptional();
      out += ` (${inner(readGroup())})`;
      return;
    }
    if (['ref', 'eqref', 'cref', 'Cref', 'autoref', 'pageref', 'vref'].includes(name)) {
      out += esc(`[${readGroup() || ''}]`);
      return;
    }
    if (name === 'url') {
      out += esc(readGroup() || '');
      return;
    }
    if (name === 'href') {
      readGroup();
      out += inner(readGroup());
      return;
    }
    if (Object.prototype.hasOwnProperty.call(DRAFTING_LATEX_TEXT, name)) {
      const value = DRAFTING_LATEX_TEXT[name];
      out += value === '\n' ? '<br>' : esc(value);
      if (s.startsWith('{}', i)) i += 2;
      else while (s[i] === ' ' || s[i] === '\t') i += 1;
      return;
    }
    if (name === 'begin' || name === 'end') {
      readGroup();
      return;
    }
    // Unknown command: keep the text of its argument, drop the command.
    readOptional();
    const raw = readGroup();
    if (raw !== null) out += inner(raw);
  };

  while (i < s.length) {
    const c = s[i];
    if (c === '\\') { command(); continue; }
    if (c === '{') {
      const raw = readGroup() || '';
      const switchMatch = /^\s*\\(em|it|itshape|sl|slshape|bf|bfseries)(?![A-Za-z])\s*/.exec(raw);
      if (switchMatch) {
        const body = inner(raw.slice(switchMatch[0].length));
        out += /^b/.test(switchMatch[1]) ? `<strong>${body}</strong>` : `<em>${body}</em>`;
      } else {
        out += inner(raw);
      }
      continue;
    }
    if (c === '}') { i += 1; continue; }
    if (c === '$') { verbatimUntil(s[i + 1] === '$' ? '$$' : '$', s[i + 1] === '$' ? 2 : 1); continue; }
    if (c === '~') { out += ' '; i += 1; continue; }
    if (s.startsWith('---', i)) { out += '—'; i += 3; continue; }
    if (s.startsWith('--', i)) { out += '–'; i += 2; continue; }
    if (s.startsWith('``', i)) { out += '“'; i += 2; continue; }
    if (s.startsWith("''", i)) { out += '”'; i += 2; continue; }
    if (c === '`') { out += '‘'; i += 1; continue; }
    if (c === '\n') { out += ' '; i += 1; continue; }
    out += esc(c);
    i += 1;
  }
  return out;
}

// LaTeX source (a whole document or a passage) → { html, keys }.
function _draftingLatexToHtml(text) {
  let src = String(text || '').replace(/\r\n?/g, '\n');
  src = src.replace(/(^|[^\\])%[^\n]*/g, '$1');
  const body = /\\begin\s*\{document\}([\s\S]*?)(?:\\end\s*\{document\}|$)/.exec(src);
  if (body) src = body[1];
  src = src.replace(/\\begin\s*\{(figure|table|thebibliography|tikzpicture)(\*?)\}[\s\S]*?\\end\s*\{\1\2\}/g, '');
  src = src.replace(/\\(?:maketitle|tableofcontents|printbibliography|noindent|centering|newpage|clearpage)\b/g, '');

  const keys = new Set();
  const inline = (value) => _draftingLatexInlineToHtml(value, keys);
  const parts = [];
  const pushParagraphs = (chunk) => {
    const cleaned = chunk.replace(/\\(?:begin|end)\s*\{[^}]*\}(?:\[[^\]]*\])?/g, '\n\n');
    cleaned.split(/\n[ \t]*\n+/).forEach((paragraph) => {
      const html = inline(paragraph.trim()).trim();
      if (html.replace(/<(?!bb-cite)[^>]*>/g, '').trim()) parts.push(`<p>${html}</p>`);
    });
  };
  const readBalanced = (from) => {
    let depth = 0;
    for (let j = from; j < src.length; j += 1) {
      if (src[j] === '\\') { j += 1; continue; }
      if (src[j] === '{') depth += 1;
      else if (src[j] === '}') {
        depth -= 1;
        if (depth === 0) return [src.slice(from + 1, j), j + 1];
      }
    }
    return [src.slice(from + 1), src.length];
  };

  const structure = /\\(chapter|section|subsection|subsubsection)\*?\s*(?:\[[^\]]*\])?\s*\{|\\begin\s*\{(itemize|enumerate|description)\}/g;
  let last = 0;
  let match;
  while ((match = structure.exec(src))) {
    pushParagraphs(src.slice(last, match.index));
    if (match[1]) {
      const level = { chapter: 1, section: 1, subsection: 2, subsubsection: 3 }[match[1]];
      const [content, end] = readBalanced(structure.lastIndex - 1);
      const html = inline(content).trim();
      if (html) parts.push(`<h${level}>${html}</h${level}>`);
      last = end;
    } else {
      const envs = /\\(begin|end)\s*\{(itemize|enumerate|description)\}/g;
      envs.lastIndex = structure.lastIndex;
      let depth = 1;
      let closing = null;
      let env;
      while ((env = envs.exec(src))) {
        depth += env[1] === 'begin' ? 1 : -1;
        if (depth === 0) { closing = env; break; }
      }
      const innerEnd = closing ? closing.index : src.length;
      const content = src.slice(structure.lastIndex, innerEnd)
        .replace(/\\(?:begin|end)\s*\{(?:itemize|enumerate|description)\}/g, '');
      const items = content.split(/\\item\b/).slice(1).map((item) => {
        let value = item;
        let label = '';
        const labelMatch = /^\s*\[([^\]]*)\]/.exec(value);
        if (labelMatch) {
          label = `<strong>${inline(labelMatch[1]).trim()}</strong> `;
          value = value.slice(labelMatch[0].length);
        }
        const html = inline(value.trim()).trim();
        return html || label ? `<li>${label}${html}</li>` : '';
      }).filter(Boolean);
      const tag = match[2] === 'enumerate' ? 'ol' : 'ul';
      if (items.length) parts.push(`<${tag}>${items.join('')}</${tag}>`);
      last = closing ? envs.lastIndex : src.length;
    }
    structure.lastIndex = last;
  }
  pushParagraphs(src.slice(last));
  return { html: parts.join(''), keys };
}

// Cited keys → citation records: first from the draft itself, then from the
// library. Unknown keys are left out of the map.
async function _draftingResolveCitationKeys(keys) {
  const resolved = new Map();
  const known = new Map();
  state.drafting.citationIndex.forEach((record) => {
    [record.citation_id, _draftingLatexKey(record)].forEach((key) => {
      if (key) known.set(String(key).toLowerCase(), record);
    });
  });
  const missing = [];
  keys.forEach((key) => {
    const record = known.get(String(key).toLowerCase());
    if (record) resolved.set(key, record);
    else missing.push(key);
  });
  if (!missing.length) return resolved;
  try {
    const response = await fetch('/api/drafting-resolve-citations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keys: missing.slice(0, 500) }),
    });
    const payload = await response.json();
    if (response.ok && payload?.records && typeof payload.records === 'object') {
      Object.entries(payload.records).forEach(([key, record]) => {
        if (record && typeof record === 'object') resolved.set(key, _draftingBuildCitationRecord(record));
      });
    }
  } catch (error) {
    console.warn('Citation keys could not be resolved; they stay as imported keys.', error);
  }
  return resolved;
}

async function _draftingPasteLatex(text, range) {
  const { html, keys } = _draftingLatexToHtml(text);
  const citations = keys.size ? await _draftingResolveCitationKeys(Array.from(keys)) : new Map();
  const fragment = _draftingSanitizeRichHtml(html, { citations });
  if (!_draftingNodeHasContent(fragment)) {
    _draftingInsertPlainText(text, range);
    return;
  }
  _draftingInsertRichFragment(fragment, range);
  const found = Array.from(keys).filter((key) => citations.has(key)).length;
  const missing = keys.size - found;
  if (!keys.size) {
    showToast(_tr('LaTeX importé.'), 2600, 'success');
  } else if (!missing) {
    showToast(`${_tr('LaTeX importé :')} ${found} ${_tr(found > 1 ? 'citations reconnues.' : 'citation reconnue.')}`, 3200, 'success');
  } else {
    showToast(
      `${_tr('LaTeX importé :')} ${missing} ${_tr(missing > 1 ? 'clés absentes de la bibliothèque, conservées telles quelles.' : 'clé absente de la bibliothèque, conservée telle quelle.')}`,
      5200,
      'info',
    );
  }
}

/* ── Editor → Markdown / LaTeX / Word ───────────────────────────────── */

function _draftingWrapMarkdown(text, marker) {
  return String(text || '').split('\n').map((line) => {
    const match = /^(\s*)([\s\S]*?)(\s*)$/.exec(line);
    if (!match[2]) return line;
    return `${match[1]}${marker}${match[2]}${marker}${match[3]}`;
  }).join('\n');
}

function _draftingMarkdownInline(node, ctx) {
  if (node.nodeType === Node.TEXT_NODE) return String(node.nodeValue || '').replace(/\u00a0/g, ' ');
  if (node.nodeType !== Node.ELEMENT_NODE && node.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) return '';
  const element = /** @type {HTMLElement} */ (node);
  if (element.classList?.contains('drafting-citation-token')) {
    if (element.dataset?.preview === '1') return '';
    if (ctx.citations === 'latex') {
      const record = _draftingGetCitationRecordFromToken(element);
      const key = record ? _draftingLatexKey(record) : '';
      return key ? `${DRAFTING_CITE_OPEN}${key}${DRAFTING_CITE_CLOSE}` : '';
    }
    return _draftingGetCitationTokenCopyLabel(element);
  }
  if (element.nodeName === 'BR') return '\n';
  if (element.classList?.contains('drafting-preview-controls') || element.classList?.contains('drafting-argument-preview')) return '';
  const own = element.nodeType === Node.ELEMENT_NODE ? _draftingInlineFormat(element) : { bold: null, italic: null };
  const bold = own.bold === null ? ctx.bold : own.bold;
  const italic = own.italic === null ? ctx.italic : own.italic;
  const childCtx = { ...ctx, bold, italic };
  let text = Array.from(element.childNodes).map((child) => _draftingMarkdownInline(child, childCtx)).join('');
  if (DRAFTING_BLOCK_TAG_RE.test(element.nodeName)) text = `\n${text}\n`;
  if (italic && !ctx.italic) text = _draftingWrapMarkdown(text, '*');
  if (bold && !ctx.bold) text = _draftingWrapMarkdown(text, '**');
  return text;
}

function _draftingMarkdownListLines(list, ctx) {
  const lines = [];
  let number = 1;
  const ordered = list.nodeName === 'OL';
  Array.from(list.children).forEach((item) => {
    const nested = [];
    const text = Array.from(item.childNodes).map((child) => {
      if (child.nodeName === 'UL' || child.nodeName === 'OL') {
        nested.push(..._draftingMarkdownListLines(child, ctx));
        return '';
      }
      return _draftingMarkdownInline(child, ctx);
    }).join('').replace(/\s*\n\s*/g, ' ').trim();
    if (text) {
      lines.push(`${ordered ? `${number}.` : '-'} ${text}`);
      number += 1;
    }
    lines.push(...nested);
  });
  return lines;
}

function _draftingMarkdownBlocks(root, ctx) {
  const blocks = [];
  let loose = '';
  const flush = () => {
    if (loose.trim()) blocks.push(loose);
    loose = '';
  };
  Array.from(root.childNodes).forEach((node) => {
    const tag = node.nodeName;
    const heading = /^H([1-6])$/.exec(tag);
    if (heading) {
      flush();
      const text = Array.from(node.childNodes)
        .map((child) => _draftingMarkdownInline(child, { ...ctx, bold: true }))
        .join('').replace(/\s*\n\s*/g, ' ').trim();
      if (text) blocks.push(`${'#'.repeat(Math.min(3, Number(heading[1])))} ${text}`);
      return;
    }
    if (tag === 'UL' || tag === 'OL') {
      flush();
      blocks.push(_draftingMarkdownListLines(node, ctx).join('\n'));
      return;
    }
    if (node.nodeType === Node.ELEMENT_NODE && DRAFTING_BLOCK_TAG_RE.test(tag)
      && !node.classList.contains('drafting-citation-token')) {
      flush();
      const hasBlockChild = Array.from(node.children).some((child) => DRAFTING_BLOCK_TAG_RE.test(child.nodeName));
      if (hasBlockChild) blocks.push(..._draftingMarkdownBlocks(node, ctx));
      else blocks.push(_draftingMarkdownInline(node, ctx));
      return;
    }
    loose += _draftingMarkdownInline(node, ctx);
  });
  flush();
  return blocks;
}

// citations: 'label' writes the visible "[n]", 'latex' writes key markers.
function _draftingSerializeMarkdown(root, { citations = 'label' } = {}) {
  if (!root) return '';
  return _draftingMarkdownBlocks(root, { bold: false, italic: false, citations })
    .map((block) => block.replace(/\r\n/g, '\n').replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]+/g, '\n').trim())
    .filter(Boolean)
    .join('\n\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

function _draftingBibliographyLines() {
  return state.drafting.citationOrder
    .map((citationId, index) => {
      const record = state.drafting.citationIndex.get(citationId);
      return record ? _draftingFormatBibliographyEntry(record, _draftingGetDisplayNumberFromIndex(index)) : '';
    })
    .filter(Boolean);
}

// HTML for word processors (no app styles, so no white-on-white text in
// Word) and the usual plain text.
function _draftingBuildClipboardPayload(root, { withBibliography = false } = {}) {
  const container = document.createElement('div');
  container.appendChild(_draftingSanitizeNode(root, { tokens: 'export' }));
  let html = container.innerHTML;
  let text = _draftingExtractCopyText(root);
  if (withBibliography) {
    const lines = _draftingBibliographyLines();
    if (lines.length) {
      const heading = _tr('Bibliographie');
      html += `<div data-drafting-bibliography="1"><h2>${_draftingEscapeHtmlText(heading)}</h2>`
        + lines.map((line) => `<p>${_draftingEscapeHtmlText(line)}</p>`).join('')
        + '</div>';
      text += `\n\n${heading}\n${lines.join('\n')}`;
    }
  }
  return { html, text };
}

async function _draftingWriteRichClipboard(html, text) {
  // A copy event writes text/html as is and needs no clipboard permission.
  let written = false;
  const onCopy = (event) => {
    event.clipboardData.setData('text/html', html);
    event.clipboardData.setData('text/plain', text);
    event.preventDefault();
    event.stopImmediatePropagation();
    written = true;
  };
  document.addEventListener('copy', onCopy, true);
  try { document.execCommand('copy'); } catch (e) {}
  document.removeEventListener('copy', onCopy, true);
  if (written) return true;
  try {
    if (navigator.clipboard?.write && typeof ClipboardItem !== 'undefined') {
      await navigator.clipboard.write([new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([text], { type: 'text/plain' }),
      })]);
      return true;
    }
  } catch (e) {}
  return copyTextWithFallback(text);
}

function _draftingOnEditorCopy(event) {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0 || selection.isCollapsed || !event.clipboardData) return;
  const range = selection.getRangeAt(0);
  if (!_draftingRangePointsIntoEditor(range)) return;
  const { html, text } = _draftingBuildClipboardPayload(range.cloneContents());
  if (!text && !html) return;
  event.clipboardData.setData('text/html', html);
  event.clipboardData.setData('text/plain', text);
  event.preventDefault();
  // execCommand keeps the cut in the editor's undo history.
  if (event.type === 'cut') document.execCommand('delete');
}

/* ── Copy for Word with live Zotero citations ───────────────────────────
 * Each citation becomes a Word field "ADDIN ZOTERO_ITEM CSL_CITATION {json}",
 * written in the <!--[if supportFields]--> comments Word reads from pasted
 * HTML (the mirror of the import above). The Zotero Word plugin then treats
 * them as its own citations: it finds the item through its URI, or falls
 * back to the CSL item data stored in the field. */

const DRAFTING_CSL_CITATION_SCHEMA = 'https://github.com/citation-style-language/schema/raw/master/csl-citation.json';

// CSL item data from an editor citation record, for items the library
// lookup does not know.
function _draftingCslItemFromRecord(record) {
  const data = { type: record.journal ? 'article-journal' : 'article', title: record.title || record.label || record.citation_id };
  const display = String(record.authors_display || '');
  const names = /\s+and\s+|;\s*/.test(display)
    ? display.split(/\s+and\s+|;\s*/)
    : [record.author || display];
  const authors = names.map((name) => String(name || '').trim()).filter(Boolean).map((name) => {
    const [family, ...given] = name.split(',');
    return given.length && family.trim()
      ? { family: family.trim(), given: given.join(',').trim() }
      : { literal: name };
  });
  if (authors.length) data.author = authors;
  if (record.journal) data['container-title'] = record.journal;
  if (/^\d{4}$/.test(String(record.year || '').trim())) data.issued = { 'date-parts': [[Number(record.year)]] };
  if (record.doi) data.DOI = record.doi;
  return data;
}

function _draftingRandomCitationId() {
  const alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let id = '';
  for (let i = 0; i < 8; i += 1) id += alphabet[Math.floor(Math.random() * alphabet.length)];
  return id;
}

// Field code for a group of adjacent citation tokens; zoteroItems maps
// citation ids to the { uris, itemData } the server resolved from the library.
function _draftingZoteroFieldCode(tokens, shown, itemIds, zoteroItems) {
  const citationItems = tokens.map((token) => {
    const citationId = token.dataset.citationId;
    const itemId = itemIds.get(citationId);
    const resolved = zoteroItems.get(citationId);
    const record = _draftingBuildCitationRecord(_draftingGetCitationRecordFromToken(token));
    const itemData = { id: itemId, ...(resolved?.itemData || _draftingCslItemFromRecord(record)) };
    return { id: itemId, uris: Array.isArray(resolved?.uris) ? resolved.uris : [], itemData };
  });
  const payload = {
    citationID: _draftingRandomCitationId(),
    // plainCitation must match the displayed text, or Zotero asks whether
    // the citation was edited by hand.
    properties: { formattedCitation: shown, plainCitation: shown, noteIndex: 0 },
    citationItems,
    schema: DRAFTING_CSL_CITATION_SCHEMA,
  };
  return `ADDIN ZOTERO_ITEM CSL_CITATION ${JSON.stringify(payload)}`;
}

// Comments are copied verbatim into the clipboard HTML; Word decodes the
// entities, and escaping ">" keeps "-->" out of the code.
function _draftingEscapeFieldCode(code) {
  return String(code).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// HTML + text for Word from editor content, citations as Zotero fields.
function _draftingBuildWordZoteroPayload(root, zoteroItems = new Map()) {
  const container = document.createElement('div');
  container.appendChild(_draftingSanitizeNode(root, { tokens: 'export' }));
  // Adjacent tokens ("[1" + ",2]") form one citation with several items.
  const groups = [];
  container.querySelectorAll('.drafting-citation-token').forEach((token) => {
    if (!token.dataset.citationId) return;
    const last = groups[groups.length - 1];
    if (last && token.previousSibling === last[last.length - 1]) last.push(token);
    else groups.push([token]);
  });
  const itemIds = new Map();
  groups.forEach((tokens) => tokens.forEach((token) => {
    if (!itemIds.has(token.dataset.citationId)) itemIds.set(token.dataset.citationId, itemIds.size + 1);
  }));
  groups.forEach((tokens) => {
    const shown = tokens.map((token) => token.textContent).join('');
    const code = _draftingZoteroFieldCode(tokens, shown, itemIds, zoteroItems);
    const begin = document.createComment(
      `[if supportFields]><span style='mso-element:field-begin'></span><span style='mso-spacerun:yes'> </span>${_draftingEscapeFieldCode(code)} <span style='mso-element:field-separator'></span><![endif]`,
    );
    const end = document.createComment("[if supportFields]><span style='mso-element:field-end'></span><![endif]");
    tokens[0].replaceWith(begin, document.createTextNode(shown), end);
    tokens.slice(1).forEach((token) => token.remove());
  });
  const fields = groups.length;
  return { html: container.innerHTML, text: _draftingExtractCopyText(root), fields };
}

// Citation ids → { uris, itemData } from the library. Missing ids are left
// out; their field is built from the editor record.
async function _draftingFetchWordCitationItems(ids) {
  const items = new Map();
  if (!ids.length) return items;
  try {
    const response = await fetch('/api/drafting-word-citations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keys: ids.slice(0, 500) }),
    });
    const payload = await response.json();
    if (response.ok && payload?.items && typeof payload.items === 'object') {
      Object.entries(payload.items).forEach(([key, item]) => {
        if (item && typeof item === 'object') items.set(key, item);
      });
    }
  } catch (e) {}
  return items;
}

async function copyDraftingSelectionForWord(range) {
  if (!range || range.collapsed || !_draftingRangePointsIntoEditor(range)) return false;
  const content = range.cloneContents();
  const ids = Array.from(new Set(Array.from(content.querySelectorAll('.drafting-citation-token'))
    .filter((token) => token.dataset.preview !== '1')
    .map((token) => token.dataset.citationId)
    .filter(Boolean)));
  const items = await _draftingFetchWordCitationItems(ids);
  const { html, text, fields } = _draftingBuildWordZoteroPayload(content, items);
  if (!html && !text) return false;
  const copied = await _draftingWriteRichClipboard(html, text);
  if (!copied) {
    showToast(_tr('Copie impossible.'), 3200, 'error');
    return false;
  }
  const inLibrary = ids.filter((id) => items.get(id)?.uris?.length).length;
  let message = _tr('Copié pour Word : collez dans Word, puis Zotero → Refresh.');
  if (fields && inLibrary < ids.length) {
    message += ` ${_tr('Certaines références ne sont pas liées à votre bibliothèque Zotero.')}`;
  }
  showToast(message, 5200, 'success');
  return true;
}

function _draftingBuildBibtexFile(records) {
  const seen = new Set();
  const entries = [];
  const clean = (value) => String(value || '').replace(/[{}]/g, '').replace(/\s+/g, ' ').trim();
  records.forEach((record) => {
    if (record?.support_status === 'unresolved') return;
    const key = _draftingLatexKey(record);
    if (!key || seen.has(key)) return;
    seen.add(key);
    const bibtex = String(record.bibtex || '').trim();
    if (bibtex) {
      entries.push(bibtex);
      return;
    }
    const fields = [
      ['author', clean(record.authors_display || record.author)],
      ['title', clean(record.title || record.label)],
      ['journal', clean(record.journal)],
      ['year', clean(record.year)],
      ['doi', clean(record.doi)],
    ].filter(([, value]) => value);
    entries.push(`@misc{${key},\n${fields.map(([name, value]) => `  ${name} = {${value}}`).join(',\n')}\n}`);
  });
  return entries.length ? `${entries.join('\n\n')}\n` : '';
}

function downloadDraftingBibtex() {
  const content = _draftingBuildBibtexFile(_draftingCollectBibliographyRecords());
  if (!content) {
    showToast(_tr('Aucune référence à exporter.'));
    return;
  }
  const url = URL.createObjectURL(new Blob([content], { type: 'application/x-bibtex;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'references.bib';
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  showToast(_tr('Fichier BibTeX généré.'), 2600, 'success');
}

/* ── Formatting toolbar ─────────────────────────────────────────────── */

let _draftingFormatRange = null;

function _draftingSelectionInEditor() {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount === 0) return null;
  const range = selection.getRangeAt(0);
  return _draftingRangePointsIntoEditor(range) ? range : null;
}

function _draftingSyncFormatToolbar() {
  const toolbar = document.getElementById('drafting-format-toolbar');
  const range = _draftingSelectionInEditor();
  if (!toolbar || !range) return;
  _draftingFormatRange = range.cloneRange();
  const queries = { bold: 'bold', italic: 'italic', ul: 'insertUnorderedList', ol: 'insertOrderedList' };
  toolbar.querySelectorAll('[data-drafting-format]').forEach((button) => {
    const query = queries[button.dataset.draftingFormat];
    if (!query) return;
    let active = false;
    try { active = document.queryCommandState(query); } catch (e) {}
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  const select = document.getElementById('drafting-format-block');
  if (select) {
    const editor = _draftingGetEditor();
    let node = range.startContainer;
    let value = 'p';
    while (node && node !== editor) {
      if (/^H[1-3]$/.test(node.nodeName)) { value = node.nodeName.toLowerCase(); break; }
      node = node.parentNode;
    }
    if (select.value !== value) select.value = value;
  }
}

// Nearest heading or list around the caret ('H2', 'UL', ...), or ''.
function _draftingCurrentBlockTag() {
  const editor = _draftingGetEditor();
  const range = _draftingSelectionInEditor();
  let node = range?.startContainer || null;
  while (node && node !== editor) {
    if (/^(H[1-6]|UL|OL)$/.test(node.nodeName)) return node.nodeName;
    node = node.parentNode;
  }
  return '';
}

function _draftingApplyFormat(format, value = '') {
  const editor = _draftingGetEditor();
  const selection = window.getSelection();
  if (!editor || !selection) return;
  if (!_draftingSelectionInEditor()) {
    let range = _draftingFormatRange && _draftingRangePointsIntoEditor(_draftingFormatRange) ? _draftingFormatRange : null;
    if (!range) {
      range = document.createRange();
      range.selectNodeContents(editor);
      range.collapse(false);
    }
    selection.removeAllRanges();
    selection.addRange(range);
  }
  editor.focus();
  const commands = { bold: 'bold', italic: 'italic', ul: 'insertUnorderedList', ol: 'insertOrderedList' };
  // Chrome nests a list inside a heading (and a heading inside a list item):
  // leave the one structure before applying the other.
  const container = _draftingCurrentBlockTag();
  if (format === 'block') {
    if (container === 'UL' || container === 'OL') {
      document.execCommand(container === 'UL' ? 'insertUnorderedList' : 'insertOrderedList');
    }
    document.execCommand('formatBlock', false, ['h1', 'h2', 'h3'].includes(value) ? value : 'p');
  } else if (format === 'ul' || format === 'ol') {
    if (/^H[1-6]$/.test(container)) document.execCommand('formatBlock', false, 'p');
    document.execCommand(commands[format]);
  } else if (commands[format]) {
    document.execCommand(commands[format]);
  }
  _draftingSyncFormatToolbar();
}

function initDraftingInterop() {
  const editor = _draftingGetEditor();
  if (!editor || editor.dataset.interopInitialized === '1') return;
  editor.dataset.interopInitialized = '1';
  try { document.execCommand('defaultParagraphSeparator', false, 'p'); } catch (e) {}
  editor.addEventListener('copy', _draftingOnEditorCopy);
  editor.addEventListener('cut', _draftingOnEditorCopy);
  document.addEventListener('selectionchange', _draftingSyncFormatToolbar);

  const toolbar = document.getElementById('drafting-format-toolbar');
  if (toolbar) {
    // Buttons must not take the focus, or the selection they apply to is lost.
    toolbar.addEventListener('mousedown', (event) => {
      if (event.target.closest('button')) event.preventDefault();
    });
    toolbar.addEventListener('click', (event) => {
      const button = event.target.closest('[data-drafting-format]');
      if (button) _draftingApplyFormat(button.dataset.draftingFormat);
    });
  }
  document.getElementById('drafting-format-block')?.addEventListener('change', (event) => {
    _draftingApplyFormat('block', event.target.value);
  });
  document.getElementById('drafting-clear-btn')?.addEventListener('click', () => clearDraftingEditor());
  document.getElementById('drafting-export-bibtex')?.addEventListener('click', () => exportDraftingDocument('bibtex'));
}
