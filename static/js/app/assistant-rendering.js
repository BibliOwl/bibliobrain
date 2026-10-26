// ---------------------------------------------
//  UTILS
// ---------------------------------------------
function _looksLikeSafeInlineMath(content) {
  const value = String(content || '').trim();
  if (!value) return false;
  if (value.length > 120) return false;
  if (/\[(?:S\d+|[A-Za-z0-9:_\-.]+:\d+)\]/.test(value)) return false;
  if (/[.!?]/.test(value)) return false;
  const longWordCount = (value.match(/\b[\p{L}]{4,}\b/gu) || []).length;
  if (longWordCount >= 5) return false;
  return /\\[A-Za-z]+|[_^{}=<>+\-*/]|(?:\d+\s*[A-Za-z])/.test(value);
}

function _normalizeSingleDollarMathInText(text) {
  let value = String(text || '');
  value = value.replace(
    /(^|[^\\])\(([^()\n]{1,120})\)/g,
    (match, prefix, inner) => {
      if (!_looksLikeSafeInlineMath(inner)) return match;
      return `${prefix}\\(${String(inner).trim()}\\)`;
    },
  );
  // Convert double parentheses ((...)) to \(...\) for KaTeX.
  // Some LLMs emit inline math as ((\rho_{eq})) instead of \(\rho_{eq}\).
  // Only convert when the inner content looks like math (contains backslash commands, subscripts/superscripts, etc.).
  value = value.replace(
    /(?<!=)\(\((?:(?!\)\)).)+?\)\)(?!\))/g,
    (match) => {
      // Extract inner content by stripping outer (( and ))
      const inner = match.slice(2, -2).trim();
      if (!_looksLikeSafeInlineMath(inner)) return match;
      return `\\(${inner}\\)`;
    },
  );
  if (!value.includes('$')) return value;
  const normalizedParenthesized = value.replace(
    /(^|[^\\$])\(\$(?!\$)([^$\n]{1,400}?)\$\)/g,
    (match, prefix, inner) => {
      if (!_looksLikeSafeInlineMath(inner)) return match;
      return `${prefix}\\(${String(inner).trim()}\\)`;
    },
  );
  return normalizedParenthesized.replace(
    /(^|[^\\$])\$(?!\$)([^$\n]{1,400}?)\$(?!\$)/g,
    (match, prefix, inner) => {
      if (!_looksLikeSafeInlineMath(inner)) return match;
      return `${prefix}\\(${String(inner).trim()}\\)`;
    },
  );
}

function _convertHtmlSubSupToLatex(text) {
  let value = String(text || '');
  // Convert <sub>...</sub> to LaTeX subscript _{...}
  value = value.replace(/<sub>(.*?)<\/sub>/gi, '_{$1}');
  // Convert <sup>...</sup> to LaTeX superscript ^{...}
  value = value.replace(/<sup>(.*?)<\/sup>/gi, '^{$1}');

  // Iteratively wrap bare _{}/^{} patterns in $...$ (math mode)
  // Repeat until stable so chained sub/sup like I_c^2 are all captured
  let prev;
  do {
    prev = value;
    value = value.replace(
      /([A-Za-z\u0391-\u03C9\u03D0-\u03FF]+)([_^]\{[^}]*\})/g,
      function (match, base, sub) {
        const idx = value.indexOf(match);
        if (idx < 0) return '$' + base + sub + '$';
        // Skip if already inside a $...$ block (odd number of $ before)
        const dollarsBefore = (value.slice(0, idx).match(/\$/g) || []).length;
        if (dollarsBefore % 2 === 1) return match;
        return '$' + base + sub + '$';
      }
    );
  } while (value !== prev);

  // Merge trailing _{} or ^{} right after a $...$ block into the math block
  // e.g. $I_{c}$^{2}$ -> $I_{c}^{2}$
  prev = null;
  while (prev !== value) {
    prev = value;
    value = value.replace(
      /(\$[^$]+\$)([_^]\{[^}]*\})/g,
      function (m, mathBlock, trailing) {
        return '$' + mathBlock.slice(1, -1) + trailing + '$';
      }
    );
  }

  return value;
}

// Delimited math spans: $$...$$, \(...\) and \[...\] (not preceded by a
// literal backslash). Single-dollar math is left to renderMath(), which
// normalizes text nodes after Markdown parsing.
const _ASSISTANT_MATH_SPAN_RE = /(?<!\\)(?:\$\$[\s\S]+?\$\$|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\])/g;
const _MATH_PLACEHOLDER_RE = /(\d+)/g;

function parseAssistantMarkdown(source) {
  // Markdown must never see LaTeX: Marked treats every backslash before
  // punctuation as an escape (\( \) \, \{ \\ ...) and the fallback parser
  // would need the opposite convention. Swap each math span for an opaque
  // placeholder, parse the Markdown around it, then put the span back
  // verbatim (HTML-escaped) for KaTeX.
  const spans = [];
  const shielded = String(source || '').replace(_ASSISTANT_MATH_SPAN_RE, (span) => {
    spans.push(span);
    return `${spans.length - 1}`;
  });
  const html = parseMarkdown(_convertHtmlSubSupToLatex(shielded));
  if (!spans.length) return html;
  return html.replace(_MATH_PLACEHOLDER_RE, (match, index) => {
    const span = spans[Number(index)];
    return span == null ? match : escapeHtml(span);
  });
}

function renderMath(el, options = {}) {
  if (!el) return;
  if (options.normalizeSingleDollar) {
    const walker = document.createTreeWalker(
      el,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode(node) {
          const text = String(node?.nodeValue || '');
          if (!text.includes('$')) return NodeFilter.FILTER_REJECT;
          const parent = node.parentElement;
          if (!parent) return NodeFilter.FILTER_REJECT;
          if (parent.closest('code, pre')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      },
    );
    const textNodes = [];
    let currentNode = walker.nextNode();
    while (currentNode) {
      textNodes.push(currentNode);
      currentNode = walker.nextNode();
    }
    textNodes.forEach((node) => {
      const normalized = _normalizeSingleDollarMathInText(node.nodeValue || '');
      if (normalized !== node.nodeValue) node.nodeValue = normalized;
    });
  }
  const text = el.textContent || '';
  const looksLikeMath = /(\$\$[\s\S]+?\$\$)|(\\\([\s\S]+?\\\))|(\\\[[\s\S]+?\\\])/.test(text);
  if (looksLikeMath && !window._katexReady) {
    loadOptionalFrontendDeps();
  }
  if (!window._katexReady || typeof window.renderMathInElement !== 'function') {
    el.dataset.pendingMath = '1';
    return;
  }
  delete el.dataset.pendingMath;
  renderMathInElement(el, {
    delimiters: [
      { left: '$$', right: '$$', display: true  },
      { left: '\\(', right: '\\)', display: false },
      { left: '\\[', right: '\\]', display: true  },
    ],
    throwOnError: false,
  });
}
function _flushPendingMath() {
  document.querySelectorAll('[data-pending-math="1"]').forEach((el) => renderMath(el));
}
function _loadExternalStyleOnce(id, href) {
  if (document.getElementById(id)) return;
  const link = document.createElement('link');
  link.id = id;
  link.rel = 'stylesheet';
  link.href = href;
  link.media = 'print';
  link.onload = () => { link.media = 'all'; };
  document.head.appendChild(link);
}
function _loadExternalScriptOnce(id, src, onLoad) {
  const existing = document.getElementById(id);
  if (existing) {
    if (existing.dataset.loaded === '1') onLoad?.();
    else if (onLoad) existing.addEventListener('load', onLoad, { once: true });
    return;
  }
  const script = document.createElement('script');
  script.id = id;
  script.src = src;
  script.async = true;
  if (onLoad) script.addEventListener('load', onLoad, { once: true });
  script.addEventListener('load', () => {
    script.dataset.loaded = '1';
  }, { once: true });
  script.addEventListener('error', () => {
    script.dataset.loaded = 'error';
  }, { once: true });
  document.head.appendChild(script);
}
function loadOptionalFrontendDeps() {
  _loadExternalScriptOnce(
    'marked-cdn',
    '/static/vendor/marked/marked.umd.js',
  );
  _loadExternalScriptOnce(
    'dompurify-cdn',
    '/static/vendor/dompurify/purify.min.js',
  );
  _loadExternalStyleOnce(
    'katex-css-cdn',
    '/static/vendor/katex/katex.min.css',
  );
  _loadExternalScriptOnce(
    'katex-cdn',
    '/static/vendor/katex/katex.min.js',
    () => {
      _loadExternalScriptOnce(
        'katex-auto-render-cdn',
        '/static/vendor/katex/contrib/auto-render.min.js',
        () => {
          window._katexReady = typeof window.renderMathInElement === 'function';
          _flushPendingMath();
        },
      );
    },
  );
}
function escapeHtml(s) {
  if (s == null) return '-';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
function escapeAttr(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'");
}
function _parseInlineMarkdown(text) {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/api\/)[^\s)]+)\)/gi, (match, label, href) => {
      const safeHref = String(href || '').trim();
      const isExternal = /^https?:\/\//i.test(safeHref);
      const isLocalApi = /^\/api\/[A-Za-z0-9/_.,~:;?&=%+-]+$/i.test(safeHref);
      if (!isExternal && !isLocalApi) return match;
      const attrs = isExternal ? ' target="_blank" rel="noopener noreferrer"' : '';
      return `<a href="${escapeAttr(safeHref)}"${attrs}>${label}</a>`;
    })
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
}
function _fallbackMarkdownToHtml(source) {
  const text = String(source || '').replace(/\r\n/g, '\n');
  const blocks = text.split(/\n{2,}/).map((block) => block.trim()).filter(Boolean);
  if (!blocks.length) return '';
  return blocks.map((block) => {
    if (block.startsWith('```') && block.endsWith('```')) {
      const lines = block.split('\n');
      const code = lines.slice(1, -1).join('\n');
      return `<pre><code>${escapeHtml(code)}</code></pre>`;
    }
    const lines = block.split('\n');
    if (lines.every((line) => /^[-*]\s+/.test(line))) {
      const items = lines.map((line) => `<li>${_parseInlineMarkdown(line.replace(/^[-*]\s+/, ''))}</li>`).join('');
      return `<ul>${items}</ul>`;
    }
    if (lines.every((line) => /^\d+\.\s+/.test(line))) {
      const items = lines.map((line) => `<li>${_parseInlineMarkdown(line.replace(/^\d+\.\s+/, ''))}</li>`).join('');
      return `<ol>${items}</ol>`;
    }
    const heading = block.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      return `<h${level}>${_parseInlineMarkdown(heading[2])}</h${level}>`;
    }
    return `<p>${_parseInlineMarkdown(block).replace(/\n/g, '<br>')}</p>`;
  }).join('\n');
}
function parseMarkdown(source) {
  if (window.marked && typeof window.marked.parse === 'function') {
    const raw = window.marked.parse(String(source || ''));
    // marked >= 5 ne sanitise plus : la sortie LLM peut contenir du HTML actif.
    if (window.DOMPurify && typeof window.DOMPurify.sanitize === 'function') {
      return window.DOMPurify.sanitize(raw, { USE_PROFILES: { html: true } });
    }
    return _fallbackMarkdownToHtml(source);
  }
  return _fallbackMarkdownToHtml(source);
}

const CHAT_DEPTH_VALUES = ['auto', 'quick', 'deep'];
function chatDepthStorageKey() { return LS_PREFIX + 'chat-depth'; }

function getChatDepth() {
  let value = '';
  try { value = String(localStorage.getItem(chatDepthStorageKey()) || ''); } catch (_) {}
  return CHAT_DEPTH_VALUES.includes(value) ? value : 'auto';
}

// Below 640 px the segmented control shrinks to its active choice: a click on
// it opens the other choices (#chat-depth.is-open), a choice closes them.
const CHAT_DEPTH_NARROW_QUERY = '(max-width: 640px)';

function setChatDepth(depth) {
  const value = CHAT_DEPTH_VALUES.includes(depth) ? depth : 'auto';
  try { localStorage.setItem(chatDepthStorageKey(), value); } catch (_) {}
  const group = document.getElementById('chat-depth');
  const previous = group?.querySelector('.chat-depth-btn.is-active')?.dataset.depth;
  document.querySelectorAll('#chat-depth .chat-depth-btn').forEach((btn) => {
    btn.classList.toggle('is-active', btn.dataset.depth === value);
  });
  if (!group) return;
  const reopen = value === previous && !group.classList.contains('is-open')
    && Boolean(window.matchMedia?.(CHAT_DEPTH_NARROW_QUERY).matches);
  group.classList.toggle('is-open', reopen);
}

document.addEventListener('pointerdown', (event) => {
  const group = document.getElementById('chat-depth');
  if (group?.classList.contains('is-open') && !group.contains(event.target)) group.classList.remove('is-open');
}, true);

function initChatDepthControl() {
  setChatDepth(getChatDepth());
  document.getElementById('chat-depth')?.classList.remove('is-open');
}
document.addEventListener('DOMContentLoaded', initChatDepthControl);

function isChatVisualAnalysisAvailable() {
  const mode = String(state.appSettings?.visual_analysis_mode || 'off').trim().toLowerCase();
  const ocrModel = String(state.appSettings?.vision_model || '').trim();
  const analysisModel = String(state.appSettings?.visual_description_model || '').trim();
  return mode !== 'off' && Boolean(ocrModel || analysisModel);
}

function updateChatVisualForceUi() {
  const button = document.getElementById('chat-force-visual');
  if (!button) return;
  const available = isChatVisualAnalysisAvailable();
  if (!available) state.chatForceVisualOnce = false;
  button.hidden = !available;
  button.disabled = !available || Boolean(state.streaming);
  button.classList.toggle('is-active', available && state.chatForceVisualOnce);
  button.setAttribute('aria-pressed', state.chatForceVisualOnce ? 'true' : 'false');
  window.updateChatOptionsCount?.();
}

function setChatForceVisual(enabled) {
  state.chatForceVisualOnce = isChatVisualAnalysisAvailable() && Boolean(enabled);
  updateChatVisualForceUi();
  return state.chatForceVisualOnce;
}

function toggleChatForceVisual() {
  return setChatForceVisual(!state.chatForceVisualOnce);
}

function initChatVisualForceControl() {
  document.getElementById('chat-force-visual')?.addEventListener('click', toggleChatForceVisual);
  updateChatVisualForceUi();
}

document.addEventListener('DOMContentLoaded', initChatVisualForceControl);
window.updateChatVisualForceUi = updateChatVisualForceUi;

function isThinkingEnabled() {
  return Boolean(document.getElementById('llm-thinking')?.checked);
}

function isForceThinkingSmallModelsEnabled() {
  return Boolean(document.getElementById('force-thinking-small-models')?.checked);
}


function isDebugMetricsEnabled() {
  return Boolean(document.getElementById('debug-metrics')?.checked);
}

function estimateTokenCount(text) {
  const raw = String(text || '').trim();
  if (!raw) return 0;
  const words = raw.split(/\s+/).filter(Boolean).length;
  const chars = raw.length;
  return Math.max(1, Math.round(Math.max(chars / 4, words * 1.3)));
}

function pipelineGenerationDurationMs(pipelineMetrics) {
  if (!Array.isArray(pipelineMetrics) || !pipelineMetrics.length) return 0;
  const preferredStages = new Set(['llm_generation', 'wiki_generation']);
  for (const entry of pipelineMetrics) {
    const stage = String(entry?.stage || '').trim();
    if (!preferredStages.has(stage)) continue;
    const duration = Number(entry?.data?.duration_ms || 0);
    if (Number.isFinite(duration) && duration > 0) return duration;
  }
  return 0;
}

function createDebugMetricsTracker(meta = {}) {
  const startedAt = performance.now();
  let firstTokenAt = 0;
  let outputText = '';
  const modelName = String(meta.model || '').trim();
  const pipelineMetrics = [];
  return {
    update(piece) {
      const text = String(piece || '');
      if (!text) return;
      if (!firstTokenAt) firstTokenAt = performance.now();
      outputText += text;
    },
    replace(text) {
      const replacement = String(text || '');
      if (replacement && !firstTokenAt) firstTokenAt = performance.now();
      outputText = replacement;
    },
    addPipelineMetric(stage, data) {
      if (!stage || !data || typeof data !== 'object') return;
      pipelineMetrics.push({ type: 'pipeline_metrics', stage: String(stage), data });
    },
    snapshot() {
      if (!outputText) return null;
      const now = performance.now();
      const first = firstTokenAt || now;
      const latencyMs = Math.max(0, first - startedAt);
      const backendGenerationMs = pipelineGenerationDurationMs(pipelineMetrics);
      const generationSeconds = Math.max(
        0.001,
        (backendGenerationMs || Math.max(0, now - first)) / 1000,
      );
      const outputTokens = estimateTokenCount(outputText);
      const payload = {
        model: modelName,
        latency_ms: latencyMs,
        generation_seconds: generationSeconds,
        output_tokens_est: outputTokens,
        tokens_per_second_est: outputTokens / generationSeconds,
      };
      if (pipelineMetrics.length) payload.pipeline_metrics = pipelineMetrics.slice();
      return payload;
    },
  };
}

function withAnswerOrigin(metrics, answerOrigin) {
  const origin = String(answerOrigin || '').trim();
  if (!metrics && !origin) return null;
  const payload = metrics && typeof metrics === 'object' ? { ...metrics } : {};
  if (origin) payload.answer_origin = origin;
  return payload;
}

function formatAnswerOriginLabel(answerOrigin) {
  const value = String(answerOrigin || '').trim().toLowerCase();
  if (!value) return '';
  if (value === 'rag') return 'pipeline rag';
  if (value === 'wiki') return 'pipeline wiki';
  if (value === 'wiki+rag' || value === 'hybrid') return 'pipeline wiki + rag';
  return `pipeline ${value}`;
}

function formatRerankerPipelineMetric(data) {
  if (!data || typeof data !== 'object') return '';
  if (data.enabled === false) return '';
  if (!data.applied) {
    if (data.load_failed) {
      return `reranker indisponible (chargement échoué)`;
    }
    if (data.predict_error) {
      if (data.skipped_reason === 'previous_predict_error') {
        return `reranker ignoré (predict indisponible)`;
      }
      return `reranker erreur predict`;
    }
    const cand = Number(data.candidates_in || 0);
    return `reranker activé (${cand} candidat${cand !== 1 ? 's' : ''}, modèle non chargé)`;
  }
  const cand = Number(data.candidates_in || 0);
  const top = Number(data.top_k || 0);
  const predict = Number(data.predict_duration_ms || 0);
  const triggered = Boolean(data.triggered_load);
  const loadMs = Number(data.load_duration_ms || 0);
  const modelName = String(data.model_name || '').split('/').pop();
  const parts = [`reranker ${modelName ? modelName + ' ' : ''}${cand}→${top}`];
  if (predict > 0) parts.push(`predict ${Math.round(predict)} ms`);
  if (triggered && loadMs > 0) parts.push(`load ${(loadMs / 1000).toFixed(1)} s`);
  if (data.unloaded_after_use) parts.push('déchargé');
  return parts.join(' · ');
}

function formatPipelineDuration(ms) {
  if (ms === undefined || ms === null || ms === '') return '';
  const value = Number(ms || 0);
  if (!Number.isFinite(value) || value < 0) return '';
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;
}

function formatGenericPipelineMetric(stage, data) {
  if (!data || typeof data !== 'object') return '';
  const duration = formatPipelineDuration(data.duration_ms);
  if (!duration) return '';
  if (stage === 'rag_init') return `init RAG ${duration}`;
  if (stage === 'reranker_warmup') return `warmup reranker ${duration}`;
  if (stage === 'llm_generation' || stage === 'tool_planner') {
    const parts = [`${stage === 'tool_planner' ? 'planificateur' : 'LLM'} ${duration}`];
    // Ollama timings: a long load means the model was reloaded for this call.
    if (data.reloaded) parts.push(`rechargement ${formatPipelineDuration(data.load_ms)}`);
    if (Number(data.prompt_tokens || 0) > 0) {
      parts.push(`prompt ${Number(data.prompt_tokens)} tok ${formatPipelineDuration(data.prefill_ms)}`);
    }
    if (Number(data.tokens_per_second || 0) > 0) {
      parts.push(`${Number(data.tokens_per_second).toFixed(0)} tok/s`);
    }
    return parts.join(' · ');
  }
  if (stage === 'answer_guard') return `garde citations ${duration}`;
  if (stage === 'wiki_lookup') return `wiki lookup ${duration}`;
  if (stage === 'wiki_retrieval') return `wiki retrieval ${duration}`;
  if (stage === 'wiki_retrieval_fallback') return `wiki fallback ${duration}`;
  if (stage === 'wiki_generation') return `wiki LLM ${duration}`;
  if (stage === 'wiki_answer_guard') return `garde wiki ${duration}`;
  if (stage === 'retrieval') {
    const returned = Number(data.returned || 0);
    const unique = Number(data.unique_candidates || 0);
    const vector = formatPipelineDuration(data.vector_search_ms);
    const parts = [`retrieval ${duration}`];
    if (unique || returned) parts.push(`${unique}→${returned}`);
    if (vector) parts.push(`vecteur ${vector}`);
    return parts.join(' · ');
  }
  return `${stage} ${duration}`;
}

function formatPipelineMetricsSummary(pipelineMetrics) {
  if (!Array.isArray(pipelineMetrics) || !pipelineMetrics.length) return '';
  const out = [];
  for (const entry of pipelineMetrics) {
    const stage = String(entry?.stage || '').trim();
    const data = entry?.data || {};
    if (stage === 'reranker') {
      const rendered = formatRerankerPipelineMetric(data);
      if (rendered) out.push(rendered);
    } else {
      const rendered = formatGenericPipelineMetric(stage, data);
      if (rendered) out.push(rendered);
    }
  }
  return out.join(' | ');
}

function formatDebugMetrics(metrics) {
  if (!metrics) return '';
  const modelName = String(metrics.model || '').trim();
  const answerOrigin = formatAnswerOriginLabel(metrics.answer_origin);
  const latencyMs = Number(metrics.latency_ms || 0);
  const backendGenerationMs = pipelineGenerationDurationMs(metrics.pipeline_metrics);
  const generationSeconds = backendGenerationMs > 0
    ? backendGenerationMs / 1000
    : Number(metrics.generation_seconds || 0);
  const outputTokens = Math.max(0, Math.round(Number(metrics.output_tokens_est || 0)));
  const tokensPerSecond = generationSeconds > 0
    ? outputTokens / generationSeconds
    : Number(metrics.tokens_per_second_est || 0);
  if (!outputTokens || !generationSeconds || !tokensPerSecond) {
    const minimalPipeline = formatPipelineMetricsSummary(metrics.pipeline_metrics);
    return minimalPipeline || '';
  }
  const modelPart = modelName ? `modele ${modelName} | ` : '';
  const originPart = answerOrigin ? `${answerOrigin} | ` : '';
  const baseLine = `${modelPart}${originPart}latence ${Math.round(latencyMs)} ms | out ~${outputTokens} tok | ~${tokensPerSecond.toFixed(1)} tok/s`;
  const pipelinePart = formatPipelineMetricsSummary(metrics.pipeline_metrics);
  return pipelinePart ? `${baseLine} | ${pipelinePart}` : baseLine;
}

function splitThinkingContent(source) {
  const raw = String(source || '');
  const thinkingParts = [];
  let answer = raw.replace(/<think>([\s\S]*?)<\/think>/gi, (_, inner) => {
    const value = String(inner || '').trim();
    if (value) thinkingParts.push(value);
    return '';
  });

  answer = answer.replace(/<\|?channel\|?>thought\s*([\s\S]*?)<channel\|>/gi, (_, inner) => {
    const value = String(inner || '').trim();
    if (value) thinkingParts.push(value);
    return '';
  });

  let thinkingOpen = false;
  const openMatch = answer.match(/<think>([\s\S]*)$/i);
  if (openMatch) {
    thinkingOpen = true;
    const partial = String(openMatch[1] || '').trim();
    if (partial) thinkingParts.push(partial);
    answer = answer.slice(0, openMatch.index);
  }

  const gemmaOpenMatch = answer.match(/<\|?channel\|?>thought\s*([\s\S]*)$/i);
  if (gemmaOpenMatch) {
    thinkingOpen = true;
    const partial = String(gemmaOpenMatch[1] || '').trim();
    if (partial) thinkingParts.push(partial);
    answer = answer.slice(0, gemmaOpenMatch.index);
  }

  answer = answer.replace(/<\/think>/gi, '');
  answer = answer.replace(/<\|think\|>/gi, '');
  answer = answer.replace(/<channel\|>/gi, '');
  answer = answer.replace(/\n{3,}/g, '\n\n').trim();

  return {
    answer,
    thinking: thinkingParts.join('\n\n').trim(),
    thinkingOpen,
  };
}

function stripThinkingMarkup(source) {
  return splitThinkingContent(source).answer;
}

function buildAssistantCopyText(source, options = {}) {
  const parts = splitThinkingContent(source);
  const answerText = options.normalizeCitations
    ? normalizeAssistantCitations(parts.answer, options.sources || [])
    : parts.answer;
  const blocks = [];

  if (parts.thinking) {
    blocks.push(`Thinking:\n${parts.thinking.trim()}`);
  }

  if (answerText.trim()) blocks.push(answerText.trim());
  return blocks.join('\n\n').trim();
}

function assistantCitationSourcePath(source) {
  return String(source?.file || source?.source_path || '').trim();
}

function renderAssistantMarkdownBlock(container, text, options = {}) {
  if (!container) return;
  const value = String(text || '').trim();
  container.innerHTML = value ? parseAssistantMarkdown(value) : '';
  if (value) {
    decorateAssistantLinks(container);
    applyAssistantCitationLinks(container, options.sources || [], options);
  }
}

function normalizeChatExportFile(exportFile) {
  if (!exportFile || typeof exportFile !== 'object') return null;
  const filename = String(exportFile.filename || '').trim();
  const url = String(exportFile.url || '').trim();
  if (!filename || !/^\/api\/chat-exports\/[A-Za-z0-9_.-]+\.(?:md|tex)$/i.test(url)) return null;
  const explicitFormat = String(exportFile.format || '').trim().toLowerCase();
  const format = explicitFormat === 'latex' || /\.tex$/i.test(filename) ? 'latex' : 'markdown';
  return { filename, url, format };
}

function extractChatExportFileFromAnswerText(text) {
  const value = String(text || '').trim();
  if (!value) return null;
  const match = value.match(/\[([^\]]+)\]\((\/api\/chat-exports\/([A-Za-z0-9_.-]+\.(?:md|tex)))\)/i);
  if (!match) return null;
  const filename = String(match[3] || match[1] || '').trim();
  const url = String(match[2] || '').trim();
  if (!filename || !url) return null;
  return normalizeChatExportFile({ filename, url });
}

function createDiscoverySessionCard(session) {
  const normalized = normalizeDiscoverySession(session);
  if (!normalized) return null;

  const card = document.createElement('div');
  card.className = 'assistant-discovery-card';

  const meta = document.createElement('div');
  meta.className = 'assistant-discovery-meta';
  const count = Number(normalized.result_count || 0);
  const provider = normalized.provider_used ? ` | ${normalized.provider_used}` : '';
  meta.textContent = `${_ti('discovery.resultCount', { count })}${provider}`;
  card.appendChild(meta);

  if (normalized.input_text) {
    const preview = document.createElement('div');
    preview.className = 'assistant-discovery-preview';
    preview.textContent = normalized.input_text;
    card.appendChild(preview);
  }

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'assistant-discovery-action';
  button.innerHTML = '<span>' + _tr('Ouvrir dans Decouverte') + '</span>';
  button.addEventListener('click', async (event) => {
    event.preventDefault();
    event.stopPropagation();
    await openDiscoverySessionFromChat(normalized, button);
  });
  card.appendChild(button);
  return card;
}

function createExternalSearchConfirmationCard(confirmation) {
  const normalized = normalizeExternalSearchConfirmation(confirmation);
  if (!normalized) return null;

  const card = document.createElement('div');
  card.className = 'assistant-external-confirm-card';

  const meta = document.createElement('div');
  meta.className = 'assistant-external-confirm-meta';
  meta.textContent = _tr('Choix de recherche');
  card.appendChild(meta);

  if (normalized.query) {
    const preview = document.createElement('div');
    preview.className = 'assistant-external-confirm-preview';
    preview.textContent = normalized.query;
    card.appendChild(preview);
  }

  const actions = document.createElement('div');
  actions.className = 'assistant-external-confirm-actions';

  const localButton = document.createElement('button');
  localButton.type = 'button';
  localButton.className = 'assistant-external-confirm-action is-primary';
  localButton.textContent = _tr('Chercher en local');
  localButton.addEventListener('click', async (event) => {
    event.preventDefault();
    event.stopPropagation();
    await confirmExternalSearchFromChat(normalized, localButton, 'local');
  });

  const externalButton = document.createElement('button');
  externalButton.type = 'button';
  externalButton.className = 'assistant-external-confirm-action';
  externalButton.textContent = _tr('Sources externes');
  if (!normalized.externalAllowed) {
    externalButton.disabled = true;
    externalButton.title = _tr('Activez les API externes dans les settings pour utiliser cette option.');
  }
  externalButton.addEventListener('click', async (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (!normalized.externalAllowed) {
      showToast(_tr('API externes désactivées dans les settings.'), 3200, 'error');
      return;
    }
    await confirmExternalSearchFromChat(normalized, externalButton, 'external');
  });

  const cancelButton = document.createElement('button');
  cancelButton.type = 'button';
  cancelButton.className = 'assistant-external-confirm-action';
  cancelButton.textContent = _tr('Annuler');
  cancelButton.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    card.classList.add('is-cancelled');
    actions.querySelectorAll('button').forEach((button) => {
      button.disabled = true;
    });
    showToast(_tr('Recherche externe annulée.'), 2400);
  });

  actions.appendChild(localButton);
  actions.appendChild(externalButton);
  actions.appendChild(cancelButton);
  card.appendChild(actions);
  return card;
}

function decorateAssistantLinks(container) {
  if (!container) return;
  container.querySelectorAll('a[href]').forEach((link) => {
    const href = String(link.getAttribute('href') || '').trim();
    if (!href) return;
    link.classList.add('assistant-inline-link');
    link.setAttribute('target', '_blank');
    link.setAttribute('rel', 'noopener');

    const exportMatch = href.match(/^\/api\/chat-exports\/([A-Za-z0-9_.-]+\.(?:md|tex))$/i);
    if (!exportMatch) return;

    const filename = String(exportMatch[1] || '').trim();
    if (!filename) return;

    link.classList.add('is-export-link');
    link.setAttribute('download', filename);
    if (link.dataset.prettyExportLink === 'true') return;

    const label = document.createElement('span');
    label.className = 'assistant-inline-link-label';
    label.textContent = _tr('Télécharger');

    const name = document.createElement('span');
    name.className = 'assistant-inline-link-name';
    name.textContent = String(link.textContent || '').trim() || filename;

    link.textContent = '';
    link.appendChild(label);
    link.appendChild(name);
    link.dataset.prettyExportLink = 'true';
  });
}

function isPlainChatExportAnswer(text) {
  const value = String(text || '').trim();
  if (!value) return true;
  return /^fichier\s+cr[ée]e?\s*:?\s*\[[^\]]+\]\(\/api\/chat-exports\/[A-Za-z0-9_.-]+\.(?:md|tex)\)\s*$/i
    .test(value.normalize('NFD').replace(/[\u0300-\u036f]/g, ''));
}

function createChatExportCard(exportFile) {
  const file = normalizeChatExportFile(exportFile);
  if (!file) return null;

  const card = document.createElement('div');
  card.className = 'assistant-export-card';

  const badge = document.createElement('div');
  badge.className = `assistant-export-badge is-${file.format}`;
  badge.textContent = file.format === 'latex' ? 'TEX' : 'MD';

  const info = document.createElement('div');
  info.className = 'assistant-export-info';

  const title = document.createElement('div');
  title.className = 'assistant-export-title';
  title.textContent = file.filename;

  const meta = document.createElement('div');
  meta.className = 'assistant-export-meta';
  meta.textContent = file.format === 'latex'
    ? 'Document LaTeX généré'
    : 'Document Markdown généré';

  info.appendChild(title);
  info.appendChild(meta);

  const action = document.createElement('a');
  action.className = 'assistant-export-download';
  action.href = file.url;
  action.download = file.filename;
  action.textContent = _tr('Télécharger');

  card.appendChild(badge);
  card.appendChild(info);
  card.appendChild(action);
  return card;
}

const _lightAssistantStreams = new WeakMap();
const _lightStreamBlocks = new WeakMap();
const _pendingLightAssistantStreams = new Map();

// Keep completed blocks (and any text selection) in place. Parse through the
// same safe Markdown/citation renderer; math and source cards wait for the final render.
function _updateLightStreamBlock(container, text, options = {}) {
  const value = String(text || '').trim();
  let units = [value];
  // Math spanning blocks and reference definitions require the full context.
  if (!/\$\$|\\\[|\\\(|^\s*\[[^\]]+\]:/m.test(value)) {
    if (window.marked?.lexer && window.DOMPurify) {
      const tokens = window.marked.lexer(value);
      if (!Object.keys(tokens.links || {}).length) units = tokens.filter(token => token.type !== 'space').map(token => token.raw);
    } else if (!/```|~~~/.test(value)) {
      units = value.replace(/\r\n/g, '\n').split(/\n{2,}/).map(block => block.trim()).filter(Boolean);
    }
  }
  const previous = _lightStreamBlocks.get(container) || [];
  const sources = options.sources?.length ? options.sources : null;
  const blocks = units.map((unit, index) => {
    const cached = previous[index];
    if (cached?.text === unit && cached.sources === sources && cached.nodes.every(node => node.parentNode === container)) return cached;
    const next = document.createElement('div');
    renderAssistantMarkdownBlock(next, unit, options);
    return { text: unit, sources, nodes: [...next.childNodes] };
  });
  let current = container.firstChild;
  for (const block of blocks) {
    for (const child of block.nodes) {
      if (current === child) current = current.nextSibling;
      else container.insertBefore(child, current);
    }
  }
  while (current) {
    const following = current.nextSibling;
    current.remove();
    current = following;
  }
  _lightStreamBlocks.set(container, blocks);
}

function _renderLightAssistantStream(root, source, options) {
  let view = _lightAssistantStreams.get(root);
  if (!view || view.wrapper.parentNode !== root) {
    const thinkingWasOpen = Boolean(root.querySelector('.thinking-block[open]'));
    root.replaceChildren();
    root.style.whiteSpace = '';
    const wrapper = document.createElement('div');
    wrapper.className = 'assistant-rich-response';
    const thinking = document.createElement('details');
    thinking.className = 'thinking-block is-live';
    thinking.open = thinkingWasOpen || Boolean(options.openThinking);
    const summary = document.createElement('summary');
    summary.className = 'thinking-summary';
    summary.innerHTML = '<span class="thinking-summary-text"><span class="thinking-dot"></span><span class="thinking-label">Thinking</span></span>';
    const panel = document.createElement('div');
    panel.className = 'thinking-panel';
    panel.setAttribute('data-i18n-skip', '');
    thinking.append(summary, panel);
    const answer = document.createElement('div');
    answer.className = 'assistant-answer';
    answer.setAttribute('data-i18n-skip', '');
    wrapper.append(thinking, answer);
    root.appendChild(wrapper);
    view = { wrapper, thinking, panel, answer, thinkingText: '', renderedThinking: null, answerText: null };
    _lightAssistantStreams.set(root, view);
    thinking.addEventListener('toggle', () => {
      if (thinking.open && view.renderedThinking !== view.thinkingText) {
        _updateLightStreamBlock(panel, view.thinkingText);
        view.renderedThinking = view.thinkingText;
      }
    });
  }
  const parts = splitThinkingContent(source);
  view.thinking.hidden = !parts.thinking;
  view.thinkingText = parts.thinking;
  if (view.thinking.open && view.renderedThinking !== parts.thinking) {
    _updateLightStreamBlock(view.panel, parts.thinking);
    view.renderedThinking = parts.thinking;
  }
  const answer = options.normalizeCitations
    ? normalizeAssistantCitations(parts.answer, options.sources || []) : parts.answer;
  if (view.answerText !== answer) {
    _updateLightStreamBlock(view.answer, answer, { sources: options.sources || [] });
    view.answerText = answer;
  }
  if (!answer) {
    const placeholder = _tr(options.emptyText || 'Réponse en préparation…');
    if (view.answer.textContent !== placeholder) view.answer.textContent = placeholder;
  }
  root.dataset.copyText = buildAssistantCopyText(source, options);
  _upsertChatCopyButton(root);
}

function _flushLightAssistantStreams() {
  for (const [root, pending] of _pendingLightAssistantStreams) {
    // A finished/failed stream or a closed conversation must never be resurrected.
    if (!root.isConnected || root.firstChild !== pending.firstChild || root.className !== pending.className) {
      _pendingLightAssistantStreams.delete(root);
    } else if (isVisualUpdateVisible(root)) {
      _pendingLightAssistantStreams.delete(root);
      renderRichAssistantOutput(root, pending.source, pending.options);
    }
  }
}
document.addEventListener('visibilitychange', _flushLightAssistantStreams);
window.addEventListener('bibliobrain:tabchange', _flushLightAssistantStreams);
window.addEventListener('bibliobrain:performancechange', _flushLightAssistantStreams);

function renderRichAssistantOutput(root, source, options = {}) {
  if (!root) return;
  _pendingLightAssistantStreams.delete(root);
  if (options.streaming && isLightPerformanceMode()
      && !options.discoverySession && !options.externalSearchConfirmation && !options.exportFile) {
    if (!isVisualUpdateVisible(root)) {
      _pendingLightAssistantStreams.set(root, { source, options, firstChild: root.firstChild, className: root.className });
      return;
    }
    _renderLightAssistantStream(root, source, options);
    return;
  }
  _lightAssistantStreams.delete(root);
  const previousThinkingOpen = Boolean(root.querySelector('.thinking-block[open]'));
  const previousSourcesOpen = Boolean(root.querySelector('.assistant-sources-block[open]'));
  const parts = splitThinkingContent(source);
  const answerText = options.normalizeCitations
    ? normalizeAssistantCitations(parts.answer, options.sources || [])
    : parts.answer;
  const renderableSources = Array.isArray(options.sources)
    ? options.sources.filter(isChatSourceRecord)
    : [];
  const citedLabels = extractAssistantCitationLabels(answerText);
  const showAllSources = Boolean(options.showAllSources);
  const inlineSources = showAllSources
    ? renderableSources
    : filterSourcesByCitationLabels(renderableSources, citedLabels);
  const hideSourcesSection = Boolean(options.hideSourcesSection);

  root.style.whiteSpace = '';
  root.innerHTML = '';

  const wrapper = document.createElement('div');
  wrapper.className = 'assistant-rich-response';

  if (parts.thinking) {
    const details = document.createElement('details');
    details.className = 'thinking-block';
    if (options.streaming && (parts.thinkingOpen || options.appendCursor)) details.classList.add('is-live');
    if (previousThinkingOpen || options.openThinking) details.open = true;

    const summary = document.createElement('summary');
    summary.className = 'thinking-summary';
    summary.innerHTML = `
      <span class="thinking-summary-text">
        <span class="thinking-dot"></span>
        <span class="thinking-label">Thinking</span>
      </span>
      ${parts.thinkingOpen ? '' : '<span class="thinking-hint">Cliquer pour afficher</span>'}
    `;

    const panel = document.createElement('div');
    panel.className = 'thinking-panel';
    panel.setAttribute('data-i18n-skip', '');
    panel.innerHTML = parseAssistantMarkdown(parts.thinking);

    details.appendChild(summary);
    details.appendChild(panel);
    wrapper.appendChild(details);
  }

  const answer = document.createElement('div');
  answer.className = 'assistant-answer';
  answer.setAttribute('data-i18n-skip', '');
  if (answerText) {
    renderAssistantMarkdownBlock(answer, answerText, { sources: options.sources || [] });
  } else if (parts.thinking && options.streaming) {
    answer.innerHTML = '<p style="color:var(--muted);font-style:italic">' + escapeHtml(_tr('Réponse en préparation…')) + '</p>';
  } else if (options.emptyText) {
    answer.innerHTML = parseAssistantMarkdown(_tr(options.emptyText));
  }
  if (options.appendCursor) {
    const cursor = document.createElement('span');
    cursor.className = 'cursor';
    const cursorHost = answer.querySelector('.assistant-answer-body')
      || answer;
    cursorHost.appendChild(cursor);
  }
  wrapper.appendChild(answer);

  const exportCard = createChatExportCard(options.exportFile || extractChatExportFileFromAnswerText(answerText));
  if (exportCard) {
    if (isPlainChatExportAnswer(answerText)) answer.innerHTML = '';
    wrapper.appendChild(exportCard);
  }

  const discoveryCard = createDiscoverySessionCard(options.discoverySession);
  if (discoveryCard) {
    wrapper.appendChild(discoveryCard);
  }

  const externalSearchConfirmationCard = createExternalSearchConfirmationCard(options.externalSearchConfirmation);
  if (externalSearchConfirmationCard) {
    wrapper.appendChild(externalSearchConfirmationCard);
  }

  // Chat answers list their sources in the side panel (openChatSourcesPanel);
  // notebook, library and drafting answers keep the inline cards below.
  const usesSourcesPanel = root.classList.contains('bubble') && typeof openChatSourcesPanel === 'function';
  root._chatSources = usesSourcesPanel && !hideSourcesSection ? inlineSources : null;
  root._chatSourcesShowAll = showAllSources;
  if (usesSourcesPanel && !hideSourcesSection && inlineSources.length) {
    const sourceCount = inlineSources.length;
    const openButton = document.createElement('button');
    openButton.type = 'button';
    openButton.className = 'assistant-sources-open';
    openButton.setAttribute('aria-controls', 'chat-sources-panel');
    openButton.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';
    const text = document.createElement('span');
    text.textContent = _ti(
      showAllSources ? 'chat.sources.openFound' : 'chat.sources.openCited',
      { count: sourceCount },
    );
    openButton.appendChild(text);
    openButton.addEventListener('click', () => openChatSourcesPanel(root, { view: 'all', trigger: openButton }));
    wrapper.appendChild(openButton);
  } else if (!hideSourcesSection && inlineSources.length) {
    const sourceDetails = document.createElement('details');
    sourceDetails.className = 'assistant-sources-block';
    // Replié par défaut : le badge "N sources" suffit, le contenu est dense.
    if (previousSourcesOpen) sourceDetails.open = true;

    const summary = document.createElement('summary');
    summary.className = 'assistant-sources-summary';
    const sourceCount = inlineSources.length;
    const sourceLabel = showAllSources ? 'retrouvee' : 'citee';
    summary.innerHTML = `
      <span class="assistant-section-label">Sources</span>
      <span class="assistant-sources-summary-text">${sourceCount} source${sourceCount > 1 ? 's' : ''} ${sourceLabel}${sourceCount > 1 ? 's' : ''}</span>
    `;
    sourceDetails.appendChild(summary);

    const sourceList = document.createElement('div');
    sourceList.className = 'assistant-source-list';
    renderSourceCards(sourceList, inlineSources, { inline: true });
    sourceDetails.appendChild(sourceList);
    wrapper.appendChild(sourceDetails);
  }

  const ragQuery = String(options.ragQuery || root._ragQuery || '').trim();
  const answerOrigin = String(options.answerOrigin || root._answerOrigin || '').trim();
  const toolsRow = document.createElement('div');
  toolsRow.className = 'assistant-tools-row';
  if (ragQuery && renderableSources.length && answerOrigin === 'rag') {
    const graphButton = document.createElement('button');
    graphButton.type = 'button';
    graphButton.className = 'assistant-rag-graph-btn';
    graphButton.textContent = _tr('Graphe RAG');
    graphButton.title = _tr('Voir le graphe de récupération RAG');
    graphButton.setAttribute('aria-label', _tr('Voir le graphe de récupération RAG'));
    graphButton.addEventListener('click', () => {
      if (typeof openRagRetrievalGraph === 'function') {
        openRagRetrievalGraph(ragQuery, renderableSources, answerOrigin);
      }
    });
    toolsRow.appendChild(graphButton);
  }

  const debugMetricsText = formatDebugMetrics(options.debugMetrics);
  if (debugMetricsText) {
    const previousDebugOpen = Boolean(root._debugWasOpen);
    const metrics = document.createElement('details');
    metrics.className = 'debug-metrics';
    if (previousDebugOpen) metrics.open = true;
    metrics.addEventListener('toggle', () => { root._debugWasOpen = metrics.open; });
    const summary = document.createElement('summary');
    summary.className = 'debug-metrics-summary';
    summary.textContent = 'Debug';
    const body = document.createElement('div');
    body.className = 'debug-metrics-body';
    body.textContent = debugMetricsText;
    metrics.appendChild(summary);
    metrics.appendChild(body);
    toolsRow.appendChild(metrics);
  }
  if (toolsRow.childElementCount) wrapper.appendChild(toolsRow);
  root.appendChild(wrapper);
  root.dataset.copyText = buildAssistantCopyText(source, {
    normalizeCitations: Boolean(options.normalizeCitations),
    sources: options.sources || [],
  });
  _upsertChatCopyButton(root);

  if (options.renderMath !== false) {
    root.querySelectorAll('.assistant-answer, .thinking-panel').forEach((el) => {
      renderMath(el, { normalizeSingleDollar: true });
    });
  }
  if (usesSourcesPanel) syncChatSourcesPanel(root);
}

function buildAssistantCitationSourceMap(sources = [], options = null) {
  const rows = Array.isArray(sources) ? sources : [];
  const byLabel = new Map();
  const preferRawLabels = Boolean(options?.preferRawLabels);
  const addLabel = (value, row) => {
    const label = normalizeAssistantCitationLabel(value)
      || String(value || '').trim().toUpperCase();
    if (!label || byLabel.has(label)) return;
    byLabel.set(label, row);
  };

  // Reserve the labels needed by the current rendering phase before adding
  // aliases. A raw label from one source can legitimately match the global
  // conversation label of another source.
  rows.forEach((row) => {
    const primaryLabel = preferRawLabels
      ? (row?.raw_ref_label || row?.ref_label)
      : (row?.chat_ref_label || row?.ref_label);
    addLabel(primaryLabel, row);
  });

  rows.forEach((row, index) => {
    const labels = collectSourceLabelAliases(row, index + 1);
    const chunkUid = String(row?.chunk_uid || '').trim().toUpperCase();
    if (chunkUid) labels.add(chunkUid);
    const articleChunk = [String(row?.article_id || '').trim(), String(row?.chunk_id ?? '').trim()]
      .filter(Boolean)
      .join(':')
      .toUpperCase();
    if (articleChunk) labels.add(articleChunk);
    labels.forEach((label) => addLabel(label, row));
  });
  return byLabel;
}

function normalizeAssistantCitationLabel(part) {
  const value = String(part || '').trim();
  const chunkMatch = value.match(/^CHUNK\s+(.+)$/i);
  if (chunkMatch) return normalizeAssistantCitationLabel(chunkMatch[1]);
  const sourceMatch = value.match(/^S(\d+)$/i);
  if (sourceMatch) return `S${parseInt(sourceMatch[1], 10)}`;
  const numericMatch = value.match(/^(\d+)$/);
  if (numericMatch) return String(parseInt(numericMatch[1], 10));
  if (/^[A-Za-z0-9._/-]+(?::[A-Za-z0-9._/-]+)+$/i.test(value)) {
    return value.toUpperCase();
  }
  return '';
}

function isAssistantCitationLikePart(part) {
  const value = String(part || '').trim();
  if (!value) return false;
  if (normalizeAssistantCitationLabel(value)) return true;
  return /^(?:CHUNK\s+)?\d+\s*:\s*.+$/i.test(value);
}

function assistantCitationEntries(rawContent, sourceMap) {
  const parts = String(rawContent || '').split(',').map((part) => part.trim()).filter(Boolean);
  return parts.map((part) => {
    const label = normalizeAssistantCitationLabel(part);
    if (!label) return null;
    const source = sourceMap.get(label);
    if (!source) return null;
    // Notebook sources carry no label of their own: without their number,
    // every citation displayed as [1].
    const displayLabel = sourceRefLabel(source, /^\d+$/.test(label) ? label : 1) || label;
    return { label, displayLabel, source };
  }).filter(Boolean);
}

// Each RAG passage has its own label, so one paper can be cited as
// [S2][S4][S5]. Passages of the same paper share one pill.
function assistantCitationPaperKey(source, fallback) {
  const articleId = String(source?.article_id || '').trim();
  if (articleId) return `article:${articleId.toLowerCase()}`;
  const doi = String(source?.doi || '').trim();
  if (doi) return `doi:${doi.toLowerCase()}`;
  const title = String(source?.title || source?.file || '').trim();
  if (title) return `title:${title.toLowerCase()}`;
  return `label:${fallback}`;
}

function _assistantCitationPassageLink(entry, className) {
  const link = document.createElement('a');
  link.href = '#';
  link.className = className;
  link.dataset.sourceLabel = entry.displayLabel;
  const labelSpan = document.createElement('span');
  labelSpan.className = 'assistant-citation-label';
  labelSpan.textContent = entry.displayLabel;
  link.appendChild(labelSpan);
  const page = String(entry.source?.page || '').trim();
  link.title = [
    sourceDisplayTitle(entry.source),
    sourceCompactMeta(entry.source),
    page ? `p.${page}` : '',
  ].filter(Boolean).join(' · ');
  link.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    revealCitationSource(entry.displayLabel, link);
  });
  return link;
}

function _appendAssistantCitationMeta(target, source) {
  const compactMeta = sourceCompactMeta(source);
  if (!compactMeta) return;
  const metaSpan = document.createElement('span');
  metaSpan.className = 'assistant-citation-meta';
  metaSpan.textContent = compactMeta;
  target.appendChild(metaSpan);
}

function createAssistantCitationGroup(entries) {
  const papers = new Map();
  const seenLabels = new Set();
  entries.forEach((entry) => {
    if (seenLabels.has(entry.displayLabel)) return;
    seenLabels.add(entry.displayLabel);
    const key = assistantCitationPaperKey(entry.source, entry.displayLabel);
    if (!papers.has(key)) papers.set(key, []);
    papers.get(key).push(entry);
  });
  if (!papers.size) return null;

  const group = document.createElement('span');
  group.className = 'assistant-citation-group';
  group.appendChild(document.createTextNode('['));
  Array.from(papers.values()).forEach((passages, index) => {
    if (index > 0) group.appendChild(document.createTextNode(', '));
    if (passages.length === 1) {
      const link = _assistantCitationPassageLink(passages[0], 'assistant-citation-link');
      _appendAssistantCitationMeta(link, passages[0].source);
      group.appendChild(link);
      return;
    }
    // Several passages: one pill, one clickable label per passage, one meta.
    const pill = document.createElement('span');
    pill.className = 'assistant-citation-link assistant-citation-paper';
    pill.title = sourceDisplayTitle(passages[0].source);
    passages.forEach((entry, passageIndex) => {
      if (passageIndex > 0) pill.appendChild(document.createTextNode(','));
      pill.appendChild(_assistantCitationPassageLink(entry, 'assistant-citation-passage'));
    });
    _appendAssistantCitationMeta(pill, passages[0].source);
    group.appendChild(pill);
  });
  group.appendChild(document.createTextNode(']'));
  return group;
}

function cleanupAssistantCitationArtifacts(text) {
  let value = String(text || '');
  value = value.replace(/\b(et|ou|and)\s+([,.;:!?])/gi, '$2');
  value = value.replace(/\(\s*\)/g, '');
  value = value.replace(/,\s*,+/g, ', ');
  value = value.replace(/[ \t]+([,.;:!?])/g, '$1');
  value = value.replace(/[ \t]{2,}/g, ' ');
  return value;
}

function applyAssistantCitationLinks(container, sources = [], options = {}) {
  if (!container) return;
  const sourceMap = buildAssistantCitationSourceMap(sources);
  if (!sourceMap.size) return;

  const walker = document.createTreeWalker(
    container,
    NodeFilter.SHOW_TEXT,
    {
      acceptNode(node) {
        const text = String(node?.nodeValue || '');
        if (!text.includes('[') || !/\[[^\]]+\]/.test(text)) return NodeFilter.FILTER_REJECT;
        const parent = node.parentElement;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (parent.closest('a, code, pre')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    },
  );

  const textNodes = [];
  let currentNode = walker.nextNode();
  while (currentNode) {
    textNodes.push(currentNode);
    currentNode = walker.nextNode();
  }

  textNodes.forEach((node) => {
    const text = String(node.nodeValue || '');
    const pattern = /\[([^\]]+)\]/g;
    let lastIndex = 0;
    let matched = false;
    let pending = null;
    const fragment = document.createDocumentFragment();
    const flushPending = () => {
      const group = pending ? createAssistantCitationGroup(pending) : null;
      if (group) fragment.appendChild(group);
      pending = null;
    };

    for (const match of text.matchAll(pattern)) {
      const fullMatch = String(match[0] || '');
      const entries = assistantCitationEntries(match[1], sourceMap);
      if (!entries.length) continue;

      const start = Number(match.index || 0);
      const between = text.slice(lastIndex, start);
      // "[S3][S6]" or "[S3] [S6]" reads as one citation group.
      if (pending && !between.trim()) {
        pending.push(...entries);
      } else {
        flushPending();
        if (between) fragment.appendChild(document.createTextNode(between));
        pending = [...entries];
      }
      lastIndex = start + fullMatch.length;
      matched = true;
    }
    flushPending();

    if (!matched) return;
    if (lastIndex < text.length) {
      fragment.appendChild(document.createTextNode(text.slice(lastIndex)));
    }
    node.parentNode?.replaceChild(fragment, node);
  });
}

function normalizeAssistantCitations(text, sources = []) {
  let value = String(text || '');
  const sourceMap = buildAssistantCitationSourceMap(sources, { preferRawLabels: true });

  if (sourceMap.size > 0) {
    value = value.replace(/\[([^\]]+)\]/g, (match, rawContent) => {
      const parts = String(rawContent || '').split(',').map((part) => part.trim()).filter(Boolean);
      if (!parts.length) return match;

      const normalized = [];
      let sawCitationLikePart = false;
      for (const part of parts) {
        const label = normalizeAssistantCitationLabel(part);
        if (!label) {
          if (isAssistantCitationLikePart(part)) {
            sawCitationLikePart = true;
            continue;
          }
          normalized.push(part);
          continue;
        }
        sawCitationLikePart = true;
        const source = sourceMap.get(label);
        if (!source) {
          continue;
        }
        normalized.push(String(source?.ref_label || label).trim().toUpperCase() || label);
      }

      if (!normalized.length) {
        return sawCitationLikePart ? '' : match;
      }
      if (!normalized.some((part) => normalizeAssistantCitationLabel(part))) return match;
      return `[${normalized.join(', ')}]`;
    });
  }

  value = value.replace(/\n{0,2}References:\s*[\s\S]*$/i, '');
  value = cleanupAssistantCitationArtifacts(value);
  value = value.replace(/\n{3,}/g, '\n\n').trim();
  return value;
}

function parseFilterInput(value) {
  if (!value) return [];
  const parts = value.split(/[,;\n|]/).map(v => v.trim()).filter(Boolean);
  const seen = new Set();
  const result = [];
  for (const part of parts) {
    const key = part.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(part);
  }
  return result;
}

function _updateChatFilterHint() {
  const tags = parseFilterInput(document.getElementById('chat-tags-filter')?.value || '');
  const collections = parseFilterInput(document.getElementById('chat-collections-filter')?.value || '');
  const parts = [];
  if (tags.length) parts.push(`Tags: ${tags.join(', ')}`);
  if (collections.length) parts.push(`Collections: ${collections.join(', ')}`);
  const el = document.getElementById('chat-filters-hint');
  if (!el) return;
  el.textContent = parts.length ? parts.join(' | ') : _tr('Aucun filtre actif');
  document.getElementById('chat-filters-panel')?.classList.toggle('is-active', parts.length > 0);
  window.updateChatOptionsCount?.();
}

function clearChatFilters() {
  const tagsInput = document.getElementById('chat-tags-filter');
  const collectionsInput = document.getElementById('chat-collections-filter');
  if (tagsInput) tagsInput.value = '';
  if (collectionsInput) collectionsInput.value = '';
  _updateChatFilterHint();
}

function _applyZoteroFacets(facets) {
  if (!facets) return;
  state.zoteroFacets = facets;

  const tagsList = document.getElementById('zotero-tags-list');
  if (tagsList) {
    tagsList.innerHTML = (facets.tags || [])
      .map(t => `<option value="${escapeHtml(t.name)}">${escapeHtml(t.name)} (${t.count})</option>`)
      .join('');
  }

  const collectionsList = document.getElementById('zotero-collections-list');
  const collectionRows = facets.collections || [];
  if (collectionsList) {
    collectionsList.innerHTML = collectionRows
      .map(c => `<option value="${escapeHtml(c.path)}">${escapeHtml(c.path)} (${c.count})</option>`)
      .join('');
  }

  const notebookCollectionSelect = document.getElementById('nb-zotero-collection');
  if (notebookCollectionSelect) {
    const previousValue = notebookCollectionSelect.value;
    notebookCollectionSelect.innerHTML = [
      `<option value="" data-i18n="notebooks.chooseZoteroCollection">${escapeHtml(_ti('notebooks.chooseZoteroCollection'))}</option>`,
      ...collectionRows.map(c => (
        `<option value="${escapeAttr(c.path)}">${escapeHtml(c.path)} (${Number(c.count) || 0})</option>`
      )),
    ].join('');
    if (collectionRows.some(c => c.path === previousValue)) {
      notebookCollectionSelect.value = previousValue;
    }
  }

}

async function loadZoteroFacets() {
  try {
    const r = await fetch('/api/zotero-facets');
    const d = await r.json();
    _applyZoteroFacets(d);
  } catch (e) {
    console.error('loadZoteroFacets failed:', e);
    showToast(_tr('Filtres Zotero indisponibles.'), 3000, 'error');
  }
}
