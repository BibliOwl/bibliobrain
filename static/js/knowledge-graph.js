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

// Singular / plural variant of a keyed count: `${key}One` or `${key}Many`.
function _kgCount(key, count, params = {}) {
  return _ti(`${key}${Number(count) > 1 ? 'Many' : 'One'}`, { count, ...params });
}

function scheduleKnowledgeGraphReload(delay = 160) {
  clearTimeout(_kgLoadTimer);
  _kgLoadTimer = setTimeout(() => {
    const panel = document.getElementById('tab-connections');
    if (!panel?.classList.contains('active')) {
      state.knowledgeGraphDirty = true;
      return;
    }
    loadKnowledgeGraph();
  }, Math.max(20, delay));
}

// offerSetup: under a "nothing to show" message, the status pill's own
// "Préparer" / "Réparer" action while the library is not loaded.
function _setKnowledgeGraphStatus(msg, { offerSetup = false } = {}) {
  const el = document.getElementById('knowledge-graph-empty');
  if (!el) return;
  el.textContent = msg ? _tr(msg) : '';
  el.style.display = msg ? 'flex' : 'none';
  const setup = msg && offerSetup && !state.ragReady && !state.ragInitBusy ? state.ragFirstInitAction : '';
  if (!setup || typeof runFirstRagInit !== 'function') return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn btn-primary';
  button.textContent = _ti(setup === 'repair' ? 'connections.repairLibrary' : 'connections.prepareLibrary');
  button.disabled = state.ragInitAvailable === false;
  button.addEventListener('click', () => runFirstRagInit());
  el.appendChild(button);
}

function _setKnowledgeGraphBusy(title = '', detail = '') {
  const overlay = document.getElementById('kg-busy-overlay');
  const titleEl = document.getElementById('kg-busy-title');
  const detailEl = document.getElementById('kg-busy-detail');
  const wrap = document.getElementById('knowledge-graph-wrap');
  const open = Boolean(title || detail);
  if (!overlay) return;
  if (titleEl) titleEl.textContent = _tr(title || 'Traitement en cours');
  if (detailEl) detailEl.textContent = _tr(detail || 'Le graphe sera mis a jour automatiquement.');
  overlay.classList.toggle('open', open);
  overlay.setAttribute('aria-hidden', open ? 'false' : 'true');
  wrap?.setAttribute('aria-busy', open ? 'true' : 'false');
}

const _KG_COMPONENT_COLORS = [
  '#8b5cf6', '#06b6d4', '#f59e0b', '#10b981', '#ec4899',
  '#3b82f6', '#f97316', '#84cc16', '#e879f9', '#14b8a6',
];
const _CITATION_FOCUS_MAX_ARTICLES = 48;

function _assignKnowledgeComponents(nodes, edges) {
  const adjacency = new Map(nodes.map(node => [node.id, new Set()]));
  for (const edge of edges) {
    adjacency.get(edge.source)?.add(edge.target);
    adjacency.get(edge.target)?.add(edge.source);
  }
  const visited = new Set();
  const connected = [];
  const isolated = [];
  for (const node of nodes) {
    if (visited.has(node.id)) continue;
    const stack = [node.id];
    const ids = [];
    visited.add(node.id);
    while (stack.length) {
      const id = stack.pop();
      ids.push(id);
      for (const neighbor of adjacency.get(id) || []) {
        if (!visited.has(neighbor)) {
          visited.add(neighbor);
          stack.push(neighbor);
        }
      }
    }
    if (ids.length === 1) isolated.push(ids[0]);
    else connected.push(ids);
  }
  connected.sort((a, b) => b.length - a.length || String(a[0]).localeCompare(String(b[0])));
  const componentById = new Map();
  connected.forEach((ids, componentId) => ids.forEach(id => componentById.set(id, componentId)));
  isolated.forEach(id => componentById.set(id, -1));
  for (const node of nodes) {
    node._kgComponentId = componentById.get(node.id) ?? -1;
  }
  return componentById;
}

function _knowledgeComponentId(node) {
  const value = Number(node?._kgComponentId ?? node?.component_id ?? -1);
  return Number.isFinite(value) ? value : -1;
}

function _knowledgeComponentColor(componentId) {
  if (Number(componentId) < 0) return '#64748b';
  return _KG_COMPONENT_COLORS[Math.abs(Number(componentId)) % _KG_COMPONENT_COLORS.length];
}

function _knowledgeCommunityId(node) {
  const value = Number(node?.community_id ?? node?._kgComponentId ?? node?.component_id ?? -1);
  return Number.isFinite(value) ? value : -1;
}

function _citationCommunities(payload) {
  const provided = Array.isArray(payload?.communities) ? payload.communities : [];
  if (provided.length) return provided;
  const nodes = Array.isArray(payload?.nodes) ? payload.nodes : [];
  const edges = Array.isArray(payload?.edges) ? payload.edges : [];
  _assignKnowledgeComponents(nodes, edges);
  const byComponent = new Map();
  for (const node of nodes) {
    const id = _knowledgeComponentId(node);
    if (id < 0) continue;
    if (!byComponent.has(id)) byComponent.set(id, []);
    byComponent.get(id).push(node);
  }
  return [...byComponent.entries()].map(([id, members]) => ({
    id,
    label: _ti('connections.communityFallback', { n: id + 1 }),
    size: members.length,
    node_ids: members.map(node => node.id),
    internal_edges: edges.filter(edge => members.some(node => node.id === edge.source) && members.some(node => node.id === edge.target)).length,
    representative_title: [...members].sort((a, b) => (b.degree || 0) - (a.degree || 0))[0]?.title || '',
  }));
}

function _communityById(payload, communityId) {
  return _citationCommunities(payload).find(community => String(community.id) === String(communityId)) || null;
}

function _buildCommunityOverviewLayout(communities, communityEdges, width = 1200, height = 780) {
  const positions = {};
  if (!communities.length) return { positions, width, height };
  const cx = width / 2;
  const cy = height / 2;
  const orbitX = width * 0.32;
  const orbitY = height * 0.31;
  communities.forEach((community, index) => {
    if (index === 0) {
      positions[community.id] = { x: cx, y: cy };
      return;
    }
    const angle = -Math.PI / 2 + (Math.PI * 2 * (index - 1)) / Math.max(1, communities.length - 1);
    const ring = 0.72 + 0.28 * ((index % 3) / 2);
    positions[community.id] = {
      x: cx + Math.cos(angle) * orbitX * ring,
      y: cy + Math.sin(angle) * orbitY * ring,
    };
  });

  const edgeList = Array.isArray(communityEdges) ? communityEdges : [];
  for (let step = 0; step < 55; step++) {
    const forces = new Map(communities.map(community => [community.id, { x: 0, y: 0 }]));
    for (let i = 0; i < communities.length; i++) {
      for (let j = i + 1; j < communities.length; j++) {
        const a = communities[i].id;
        const b = communities[j].id;
        const dx = positions[a].x - positions[b].x;
        const dy = positions[a].y - positions[b].y;
        const distance2 = dx * dx + dy * dy + 1;
        const distance = Math.sqrt(distance2);
        const strength = 12000 / distance2;
        forces.get(a).x += dx / distance * strength;
        forces.get(a).y += dy / distance * strength;
        forces.get(b).x -= dx / distance * strength;
        forces.get(b).y -= dy / distance * strength;
      }
    }
    for (const edge of edgeList) {
      const source = positions[edge.source];
      const target = positions[edge.target];
      if (!source || !target) continue;
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.sqrt(dx * dx + dy * dy) || 1;
      const pull = (distance - 250) * 0.008 * Math.min(3, 1 + Number(edge.count || 1) / 4);
      forces.get(edge.source).x += dx / distance * pull;
      forces.get(edge.source).y += dy / distance * pull;
      forces.get(edge.target).x -= dx / distance * pull;
      forces.get(edge.target).y -= dy / distance * pull;
    }
    for (const community of communities) {
      const point = positions[community.id];
      const force = forces.get(community.id);
      point.x = Math.max(110, Math.min(width - 110, point.x + force.x + (cx - point.x) * 0.002));
      point.y = Math.max(100, Math.min(height - 100, point.y + force.y + (cy - point.y) * 0.002));
    }
  }
  return { positions, width, height };
}

function _buildKnowledgeLayout(nodes, edges) {
  const width = 1200;
  const height = 820;
  const positions = {};
  if (!nodes.length) return { positions, groups: [], width, height };
  const adjacency = new Map(nodes.map(node => [node.id, new Set()]));
  for (const edge of edges) {
    adjacency.get(edge.source)?.add(edge.target);
    adjacency.get(edge.target)?.add(edge.source);
  }
  const hub = [...nodes].sort((a, b) => (b.degree || 0) - (a.degree || 0))[0];
  const levelById = new Map([[hub.id, 0]]);
  const queue = [hub.id];
  while (queue.length) {
    const current = queue.shift();
    for (const neighbor of adjacency.get(current) || []) {
      if (!levelById.has(neighbor)) {
        levelById.set(neighbor, levelById.get(current) + 1);
        queue.push(neighbor);
      }
    }
  }
  const layers = new Map();
  for (const node of nodes) {
    const level = levelById.get(node.id) ?? 1;
    if (!layers.has(level)) layers.set(level, []);
    layers.get(level).push(node);
  }
  const cx = width / 2;
  const cy = height / 2;
  for (const [level, layerNodes] of [...layers.entries()].sort((a, b) => a[0] - b[0])) {
    const ordered = [...layerNodes].sort((a, b) => (b.degree || 0) - (a.degree || 0));
    if (level === 0) {
      positions[ordered[0].id] = { x: cx, y: cy };
      continue;
    }
    ordered.forEach((node, index) => {
      const ringOffset = Math.floor(index / 18);
      const ringNodes = ordered.slice(ringOffset * 18, (ringOffset + 1) * 18);
      const indexInRing = index % 18;
      const radius = 95 + level * 112 + ringOffset * 64;
      const angle = -Math.PI / 2 + Math.PI * 2 * indexInRing / Math.max(1, ringNodes.length) + level * 0.27;
      positions[node.id] = { x: cx + Math.cos(angle) * radius, y: cy + Math.sin(angle) * radius };
    });
  }
  return { positions, groups: [], width, height };
}

function _setKnowledgeFootnote(text) {
  const el = document.getElementById('knowledge-graph-footnote');
  if (el) el.textContent = text ? _tr(text) : '';
}

function _clearKnowledgeHighlight() {
  const svg = document.getElementById('knowledge-graph-svg');
  if (!svg) return;
  svg.classList.remove('kg-highlight-active');
  for (const element of state.knowledgeGraph?.highlightedElements || []) {
    element.classList.remove('kg-related');
  }
  if (state.knowledgeGraph) state.knowledgeGraph.highlightedElements = new Set();
}

function _highlightKnowledgeNode(nodeId) {
  const svg = document.getElementById('knowledge-graph-svg');
  if (!svg || !nodeId) return;
  _clearKnowledgeHighlight();
  const neighbors = state.knowledgeGraphAdj?.get(nodeId) || new Set();
  const index = state.knowledgeGraph?.domIndex;
  if (!index) return;
  const highlighted = new Set();
  for (const id of [nodeId, ...neighbors]) {
    for (const element of index.nodes.get(id) || []) {
      element.classList.add('kg-related');
      highlighted.add(element);
    }
  }
  for (const element of index.edgesByNode.get(nodeId) || []) {
    element.classList.add('kg-related');
    highlighted.add(element);
  }
  state.knowledgeGraph.highlightedElements = highlighted;
  svg.classList.add('kg-highlight-active');
}

async function selectRowByPath(relPath) {
  if (!relPath) return;
  const idx = state.libPaths.indexOf(relPath);
  if (idx < 0) return;
  const libBtn = document.querySelector('.nav-item[data-tab="library"]');
  switchTab('library', libBtn);
  setTimeout(async () => {
    await selectRow(idx);
    const row = document.querySelector(`#lib-tbody tr[data-i="${idx}"]`);
    if (row) {
      row.scrollIntoView({ block: 'center', behavior: preferredScrollBehavior() });
      row.classList.add('highlight-flash');
      setTimeout(() => row.classList.remove('highlight-flash'), 1200);
    }
  }, 80);
}

async function refreshKgNotebookTargets() {
  const select = document.getElementById('kg-notebook-target');
  try {
    const r = await fetch('/api/notebooks');
    const d = await r.json();
    const notebooks = Array.isArray(d?.notebooks) ? d.notebooks : [];
    state.knowledgeGraphNotebookList = notebooks;
    if (select) {
      select.innerHTML = '<option value="">' + _tr('Choisir un carnet existant…') + '</option>' +
        notebooks.map(nb => `<option value="${escapeAttr(nb.id)}">${escapeHtml(nb.title || _tr('Sans titre'))}</option>`).join('');
    }
  } catch (e) {
    state.knowledgeGraphNotebookList = [];
    if (select) select.innerHTML = '<option value="">' + _tr('Erreur chargement carnets') + '</option>';
  }
}

async function _importPathsToNotebook(paths, payload) {
  const response = await fetch('/api/notebooks/import-paths', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      ...payload,
      paths,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data?.detail || 'Import impossible');
  }
  return data;
}

async function _appendAnalysisToNotebook(text, payload) {
  const response = await fetch('/api/notebooks/append-analysis', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      ...payload,
      text,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data?.detail || 'Copie impossible');
  }
  return data;
}

function _setNotebookImportContext(context) {
  state.notebookImportContext = {
    source: 'library',
    action: 'import_paths',
    paths: [],
    modalTitle: 'Ajouter à un carnet',
    focusCountText: '0 article sélectionné.',
    defaultNewTitle: 'Nouveau carnet',
    emptyToast: 'Aucun article sélectionné.',
    submitLabelNew: 'Créer et ajouter',
    submitLabelExisting: 'Ajouter',
    noteText: '',
    ...context,
  };
}

function _refreshNotebookImportModalContent() {
  const ctx = state.notebookImportContext || {};
  const titleEl = document.getElementById('kg-modal-title');
  const countEl = document.getElementById('kg-modal-focus-count');
  const newTitle = document.getElementById('kg-modal-new-title');

  if (titleEl) titleEl.textContent = ctx.modalTitle || _tr('Ajouter à un carnet');
  if (countEl) countEl.textContent = ctx.focusCountText || _tr('0 article sélectionné.');
  if (newTitle && !newTitle.value.trim()) newTitle.value = ctx.defaultNewTitle || 'Nouveau carnet';
}

function _openNotebookImportModal(preferredMode = '') {
  const ctx = state.notebookImportContext || {};
  const count = Array.isArray(ctx.paths) ? ctx.paths.length : 0;
  if (!count) {
    showToast(ctx.emptyToast || 'Aucun article sélectionné.');
    return;
  }

  const overlay = document.getElementById('kg-modal-overlay');
  const modal = document.getElementById('kg-modal');
  const newTitle = document.getElementById('kg-modal-new-title');
  const hasNotebooks = Array.isArray(state.knowledgeGraphNotebookList) && state.knowledgeGraphNotebookList.length > 0;

  if (newTitle) newTitle.value = ctx.defaultNewTitle || 'Nouveau carnet';
  _refreshNotebookImportModalContent();

  let mode = preferredMode || (hasNotebooks ? 'existing' : 'new');
  if (mode === 'existing' && !hasNotebooks) mode = 'new';
  const radio = document.querySelector(`input[name="kg-notebook-mode"][value="${mode}"]`);
  if (radio) radio.checked = true;

  onKgNotebookModeChange();
  overlay?.classList.add('open');
  modal?.classList.add('open');

  setTimeout(() => {
    if (mode === 'new') {
      document.getElementById('kg-modal-new-title')?.focus();
    } else {
      document.getElementById('kg-notebook-target')?.focus();
    }
  }, 40);
}

function openKgNotebookModal(preferredMode = '') {
  const hasFocus = !!state.knowledgeGraphFocusNodeId;
  const hasCommunity = _kgIsInternalCitationPayload() && String(state.knowledgeGraphCommunityId ?? '') !== '';
  const paths = Array.isArray(state.knowledgeGraphVisiblePaths) ? state.knowledgeGraphVisiblePaths : [];
  const count = (hasFocus || hasCommunity) ? paths.length : 0;
  if ((!hasFocus && !hasCommunity) || !count) {
    showToast("Ouvrez une communauté ou sélectionnez un article.");
    return;
  }

  _setNotebookImportContext({
    source: 'graph',
    paths,
    modalTitle: hasFocus ? 'Ajouter les articles du focus à un carnet' : 'Ajouter cette communauté à un carnet',
    focusCountText: `${count} article(s) seront ajoutés au carnet.`,
    defaultNewTitle: 'Nouveau carnet (graphe)',
    emptyToast: "Ouvrez une communauté ou sélectionnez un article.",
  });
  _openNotebookImportModal(preferredMode);
}

function openLibraryNotebookModal(preferredMode = '') {
  const select = document.getElementById('article-select');
  const relPath = (select?.value || '').trim();
  if (!relPath) {
    showToast('Sélectionnez un article.');
    return;
  }

  const selectedLabel = select?.selectedOptions?.[0]?.textContent?.trim() || relPath.split('/').pop() || relPath;
  const safeTitle = selectedLabel.length > 72 ? `${selectedLabel.slice(0, 69)}…` : selectedLabel;

  _setNotebookImportContext({
    source: 'library',
    action: 'import_paths',
    paths: [relPath],
    modalTitle: "Ajouter l'article sélectionné à un carnet",
    focusCountText: `Article sélectionné : ${selectedLabel}`,
    defaultNewTitle: safeTitle || 'Nouveau carnet',
    emptyToast: 'Sélectionnez un article.',
  });
  _openNotebookImportModal(preferredMode);
}

function _currentAnalysisText() {
  const relPath = document.getElementById('article-select')?.value || '';
  const analysisType = document.getElementById('analysis-type-select')?.value || '';
  if (!relPath) return '';
  const bucket = _ensureArticleAnalysisState(relPath);
  if (bucket.lastView === 'visual') return String(bucket.visualText || '').trim();
  if (!analysisType) return '';
  return String(bucket.byType?.[analysisType] || '').trim();
}

function openAnalysisNotebookModal(preferredMode = '') {
  const relPath = document.getElementById('article-select')?.value || '';
  const analysisType = document.getElementById('analysis-type-select')?.value || '';
  const bucket = relPath ? _ensureArticleAnalysisState(relPath) : null;
  const analysisLabel = bucket?.lastView === 'visual' ? 'Lecture des figures' : analysisType;
  const analysisText = _currentAnalysisText();
  const visibleAnalysisText = stripThinkingMarkup(analysisText);
  if (!relPath || !analysisLabel || !visibleAnalysisText) {
    showToast("Aucune analyse à copier pour cet article.", 3200, 'error');
    return;
  }

  const articleTitle = document.getElementById('library-detail-title')?.textContent?.trim()
    || document.getElementById('article-select')?.selectedOptions?.[0]?.textContent?.trim()
    || relPath.split('/').pop()
    || relPath;
  const safeTitle = articleTitle.length > 68 ? `${articleTitle.slice(0, 65)}…` : articleTitle;
  const noteText = `## ${articleTitle} — ${analysisLabel}\n\n${visibleAnalysisText}`;

  _setNotebookImportContext({
    source: 'analysis',
    action: 'append_analysis',
    paths: [relPath],
    noteText,
    modalTitle: "Copier l'analyse dans les notes d'un carnet",
    focusCountText: `Analyse prête pour : ${articleTitle}`,
    defaultNewTitle: safeTitle || 'Nouveau carnet',
    emptyToast: "Aucune analyse à copier pour cet article.",
    submitLabelNew: 'Créer et copier',
    submitLabelExisting: 'Copier dans les notes',
  });
  _openNotebookImportModal(preferredMode);
}

function closeKgNotebookModal() {
  document.getElementById('kg-modal-overlay')?.classList.remove('open');
  document.getElementById('kg-modal')?.classList.remove('open');
}

function onKgNotebookModeChange() {
  const mode = document.querySelector('input[name="kg-notebook-mode"]:checked')?.value || 'new';
  const newBlock = document.getElementById('kg-modal-new');
  const existingBlock = document.getElementById('kg-modal-existing');
  const submitBtn = document.getElementById('kg-modal-submit-btn');
  const hasNotebooks = Array.isArray(state.knowledgeGraphNotebookList) && state.knowledgeGraphNotebookList.length > 0;
  const ctx = state.notebookImportContext || {};
  const submitLabelNew = ctx.submitLabelNew || 'Créer et ajouter';
  const submitLabelExisting = ctx.submitLabelExisting || 'Ajouter';

  if (newBlock) newBlock.style.display = mode === 'new' ? 'flex' : 'none';
  if (existingBlock) existingBlock.style.display = mode === 'existing' ? 'flex' : 'none';
  if (submitBtn) submitBtn.textContent = mode === 'new' ? submitLabelNew : submitLabelExisting;

  const existingRadio = document.querySelector('input[name="kg-notebook-mode"][value="existing"]');
  if (existingRadio) existingRadio.disabled = !hasNotebooks;
  if (mode === 'existing' && !hasNotebooks) {
    const newRadio = document.querySelector('input[name="kg-notebook-mode"][value="new"]');
    if (newRadio) newRadio.checked = true;
    if (newBlock) newBlock.style.display = 'flex';
    if (existingBlock) existingBlock.style.display = 'none';
    if (submitBtn) submitBtn.textContent = submitLabelNew;
  }
}

async function submitKgNotebookFromModal() {
  const mode = document.querySelector('input[name="kg-notebook-mode"]:checked')?.value || 'new';
  const ctx = state.notebookImportContext || {};
  const paths = Array.isArray(ctx.paths) ? ctx.paths : [];
  const submitBtn = document.getElementById('kg-modal-submit-btn');
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="spinner"></span>';
  }
  try {
    if (!paths.length) {
      showToast(ctx.emptyToast || 'Aucun article sélectionné.');
      return;
    }

    let payload = {};
    if (mode === 'new') {
      const title = (document.getElementById('kg-modal-new-title')?.value || '').trim() || ctx.defaultNewTitle || 'Nouveau carnet';
      payload = { title };
    } else {
      const select = document.getElementById('kg-notebook-target');
      const nbId = (select?.value || '').trim();
      if (!nbId) {
        showToast('Choisissez un carnet existant.');
        return;
      }
      payload = { notebook_id: nbId };
    }

    let d = null;
    if (ctx.action === 'append_analysis') {
      const noteText = String(ctx.noteText || '').trim();
      if (!noteText) {
        showToast("Aucune analyse à copier pour cet article.");
        return;
      }
      d = await _appendAnalysisToNotebook(noteText, payload);
    } else {
      d = await _importPathsToNotebook(paths, payload);
    }
    if (!d) return;
    closeKgNotebookModal();
    if (ctx.action === 'append_analysis') {
      showToast(`Analyse copiée dans "${d.notebook_title || 'le carnet'}".`, 4000, 'success');
    } else {
      showToast(`Import terminé : ${d.added} ajoutés, ${d.skipped} déjà présents, ${d.failed} échecs.`, 4000, 'success');
    }
    await refreshKgNotebookTargets();
    if (_nb.id === d.notebook_id) {
      await openNotebook(d.notebook_id);
    } else if (document.getElementById('tab-notebooks')?.classList.contains('active')) {
      await loadNotebooks();
    }
    state.discoveryNotebooksLoaded = false;
    _updateKnowledgeFocusActions();
  } catch (e) {
    showToast(`Erreur ajout au carnet : ${e?.message || 'inconnue'}`, 4200, 'error');
  } finally {
    if (submitBtn) {
      submitBtn.disabled = false;
      const labelMode = (document.querySelector('input[name="kg-notebook-mode"]:checked')?.value || 'new') === 'new';
      submitBtn.textContent = labelMode
        ? (ctx.submitLabelNew || 'Créer et ajouter')
        : (ctx.submitLabelExisting || 'Ajouter');
    }
    _updateKnowledgeFocusActions();
  }
}

function _knowledgeGraphPointFromClient(svg, clientX, clientY) {
  const rect = svg.getBoundingClientRect();
  const vb = svg.viewBox?.baseVal;
  if (!vb || !rect.width || !rect.height) return { x: 500, y: 215 };

  const vbRatio = vb.width / vb.height;
  const rectRatio = rect.width / rect.height;

  let drawX = rect.left;
  let drawY = rect.top;
  let drawW = rect.width;
  let drawH = rect.height;

  if (rectRatio > vbRatio) {
    drawW = rect.height * vbRatio;
    drawX = rect.left + (rect.width - drawW) / 2;
  } else {
    drawH = rect.width / vbRatio;
    drawY = rect.top + (rect.height - drawH) / 2;
  }

  const nx = Math.max(0, Math.min(1, (clientX - drawX) / Math.max(1, drawW)));
  const ny = Math.max(0, Math.min(1, (clientY - drawY) / Math.max(1, drawH)));
  return { x: vb.x + nx * vb.width, y: vb.y + ny * vb.height };
}

function _applyKnowledgeGraphView() {
  const svg = document.getElementById('knowledge-graph-svg');
  if (!svg) return;
  const viewport = svg.querySelector('#kg-viewport');
  if (!viewport) return;
  const { scale, tx, ty } = state.knowledgeGraphView;
  viewport.setAttribute(
    'transform',
    `matrix(${scale.toFixed(4)} 0 0 ${scale.toFixed(4)} ${tx.toFixed(2)} ${ty.toFixed(2)})`
  );
  const zoomValue = document.getElementById('kg-network-zoom-value');
  if (zoomValue) zoomValue.textContent = `${Math.round(scale * 100)}%`;
}

function _resetKnowledgeGraphView() {
  state.knowledgeGraphView = { scale: 1, tx: 0, ty: 0 };
  _applyKnowledgeGraphView();
}

function zoomKnowledgeGraph(factor) {
  if (!_kgIsInternalCitationPayload()) return;
  const graph = state.knowledgeGraph;
  if (!graph) return;
  const oldScale = Number(state.knowledgeGraphView.scale || 1);
  const newScale = Math.max(0.2, Math.min(8, oldScale * Number(factor || 1)));
  const cx = Number(graph.canvasWidth || 1200) / 2;
  const cy = Number(graph.canvasHeight || 900) / 2;
  state.knowledgeGraphView.tx += (oldScale - newScale) * cx;
  state.knowledgeGraphView.ty += (oldScale - newScale) * cy;
  state.knowledgeGraphView.scale = newScale;
  _applyKnowledgeGraphView();
}

function _fitKnowledgeGraphView(nodes = null, layout = null) {
  const graph = state.knowledgeGraph;
  const visibleNodes = nodes || graph?.payload?.nodes || [];
  const positions = layout || graph?.layout || {};
  if (!graph || !visibleNodes.length) {
    _resetKnowledgeGraphView();
    return;
  }
  const points = visibleNodes.map(node => positions[node.id]).filter(Boolean);
  if (!points.length) {
    _resetKnowledgeGraphView();
    return;
  }
  const minX = Math.min(...points.map(point => point.x));
  const maxX = Math.max(...points.map(point => point.x));
  const minY = Math.min(...points.map(point => point.y));
  const maxY = Math.max(...points.map(point => point.y));
  const canvasWidth = Number(graph.canvasWidth || 1200);
  const canvasHeight = Number(graph.canvasHeight || 900);
  const contentWidth = Math.max(120, maxX - minX + 150);
  const contentHeight = Math.max(120, maxY - minY + 150);
  const scale = Math.max(0.2, Math.min(8, Math.min(canvasWidth / contentWidth, canvasHeight / contentHeight) * 0.92));
  state.knowledgeGraphView = {
    scale,
    tx: canvasWidth / 2 - ((minX + maxX) / 2) * scale,
    ty: canvasHeight / 2 - ((minY + maxY) / 2) * scale,
  };
  _applyKnowledgeGraphView();
}

function fitKnowledgeGraphView() {
  const graph = state.knowledgeGraph;
  if (!graph) return;
  const payload = graph.payload || {};
  const communityId = String(state.knowledgeGraphCommunityId ?? '');
  if (!state.knowledgeGraphFocusNodeId && communityId === '') {
    const communities = _citationCommunities(payload);
    _fitKnowledgeGraphView(communities, graph.overviewLayout || {});
    return;
  }
  const visible = _resolveKnowledgeFocus(payload.nodes || [], payload.edges || []);
  let layout = visible.focusNode
    ? _buildCitationFocusLayout(
        visible.focusNode,
        visible.nodes,
        visible.edges,
        Number(graph.canvasWidth || 1200),
        Number(graph.canvasHeight || 900)
      )
    : graph.communityLayouts?.get(communityId)?.positions;
  if (!layout) {
    const built = _buildKnowledgeLayout(visible.nodes, visible.edges);
    layout = built.positions;
  }
  _fitKnowledgeGraphView(visible.nodes, layout);
}

function _bindKnowledgeGraphUi() {
  const svg = document.getElementById('knowledge-graph-svg');
  const wrap = document.getElementById('knowledge-graph-wrap');
  if (!svg || !wrap || state.knowledgeGraphUiBound) return;
  state.knowledgeGraphUiBound = true;

  svg.addEventListener('wheel', (event) => {
    event.preventDefault();
    const oldScale = state.knowledgeGraphView.scale;
    const zoomFactor = event.deltaY < 0 ? 1.12 : 0.9;
    const newScale = Math.max(0.2, Math.min(8, oldScale * zoomFactor));
    if (Math.abs(newScale - oldScale) < 0.0001) return;
    const pivot = _knowledgeGraphPointFromClient(svg, event.clientX, event.clientY);
    state.knowledgeGraphView.tx += (oldScale - newScale) * pivot.x;
    state.knowledgeGraphView.ty += (oldScale - newScale) * pivot.y;
    state.knowledgeGraphView.scale = newScale;
    _applyKnowledgeGraphView();
  }, { passive: false });

  svg.addEventListener('mousedown', (event) => {
    if (event.button !== 0) return;
    if (event.target.closest('.kg-node-wrap')) return;
    state.knowledgeGraphDrag.active = true;
    state.knowledgeGraphDrag.moved = false;
    state.knowledgeGraphDrag.lastX = event.clientX;
    state.knowledgeGraphDrag.lastY = event.clientY;
    wrap.classList.add('dragging');
    document.body.style.userSelect = 'none';
  });

  window.addEventListener('mousemove', (event) => {
    if (!state.knowledgeGraphDrag.active) return;
    const vb = svg.viewBox?.baseVal;
    const rect = svg.getBoundingClientRect();
    if (!vb || !rect.width || !rect.height) return;

    const dxPx = event.clientX - state.knowledgeGraphDrag.lastX;
    const dyPx = event.clientY - state.knowledgeGraphDrag.lastY;
    if (Math.abs(dxPx) + Math.abs(dyPx) > 1) state.knowledgeGraphDrag.moved = true;

    const dx = (dxPx / rect.width) * vb.width;
    const dy = (dyPx / rect.height) * vb.height;
    state.knowledgeGraphView.tx += dx;
    state.knowledgeGraphView.ty += dy;
    state.knowledgeGraphDrag.lastX = event.clientX;
    state.knowledgeGraphDrag.lastY = event.clientY;
    _applyKnowledgeGraphView();
  });

  window.addEventListener('mouseup', () => {
    if (!state.knowledgeGraphDrag.active) return;
    state.knowledgeGraphDrag.active = false;
    wrap.classList.remove('dragging');
    document.body.style.userSelect = '';
  });

  svg.addEventListener('mouseover', (event) => {
    const communityWrap = event.target.closest?.('.kg-community-node-wrap');
    if (communityWrap && !communityWrap.contains(event.relatedTarget)) {
      const community = _communityById(state.knowledgeGraph?.payload, communityWrap.dataset.communityId || '');
      if (community) {
        const years = community.year_min && community.year_max ? ` · ${community.year_min}–${community.year_max}` : '';
        _setKnowledgeFootnote(`${community.label} · ${_ti('connections.communityTooltip', { size: community.size, edges: community.internal_edges || 0 })}${years}`);
      }
      return;
    }
    const nodeWrap = event.target.closest?.('.kg-node-wrap');
    if (!nodeWrap || nodeWrap.contains(event.relatedTarget)) return;
    const id = nodeWrap.dataset.id || '';
    _highlightKnowledgeNode(id);
    const node = state.knowledgeGraph?.nodeById?.get(id);
    if (node) {
      _setKnowledgeFootnote(`${node.author || _tr('Auteur inconnu')} (${node.year || 'n.d.'}) - ${node.title || _tr('Sans titre')}`);
    }
  });

  svg.addEventListener('mouseout', (event) => {
    const communityWrap = event.target.closest?.('.kg-community-node-wrap');
    if (communityWrap && !communityWrap.contains(event.relatedTarget)) {
      _setKnowledgeFootnote(state.knowledgeGraph?.baseFootnote || '');
      return;
    }
    const nodeWrap = event.target.closest?.('.kg-node-wrap');
    if (!nodeWrap || nodeWrap.contains(event.relatedTarget)) return;
    _clearKnowledgeHighlight();
    _setKnowledgeFootnote(state.knowledgeGraph?.baseFootnote || '');
  });

  svg.addEventListener('click', (event) => {
    if (state.knowledgeGraphDrag.moved) {
      state.knowledgeGraphDrag.moved = false;
      return;
    }
    const communityWrap = event.target.closest?.('.kg-community-node-wrap');
    if (communityWrap) {
      event.stopPropagation();
      openKnowledgeCommunity(communityWrap.dataset.communityId || '');
      return;
    }
    const nodeWrap = event.target.closest?.('.kg-node-wrap');
    if (nodeWrap) {
      event.stopPropagation();
      const id = nodeWrap.dataset.id || '';
      if (state.knowledgeGraphNodeClickTimer) clearTimeout(state.knowledgeGraphNodeClickTimer);
      state.knowledgeGraphNodeClickTimer = setTimeout(() => {
        _toggleKnowledgeFocus(id);
        state.knowledgeGraphNodeClickTimer = null;
      }, 220);
      return;
    }
    if (state.knowledgeGraphFocusNodeId) clearKnowledgeFocus();
  });

  svg.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const communityWrap = event.target.closest?.('.kg-community-node-wrap');
    if (!communityWrap) return;
    event.preventDefault();
    openKnowledgeCommunity(communityWrap.dataset.communityId || '');
  });

  svg.addEventListener('dblclick', (event) => {
    const nodeWrap = event.target.closest?.('.kg-node-wrap');
    if (!nodeWrap) return;
    event.stopPropagation();
    if (state.knowledgeGraphNodeClickTimer) {
      clearTimeout(state.knowledgeGraphNodeClickTimer);
      state.knowledgeGraphNodeClickTimer = null;
    }
    const path = nodeWrap.dataset.path || '';
    if (path) selectRowByPath(path);
  });
}

function _resolveKnowledgeFocus(nodes, edges) {
  const focusId = state.knowledgeGraphFocusNodeId;
  const communityId = String(state.knowledgeGraphCommunityId ?? '');
  if (!focusId && communityId !== '') {
    const keep = new Set(
      nodes
        .filter(node => String(_knowledgeCommunityId(node)) === communityId)
        .map(node => node.id)
    );
    return {
      nodes: nodes.filter(node => keep.has(node.id)),
      edges: edges.filter(edge => keep.has(edge.source) && keep.has(edge.target)),
      focusNode: null,
    };
  }
  if (!focusId) return { nodes, edges, focusNode: null };
  const focusNode = nodes.find(n => n.id === focusId);
  if (!focusNode) return { nodes, edges, focusNode: null };

  const nodeById = new Map(nodes.map(node => [node.id, node]));
  const focusCommunityId = _knowledgeCommunityId(focusNode);
  const neighbors = [...(state.knowledgeGraphAdj?.get(focusId) || new Set())]
    .sort((leftId, rightId) => {
      const left = nodeById.get(leftId) || {};
      const right = nodeById.get(rightId) || {};
      const leftSameCommunity = _knowledgeCommunityId(left) === focusCommunityId ? 1 : 0;
      const rightSameCommunity = _knowledgeCommunityId(right) === focusCommunityId ? 1 : 0;
      return rightSameCommunity - leftSameCommunity
        || Number(right.degree || 0) - Number(left.degree || 0)
        || String(left.title || '').localeCompare(String(right.title || ''));
    })
    .slice(0, _CITATION_FOCUS_MAX_ARTICLES - 1);
  const keep = new Set([focusId, ...neighbors]);
  const filteredNodes = nodes.filter(n => keep.has(n.id));
  const filteredEdges = edges.filter(
    edge => (edge.source === focusId || edge.target === focusId)
      && keep.has(edge.source)
      && keep.has(edge.target)
  );
  return { nodes: filteredNodes, edges: filteredEdges, focusNode };
}

function _buildCitationFocusLayout(focusNode, nodes, edges, canvasWidth, canvasHeight) {
  const layout = {};
  const centerX = canvasWidth / 2;
  const centerY = canvasHeight / 2;
  layout[focusNode.id] = { x: centerX, y: centerY };
  const outgoing = [];
  const incoming = [];
  for (const edge of edges) {
    if (edge.source === focusNode.id && edge.target !== focusNode.id) outgoing.push(edge.target);
    else if (edge.target === focusNode.id && edge.source !== focusNode.id) incoming.push(edge.source);
  }
  const placed = new Set([focusNode.id]);
  const placeSide = (ids, direction) => {
    const uniqueIds = [...new Set(ids)].filter(id => !placed.has(id));
    const perColumn = Math.max(6, Math.min(12, Math.ceil(Math.sqrt(uniqueIds.length) * 2.2)));
    uniqueIds.forEach((id, index) => {
      const column = Math.floor(index / perColumn);
      const row = index % perColumn;
      const countInColumn = Math.min(perColumn, uniqueIds.length - column * perColumn);
      layout[id] = {
        x: centerX + direction * (175 + column * 125),
        y: centerY + (row - (countInColumn - 1) / 2) * Math.min(74, canvasHeight * 0.72 / Math.max(1, countInColumn - 1)),
      };
      placed.add(id);
    });
  };
  placeSide(outgoing, -1);
  placeSide(incoming, 1);
  for (const node of nodes) {
    if (!placed.has(node.id)) layout[node.id] = { x: centerX, y: centerY };
  }
  return layout;
}

function _toggleKnowledgeFocus(nodeId) {
  if (!nodeId) return;
  state.knowledgeGraphFocusNodeId = state.knowledgeGraphFocusNodeId === nodeId ? '' : nodeId;
  _renderKnowledgeGraphScene(false);
}

function setKnowledgeGraphComponentFilter(value) {
  state.knowledgeGraphComponentFilter = String(value ?? '');
  state.knowledgeGraphCommunityId = String(value ?? '');
  state.knowledgeGraphFocusNodeId = '';
  _renderKnowledgeGraphScene(false);
}

function openKnowledgeCommunity(communityId) {
  const value = String(communityId ?? '');
  if (!value) return;
  state.knowledgeGraphCommunityId = value;
  state.knowledgeGraphComponentFilter = value;
  state.knowledgeGraphFocusNodeId = '';
  const filter = document.getElementById('kg-component-filter');
  if (filter) filter.value = value;
  _renderKnowledgeGraphScene(false);
}

function showCitationCommunityOverview() {
  state.knowledgeGraphCommunityId = '';
  state.knowledgeGraphComponentFilter = '';
  state.knowledgeGraphFocusNodeId = '';
  const filter = document.getElementById('kg-component-filter');
  if (filter) filter.value = '';
  _renderKnowledgeGraphScene(false);
}

function _renderCitationBreadcrumb(payload, focusNode = null) {
  const breadcrumb = document.getElementById('kg-network-breadcrumb');
  if (!breadcrumb) return;
  const communityId = String(state.knowledgeGraphCommunityId ?? '');
  const community = communityId !== '' ? _communityById(payload, communityId) : null;
  const isolated = Number(payload?.stats?.isolated_articles || 0);
  breadcrumb.hidden = false;
  breadcrumb.innerHTML = `
    <button type="button" class="kg-crumb ${community ? '' : 'active'}" ${bindBbHandler('click', function() { showCitationCommunityOverview(); })}>${escapeHtml(_ti('connections.communities'))}</button>
    ${community ? `<span class="kg-crumb-separator">›</span><button type="button" class="kg-crumb ${focusNode ? '' : 'active'}" ${bindBbHandler('click', function() { openKnowledgeCommunity(String(community.id)); })}>${escapeHtml(community.label || _ti('connections.communityFallback', { n: Number(community.id) + 1 }))}</button>` : ''}
    ${focusNode ? `<span class="kg-crumb-separator">›</span><span class="kg-crumb-current">${escapeHtml(focusNode.title || _ti('connections.selectedPaper'))}</span>` : ''}
    ${community && !focusNode ? `<button type="button" class="kg-crumb kg-crumb-action" ${bindBbHandler('click', function() { openKgNotebookModal(); })}>${escapeHtml(_ti('connections.addCommunity'))}</button>` : ''}
    ${!community && isolated ? `<span class="kg-isolated-summary">${escapeHtml(_kgCount('connections.isolated', isolated))}</span>` : ''}
  `;
}

function _renderCitationCommunityOverview(preserveView = true) {
  const svg = document.getElementById('knowledge-graph-svg');
  const wrap = document.getElementById('knowledge-graph-wrap');
  const graph = state.knowledgeGraph;
  const payload = graph?.payload || {};
  if (!svg || !graph) return;
  const communities = _citationCommunities(payload);
  const rawEdges = Array.isArray(payload.community_edges) ? payload.community_edges : [];
  const aggregateByPair = new Map();
  for (const edge of rawEdges) {
    const pair = [Number(edge.source), Number(edge.target)].sort((a, b) => a - b);
    const key = pair.join(':');
    const current = aggregateByPair.get(key) || { source: pair[0], target: pair[1], count: 0 };
    current.count += Number(edge.count || 1);
    aggregateByPair.set(key, current);
  }
  const communityEdges = [...aggregateByPair.values()];
  const overview = _buildCommunityOverviewLayout(communities, communityEdges);
  graph.overviewLayout = overview.positions;
  graph.canvasWidth = overview.width;
  graph.canvasHeight = overview.height;
  wrap?.classList.remove('citation-has-focus');
  wrap?.classList.add('citation-community-overview');
  svg.setAttribute('viewBox', `0 0 ${overview.width} ${overview.height}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  state.knowledgeGraphVisiblePaths = [];

  const edgeSvg = communityEdges.map(edge => {
    const source = overview.positions[edge.source];
    const target = overview.positions[edge.target];
    if (!source || !target) return '';
    const width = Math.min(8, 1.2 + Math.sqrt(Number(edge.count || 1)) * 1.2);
    return `<line class="kg-community-edge" x1="${source.x.toFixed(1)}" y1="${source.y.toFixed(1)}" x2="${target.x.toFixed(1)}" y2="${target.y.toFixed(1)}" stroke-width="${width.toFixed(1)}"><title>${escapeHtml(_ti('connections.edgeBetweenCommunities', { count: Number(edge.count || 0) }))}</title></line>`;
  }).join('');
  const nodeSvg = communities.map(community => {
    const point = overview.positions[community.id];
    if (!point) return '';
    const radius = Math.max(34, Math.min(72, 27 + Math.sqrt(Number(community.size || 1)) * 5.3));
    const color = _knowledgeComponentColor(community.id);
    const yearText = community.year_min && community.year_max
      ? `${community.year_min}–${community.year_max}`
      : '';
    return `<g class="kg-community-node-wrap" data-community-id="${escapeAttr(String(community.id))}" tabindex="0" role="button" aria-label="${escapeAttr(_ti('connections.openCommunityAria', { label: community.label || _ti('connections.thisCommunity') }))}">
      <circle class="kg-community-node" cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="${radius.toFixed(1)}" style="--kg-community-color:${escapeAttr(color)}"></circle>
      <text class="kg-community-count" x="${point.x.toFixed(1)}" y="${(point.y + 6).toFixed(1)}">${Number(community.size || 0)}</text>
      <text class="kg-community-label" data-i18n-skip x="${point.x.toFixed(1)}" y="${(point.y + radius + 25).toFixed(1)}">${escapeHtml(community.label || _ti('connections.communityFallback', { n: Number(community.id) + 1 }))}</text>
      ${yearText ? `<text class="kg-community-years" x="${point.x.toFixed(1)}" y="${(point.y + radius + 43).toFixed(1)}">${yearText}</text>` : ''}
      <title>${escapeHtml(community.representative_title || community.label || '')}\n${escapeHtml(_ti('connections.communityTooltip', { size: Number(community.size || 0), edges: Number(community.internal_edges || 0) }))}</title>
    </g>`;
  }).join('');
  svg.innerHTML = `<g id="kg-viewport"><g>${edgeSvg}</g><g>${nodeSvg}</g></g>`;
  graph.domIndex = { nodes: new Map(), edgesByNode: new Map() };
  graph.highlightedElements = new Set();
  _bindKnowledgeGraphUi();
  if (!preserveView) _fitKnowledgeGraphView(communities, overview.positions);
  else _applyKnowledgeGraphView();
  _renderCitationBreadcrumb(payload);
  _updateKnowledgeFocusButtonState();
  _updateKnowledgeFocusActions();
  const stats = payload.stats || {};
  const cacheTxt = stats.cached ? ' · cache' : '';
  graph.baseFootnote = _ti('connections.overviewFootnote', {
    communities: communities.length,
    connected: Number(stats.connected_articles || 0),
    isolated: Number(stats.isolated_articles || 0),
    cache: cacheTxt,
  });
  _setKnowledgeFootnote(graph.baseFootnote);
  _renderInternalCitationDetail('');
  _applyInternalCitationSearchHighlight();
}

function _renderKnowledgeGraphScene(preserveView = true) {
  const svg = document.getElementById('knowledge-graph-svg');
  const wrap = document.getElementById('knowledge-graph-wrap');
  const graph = state.knowledgeGraph;
  if (!svg || !graph?.payload) return;

  const payload = graph.payload;
  const allNodes = Array.isArray(payload?.nodes) ? payload.nodes : [];
  const allEdges = Array.isArray(payload?.edges) ? payload.edges : [];
  const communityId = String(state.knowledgeGraphCommunityId ?? '');
  if (_kgIsInternalCitationPayload(payload) && !state.knowledgeGraphFocusNodeId && communityId === '') {
    _renderCitationCommunityOverview(preserveView);
    return;
  }

  const visible = _resolveKnowledgeFocus(allNodes, allEdges);
  const nodes = visible.nodes;
  const edges = visible.edges;
  const focusNode = visible.focusNode;
  if (_kgIsInternalCitationPayload(payload)) {
    wrap?.classList.toggle('citation-has-focus', Boolean(focusNode));
  }
  let canvasWidth = 1200;
  let canvasHeight = 820;
  let layout;
  if (focusNode) {
    layout = _buildCitationFocusLayout(focusNode, nodes, edges, canvasWidth, canvasHeight);
  } else {
    graph.communityLayouts ||= new Map();
    let communityLayout = graph.communityLayouts.get(communityId);
    if (!communityLayout) {
      communityLayout = _buildKnowledgeLayout(nodes, edges);
      graph.communityLayouts.set(communityId, communityLayout);
    }
    layout = communityLayout.positions;
    canvasWidth = communityLayout.width;
    canvasHeight = communityLayout.height;
  }
  graph.canvasWidth = canvasWidth;
  graph.canvasHeight = canvasHeight;
  wrap?.classList.remove('citation-community-overview');
  svg.setAttribute('viewBox', `0 0 ${canvasWidth.toFixed(1)} ${canvasHeight.toFixed(1)}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  state.knowledgeGraphVisiblePaths = nodes
    .map(n => String(n?.path || '').trim())
    .filter(Boolean)
    .filter((p, idx, arr) => arr.indexOf(p) === idx);
  graph.visibleNodeIds = new Set(nodes.map(node => node.id));

  const topLabelIds = new Set(
    [...nodes]
      .sort((a, b) => (b.degree || 0) - (a.degree || 0))
      .slice(0, Math.min(focusNode ? 5 : 1, nodes.length))
      .map(node => node.id)
  );

  const nodeById = new Map(allNodes.map(n => [n.id, n]));
  const nodeRadius = node => Math.max(4.5, Math.min(15, Number(node?.size) || 7));
  const groupSvg = '';
  const edgeSvg = edges.map(edge => {
    const source = layout[edge.source];
    const target = layout[edge.target];
    if (!source || !target) return '';
    const score = Number(edge.score ?? 1);
    const widthPx = (0.65 + Math.min(1.7, score * 2.2)).toFixed(2);
    const alpha = (_kgIsInternalCitationPayload(payload) && !focusNode)
      ? '0.18'
      : Math.max(0.22, Math.min(0.82, score + 0.12)).toFixed(2);
    const dx = target.x - source.x;
    const dy = target.y - source.y;
    const distance = Math.sqrt(dx * dx + dy * dy) || 1;
    const targetRadius = nodeRadius(nodeById.get(edge.target)) + 4;
    const x2 = target.x - (dx / distance) * targetRadius;
    const y2 = target.y - (dy / distance) * targetRadius;
    const marker = edge.kind === 'citation' ? ' marker-end="url(#kg-citation-arrow)"' : '';
    const directionClass = focusNode
      ? (edge.source === focusNode.id ? 'outgoing' : edge.target === focusNode.id ? 'incoming' : '')
      : '';
    return `<line class="kg-edge ${escapeAttr(edge.kind || 'semantic')} ${directionClass}" data-source="${escapeAttr(edge.source)}" data-target="${escapeAttr(edge.target)}" x1="${source.x.toFixed(1)}" y1="${source.y.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke-width="${widthPx}" opacity="${alpha}"${marker} />`;
  }).join('');

  const nodeSvg = nodes.map(node => {
    const p = layout[node.id];
    if (!p) return '';
    const radius = nodeRadius(node);
    const componentId = _knowledgeCommunityId(node);
    const color = _knowledgeComponentColor(componentId);
    const citationMeta = _kgIsInternalCitationPayload(payload)
      ? `\n${_ti('connections.nodeCitationMeta', { out: Number(node.out_degree || 0), in: Number(node.in_degree || 0) })}`
      : '';
    const title = `${node.title || ''}\n${node.author || ''} (${node.year || 'n.d.'})${citationMeta}`;
    const label = topLabelIds.has(node.id) ? `<text class="kg-node-label" data-i18n-skip data-id="${escapeAttr(node.id)}" x="${(p.x + radius + 5).toFixed(1)}" y="${(p.y - radius - 3).toFixed(1)}">${escapeHtml(node.title || '').slice(0, 44)}</text>` : '';
    return `
      <g class="kg-node-wrap" data-id="${escapeAttr(node.id)}" data-path="${escapeAttr(node.path || '')}" data-group="${escapeAttr(node.group || '')}" data-component="${componentId}">
        <circle class="kg-node" data-id="${escapeAttr(node.id)}" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${radius.toFixed(1)}" fill="${color}">
          <title>${escapeHtml(title)}</title>
        </circle>
        ${label}
      </g>
    `;
  }).join('');

  svg.innerHTML = `<defs><marker id="kg-citation-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke"></path></marker></defs><g id="kg-viewport"><g>${groupSvg}</g><g>${edgeSvg}</g><g>${nodeSvg}</g></g>`;
  const nodesIndex = new Map();
  svg.querySelectorAll('.kg-node-wrap').forEach(element => {
    const id = element.dataset.id || '';
    nodesIndex.set(id, [element.querySelector('.kg-node'), element.querySelector('.kg-node-label')].filter(Boolean));
  });
  const edgesByNode = new Map();
  svg.querySelectorAll('.kg-edge').forEach(element => {
    for (const id of [element.dataset.source || '', element.dataset.target || '']) {
      if (!edgesByNode.has(id)) edgesByNode.set(id, []);
      edgesByNode.get(id).push(element);
    }
  });
  graph.domIndex = { nodes: nodesIndex, edgesByNode };
  graph.highlightedElements = new Set();
  _bindKnowledgeGraphUi();
  if (!preserveView) _fitKnowledgeGraphView(nodes, layout);
  else _applyKnowledgeGraphView();
  _updateKnowledgeFocusButtonState();
  _updateKnowledgeFocusActions();

  const stats = payload?.stats || {};
  const modeLabel = _kgIsInternalCitationPayload(payload)
    ? _ti('connections.modeInternalCitations')
    : payload?.mode || 'hybrid';
  const buildMs = Number(stats.build_ms || 0);
  const warn = payload?.warning ? ` | ${payload.warning}` : '';
  const focusTxt = focusNode ? ` | focus: ${focusNode.title || _tr('Sans titre')}` : '';
  const cacheTxt = stats.cached ? ` | ${_ti('connections.instantCache')}` : '';
  const currentCommunity = communityId !== '' ? _communityById(payload, communityId) : null;
  const communityTxt = currentCommunity ? ` | ${_ti('connections.communityPart', { label: currentCommunity.label })}` : '';
  const baseFootnote = `${_ti('connections.networkCounts', { nodes: nodes.length, edges: edges.length })}${communityTxt} | mode: ${modeLabel} | ${_ti('connections.computedIn', { ms: buildMs })}${cacheTxt}${focusTxt}${warn} | ${_ti('connections.networkGestures')}`;
  graph.baseFootnote = baseFootnote;
  _setKnowledgeFootnote(baseFootnote);
  _renderCitationBreadcrumb(payload, focusNode);
  if (_kgIsInternalCitationPayload(payload)) {
    _renderInternalCitationDetail(state.knowledgeGraphFocusNodeId);
    _applyInternalCitationSearchHighlight();
  }
}

function _renderInternalCitationDetail(focusId = '') {
  const payload = state.knowledgeGraph?.payload || {};
  const nodes = Array.isArray(payload.nodes) ? payload.nodes : [];
  const edges = Array.isArray(payload.edges) ? payload.edges : [];
  const list = document.getElementById('kg-article-list');
  const summary = document.getElementById('kg-concept-summary');
  const focus = document.getElementById('kg-focus-brief');
  if (!list || !summary || !focus) return;

  const nodeById = new Map(nodes.map(node => [node.id, node]));
  const selected = focusId ? nodeById.get(focusId) : null;
  let rows = [];
  if (selected) {
    const relations = new Map();
    const visibleNodeIds = state.knowledgeGraph?.visibleNodeIds;
    for (const edge of edges) {
      if (edge.source === selected.id && nodeById.has(edge.target)) {
        relations.set(edge.target, {
          relation: _ti('connections.relationOutgoing'),
          relationKind: 'outgoing',
        });
      } else if (edge.target === selected.id && nodeById.has(edge.source)) {
        relations.set(edge.source, {
          relation: _ti('connections.relationIncoming'),
          relationKind: 'incoming',
        });
      }
    }
    const totalRelations = relations.size;
    rows = [...relations.entries()]
      .filter(([id]) => !(visibleNodeIds instanceof Set) || visibleNodeIds.has(id))
      .map(([id, relationMeta]) => ({ ...nodeById.get(id), ...relationMeta }))
      .sort((a, b) => a.relationKind.localeCompare(b.relationKind) || (b.degree || 0) - (a.degree || 0));
    focus.textContent = selected.title || _ti('connections.selectedPaper');
    const limitedHint = rows.length < totalRelations
      ? ` · ${_ti('connections.priorityLinksShown', { shown: rows.length, total: totalRelations })}`
      : '';
    summary.innerHTML = `
      <span class="kg-detail-hint">${escapeHtml(_ti('connections.directCitationsHint'))}</span>
      <span class="kg-detail-mini">${escapeHtml(_ti('connections.selectedCitationCounts', { out: Number(selected.out_degree || 0), in: Number(selected.in_degree || 0) }))}${escapeHtml(limitedHint)}</span>
    `;
  } else {
    focus.textContent = _ti('connections.selectCitationFocus');
    summary.innerHTML = `<span class="kg-detail-hint">${escapeHtml(_ti('connections.internalLinksOnly'))}</span>`;
  }

  if (!rows.length) {
    list.innerHTML = selected
      ? `<div class="discovery-empty">${escapeHtml(_ti('connections.noDirectCitation'))}</div>`
      : '';
    return;
  }
  list.innerHTML = rows.map(node => {
    const meta = [node.author || _tr('Auteur inconnu'), node.year || 'n.d.', node.relation]
      .filter(Boolean)
      .join(' · ');
    const encodedPath = encodeURIComponent(String(node.path || ''));
    return `
      <div class="kg-article-item citation-${escapeAttr(node.relationKind || '')}">
        <div class="kg-citation-relation">${escapeHtml(node.relation || '')}</div>
        <div class="kg-article-title" data-i18n-skip>${escapeHtml(node.title || _tr('Sans titre'))}</div>
        <div class="kg-article-meta" data-i18n-skip>${escapeHtml(meta)}</div>
        <div class="kg-article-actions">
          <button class="btn btn-indigo" ${bindBbHandler('click', function() { selectRowByPath(decodeURIComponent(encodedPath)); })} ${node.path ? '' : 'disabled'}>
            ${escapeHtml(_tr('Ouvrir dans la bibliothèque'))}
          </button>
        </div>
      </div>
    `;
  }).join('');
}

function _renderKnowledgeGraphNetwork(payload) {
  const svg = document.getElementById('knowledge-graph-svg');
  const wrap = document.getElementById('knowledge-graph-wrap');
  const columns = document.getElementById('knowledge-graph-columns');
  if (!svg) return;

  const nodes = Array.isArray(payload?.nodes) ? payload.nodes : [];
  const edges = Array.isArray(payload?.edges) ? payload.edges : [];
  const componentFilter = document.getElementById('kg-component-filter');
  const networkControls = document.getElementById('kg-network-controls');
  const searchInput = document.getElementById('kg-search-input');
  _kgSetThematicTools(false);
  if (networkControls) networkControls.hidden = !nodes.length;
  if (searchInput) {
    searchInput.placeholder = _ti('connections.searchPaperPlaceholder');
    searchInput.setAttribute('aria-label', _ti('connections.searchPaperAria'));
  }
  wrap?.classList.add('citation-network');
  wrap?.classList.remove('citation-has-focus');
  wrap?.classList.toggle('has-graph', nodes.length > 0);
  // The taxonomy renderer sizes this shared SVG with inline pixel values.
  // Reset them here so the citation canvas follows its now full-width stage.
  svg.style.left = '0';
  svg.style.top = '0';
  svg.style.right = '0';
  svg.style.bottom = '0';
  svg.style.width = '100%';
  svg.style.height = '100%';
  if (columns) columns.innerHTML = '';
  state.knowledgeGraphAdj = new Map(nodes.map(n => [n.id, new Set()]));
  for (const edge of edges) {
    state.knowledgeGraphAdj.get(edge.source)?.add(edge.target);
    state.knowledgeGraphAdj.get(edge.target)?.add(edge.source);
  }

  if (!nodes.length) {
    svg.innerHTML = '';
    state.knowledgeGraph = null;
    state.knowledgeGraphFocusNodeId = '';
    state.knowledgeGraphVisiblePaths = [];
    state.knowledgeGraphComponentFilter = '';
    state.knowledgeGraphCommunityId = '';
    if (componentFilter) componentFilter.hidden = true;
    _updateKnowledgeFocusButtonState();
    _updateKnowledgeFocusActions();
    _setKnowledgeGraphStatus(payload?.msg || payload?.warning || 'connections.noInternalCitation', { offerSetup: true });
    _setKnowledgeStatsPill(_ti('connections.zeroPapers'));
    _renderInternalCitationDetail('');
    _setKnowledgeFootnote('');
    return;
  }

  _setKnowledgeGraphStatus('');
  const communities = _citationCommunities(payload);
  state.knowledgeGraphComponentFilter = '';
  state.knowledgeGraphCommunityId = '';
  if (componentFilter) {
    componentFilter.hidden = false;
    componentFilter.innerHTML = [
      `<option value="">${escapeHtml(_ti('connections.allCommunities'))}</option>`,
      ...communities.map(community => `<option value="${community.id}">${escapeHtml(_ti('connections.communityOption', { label: community.label || _ti('connections.communityFallback', { n: Number(community.id) + 1 }), size: community.size }))}</option>`),
    ].join('');
    componentFilter.value = '';
  }
  state.knowledgeGraph = {
    payload,
    layout: {},
    groups: [],
    communityLayouts: new Map(),
    canvasWidth: 1200,
    canvasHeight: 780,
    nodeById: new Map(nodes.map(node => [node.id, node])),
  };
  state.knowledgeGraphDirty = false;
  state.knowledgeGraphFocusNodeId = '';
  state.knowledgeGraphFocusPath = [];
  _setKnowledgeStatsPill(_kgCount('connections.networkStats', communities.length, { nodes: nodes.length, edges: edges.length }));
  _renderKnowledgeGraphScene(false);
}

function _kgUpdateGraphKindPill(payload) {
  const pill = document.getElementById('kg-graph-kind-pill');
  if (!pill) return;
  const engine = String(payload?.engine || '').toLowerCase();
  if (engine === 'rag_article_taxonomy' || payload?.graph_type === 'article_taxonomy') {
    pill.textContent = payload?.stats?.taxonomy_strategy === 'semantic_content_clusters'
      ? _tr('Regroupement sémantique')
      : _tr('Carte de la bibliothèque');
  } else if (engine === 'rag_retrieval' || payload?.graph_type === 'rag_retrieval') {
    pill.textContent = _ti('connections.ragMap');
  } else if (engine === 'openalex_internal' || payload?.graph_type === 'library_citations') {
    pill.textContent = _ti('connections.internalCitations');
  }
}

function _kgIsRetrievalPayload(payload = state.knowledgeGraph?.payload) {
  return payload?.graph_type === 'rag_retrieval' || payload?.engine === 'rag_retrieval';
}

function _kgIsInternalCitationPayload(payload = state.knowledgeGraph?.payload) {
  return payload?.graph_type === 'library_citations' || payload?.engine === 'openalex_internal';
}

function _kgNodeMap() {
  const nodes = Array.isArray(state.knowledgeGraph?.payload?.nodes) ? state.knowledgeGraph.payload.nodes : [];
  return new Map(nodes.map(node => [node.id, node]));
}

function _kgChildrenByParent() {
  return state.knowledgeGraph?.payload?.children_by_parent || {};
}

function _kgArticlesByConcept() {
  return state.knowledgeGraph?.payload?.articles_by_concept || {};
}

function _kgDecodeHtmlEntities(value) {
  const text = String(value || '');
  if (!/[&<>]/.test(text) || typeof document === 'undefined') return text;
  const el = document.createElement('textarea');
  el.innerHTML = text;
  return el.value;
}

function _kgStripInlineHtml(value) {
  return _kgDecodeHtmlEntities(value)
    .replace(/<\s*sub\s*>(.*?)<\s*\/\s*sub\s*>/gi, '_$1')
    .replace(/<\s*sup\s*>(.*?)<\s*\/\s*sup\s*>/gi, '^$1')
    .replace(/<\/?[a-z][a-z0-9:-]*(?:\s+[^<>]*)?>/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function _kgCleanText(value) {
  return _kgStripInlineHtml(value)
    .replaceAll('â€“', '-')
    .replaceAll('â€”', '-')
    .replaceAll('â€™', "'")
    .replaceAll('â€˜', "'")
    .replaceAll('â€œ', '"')
    .replaceAll('â€', '"')
    .replaceAll('â€¢', '-')
    .replaceAll('Î¦', 'Phi')
    .replaceAll('â‚“', 'c')
    .replaceAll('dÃ©tectÃ©', 'detecte')
    .replaceAll('trouvÃ©', 'trouve')
    .replaceAll('GÃ©nÃ©ration', 'Generation')
    .replaceAll('RÃ©gÃ©nÃ©ration', 'Regeneration')
    .replaceAll('rÃ©gÃ©nÃ©ration', 'regeneration');
}

function _kgNodeBreadcrumb(nodeId) {
  const nodeMap = _kgNodeMap();
  const node = nodeMap.get(nodeId);
  if (!node) return '';
  const explicit = Array.isArray(node.taxonomy_path) ? node.taxonomy_path.filter(Boolean) : [];
  if (explicit.length) return explicit.map(_kgCleanText).join(' > ');
  return _kgPathToNode(nodeId)
    .map(id => _kgCleanText(nodeMap.get(id)?.label || id))
    .filter(Boolean)
    .join(' > ');
}

function _kgTaxonomyBadges(node) {
  if (!node) return [];
  if (_kgIsRetrievalPayload()) {
    const score = Number(node.retrieval_score ?? node.score ?? 0);
    if (node.group === 'rag_query') return [{ label: 'question', kind: 'neutral' }];
    if (node.group === 'rag_chunk') {
      return [{ label: score > 0 ? `score ${score.toFixed(2)}` : 'chunk retenu', kind: 'local' }];
    }
    return [{ label: score > 0 ? `meilleur score ${score.toFixed(2)}` : 'article retrouvé', kind: 'openalex' }];
  }
  const source = String(node.taxonomy_source || '');
  const confidence = Number(node.taxonomy_confidence ?? node.score ?? 0);
  const badges = [];
  if (source.startsWith('openalex_')) badges.push({ label: 'OpenAlex', kind: 'openalex' });
  if (source.includes('local') || source.includes('fallback')) badges.push({ label: 'fallback local', kind: 'local' });
  if (confidence > 0 && confidence < 0.45) badges.push({ label: 'confiance faible', kind: 'warning' });
  if (source === 'local_no_openalex_match') badges.push({ label: 'sans OpenAlex', kind: 'warning' });
  if ((node.candidate_count || 0) > 0) badges.push({ label: `${Number(node.candidate_count)} candidat(s)`, kind: 'neutral' });
  if (!badges.length && node.display_level === 'taxonomy') badges.push({ label: _ti('connections.taxonomyBadge'), kind: 'neutral' });
  return badges;
}

function _kgBadgeHtml(node) {
  const badges = _kgTaxonomyBadges(node);
  if (!badges.length) return '';
  return `<div class="kg-badges">${badges.map(b => (
    `<span class="kg-badge ${escapeAttr(b.kind)}">${escapeHtml(b.label)}</span>`
  )).join('')}</div>`;
}

function _kgMatchesQualityFilter(node) {
  const filter = String(state.knowledgeGraphQualityFilter || '');
  if (!filter || !node) return true;
  const source = String(node.taxonomy_source || '');
  const confidence = Number(node.taxonomy_confidence ?? node.score ?? 0);
  if (filter === 'low-confidence') return confidence > 0 && confidence < 0.45;
  if (filter === 'no-openalex') return source.includes('local') || !source.startsWith('openalex_');
  if (filter === 'review') {
    return (confidence > 0 && confidence < 0.45)
      || source === 'local_no_openalex_match'
      || Number(node.article_count || 0) === 0
      || /bean|pfm|critical current|ac loss|thermal/i.test(String(node.label || ''));
  }
  return true;
}

function _setKnowledgeStatsPill(text) {
  const el = document.getElementById('kg-stats-pill');
  if (el) el.textContent = text || _tr('0 concept');
}

function _renderKnowledgeArticleList(conceptId, payloadOverride = null) {
  const list = document.getElementById('kg-article-list');
  const summary = document.getElementById('kg-concept-summary');
  const focus = document.getElementById('kg-focus-brief');
  if (!list || !summary || !focus) return;

  const nodeMap = _kgNodeMap();
  const payload = payloadOverride || state.knowledgeGraph?.payload || {};
  const isArticleTaxonomy = payload.graph_type === 'article_taxonomy';
  const isRetrieval = _kgIsRetrievalPayload(payload);
  const node = conceptId ? nodeMap.get(conceptId) : null;
  const articles = conceptId ? (_kgArticlesByConcept()[conceptId] || []) : [];

  if (!node) {
    const emptyLabel = isRetrieval
      ? _ti('connections.noItemSelected')
      : isArticleTaxonomy ? _ti('connections.noCategorySelected') : _ti('connections.noConceptSelectedLabel');
    focus.textContent = emptyLabel;
    summary.innerHTML = `<span class="kg-detail-hint">${isRetrieval
      ? _tr('Sélectionnez la question, un article ou un chunk pour inspecter la provenance du RAG.')
      : _ti('connections.selectCardForArticles', {
        kind: isArticleTaxonomy ? _ti('connections.classified') : _ti('connections.related'),
      })}</span>`;
    list.innerHTML = `<div class="discovery-empty">${escapeHtml(emptyLabel)}.</div>`;
    state.knowledgeGraphVisiblePaths = [];
    _updateKnowledgeFocusActions();
    return;
  }

  const breadcrumb = _kgNodeBreadcrumb(conceptId);
  const confidence = Number(node.taxonomy_confidence ?? node.score ?? 0);
  const source = _kgCleanText(node.taxonomy_source || 'taxonomie');
  const sourceLabel = isRetrieval
    ? (node.group === 'rag_query' ? 'requête RAG' : node.group === 'rag_chunk' ? 'chunk retenu' : 'article retrouvé')
    : isArticleTaxonomy
    ? (source === 'llm' ? 'LLM' : 'métadonnées')
    : source.startsWith('openalex_') ? 'OpenAlex' : source.includes('local') ? 'local' : 'taxonomie';
  focus.textContent = _kgCleanText(node.label || _tr('Concept'));
  const nodeSummary = isRetrieval ? _kgCleanText(node.summary || '') : '';
  summary.innerHTML = `
    <span class="kg-breadcrumb" title="${escapeAttr(breadcrumb || _kgCleanText(node.label || 'Concept'))}">
      ${escapeHtml(breadcrumb || _kgCleanText(node.label || 'Concept'))}
    </span>
    <span class="kg-detail-mini">${escapeHtml(sourceLabel)}${confidence ? ` · ${confidence.toFixed(2)}` : ''}</span>
    ${nodeSummary ? `<span class="kg-detail-hint">${escapeHtml(nodeSummary)}</span>` : ''}
  `;
  state.knowledgeGraphVisiblePaths = articles
    .map(article => String(article?.path || '').trim())
    .filter(Boolean)
    .filter((value, index, arr) => arr.indexOf(value) === index);

  if (!articles.length) {
    list.innerHTML = `<div class="discovery-empty">${_tr('Aucun article ')}${isArticleTaxonomy ? _tr('classe dans cette catégorie') : _tr('lie a ce concept')}.</div>`;
    _updateKnowledgeFocusActions();
    return;
  }

  list.innerHTML = articles.map((article) => {
    const metaBits = [
      _kgCleanText(article.author || _tr('Auteur inconnu')),
      article.year || 'n.d.',
    ].filter(Boolean).join(' - ');
    return `
      <div class="kg-article-item">
        <div class="kg-article-title" data-i18n-skip>${escapeHtml(_kgCleanText(article.title || article.article_id || _tr('Sans titre')))}</div>
        <div class="kg-article-meta" data-i18n-skip>${escapeHtml(metaBits || 'Reference liee au concept')}</div>
        <div class="kg-article-actions">
          <button class="btn btn-indigo" ${bindBbHandler('click', function() { selectRowByPath(article.path || ''); })}
            ${article.path ? '' : 'disabled'}>
            Ouvrir dans la bibliotheque
          </button>
        </div>
      </div>
    `;
  }).join('');
  _updateKnowledgeFocusActions();
}

function _renderKnowledgeGraphColumns() {
  const container = document.getElementById('knowledge-graph-columns');
  if (!container) return;

  const payload = state.knowledgeGraph?.payload;
  const isArticleTaxonomy = payload?.graph_type === 'article_taxonomy';
  const isRetrieval = _kgIsRetrievalPayload(payload);
  const nodeMap = _kgNodeMap();
  const childrenByParent = _kgChildrenByParent();
  const rootIds = Array.isArray(payload?.root_ids) ? payload.root_ids : [];
  const focusPath = Array.isArray(state.knowledgeGraphFocusPath) ? state.knowledgeGraphFocusPath : [];
  const columns = [];

  columns.push({
    key: 'roots',
    title: _ti(isRetrieval
      ? 'connections.questionTitle'
      : isArticleTaxonomy ? 'connections.categoriesTitle' : 'connections.conceptsTitle'),
    subtitle: _ti('connections.rootCount', { count: rootIds.length }),
    ids: rootIds,
    depth: 0,
    parentId: '',
  });

  focusPath.forEach((nodeId, index) => {
    const childIds = Array.isArray(childrenByParent[nodeId]) ? childrenByParent[nodeId] : [];
    if (!childIds.length) return;
    const node = nodeMap.get(nodeId);
    columns.push({
      key: `children-${nodeId}`,
      title: _kgCleanText(node?.label || (isRetrieval ? 'Sources récupérées' : isArticleTaxonomy ? 'Sous-catégories' : 'Sous-concepts')),
      subtitle: _ti(isRetrieval
        ? (index === 0 ? 'connections.paperCount' : 'connections.chunkCount')
        : isArticleTaxonomy ? 'connections.subcategoryCount' : 'connections.subconceptCount', { count: childIds.length }),
      ids: childIds,
      depth: index + 1,
      parentId: nodeId,
    });
  });

  container.innerHTML = columns.map((column) => {
    const itemsHtml = column.ids.map((id) => {
      const node = nodeMap.get(id);
      if (!node) return '';
      const isActive = state.knowledgeGraphFocusPath[column.depth] === id;
      const childCount = Number(node.child_count || 0);
      const articleCount = Number(node.article_count || 0);
      const filterClass = _kgMatchesQualityFilter(node) ? '' : 'kg-filter-dimmed';
      const claimCount = Number(node.claim_count || 0);
      return `
        <button
          class="kg-card ${isActive ? 'active' : ''} ${column.parentId ? 'child-branch' : ''} ${filterClass}"
          data-node-id="${escapeAttr(id)}"
          data-parent-id="${escapeAttr(column.parentId || '')}"
          data-depth="${column.depth}"
          ${bindBbHandler('click', function() { toggleKnowledgeConcept(id, column.depth); })}
        >
          <div class="kg-card-main">
            <div class="kg-card-label">${escapeHtml(_kgCleanText(node.label || (isRetrieval ? 'Source RAG' : isArticleTaxonomy ? 'Catégorie' : 'Concept')))}</div>
            ${childCount > 0 ? `<span class="kg-card-arrow">›</span>` : ''}
          </div>
          ${_kgBadgeHtml(node)}
          <div class="kg-card-meta">
            <span class="kg-chip">${escapeHtml(_ti('connections.paperCount', { count: articleCount }))}</span>
            ${!isArticleTaxonomy && !isRetrieval ? `<span class="kg-chip">${escapeHtml(_ti('connections.claimCount', { count: claimCount }))}</span>` : ''}
            ${childCount > 0 ? `<span class="kg-chip">${escapeHtml(_ti(isRetrieval
              ? (node.group === 'rag_query' ? 'connections.paperCount' : 'connections.chunkCount')
              : isArticleTaxonomy ? 'connections.subcategoryCount' : 'connections.subconceptCount', { count: childCount }))}</span>` : ''}
          </div>
        </button>
      `;
    }).join('');

    return `
      <div
        class="kg-column"
        data-column-depth="${column.depth}"
        data-parent-id="${escapeAttr(column.parentId || '')}"
      >
        <div class="kg-column-head">
          <div class="kg-column-title">${escapeHtml(_kgCleanText(column.title))}</div>
          <div class="kg-column-subtitle">${escapeHtml(column.subtitle)}</div>
        </div>
        ${itemsHtml || `<div class="discovery-empty">Aucun ${isRetrieval ? 'élément RAG' : isArticleTaxonomy ? 'catégorie' : 'concept'}.</div>`}
      </div>
    `;
  }).join('');
  _scheduleKnowledgeGraphLinksRender();
}

function _scheduleKnowledgeGraphLinksRender() {
  if (state.knowledgeGraphLinkRaf) {
    cancelAnimationFrame(state.knowledgeGraphLinkRaf);
  }
  state.knowledgeGraphLinkRaf = requestAnimationFrame(() => {
    state.knowledgeGraphLinkRaf = null;
    _renderKnowledgeGraphLinks();
  });
}

function _renderKnowledgeGraphLinks() {
  const stage = document.getElementById('knowledge-graph-stage');
  const container = document.getElementById('knowledge-graph-columns');
  const svg = document.getElementById('knowledge-graph-svg');
  if (!stage || !container || !svg) return;
  if (document.getElementById('knowledge-graph-wrap')?.classList.contains('citation-network')) {
    return;
  }

  const cards = Array.from(container.querySelectorAll('.kg-card'));
  const width = Math.max(1, container.scrollWidth, container.offsetWidth);
  const height = Math.max(1, container.scrollHeight, container.offsetHeight);
  svg.style.left = `${container.offsetLeft}px`;
  svg.style.top = `${container.offsetTop}px`;
  svg.style.width = `${width}px`;
  svg.style.height = `${height}px`;
  svg.setAttribute('width', String(width));
  svg.setAttribute('height', String(height));
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);

  if (!cards.length) {
    svg.style.left = '0px';
    svg.style.top = '0px';
    svg.style.width = '0px';
    svg.style.height = '0px';
    svg.innerHTML = '';
    return;
  }

  const containerRect = container.getBoundingClientRect();
  const focusPath = Array.isArray(state.knowledgeGraphFocusPath) ? state.knowledgeGraphFocusPath : [];
  const animateFromDepth = Number.isFinite(Number(state.knowledgeGraphAnimateFromDepth))
    ? Number(state.knowledgeGraphAnimateFromDepth)
    : null;
  const findCard = (nodeId, depth) => cards.find((card) => (
    card.dataset.nodeId === nodeId && Number(card.dataset.depth || -1) === depth
  ));
  const childCardsFor = (parentId, depth) => cards.filter((card) => (
    card.dataset.parentId === parentId && Number(card.dataset.depth || -1) === depth
  ));
  const projectPoint = (element, edge) => {
    const rect = element.getBoundingClientRect();
    return {
      x: (edge === 'left' ? rect.left : rect.right) - containerRect.left,
      y: rect.top - containerRect.top + (rect.height / 2),
    };
  };

  const ns = 'http://www.w3.org/2000/svg';
  const desiredKeys = new Set();
  const existing = new Map();
  svg.querySelectorAll('[data-link-key]').forEach((el) => {
    const key = el.getAttribute('data-link-key') || '';
    const role = el.getAttribute('data-link-role') || '';
    if (!key || !role) return;
    const entry = existing.get(key) || {};
    entry[role] = el;
    existing.set(key, entry);
  });

  focusPath.forEach((parentId, depth) => {
    const source = findCard(parentId, depth);
    const targets = childCardsFor(parentId, depth + 1);
    if (!source || !targets.length) return;
    const start = projectPoint(source, 'right');
    targets.forEach((target, targetIndex) => {
      const end = projectPoint(target, 'left');
      const dx = Math.max(34, (end.x - start.x) * 0.45);
      const delay = (depth * 85) + (targetIndex * 38);
      const shouldAnimate = preferredScrollBehavior() === 'smooth'
        && animateFromDepth !== null && depth >= animateFromDepth;
      const path = [
        `M ${start.x.toFixed(1)} ${start.y.toFixed(1)}`,
        `C ${(start.x + dx).toFixed(1)} ${start.y.toFixed(1)},`,
        `${(end.x - dx).toFixed(1)} ${end.y.toFixed(1)},`,
        `${end.x.toFixed(1)} ${end.y.toFixed(1)}`,
      ].join(' ');
      const key = `${parentId}::${target.dataset.nodeId || ''}::${depth}`;
      desiredKeys.add(key);
      let entry = existing.get(key);
      const isNew = !entry?.path || !entry?.dot;
      if (!entry) {
        entry = {};
        existing.set(key, entry);
      }
      if (!entry.path) {
        entry.path = document.createElementNS(ns, 'path');
        entry.path.setAttribute('data-link-key', key);
        entry.path.setAttribute('data-link-role', 'path');
        entry.path.setAttribute('pathLength', '1');
        svg.appendChild(entry.path);
      }
      if (!entry.dot) {
        entry.dot = document.createElementNS(ns, 'circle');
        entry.dot.setAttribute('data-link-key', key);
        entry.dot.setAttribute('data-link-role', 'dot');
        entry.dot.setAttribute('r', '2.6');
        svg.appendChild(entry.dot);
      }
      const animateThisLink = shouldAnimate && isNew;
      entry.path.setAttribute('class', animateThisLink ? 'kg-link active' : 'kg-link active static');
      entry.dot.setAttribute('class', animateThisLink ? 'kg-link-dot' : 'kg-link-dot static');
      entry.path.style.setProperty('--kg-link-delay', `${delay}ms`);
      entry.dot.style.setProperty('--kg-link-delay', `${delay}ms`);
      entry.path.setAttribute('d', path);
      entry.dot.setAttribute('cx', end.x.toFixed(1));
      entry.dot.setAttribute('cy', end.y.toFixed(1));
    });
  });

  svg.querySelectorAll('[data-link-key]').forEach((el) => {
    const key = el.getAttribute('data-link-key') || '';
    if (!desiredKeys.has(key)) el.remove();
  });
  state.knowledgeGraphAnimateFromDepth = null;
}

function _scrollKnowledgeGraphToFocusColumn() {
  const stage = document.getElementById('knowledge-graph-stage');
  const container = document.getElementById('knowledge-graph-columns');
  if (!stage || !container) return;
  const columns = Array.from(container.querySelectorAll('.kg-column'));
  if (!columns.length) return;
  const targetColumn = columns[columns.length - 1];
  if (!targetColumn) return;
  const targetLeft = Math.max(0, targetColumn.offsetLeft - 22);
  stage.scrollTo({ left: targetLeft, behavior: preferredScrollBehavior() });
}

function _initKnowledgeGraphColumnLinks() {
  if (state.knowledgeGraphColumnUiBound) return;
  state.knowledgeGraphColumnUiBound = true;
  const stage = document.getElementById('knowledge-graph-stage');
  stage?.addEventListener('scroll', () => {
    _scheduleKnowledgeGraphLinksRender();
  });
  stage?.addEventListener('pointerdown', (event) => {
    if (document.getElementById('knowledge-graph-wrap')?.classList.contains('citation-network')) return;
    if (event.button !== 0) return;
    if (event.target?.closest?.('button, input, textarea, select, a, .kg-card, #knowledge-controls')) return;
    const drag = state.knowledgeGraphDrag;
    drag.active = true;
    drag.moved = false;
    drag.pointerId = event.pointerId;
    drag.lastX = event.clientX;
    drag.lastY = event.clientY;
    drag.startX = event.clientX;
    drag.startY = event.clientY;
    drag.startScrollLeft = stage.scrollLeft;
    drag.startScrollTop = stage.scrollTop;
    stage.classList.add('is-grabbing');
    try {
      stage.setPointerCapture(event.pointerId);
    } catch (_e) {}
  });
  stage?.addEventListener('pointermove', (event) => {
    if (document.getElementById('knowledge-graph-wrap')?.classList.contains('citation-network')) return;
    const drag = state.knowledgeGraphDrag;
    if (!drag.active || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    stage.scrollLeft = drag.startScrollLeft - dx;
    stage.scrollTop = drag.startScrollTop - dy;
    event.preventDefault();
  });
  const stopDrag = (event) => {
    const drag = state.knowledgeGraphDrag;
    if (!drag.active || (event?.pointerId != null && drag.pointerId !== event.pointerId)) return;
    drag.active = false;
    drag.pointerId = null;
    stage.classList.remove('is-grabbing');
    try {
      stage.releasePointerCapture(event.pointerId);
    } catch (_e) {}
  };
  stage?.addEventListener('pointerup', stopDrag);
  stage?.addEventListener('pointercancel', stopDrag);
  stage?.addEventListener('click', (event) => {
    if (document.getElementById('knowledge-graph-wrap')?.classList.contains('citation-network')) return;
    if (!state.knowledgeGraphDrag.moved) return;
    state.knowledgeGraphDrag.moved = false;
    event.preventDefault();
    event.stopPropagation();
  }, true);
  window.addEventListener('resize', () => {
    _scheduleKnowledgeGraphLinksRender();
  });
}

function _renderKnowledgeGraph(payload) {
  const empty = document.getElementById('knowledge-graph-empty');
  const wrap = document.getElementById('knowledge-graph-wrap');
  const svg = document.getElementById('knowledge-graph-svg');
  const stats = payload?.stats || {};
  const nodes = Array.isArray(payload?.nodes) ? payload.nodes : [];
  const isArticleTaxonomy = payload?.graph_type === 'article_taxonomy';
  const isRetrieval = _kgIsRetrievalPayload(payload);
  if (_kgIsInternalCitationPayload(payload)) {
    _renderKnowledgeGraphNetwork(payload);
    return;
  }
  const componentFilter = document.getElementById('kg-component-filter');
  const networkControls = document.getElementById('kg-network-controls');
  const networkBreadcrumb = document.getElementById('kg-network-breadcrumb');
  const searchInput = document.getElementById('kg-search-input');
  if (wrap?.classList.contains('citation-network') && svg) {
    svg.innerHTML = '';
    for (const property of ['left', 'top', 'right', 'bottom', 'width', 'height']) {
      svg.style.removeProperty(property);
    }
    svg.setAttribute('preserveAspectRatio', 'none');
  }
  wrap?.classList.remove('citation-network', 'citation-has-focus');
  state.knowledgeGraphComponentFilter = '';
  state.knowledgeGraphCommunityId = '';
  _kgSetThematicTools(!isRetrieval);
  if (componentFilter) componentFilter.hidden = true;
  if (networkControls) networkControls.hidden = true;
  if (networkBreadcrumb) {
    networkBreadcrumb.hidden = true;
    networkBreadcrumb.innerHTML = '';
  }
  if (searchInput) {
    searchInput.placeholder = _ti(isRetrieval ? 'connections.searchSourcePlaceholder' : 'connections.searchCategoryPlaceholder');
    searchInput.setAttribute('aria-label', _ti(isRetrieval ? 'connections.searchSourceAria' : 'connections.searchCategoryAria'));
  }

  if (!nodes.length) {
    state.knowledgeGraph = null;
    state.knowledgeGraphFocusNodeId = '';
    state.knowledgeGraphFocusPath = [];
    state.knowledgeGraphVisiblePaths = [];
    state.knowledgeGraphAnimateFromDepth = null;
    _setKnowledgeGraphStatus(payload?.warning || (isRetrieval
      ? 'Aucune provenance RAG disponible.'
      : isArticleTaxonomy ? 'Aucune catégorie disponible.' : 'Aucun concept disponible.'), { offerSetup: !isRetrieval });
    if (svg) svg.innerHTML = '';
    if (wrap) wrap.classList.remove('has-graph');
    _setKnowledgeStatsPill(isRetrieval ? '0 source' : isArticleTaxonomy ? '0 catégorie' : '0 concept');
    _renderKnowledgeArticleList('', payload);
    _setKnowledgeFootnote('');
    _updateKnowledgeFocusButtonState();
    return;
  }

  state.knowledgeGraph = { payload };
  const currentPath = Array.isArray(state.knowledgeGraphFocusPath) ? state.knowledgeGraphFocusPath : [];
  const nodeIds = new Set(nodes.map(node => node.id));
  state.knowledgeGraphFocusPath = currentPath.filter(id => nodeIds.has(id));
  state.knowledgeGraphFocusNodeId = state.knowledgeGraphFocusPath[state.knowledgeGraphFocusPath.length - 1] || '';
  if (empty) empty.style.display = 'none';
  if (wrap) wrap.classList.add('has-graph');

  _initKnowledgeGraphColumnLinks();
  const graphItemCount = Number(stats.categories || stats.concepts || nodes.length);
  _setKnowledgeStatsPill(isRetrieval
    ? _ti('connections.sourceCount', { count: Number(stats.sources || 0) })
    : _ti(isArticleTaxonomy ? 'connections.categoryCount' : 'connections.conceptCount', { count: graphItemCount }));
  _renderKnowledgeGraphColumns();
  _renderKnowledgeArticleList(state.knowledgeGraphFocusNodeId);
  _applyKnowledgeGraphSearchHighlight();

  const strategy = isArticleTaxonomy
    ? stats.taxonomy_strategy === 'semantic_content_clusters'
      ? _ti('connections.strategyContent')
      : _ti('connections.strategyZotero')
    : stats.concept_graph_strategy
      ? _ti('connections.strategyHierarchy', { strategy: stats.concept_graph_strategy })
      : '';
  const warning = payload?.warning ? ` | ${payload.warning}` : '';
  _setKnowledgeFootnote(`${_ti('connections.footnote', {
    roots: Number(stats.roots || 0),
    articles: Number(stats.articles || 0),
    ms: Number(stats.build_ms || 0),
  })}${strategy}${warning}`);
  if (isArticleTaxonomy) {
    _setKnowledgeFootnote(`${_ti('connections.taxonomyFootnote', {
      roots: Number(stats.roots || 0),
      articles: Number(stats.articles || 0),
      ms: Number(stats.build_ms || 0),
    })}${strategy}${warning}`);
  } else if (isRetrieval) {
    _setKnowledgeFootnote(
      _ti('connections.retrievalFootnote', { articles: Number(stats.articles || 0), sources: Number(stats.sources || 0) })
    );
  }
  _updateKnowledgeFocusButtonState();
}

function _normalizeKgSearchText(value) {
  return String(value || '')
    .toLowerCase()
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .trim();
}

function _kgFindMatchingNodeIds(query) {
  const needle = _normalizeKgSearchText(query);
  if (!needle) return [];
  const payload = state.knowledgeGraph?.payload;
  const nodes = Array.isArray(payload?.nodes) ? payload.nodes : [];
  const matches = [];
  for (const node of nodes) {
    const haystack = `${_normalizeKgSearchText(node.label)} ${_normalizeKgSearchText(node.summary)}`;
    if (haystack.includes(needle)) {
      matches.push(node.id);
    }
  }
  return matches;
}

function _kgParentByChild() {
  const map = new Map();
  const childrenByParent = state.knowledgeGraph?.payload?.children_by_parent || {};
  for (const [parentId, childIds] of Object.entries(childrenByParent)) {
    for (const childId of childIds || []) {
      map.set(childId, parentId);
    }
  }
  return map;
}

function _kgPathToNode(nodeId) {
  if (!nodeId) return [];
  const parentByChild = _kgParentByChild();
  const path = [nodeId];
  let cursor = nodeId;
  const guard = new Set();
  while (parentByChild.has(cursor) && !guard.has(cursor)) {
    guard.add(cursor);
    cursor = parentByChild.get(cursor);
    path.unshift(cursor);
  }
  return path;
}

function _kgPathAncestors(nodeId) {
  const path = _kgPathToNode(nodeId);
  return new Set(path.slice(0, Math.max(0, path.length - 1)));
}

function _applyKnowledgeGraphSearchHighlight() {
  if (_kgIsInternalCitationPayload()) {
    _applyInternalCitationSearchHighlight();
    return;
  }
  const container = document.getElementById('knowledge-graph-columns');
  if (!container) return;
  const cards = Array.from(container.querySelectorAll('.kg-card'));
  cards.forEach((card) => {
    card.classList.remove('kg-search-match', 'kg-search-path', 'kg-search-dimmed');
    const labelEl = card.querySelector('.kg-card-label');
    if (labelEl && labelEl.dataset.kgRawLabel != null) {
      labelEl.textContent = labelEl.dataset.kgRawLabel;
    }
  });

  const query = String(state.knowledgeGraphSearchQuery || '').trim();
  const countEl = document.getElementById('kg-search-count');
  const clearBtn = document.getElementById('kg-search-clear-btn');
  if (!query) {
    if (countEl) {
      countEl.style.display = 'none';
      countEl.textContent = '0';
    }
    if (clearBtn) clearBtn.style.display = 'none';
    state.knowledgeGraphSearchMatches = [];
    return;
  }

  const matches = _kgFindMatchingNodeIds(query);
  state.knowledgeGraphSearchMatches = matches;
  const matchSet = new Set(matches);
  const ancestorSet = new Set();
  for (const id of matches) {
    for (const ancestor of _kgPathAncestors(id)) {
      ancestorSet.add(ancestor);
    }
  }

  const needle = _normalizeKgSearchText(query);
  cards.forEach((card) => {
    const id = card.dataset.nodeId || '';
    if (matchSet.has(id)) {
      card.classList.add('kg-search-match');
      const labelEl = card.querySelector('.kg-card-label');
      if (labelEl) {
        if (labelEl.dataset.kgRawLabel == null) {
          labelEl.dataset.kgRawLabel = labelEl.textContent;
        }
        const raw = labelEl.dataset.kgRawLabel;
        const rawNorm = _normalizeKgSearchText(raw);
        const idx = rawNorm.indexOf(needle);
        if (idx >= 0) {
          const before = raw.slice(0, idx);
          const hit = raw.slice(idx, idx + needle.length);
          const after = raw.slice(idx + needle.length);
          labelEl.innerHTML = `${escapeHtml(before)}<mark>${escapeHtml(hit)}</mark>${escapeHtml(after)}`;
        }
      }
    } else if (ancestorSet.has(id)) {
      card.classList.add('kg-search-path');
    } else if (matches.length) {
      card.classList.add('kg-search-dimmed');
    }
  });

  if (countEl) {
    countEl.textContent = String(matches.length);
    countEl.style.display = '';
    countEl.title = matches.length === 0
      ? 'Aucun concept trouvé'
      : `${matches.length} concept(s) trouvé(s)`;
  }
  if (clearBtn) clearBtn.style.display = '';
}

function _applyInternalCitationSearchHighlight() {
  const svg = document.getElementById('knowledge-graph-svg');
  if (!svg) return;
  const query = _normalizeKgSearchText(state.knowledgeGraphSearchQuery || '');
  const nodes = Array.isArray(state.knowledgeGraph?.payload?.nodes)
    ? state.knowledgeGraph.payload.nodes
    : [];
  const matches = query
    ? nodes.filter(node => {
        const haystack = _normalizeKgSearchText(
          `${node.title || ''} ${node.author || ''} ${(node.authors || []).join(' ')} ${node.doi || ''}`
        );
        return haystack.includes(query);
      }).map(node => node.id)
    : [];
  const matchSet = new Set(matches);
  const matchingCommunityIds = new Set(
    nodes
      .filter(node => matchSet.has(node.id) && _knowledgeCommunityId(node) >= 0)
      .map(node => String(_knowledgeCommunityId(node)))
  );
  state.knowledgeGraphSearchMatches = matches;

  svg.querySelectorAll('.kg-node, .kg-node-label').forEach(element => {
    const id = element.dataset.id || '';
    element.classList.toggle('muted', Boolean(query) && !matchSet.has(id));
    element.classList.toggle('search-match', Boolean(query) && matchSet.has(id));
  });
  svg.querySelectorAll('.kg-edge').forEach(element => {
    const touchesMatch = matchSet.has(element.dataset.source || '') || matchSet.has(element.dataset.target || '');
    element.classList.toggle('muted', Boolean(query) && !touchesMatch);
  });
  svg.querySelectorAll('.kg-community-node-wrap').forEach(element => {
    const matchesCommunity = matchingCommunityIds.has(element.dataset.communityId || '');
    element.classList.toggle('search-match', Boolean(query) && matchesCommunity);
    element.classList.toggle('search-dimmed', Boolean(query) && !matchesCommunity);
  });

  const countEl = document.getElementById('kg-search-count');
  const clearBtn = document.getElementById('kg-search-clear-btn');
  if (countEl) {
    countEl.textContent = String(matches.length);
    countEl.style.display = query ? '' : 'none';
  }
  if (clearBtn) clearBtn.style.display = query ? '' : 'none';
}

function _focusKnowledgeGraphOnNode(nodeId) {
  if (!nodeId) return;
  const path = _kgPathToNode(nodeId);
  if (!path.length) return;
  state.knowledgeGraphFocusPath = path;
  state.knowledgeGraphFocusNodeId = path[path.length - 1] || '';
  state.knowledgeGraphAnimateFromDepth = 0;
  if (state.knowledgeGraph?.payload) {
    _renderKnowledgeGraph(state.knowledgeGraph.payload);
    requestAnimationFrame(() => {
      _scrollKnowledgeGraphToFocusColumn();
    });
  }
}

let _kgSearchDebounce = null;
function onKnowledgeGraphSearchInput(value) {
  state.knowledgeGraphSearchQuery = String(value || '');
  if (_kgSearchDebounce) clearTimeout(_kgSearchDebounce);
  _kgSearchDebounce = setTimeout(() => {
    _kgSearchDebounce = null;
    _applyKnowledgeGraphSearchHighlight();
  }, 80);
}

function onKnowledgeGraphSearchKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault();
    clearKnowledgeGraphSearch();
    return;
  }
  if (event.key === 'Enter') {
    event.preventDefault();
    if (_kgSearchDebounce) {
      clearTimeout(_kgSearchDebounce);
      _kgSearchDebounce = null;
      _applyKnowledgeGraphSearchHighlight();
    }
    const matches = state.knowledgeGraphSearchMatches || [];
    if (!matches.length) return;
    if (_kgIsInternalCitationPayload()) {
      const currentIdx = matches.indexOf(state.knowledgeGraphFocusNodeId || '');
      const nextIdx = event.shiftKey
        ? (currentIdx <= 0 ? matches.length - 1 : currentIdx - 1)
        : (currentIdx < 0 || currentIdx >= matches.length - 1 ? 0 : currentIdx + 1);
      const nextId = matches[nextIdx];
      const nextNode = state.knowledgeGraph?.nodeById?.get(nextId);
      if (nextNode && _knowledgeCommunityId(nextNode) >= 0) {
        state.knowledgeGraphCommunityId = String(_knowledgeCommunityId(nextNode));
        state.knowledgeGraphComponentFilter = state.knowledgeGraphCommunityId;
        const filter = document.getElementById('kg-component-filter');
        if (filter) filter.value = state.knowledgeGraphCommunityId;
      }
      state.knowledgeGraphFocusNodeId = nextId;
      _renderKnowledgeGraphScene(false);
      return;
    }
    const currentFocus = state.knowledgeGraphFocusNodeId || '';
    const currentIdx = matches.indexOf(currentFocus);
    const nextIdx = event.shiftKey
      ? (currentIdx <= 0 ? matches.length - 1 : currentIdx - 1)
      : (currentIdx < 0 || currentIdx >= matches.length - 1 ? 0 : currentIdx + 1);
    _focusKnowledgeGraphOnNode(matches[nextIdx]);
  }
}

function clearKnowledgeGraphSearch() {
  state.knowledgeGraphSearchQuery = '';
  state.knowledgeGraphSearchMatches = [];
  const input = document.getElementById('kg-search-input');
  if (input) input.value = '';
  _applyKnowledgeGraphSearchHighlight();
}

function setKnowledgeGraphQualityFilter(filter) {
  state.knowledgeGraphQualityFilter = String(filter || '');
  const select = document.getElementById('kg-quality-filter');
  if (select && select.value !== state.knowledgeGraphQualityFilter) {
    select.value = state.knowledgeGraphQualityFilter;
  }
  document.querySelectorAll('#kg-quality-filters .kg-filter-btn').forEach(btn => {
    btn.classList.toggle('active', (btn.dataset.filter || '') === state.knowledgeGraphQualityFilter);
  });
  if (state.knowledgeGraph?.payload) {
    _renderKnowledgeGraph(state.knowledgeGraph.payload);
  }
}

function clearKnowledgeFocus() {
  if (_kgIsInternalCitationPayload()) {
    if (state.knowledgeGraphFocusNodeId) {
      state.knowledgeGraphFocusNodeId = '';
      state.knowledgeGraphFocusPath = [];
      _renderKnowledgeGraphScene(false);
      return;
    }
    state.knowledgeGraphCommunityId = '';
    state.knowledgeGraphComponentFilter = '';
    state.knowledgeGraphFocusPath = [];
    const filter = document.getElementById('kg-component-filter');
    if (filter) filter.value = '';
    _renderKnowledgeGraphScene(false);
    return;
  }
  state.knowledgeGraphFocusNodeId = '';
  state.knowledgeGraphFocusPath = [];
  state.knowledgeGraphAnimateFromDepth = null;
  if (state.knowledgeGraph?.payload) {
    _renderKnowledgeGraph(state.knowledgeGraph.payload);
  } else {
    _renderKnowledgeArticleList('');
    _updateKnowledgeFocusButtonState();
  }
}

function toggleKnowledgeConcept(nodeId, depth) {
  const nodeMap = _kgNodeMap();
  const node = nodeMap.get(nodeId);
  if (!node) return;

  const path = Array.isArray(state.knowledgeGraphFocusPath) ? [...state.knowledgeGraphFocusPath] : [];
  const currentAtDepth = path[depth];
  if (currentAtDepth === nodeId && path.length === depth + 1) {
    path.splice(depth);
    state.knowledgeGraphAnimateFromDepth = null;
  } else {
    path.splice(depth);
    path.push(nodeId);
    state.knowledgeGraphAnimateFromDepth = depth;
  }

  state.knowledgeGraphFocusPath = path;
  state.knowledgeGraphFocusNodeId = path[path.length - 1] || '';
  if (state.knowledgeGraph?.payload) {
    _renderKnowledgeGraph(state.knowledgeGraph.payload);
    requestAnimationFrame(() => {
      _scrollKnowledgeGraphToFocusColumn();
    });
  }
}

function _updateKnowledgeFocusButtonState() {
  const btn = document.getElementById('kg-reset-focus-btn');
  if (!btn) return;
  if (_kgIsInternalCitationPayload()) {
    btn.disabled = !state.knowledgeGraphFocusNodeId && String(state.knowledgeGraphCommunityId ?? '') === '';
    btn.title = state.knowledgeGraphFocusNodeId
      ? _ti('connections.returnToCommunity')
      : _ti('connections.returnToCommunities');
    return;
  }
  btn.disabled = !(state.knowledgeGraphFocusPath && state.knowledgeGraphFocusPath.length);
}

function _updateKnowledgeFocusActions() {
  const summary = document.getElementById('kg-focus-brief');
  const openBtn = document.getElementById('kg-open-notebook-modal-btn');
  const count = Array.isArray(state.knowledgeGraphVisiblePaths) ? state.knowledgeGraphVisiblePaths.length : 0;
  const hasFocus = Boolean(state.knowledgeGraphFocusNodeId);
  const hasCommunity = _kgIsInternalCitationPayload() && String(state.knowledgeGraphCommunityId ?? '') !== '';
  if (summary && _kgIsInternalCitationPayload()) {
    summary.textContent = hasFocus
      ? _ti('connections.citationNeighborhoodCount', { count })
      : _ti('connections.selectCitationFocus');
  } else if (summary) {
    summary.textContent = hasFocus
      ? _ti('connections.subgraphFocusCount', { count })
      : _ti('connections.selectSubgraphFocus');
  }
  if (openBtn) {
    openBtn.disabled = (!state.knowledgeGraphFocusNodeId && !hasCommunity) || count === 0;
    openBtn.title = _ti(openBtn.disabled ? 'connections.addDisabledTitle' : 'connections.addTitle');
  }
}

function _ragGraphStableId(prefix, value) {
  const raw = String(value || 'source');
  let hash = 2166136261;
  for (let index = 0; index < raw.length; index += 1) {
    hash ^= raw.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `${prefix}-${(hash >>> 0).toString(16)}`;
}

function rememberLastRagRetrieval(query, sources, answerOrigin = 'rag') {
  const cleanQuery = String(query || '').trim();
  const rows = Array.isArray(sources) ? sources.filter(Boolean).slice(0, 40) : [];
  if (!cleanQuery || !rows.length || String(answerOrigin || '').trim() !== 'rag') return null;
  state.lastRagRetrieval = {
    query: cleanQuery,
    sources: rows.map(source => ({ ...source })),
    capturedAt: Date.now(),
  };
  if (document.getElementById('kg-view-mode')?.value === 'retrieval') {
    state.knowledgeGraphDirty = true;
  }
  return state.lastRagRetrieval;
}

function _buildRagRetrievalGraphPayload(record = state.lastRagRetrieval) {
  const query = String(record?.query || '').trim();
  const sources = Array.isArray(record?.sources) ? record.sources : [];
  if (!query || !sources.length) {
    return {
      ok: true,
      engine: 'rag_retrieval',
      graph_type: 'rag_retrieval',
      root_ids: [],
      nodes: [],
      edges: [],
      children_by_parent: {},
      articles_by_concept: {},
      stats: { concepts: 0, roots: 0, articles: 0, sources: 0, build_ms: 0 },
      warning: 'Aucune recherche RAG récente. Posez une question dans le chat, puis ouvrez le graphe depuis la réponse.',
    };
  }

  const queryId = _ragGraphStableId('rag-query', query);
  const nodes = [{
    id: queryId,
    node_id: queryId,
    label: query.length > 140 ? `${query.slice(0, 137)}…` : query,
    summary: query,
    group: 'rag_query',
    depth: 0,
    parent_id: null,
    article_count: 0,
    child_count: 0,
    claim_count: 0,
    score: 1,
    taxonomy_source: 'rag_query',
    taxonomy_confidence: 1,
    taxonomy_path: ['Question RAG'],
  }];
  const edges = [];
  const childrenByParent = { [queryId]: [] };
  const articlesByConcept = { [queryId]: [] };
  const articleMap = new Map();

  sources.forEach((source, index) => {
    const path = String(source?.file || source?.path || '').trim();
    const title = String(source?.title || path.split(/[\\/]/).pop() || source?.article_id || `Source ${index + 1}`).trim();
    const articleKey = String(source?.article_id || path || title).trim();
    const articleId = _ragGraphStableId('rag-article', articleKey);
    const rawScore = Number(source?.retrieval_score);
    const fallbackScore = Math.max(0.05, 1 - (index / Math.max(1, sources.length)) * 0.65);
    const score = Number.isFinite(rawScore) ? Math.max(0, Math.min(1, rawScore)) : fallbackScore;
    const articlePayload = {
      article_id: String(source?.article_id || articleKey),
      title,
      path,
      year: source?.year || '',
      author: source?.author || source?.authors || '',
      match_count: 1,
      retrieval_score: score,
    };

    if (!articleMap.has(articleId)) {
      articleMap.set(articleId, { id: articleId, bestScore: score, article: articlePayload, chunks: [] });
      childrenByParent[queryId].push(articleId);
      childrenByParent[articleId] = [];
      articlesByConcept[articleId] = [articlePayload];
      articlesByConcept[queryId].push(articlePayload);
      nodes.push({
        id: articleId,
        node_id: articleId,
        label: title,
        summary: [source?.author || source?.authors, source?.year].filter(Boolean).join(' · '),
        group: 'rag_article',
        depth: 1,
        parent_id: queryId,
        article_count: 1,
        child_count: 0,
        claim_count: 0,
        score,
        retrieval_score: score,
        taxonomy_source: 'rag_retrieval',
        taxonomy_confidence: score,
        taxonomy_path: ['Question RAG', title],
      });
      edges.push({ source: queryId, target: articleId, score, kind: 'retrieval' });
    } else {
      const entry = articleMap.get(articleId);
      entry.bestScore = Math.max(entry.bestScore, score);
      entry.article.match_count += 1;
    }

    const chunkKey = String(source?.chunk_uid || `${articleKey}:${source?.chunk_id ?? index}:${source?.page ?? ''}`);
    const chunkNodeId = _ragGraphStableId('rag-chunk', chunkKey);
    if (childrenByParent[articleId].includes(chunkNodeId)) return;
    const sourceLabel = String(source?.ref_label || `S${index + 1}`).trim();
    const page = String(source?.page ?? '').trim();
    const chunkLabel = `${sourceLabel}${page ? ` · page ${page}` : ''}`;
    childrenByParent[articleId].push(chunkNodeId);
    articlesByConcept[chunkNodeId] = [articlePayload];
    nodes.push({
      id: chunkNodeId,
      node_id: chunkNodeId,
      label: chunkLabel,
      summary: String(source?.snippet || '').trim(),
      group: 'rag_chunk',
      depth: 2,
      parent_id: articleId,
      article_count: 1,
      child_count: 0,
      claim_count: 0,
      score,
      retrieval_score: score,
      taxonomy_source: 'rag_retrieval',
      taxonomy_confidence: score,
      taxonomy_path: ['Question RAG', title, chunkLabel],
    });
    edges.push({ source: articleId, target: chunkNodeId, score, kind: 'evidence' });
  });

  const nodeById = new Map(nodes.map(node => [node.id, node]));
  articleMap.forEach((entry) => {
    const node = nodeById.get(entry.id);
    if (node) {
      node.child_count = childrenByParent[entry.id].length;
      node.retrieval_score = entry.bestScore;
      node.score = entry.bestScore;
      node.taxonomy_confidence = entry.bestScore;
    }
  });
  nodes[0].article_count = articleMap.size;
  nodes[0].child_count = articleMap.size;

  return {
    ok: true,
    engine: 'rag_retrieval',
    graph_type: 'rag_retrieval',
    root_ids: [queryId],
    nodes,
    edges,
    children_by_parent: childrenByParent,
    articles_by_concept: articlesByConcept,
    stats: {
      concepts: nodes.length,
      roots: 1,
      articles: articleMap.size,
      sources: sources.length,
      build_ms: 0,
      concept_graph_strategy: 'retrieval_provenance',
    },
    warning: '',
    hidden_node_count: 0,
  };
}

let _researchScopeFeatureEnabled = true;

function setResearchScopeFeatureEnabled(enabled) {
  _researchScopeFeatureEnabled = Boolean(enabled);
  document.querySelectorAll('[data-research-scope-feature]').forEach((element) => {
    element.hidden = !_researchScopeFeatureEnabled;
    if ('disabled' in element) element.disabled = !_researchScopeFeatureEnabled;
  });
  if (!_researchScopeFeatureEnabled && document.getElementById('kg-view-mode')?.value === 'research') {
    setCartographyView('articles');
  }
}

// The ⋯ menu always holds "Refresh"; the quality filter, "Recalculate" and
// "Empty cache" only apply to the thematic map.
function _kgSetThematicTools(visible) {
  document.querySelectorAll('[data-kg-thematic-tool]').forEach((element) => { element.hidden = !visible; });
}

function setCartographyView(view, { load = true } = {}) {
  const allowedViews = new Set(['articles', 'research', 'retrieval', 'library']);
  const requestedView = allowedViews.has(String(view || '')) ? String(view) : 'articles';
  const nextView = requestedView === 'research' && !_researchScopeFeatureEnabled
    ? 'articles'
    : requestedView;
  const viewSelect = document.getElementById('kg-view-mode');
  if (viewSelect) viewSelect.value = nextView;
  document.querySelectorAll('[data-cartography-view]').forEach((button) => {
    const active = button.dataset.cartographyView === nextView;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
    button.tabIndex = active ? 0 : -1;
  });

  const copyByView = {
    articles: ['connections.articlesTitle', 'connections.articlesDescription'],
    research: ['connections.researchTitle', 'connections.researchDescription'],
    retrieval: ['connections.retrievalTitle', 'connections.retrievalDescription'],
    library: ['connections.libraryTitle', 'connections.libraryDescription'],
  };
  const [titleKey, descriptionKey] = copyByView[nextView];
  const viewTab = document.querySelector(`[data-cartography-view="${nextView}"]`);
  if (viewTab) viewTab.title = `${_ti(titleKey)} — ${_ti(descriptionKey)}`;

  const graphSection = document.getElementById('knowledge-graph-section');
  const researchWorkspace = document.getElementById('research-scope-workspace');
  const articleWorkspace = document.getElementById('article-graph-workspace');
  const isResearch = nextView === 'research';
  const isArticles = nextView === 'articles';
  // The author/lab graph is rebuilt on demand; a graph built from articles
  // stays in its screen so switching to the overview and back keeps it.
  if (!isResearch && typeof _nbCg !== 'undefined' && _nbCg.embedded === 'research') closeNbCitationGraph();
  if (graphSection) graphSection.hidden = isResearch || isArticles;
  if (researchWorkspace) researchWorkspace.hidden = !isResearch;
  if (articleWorkspace) articleWorkspace.hidden = !isArticles;
  const headerTools = document.getElementById('cartography-header-tools');
  if (headerTools) headerTools.hidden = isResearch || isArticles;
  if (isArticles) {
    if (typeof showArticleGraphView === 'function') showArticleGraphView();
    window.requestAnimationFrame(() => {
      if (typeof refreshLayoutResizers === 'function') refreshLayoutResizers();
    });
    return;
  }
  if (isResearch) {
    if (typeof _researchScopeBindUiOnce === 'function') _researchScopeBindUiOnce();
    const results = document.getElementById('research-scope-results');
    if (results && !results.childElementCount && typeof _researchScopeRenderResults === 'function') {
      _researchScopeRenderResults();
    }
    window.requestAnimationFrame(() => {
      if (typeof refreshLayoutResizers === 'function') refreshLayoutResizers();
    });
    return;
  }

  _kgSetThematicTools(nextView === 'library');
  if (load) window.setTimeout(() => loadKnowledgeGraph(), 0);
}

function handleCartographyViewKeydown(event, currentButton) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  const buttons = Array.from(document.querySelectorAll('[data-cartography-view]'));
  if (!buttons.length) return;
  event.preventDefault();
  const currentIndex = Math.max(0, buttons.indexOf(currentButton));
  const nextIndex = event.key === 'Home'
    ? 0
    : event.key === 'End'
      ? buttons.length - 1
      : (currentIndex + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
  const nextButton = buttons[nextIndex];
  setCartographyView(nextButton.dataset.cartographyView);
  nextButton.focus();
}

function openRagRetrievalGraph(query, sources, answerOrigin = 'rag') {
  const record = rememberLastRagRetrieval(query, sources, answerOrigin);
  if (!record) return;
  switchTab('connections');
  setCartographyView('retrieval');
}

async function loadKnowledgeGraph(options = {}) {
  const selectedView = String(document.getElementById('kg-view-mode')?.value || 'articles');
  // These two views draw their own graph (citation-graph.js).
  if (selectedView === 'research' || selectedView === 'articles') return null;
  if (!options.silent) _setKnowledgeGraphStatus('Chargement du graphe conceptuel…');
  try {
    const viewMode = selectedView;
    if (viewMode === 'retrieval') {
      const payload = _buildRagRetrievalGraphPayload();
      if (!options.silent) _setKnowledgeGraphStatus('');
      _kgUpdateGraphKindPill(payload);
      _renderKnowledgeGraph(payload);
      return payload;
    }
    const r = await fetch('/api/knowledge-graph?engine=taxonomy');
    const d = await r.json();
    if (!options.silent) {
      _setKnowledgeGraphStatus('');
    }
    _kgUpdateGraphKindPill(d);
    _renderKnowledgeGraph(d);
    return d;
  } catch (e) {
    console.error('Knowledge graph load failed', e);
    _setKnowledgeGraphStatus('Erreur de chargement du graphe.');
    _setKnowledgeFootnote('');
    if (options.rethrow) throw e;
  }
}

async function regenerateKnowledgeTaxonomy(mode = 'openalex') {
  document.getElementById('kg-tools-menu')?.removeAttribute('open');
  const useOpenAlex = mode !== 'local';
  const btnId = useOpenAlex ? 'kg-regen-openalex-btn' : 'kg-regen-local-btn';
  const btn = document.getElementById(btnId);
  const original = btn?.textContent || '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = useOpenAlex ? _tr('OpenAlex…') : _tr('Local…');
  }
  _setKnowledgeGraphStatus('');
  _setKnowledgeGraphBusy(
    useOpenAlex ? 'Régénération avec OpenAlex' : 'Régénération locale',
    useOpenAlex
      ? 'Recherche des ancres OpenAlex, puis reclassement local des concepts fins…'
      : 'Reconstruction deterministe de la taxonomie locale…'
  );
  try {
    const r = await fetch('/api/article-taxonomy/regenerate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) {
      _setKnowledgeGraphStatus((d && (d.detail || d.msg)) || 'Erreur lors de la regeneration de la taxonomie.');
      return;
    }
    _setKnowledgeGraphBusy('Mise a jour du graphe', 'Chargement de la nouvelle hiérarchie…');
    await loadKnowledgeGraph({ silent: true });
  } catch (e) {
    _setKnowledgeGraphStatus('Erreur lors de la regeneration de la taxonomie.');
  } finally {
    _setKnowledgeGraphBusy('', '');
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}

async function clearKnowledgeTaxonomyCache() {
  document.getElementById('kg-tools-menu')?.removeAttribute('open');
  const btn = document.getElementById('kg-clear-taxonomy-cache-btn');
  const original = btn?.textContent || '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = _tr('Cache…');
  }
  _setKnowledgeGraphStatus('');
  _setKnowledgeGraphBusy('Vidage du cache', 'Suppression du cache taxonomique avant reconstruction…');
  try {
    const r = await fetch('/api/article-taxonomy/cache', { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok || !d.ok) {
      _setKnowledgeGraphStatus((d && (d.detail || d.msg)) || 'Vidage du cache impossible.');
      return;
    }
    _setKnowledgeGraphBusy('Cache vide', 'Reconstruction de la taxonomie d articles…');
    await regenerateKnowledgeTaxonomy('local');
  } catch (e) {
    _setKnowledgeGraphStatus('Vidage du cache impossible.');
  } finally {
    _setKnowledgeGraphBusy('', '');
    if (btn) {
      btn.disabled = false;
      btn.textContent = original;
    }
  }
}
