// ---------------------------------------------
//  UPDATES
//  Daily check of a static latest.json (src/update_check.py). Nothing is sent
//  before the user answered: the setup assistant asks new users, a one-time
//  card asks existing ones. Installing always takes a click.
//  Each installation updates its own way: Setup.exe and ZIP installs get the
//  new package downloaded ahead of time (ZIP installs also get the manual
//  steps, in case the one-click update fails), Git checkouts get a "git pull"
//  button (refused while tracked files have local changes).
// ---------------------------------------------
const UPDATE_STATUS_REFRESH_MS = 30 * 60 * 1000;
const UPDATE_JOB_POLL_MS = 1000;
const UPDATE_RESTART_POLL_MS = 2000;
const UPDATE_RESTART_TIMEOUT_MS = 20 * 60 * 1000;

let _updateStatus = null;
let _updateWatch = null;

function _updateT(key, params = null) {
  return typeof _ti === 'function' ? _ti(key, params) : key;
}

async function _updateRequest(url, options = {}) {
  const response = await fetch(url, { cache: 'no-store', ...options });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function _updateFormatDate(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const language = window.BibliobrainI18n?.getLanguage?.() || 'fr';
  return date.toLocaleString(language, { dateStyle: 'medium', timeStyle: 'short' });
}

function _updateJobIsForeground(job) {
  // A background download (installer fetched ahead of the click) is not "working".
  return ['downloading', 'verifying', 'restarting'].includes(job?.phase) && !job?.background;
}

function _updateStatusLine(status) {
  if (!status) return '';
  if (status.update_available && ['packaged', 'zip'].includes(status.install_mode)) {
    const job = status.job || {};
    if (status.downloaded) {
      return _updateT('update.readyLine', { version: status.latest_version, current: status.current_version });
    }
    if (job.background && ['downloading', 'verifying'].includes(job.phase)) {
      const percent = Number.isFinite(job.percent) ? `${job.percent} %` : '';
      return _updateT('update.backgroundDownloading', { version: status.latest_version, percent });
    }
  }
  if (status.update_available) {
    return _updateT('update.available', { version: status.latest_version, current: status.current_version });
  }
  const errorKey = {
    offline: 'update.errorOffline',
    not_found: 'update.errorNotPublished',
  }[status.last_error] || (status.last_error ? 'update.errorGeneric' : '');
  if (errorKey) return _updateT(errorKey);
  if (status.checked_at) {
    return _updateT('update.upToDate', {
      version: status.current_version,
      date: _updateFormatDate(status.checked_at),
    });
  }
  return _updateT('update.neverChecked', { version: status.current_version });
}

// The notes are the CHANGELOG section: show its Markdown as plain, readable text.
function _updateNotesText(notes) {
  return String(notes || '')
    .replace(/^#{1,6}\s*/gm, '')
    .replace(/^(\s*)[-*]\s+/gm, '$1• ')
    .replace(/[`*]/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .trim();
}

function _updateBlockerText(code) {
  const key = {
    not_managed: 'update.blockedNotManaged',
    unsupported: 'update.blockedUnsupported',
    git_missing: 'update.blockedGitMissing',
    no_installer: 'update.blockedNoInstaller',
    no_archive: 'update.blockedNoArchive',
    busy: 'update.blockedBusy',
    git_dirty: 'update.blockedGitDirty',
    git_no_upstream: 'update.blockedGitNoUpstream',
  }[code];
  return key ? _updateT(key) : '';
}

function _updateEl(tag, { className = '', text = '' } = {}) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

async function openUpdateInstallFolder(dir) {
  let ok = false;
  try {
    ok = Boolean((await _updateRequest('/api/update/open-folder', { method: 'POST' }))?.ok);
  } catch (error) {
    ok = false;
  }
  if (!ok) showToast(_updateT('update.zipOpenFolderError', { dir }), 6000, 'error');
}

function _renderZipGuide(panel, status) {
  const guide = status.zip_guide || {};
  const file = guide.file_name || 'Bibliobrain-Windows.zip';
  const dir = guide.install_dir || '';
  // A fallback when the one-click update cannot run or did not work.
  const failed = Boolean(status.install_blocker)
    || status.job?.phase === 'failed'
    || status.last_install?.ok === false;
  const details = _updateEl('details', { className: 'update-zip-manual' });
  details.open = failed;
  details.append(_updateEl('summary', { className: 'disclosure', text: _updateT('update.zipTitle') }));
  panel.append(details);
  const steps = _updateEl('ol');
  [
    _updateT(guide.download_url ? 'update.zipStep1' : 'update.zipStep1Page', { file }),
    _updateT('update.zipStep2', { dir }),
    _updateT('update.zipStep3', { file }),
    _updateT('update.zipStep4'),
  ].forEach((text) => steps.append(_updateEl('li', { text })));
  details.append(steps);

  const actions = _updateEl('div', { className: 'update-mode-actions' });
  if (guide.download_url) {
    const download = _updateEl('a', { className: 'btn btn-indigo', text: _updateT('update.zipDownload', { file }) });
    download.href = guide.download_url;
    download.rel = 'noopener noreferrer';
    actions.append(download);
  }
  if (guide.releases_url) {
    const page = _updateEl('a', { className: 'btn btn-ghost', text: _updateT('update.notesLink') });
    page.href = guide.releases_url;
    page.target = '_blank';
    page.rel = 'noopener noreferrer';
    actions.append(page);
  }
  const folder = _updateEl('button', { className: 'btn btn-ghost', text: _updateT('update.zipOpenFolder') });
  folder.type = 'button';
  folder.addEventListener('click', () => openUpdateInstallFolder(dir));
  actions.append(folder);
  details.append(actions);
}

function _renderGitState(panel, status) {
  const git = status.git || {};
  panel.append(_updateEl('h3', { text: _updateT('update.gitTitle') }));
  let warning = false;
  if (!git.available) {
    panel.append(_updateEl('p', { text: _updateT('update.gitMissing') }));
    warning = true;
  } else if (!git.branch) {
    panel.append(_updateEl('p', { text: _updateT('update.gitDetached') }));
  } else if (!git.upstream) {
    panel.append(_updateEl('p', { text: _updateT('update.gitNoUpstream', { branch: git.branch }) }));
  } else {
    panel.append(_updateEl('p', { text: _updateT('update.gitBranch', { branch: git.branch, upstream: git.upstream }) }));
  }
  if (git.change_count) {
    warning = true;
    panel.append(_updateEl('p', { text: _updateT('update.gitDirty', { count: git.change_count }) }));
    const list = _updateEl('ul', { className: 'update-git-changes' });
    (git.changes || []).forEach((path) => list.append(_updateEl('li', { text: path })));
    const more = git.change_count - (git.changes || []).length;
    if (more > 0) list.append(_updateEl('li', { text: _updateT('update.gitDirtyMore', { count: more }) }));
    panel.append(list);
  }
  panel.dataset.state = warning ? 'warning' : '';
}

function _renderUpdateModePanel(status) {
  const panel = document.getElementById('update-mode-panel');
  if (!panel) return;
  panel.replaceChildren();
  const showZip = status.install_mode === 'zip' && status.update_available;
  const showGit = status.install_mode === 'git' && Boolean(status.git);
  panel.hidden = !(showZip || showGit);
  panel.dataset.state = '';
  if (showZip) _renderZipGuide(panel, status);
  else if (showGit) _renderGitState(panel, status);
}

function _installButtonLabel(status) {
  if (status.install_mode === 'git') return _updateT('update.gitPull');
  if (status.downloaded) {
    return _updateT('update.installReady', { version: status.latest_version || '' });
  }
  return _updateT('update.install', { version: status.latest_version || '' });
}

function renderUpdateUi(status = _updateStatus) {
  if (!status) return;
  const job = status.job || {};
  const working = _updateJobIsForeground(job);
  const isGit = status.install_mode === 'git';

  const sidebarButton = document.getElementById('app-update-btn');
  if (sidebarButton) {
    sidebarButton.hidden = !status.update_available || working;
    const label = document.getElementById('app-update-btn-version');
    if (label) label.textContent = status.latest_version || '';
  }
  const consent = document.getElementById('update-consent');
  if (consent) consent.hidden = !status.ask_consent;

  const toggle = document.getElementById('update-check-enabled');
  if (toggle) toggle.checked = status.enabled === true;
  const startupChoice = document.getElementById('startup-update-check');
  if (startupChoice && status.enabled !== null && status.enabled !== undefined) {
    startupChoice.checked = status.enabled;
  }

  const line = document.getElementById('update-status-text');
  if (line) {
    line.textContent = _updateStatusLine(status);
    line.dataset.state = status.update_available ? 'available' : (status.last_error ? 'error' : 'ok');
  }
  const notes = document.getElementById('update-notes');
  if (notes) {
    const hasNotes = Boolean(status.update_available && (status.notes || status.notes_url));
    notes.hidden = !hasNotes;
    const body = document.getElementById('update-notes-body');
    if (body) body.textContent = _updateNotesText(status.notes);
    const link = document.getElementById('update-notes-link');
    if (link) {
      link.hidden = !status.notes_url;
      if (status.notes_url) link.href = status.notes_url;
    }
  }
  const installButton = document.getElementById('update-install-btn');
  if (installButton) {
    // A Git checkout can pull at any time; the others wait for a release.
    installButton.hidden = !(status.update_available || isGit) || Boolean(status.install_blocker);
    installButton.disabled = working;
    installButton.textContent = _installButtonLabel(status);
  }
  const blocker = document.getElementById('update-install-hint');
  if (blocker) {
    // The Git panel and the ZIP steps already say it in full.
    const explained = ['git_dirty', 'git_no_upstream', 'git_missing'].includes(status.install_blocker);
    const text = explained ? '' : _updateBlockerText(status.install_blocker);
    blocker.hidden = !text;
    blocker.textContent = text;
  }
  _renderUpdateModePanel(status);
}

async function fetchUpdateStatus() {
  try {
    _updateStatus = await _updateRequest('/api/update/status');
    renderUpdateUi();
  } catch (error) {
    // The server may be restarting; the next refresh will catch up.
  }
  return _updateStatus;
}

function _setConsentCardBusy(busy) {
  const consent = document.getElementById('update-consent');
  if (!consent) return;
  // The answer is taken at once: the card must not wait for the network check,
  // which can take seconds while the library is being indexed.
  if (busy) consent.hidden = true;
  consent.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
}

async function saveUpdateCheckChoice(enabled, { fromCard = false } = {}) {
  if (fromCard) _setConsentCardBusy(true);
  try {
    const settings = await _updateRequest('/api/app-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ update_check_enabled: Boolean(enabled) }),
    });
    if (settings?.ok && typeof state === 'object') state.appSettings = settings;
    _updateStatus = await _updateRequest('/api/update/status');
  } catch (error) {
    if (fromCard) {
      _setConsentCardBusy(false);
      renderUpdateUi();
    }
    showToast(_updateT('update.saveError'), 4000, 'error');
    return;
  }
  if (fromCard) _setConsentCardBusy(false);
  renderUpdateUi();
  if (fromCard && enabled) showToast(_updateT('update.consentSaved'), 2600, 'success');
  if (!enabled) return;
  // Enabling is the consent: check right away instead of waiting for the loop.
  try {
    _updateStatus = await _updateRequest('/api/update/check', { method: 'POST' });
    renderUpdateUi();
  } catch (error) {
    // The daily loop retries; the choice itself is saved.
  }
}

async function checkForUpdatesNow() {
  const button = document.getElementById('update-check-now-btn');
  if (button) button.disabled = true;
  try {
    _updateStatus = await _updateRequest('/api/update/check?force=true', { method: 'POST' });
    renderUpdateUi();
    if (!_updateStatus.update_available && !_updateStatus.last_error) {
      showToast(_updateT('update.noneToast'), 2600, 'success');
    }
  } catch (error) {
    showToast(_updateT('update.errorGeneric'), 4000, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

function openUpdateSettings() {
  if (typeof openSettingsAt === 'function') openSettingsAt('expert-diagnostics', 'settings-updates');
}

function _setUpdateOverlay({ title = '', text = '', percent = null, failed = false } = {}) {
  const overlay = document.getElementById('update-overlay');
  if (!overlay) return;
  overlay.hidden = false;
  overlay.dataset.state = failed ? 'failed' : 'working';
  document.getElementById('update-overlay-title').textContent = title;
  document.getElementById('update-overlay-text').textContent = text;
  const bar = document.getElementById('update-overlay-progress');
  if (bar) {
    bar.hidden = failed;
    bar.classList.toggle('is-indeterminate', percent === null);
    const fill = bar.querySelector('span');
    if (fill) fill.style.width = percent === null ? '' : `${Math.max(2, percent)}%`;
  }
  const close = document.getElementById('update-overlay-close');
  if (close) {
    close.hidden = !failed;
    if (failed) close.focus();
  }
}

function closeUpdateOverlay() {
  const overlay = document.getElementById('update-overlay');
  if (overlay) overlay.hidden = true;
  if (_updateWatch) clearTimeout(_updateWatch.timer);
  _updateWatch = null;
  fetchUpdateStatus();
}

function _updateJobErrorText(code) {
  if (code === 'offline') return _updateT('update.jobErrorOffline');
  if (code === 'invalid_manifest') return _updateT('update.jobErrorChecksum');
  return _updateT('update.jobErrorGeneric');
}

async function _pollUpdateProgress() {
  const watch = _updateWatch;
  if (!watch) return;
  let status = null;
  try {
    status = await _updateRequest('/api/update/status');
  } catch (error) {
    // Expected while the manager has stopped the server.
    watch.serverWasDown = true;
  }
  if (_updateWatch !== watch) return;

  if (status) {
    _updateStatus = status;
    const phase = status.job?.phase;
    const install = status.last_install;
    const finishedAt = Date.parse(install?.finished_at || '');
    const finishedNow = watch.serverWasDown && install?.ok === true
      && Number.isFinite(finishedAt) && finishedAt >= watch.startedAt - 5000;
    // A git pull can bring commits without changing VERSION: the manager's
    // "ok" result written after this click also means it is done.
    if (status.current_version !== watch.fromVersion || finishedNow) {
      _setUpdateOverlay({ title: _updateT('update.overlayDone'), text: _updateT('update.overlayReloading') });
      window.setTimeout(() => window.location.reload(), 800);
      return;
    }
    if (phase === 'failed') {
      _setUpdateOverlay({
        title: _updateT('update.overlayFailed'),
        text: _updateJobErrorText(status.job?.error),
        failed: true,
      });
      return;
    }
    if (watch.serverWasDown && status.last_install && status.last_install.ok === false) {
      _setUpdateOverlay({
        title: _updateT('update.overlayFailed'),
        text: _updateT('update.installFailed', { message: status.last_install.message || '' }),
        failed: true,
      });
      return;
    }
    if (phase === 'downloading') {
      _setUpdateOverlay({
        title: watch.title,
        text: _updateT('update.overlayDownloading'),
        percent: Number.isFinite(status.job?.percent) ? status.job.percent : null,
      });
    } else if (phase === 'verifying') {
      _setUpdateOverlay({
        title: watch.title,
        text: _updateT('update.overlayVerifying'),
      });
    }
  }
  if (Date.now() - watch.startedAt > UPDATE_RESTART_TIMEOUT_MS) {
    _setUpdateOverlay({
      title: _updateT('update.overlayFailed'),
      text: _updateT('update.overlayTimeout'),
      failed: true,
    });
    return;
  }
  const restarting = watch.serverWasDown || status?.job?.phase === 'restarting';
  if (restarting && !watch.restartShown) {
    watch.restartShown = true;
    _setUpdateOverlay({
      title: watch.title,
      text: watch.restartText,
    });
  }
  watch.timer = window.setTimeout(_pollUpdateProgress, restarting ? UPDATE_RESTART_POLL_MS : UPDATE_JOB_POLL_MS);
}

async function startAppUpdate() {
  const status = _updateStatus;
  if (!status?.update_available && status?.install_mode !== 'git') return;
  const button = document.getElementById('update-install-btn');
  if (button) button.disabled = true;
  let result = null;
  try {
    result = await _updateRequest('/api/update/start', { method: 'POST' });
  } catch (error) {
    result = { ok: false, code: '' };
  }
  if (button) button.disabled = false;
  if (!result?.ok) {
    showToast(_updateBlockerText(result?.code) || _updateT('update.jobErrorGeneric'), 5200, 'error');
    if (result?.status) {
      _updateStatus = result.status;
      renderUpdateUi();
    }
    return;
  }
  const isGit = status.install_mode === 'git';
  const ready = Boolean(status.downloaded);
  _updateWatch = {
    title: isGit ? _updateT('update.gitPull') : _updateT('update.overlayTitle', { version: status.latest_version }),
    restartText: _updateT(isGit ? 'update.overlayGit' : 'update.overlayRestarting'),
    fromVersion: status.current_version,
    toVersion: status.latest_version,
    startedAt: Date.now(),
    serverWasDown: false,
    restartShown: false,
    timer: null,
  };
  _setUpdateOverlay({
    title: _updateWatch.title,
    text: _updateT(isGit ? 'update.overlayGit' : (ready ? 'update.overlayRestarting' : 'update.overlayDownloading')),
    percent: isGit || ready ? null : 0,
  });
  _pollUpdateProgress();
}

function _bindUpdateUi() {
  document.getElementById('app-update-btn')?.addEventListener('click', openUpdateSettings);
  document.getElementById('update-consent-accept')?.addEventListener('click', () => saveUpdateCheckChoice(true, { fromCard: true }));
  document.getElementById('update-consent-decline')?.addEventListener('click', () => saveUpdateCheckChoice(false, { fromCard: true }));
  document.getElementById('update-check-enabled')?.addEventListener('change', (event) => {
    saveUpdateCheckChoice(event.target.checked);
  });
  document.getElementById('update-check-now-btn')?.addEventListener('click', checkForUpdatesNow);
  document.getElementById('update-install-btn')?.addEventListener('click', startAppUpdate);
  document.getElementById('update-overlay-close')?.addEventListener('click', closeUpdateOverlay);
  window.addEventListener('bibliobrain:languagechange', () => renderUpdateUi());
  // Only local reads here: the server decides when a network check is due.
  window.setTimeout(fetchUpdateStatus, 1500);
  window.setInterval(fetchUpdateStatus, UPDATE_STATUS_REFRESH_MS);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _bindUpdateUi);
} else {
  _bindUpdateUi();
}
