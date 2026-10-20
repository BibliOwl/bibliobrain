// ---------------------------------------------
//  UPDATES
//  Daily check of a static latest.json (src/update_check.py). Nothing is sent
//  before the user answered: the setup assistant asks new users, a one-time
//  card asks existing ones. Installing always takes a click.
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

function _updateStatusLine(status) {
  if (!status) return '';
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
  }[code];
  return key ? _updateT(key) : '';
}

function renderUpdateUi(status = _updateStatus) {
  if (!status) return;
  const job = status.job || {};
  const working = ['downloading', 'verifying', 'restarting'].includes(job.phase);

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
    installButton.hidden = !status.update_available || Boolean(status.install_blocker);
    installButton.disabled = working;
    installButton.textContent = _updateT('update.install', { version: status.latest_version || '' });
  }
  const blocker = document.getElementById('update-install-hint');
  if (blocker) {
    const text = _updateBlockerText(status.install_blocker);
    blocker.hidden = !text;
    blocker.textContent = text;
  }
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
    if (status.current_version !== watch.fromVersion) {
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
        title: _updateT('update.overlayTitle', { version: watch.toVersion }),
        text: _updateT('update.overlayDownloading'),
        percent: Number.isFinite(status.job?.percent) ? status.job.percent : null,
      });
    } else if (phase === 'verifying') {
      _setUpdateOverlay({
        title: _updateT('update.overlayTitle', { version: watch.toVersion }),
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
      title: _updateT('update.overlayTitle', { version: watch.toVersion }),
      text: _updateT('update.overlayRestarting'),
    });
  }
  watch.timer = window.setTimeout(_pollUpdateProgress, restarting ? UPDATE_RESTART_POLL_MS : UPDATE_JOB_POLL_MS);
}

async function startAppUpdate() {
  const status = _updateStatus;
  if (!status?.update_available) return;
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
  _updateWatch = {
    fromVersion: status.current_version,
    toVersion: status.latest_version,
    startedAt: Date.now(),
    serverWasDown: false,
    restartShown: false,
    timer: null,
  };
  _setUpdateOverlay({
    title: _updateT('update.overlayTitle', { version: status.latest_version }),
    text: _updateT(status.install_mode === 'git' ? 'update.overlayRestarting' : 'update.overlayDownloading'),
    percent: status.install_mode === 'git' ? null : 0,
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
