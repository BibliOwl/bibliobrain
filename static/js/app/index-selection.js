// ---------------------------------------------
//  WHAT BIBLIOBRAIN READS (Settings > My documents)
// ---------------------------------------------
// Picks the Zotero collections, sub-folders and very large files the search
// index leaves out. The choice applies at the next document reload (see
// src/index_selection.py).
(function () {
  let overview = null;
  let saving = Promise.resolve();

  const byId = (id) => document.getElementById(id);
  const t = (key, params) => (typeof _ti === 'function' ? _ti(key, params) : key);

  function formatSize(bytes) {
    const mb = Number(bytes || 0) / (1024 * 1024);
    return mb >= 1024 ? `${(mb / 1024).toFixed(1)} Go` : `${Math.round(mb)} Mo`;
  }

  function setFeedback(text, stateName = '') {
    const box = byId('index-selection-feedback');
    if (!box) return;
    box.hidden = !text;
    box.textContent = text || '';
    if (stateName) box.dataset.state = stateName;
    else delete box.dataset.state;
  }

  function renderSummary() {
    const box = byId('index-selection-summary');
    if (!box || !overview) return;
    const total = Number(overview.total_count || 0);
    const kept = Number(overview.kept_count || 0);
    const reasons = overview.skipped_by_reason || {};
    const parts = [];
    if (reasons.collection) parts.push(t('indexSelection.skippedCollection', { count: reasons.collection }));
    if (reasons.folder) parts.push(t('indexSelection.skippedFolder', { count: reasons.folder }));
    if (reasons.too_large) parts.push(t('indexSelection.skippedLarge', { count: reasons.too_large }));
    const summaryKey = kept === 1 ? 'indexSelection.summaryOne' : 'indexSelection.summary';
    let text = kept === total
      ? t('indexSelection.summaryAll', { count: total })
      : `${t(summaryKey, { kept, total })}${parts.length ? ` (${parts.join(', ')})` : ''}`;
    const selection = overview.selection || {};
    if (overview.metadata_loaded && selection.collection_mode !== 'all' && !(selection.collections || []).length) {
      // Nothing ticked leaves the collection choice off rather than emptying the index.
      text += ` ${t('indexSelection.tickOne')}`;
    }
    box.textContent = text;
    const startupBox = byId('startup-index-selection-summary');
    if (startupBox) startupBox.textContent = text;
  }

  // A ticked parent already covers its children: they show ticked and locked.
  function coveredBy(path, chosen, separator) {
    const parts = String(path).toLowerCase().split(separator);
    for (let depth = 1; depth < parts.length; depth += 1) {
      if (chosen.has(parts.slice(0, depth).join(separator))) return true;
    }
    return false;
  }

  function checkboxRow(value, label, count, depth, checked, onChange, covered = false) {
    const row = document.createElement('label');
    row.className = 'index-selection-item';
    row.style.setProperty('--depth', String(depth || 0));
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = value;
    input.checked = checked || covered;
    input.disabled = covered;
    if (covered) {
      input.dataset.covered = 'true';
      row.classList.add('is-covered');
    }
    input.addEventListener('change', onChange);
    const name = document.createElement('span');
    name.className = 'index-selection-item-name';
    name.textContent = label;
    const badge = document.createElement('span');
    badge.className = 'index-selection-item-count';
    badge.textContent = String(count);
    row.append(input, name, badge);
    return row;
  }

  function lastSegment(path, separator) {
    const parts = String(path).split(separator);
    return parts[parts.length - 1];
  }

  function renderCollections() {
    const block = byId('index-selection-collections');
    const list = byId('index-selection-collection-list');
    if (!block || !list || !overview) return;
    const collections = overview.collections || [];
    block.hidden = !overview.metadata_loaded || !collections.length;
    const selection = overview.selection || {};
    const mode = selection.collection_mode || 'all';
    document.querySelectorAll('input[name="index-selection-mode"]').forEach((input) => {
      input.checked = input.value === mode;
    });
    list.hidden = mode === 'all';
    const chosen = new Set((selection.collections || []).map((path) => path.toLowerCase()));
    list.replaceChildren(...collections.map((collection) => checkboxRow(
      collection.path,
      lastSegment(collection.path, ' / '),
      collection.count,
      collection.depth,
      chosen.has(collection.path.toLowerCase()),
      () => save(),
      coveredBy(collection.path, chosen, ' / '),
    )));
    list.querySelectorAll('.index-selection-item').forEach((row, index) => {
      row.title = collections[index].path;
    });
  }

  function renderFolders() {
    const details = byId('index-selection-folders');
    const list = byId('index-selection-folder-list');
    if (!details || !list || !overview) return;
    const folders = overview.folders || [];
    details.hidden = !folders.length;
    const excluded = new Set((overview.selection?.excluded_folders || []).map((path) => path.toLowerCase()));
    list.replaceChildren(...folders.map((folder) => checkboxRow(
      folder.path,
      lastSegment(folder.path, '/'),
      folder.count,
      folder.depth,
      excluded.has(folder.path.toLowerCase()),
      () => save(),
      coveredBy(folder.path, excluded, '/'),
    )));
    list.querySelectorAll('.index-selection-item').forEach((row, index) => {
      row.title = folders[index].path;
    });
  }

  function fileRow(file, actionLabel, onClick) {
    const item = document.createElement('li');
    const name = document.createElement('span');
    name.className = 'index-selection-file-name';
    name.textContent = file.title || file.rel_path;
    name.title = file.rel_path;
    item.append(name);
    if (file.size) {
      const size = document.createElement('span');
      size.className = 'index-selection-item-count';
      size.textContent = formatSize(file.size);
      item.append(size);
    }
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-ghost btn-small';
    button.textContent = actionLabel;
    button.addEventListener('click', onClick);
    item.append(button);
    return item;
  }

  function renderLarge() {
    const input = byId('index-selection-max-mb');
    const list = byId('index-selection-large-list');
    if (!input || !list || !overview) return;
    if (document.activeElement !== input) input.value = String(overview.selection?.max_file_mb ?? 0);
    const allowed = overview.selection?.allowed_large || [];
    const rows = [
      ...(overview.too_large || []).map((file) => fileRow(file, t('indexSelection.readAnyway'), () => {
        save({ allowed_large: [...allowed, file.rel_path] });
      })),
      ...(overview.allowed_large || []).map((file) => fileRow(file, t('indexSelection.skipAgain'), () => {
        save({ allowed_large: allowed.filter((path) => path !== file.rel_path) });
      })),
    ];
    list.replaceChildren(...rows);
    list.hidden = !rows.length;
  }

  function render() {
    renderSummary();
    renderCollections();
    renderFolders();
    renderLarge();
  }

  function collectSelection() {
    const mode = document.querySelector('input[name="index-selection-mode"]:checked')?.value || 'all';
    const checked = (id) => Array.from(document.querySelectorAll(`#${id} input[type=checkbox]:checked:not([data-covered])`))
      .map((input) => input.value);
    const maxMb = parseInt(byId('index-selection-max-mb')?.value || '', 10);
    return {
      collection_mode: mode,
      collections: checked('index-selection-collection-list'),
      excluded_folders: checked('index-selection-folder-list'),
      max_file_mb: Number.isFinite(maxMb) && maxMb >= 0 ? maxMb : 0,
    };
  }

  function save(extra = {}) {
    const payload = { ...collectSelection(), ...extra };
    saving = saving.then(async () => {
      try {
        const response = await fetch('/api/index-selection', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        overview = await response.json();
        render();
        setFeedback(t('indexSelection.saved'), 'saved');
        // The reload badge compares the index with this new selection.
        if (typeof checkRagRefreshNeeded === 'function') void checkRagRefreshNeeded(true);
      } catch (error) {
        setFeedback(t('indexSelection.saveError', { error: error.message }), 'error');
      }
    });
    return saving;
  }

  async function loadIndexSelection() {
    try {
      const response = await fetch('/api/index-selection');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      overview = await response.json();
      render();
    } catch (error) {
      const box = byId('index-selection-summary');
      if (box) box.textContent = t('indexSelection.loadError');
    }
    return overview;
  }


  // ---- Library profiles: one index per subject (src/library_profiles.py) ----
  let profiles = null;

  function activeProfile() {
    return (profiles?.profiles || []).find((profile) => profile.active) || null;
  }

  function profileLabel(profile) {
    return profile.name || t('libraryProfiles.mainName');
  }

  function renderProfiles() {
    const select = byId('library-profile-select');
    if (!select || !profiles) return;
    // One library is the usual case: the switcher only shows once there are two.
    select.hidden = profiles.profiles.length < 2;
    select.replaceChildren(...profiles.profiles.map((profile) => {
      const option = document.createElement('option');
      option.value = profile.id;
      const suffix = profile.built
        ? t('libraryProfiles.readySuffix')
        : profile.index_state === 'outdated'
          ? t('libraryProfiles.outdatedSuffix')
          : t('libraryProfiles.notBuiltSuffix');
      option.textContent = `${profileLabel(profile)} ${suffix}`;
      option.selected = profile.active;
      return option;
    }));
    const active = activeProfile();
    const del = byId('library-profile-delete');
    if (del) {
      del.disabled = !active || active.is_default;
      del.hidden = profiles.profiles.length < 2;
    }
    const rename = byId('library-profile-rename');
    if (rename) rename.hidden = profiles.profiles.length < 2;
    const status = byId('library-profile-status');
    const text = byId('library-profile-status-text');
    const build = byId('library-profile-build');
    const needsBuild = Boolean(active && !active.built);
    if (status) status.hidden = !needsBuild;
    const statusKey = active?.index_state === 'outdated' ? 'libraryProfiles.outdated' : 'libraryProfiles.notBuilt';
    if (text) text.textContent = needsBuild ? t(statusKey, { name: profileLabel(active) }) : '';
    if (build) build.hidden = !needsBuild;
  }

  async function loadProfiles() {
    try {
      const response = await fetch('/api/library-profiles');
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      profiles = await response.json();
      renderProfiles();
    } catch (_) {
      // The selection below still works on the active library.
    }
    return profiles;
  }

  async function profileRequest(url, options = {}) {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) throw new Error(data.msg || `HTTP ${response.status}`);
    profiles = data;
    renderProfiles();
    return data;
  }

  async function refreshAfterSwitch() {
    await loadIndexSelection();
    if (typeof loadLibrary === 'function') await loadLibrary();
    if (typeof loadZoteroFacets === 'function') await loadZoteroFacets();
  }

  async function buildActive() {
    if (state.ragInitBusy) return;
    const result = await initRAG(false, { silentSuccess: true });
    await loadProfiles();
    await refreshAfterSwitch();
    return result;
  }

  async function switchProfile(profileId) {
    const current = activeProfile();
    if (!profileId || profileId === current?.id) return;
    if (state.ragInitBusy) {
      showToast(t('libraryProfiles.busy'), 4200, 'info');
      renderProfiles();
      return;
    }
    try {
      await profileRequest(`/api/library-profiles/${encodeURIComponent(profileId)}/activate`, { method: 'POST' });
    } catch (error) {
      showToast(error.message, 5200, 'error');
      await loadProfiles();
      return;
    }
    state.ragReady = false;
    if (typeof _resetRagStatusUI === 'function') _resetRagStatusUI();
    const active = activeProfile();
    showToast(t('libraryProfiles.switched', { name: profileLabel(active) }), 3200, 'success');
    // A built index loads in seconds; an empty one waits for its selection.
    if (active?.built) await buildActive();
    else await refreshAfterSwitch();
  }

  async function createProfile() {
    const name = window.prompt(t('libraryProfiles.newPrompt'), '');
    if (!name || !name.trim()) return;
    try {
      const data = await profileRequest('/api/library-profiles', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), copy_current: true }),
      });
      // No switch to an empty index: the current library stays in use, and
      // the new one waits in the list until it is chosen and prepared.
      const created = (data.profiles || []).find((profile) => profile.id === data.created);
      showToast(t('libraryProfiles.created', { name: created ? profileLabel(created) : name.trim() }), 5200, 'success');
    } catch (error) {
      showToast(error.message, 5200, 'error');
    }
  }

  async function renameProfile() {
    const active = activeProfile();
    if (!active) return;
    const name = window.prompt(t('libraryProfiles.renamePrompt'), profileLabel(active));
    if (name === null) return;
    try {
      await profileRequest(`/api/library-profiles/${encodeURIComponent(active.id)}`, {
        method: 'PUT',
        body: JSON.stringify({ name: name.trim() }),
      });
    } catch (error) {
      showToast(error.message, 5200, 'error');
    }
  }

  async function deleteProfile() {
    const active = activeProfile();
    if (!active || active.is_default) return;
    if (!window.confirm(t('libraryProfiles.deleteConfirm', { name: profileLabel(active) }))) return;
    const main = profiles.profiles.find((profile) => profile.is_default);
    await switchProfile(main.id);
    try {
      await profileRequest(`/api/library-profiles/${encodeURIComponent(active.id)}`, { method: 'DELETE' });
      showToast(t('libraryProfiles.deleted', { name: profileLabel(active) }), 3200, 'success');
    } catch (error) {
      showToast(error.message, 5200, 'error');
    }
  }

  function bind() {
    document.querySelectorAll('input[name="index-selection-mode"]').forEach((input) => {
      input.addEventListener('change', () => {
        const list = byId('index-selection-collection-list');
        if (list) list.hidden = input.value === 'all';
        save();
      });
    });
    byId('index-selection-max-mb')?.addEventListener('change', () => save());
    byId('library-profile-select')?.addEventListener('change', (event) => switchProfile(event.target.value));
    byId('library-profile-new')?.addEventListener('click', createProfile);
    byId('library-profile-rename')?.addEventListener('click', renameProfile);
    byId('library-profile-delete')?.addEventListener('click', deleteProfile);
    byId('library-profile-build')?.addEventListener('click', buildActive);
    byId('index-selection-reload')?.addEventListener('click', () => {
      if (typeof reloadLibraryRag === 'function') void reloadLibraryRag();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();

  // The first-run assistant shows the same options, moved into its folded
  // "read only part of the folder" section while that section is open, so
  // the choice is made once, before the first preparation.
  function placeOptions(where) {
    const options = document.querySelector('.index-selection-options');
    const target = where === 'startup'
      ? byId('startup-index-selection-slot')
      : byId('index-selection-options');
    if (options && target && options.parentElement !== target) target.appendChild(options);
  }

  window.loadIndexSelection = async () => {
    placeOptions('settings');
    await Promise.all([loadProfiles(), loadIndexSelection()]);
    return overview;
  };

  function bindStartup() {
    const details = byId('startup-index-selection');
    details?.addEventListener('toggle', async () => {
      if (!details.open) {
        placeOptions('settings');
        return;
      }
      const summary = byId('startup-index-selection-summary');
      const typed = typeof normalizeUserPathInput === 'function'
        ? normalizeUserPathInput(byId('startup-data-path')?.value)
        : String(byId('startup-data-path')?.value || '').trim();
      if (!typed) {
        if (summary) summary.textContent = t('startup.indexSelectionNoFolder');
        return;
      }
      // The folder may be typed but not saved yet: the options list its contents.
      if (typed !== state.appSettings?.data_path && typeof applyPdfDataPath === 'function') {
        const applied = await applyPdfDataPath(typed);
        if (!applied?.ok) {
          if (summary) summary.textContent = applied?.msg || '';
          return;
        }
        if (state.appSettings) state.appSettings.data_path = typed;
      }
      placeOptions('startup');
      await loadIndexSelection();
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindStartup);
  else bindStartup();
  window.placeIndexSelectionOptions = placeOptions;
  // Documents the next preparation will read, for the first-run count.
  window.fetchIndexSelectionKeptCount = async () => {
    try {
      const response = await fetch('/api/index-selection');
      if (!response.ok) return null;
      const data = await response.json();
      return Number.isInteger(data.kept_count) ? data.kept_count : null;
    } catch (_) {
      return null;
    }
  };
  window.saveIndexSelection = save;
})();
