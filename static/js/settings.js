// ---------------------------------------------
//  I18N HELPER
// ---------------------------------------------
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

// ---------------------------------------------
//  PERSISTENCE (localStorage)
// ---------------------------------------------
const PERSIST_FIELDS = [
];
const LS_PREFIX = 'iassistant_';
const LLM_GENERATION_RECOMMENDED_DEFAULTS = Object.freeze({
  temperature: 0.1,
  repeatPenalty: 1.0,
  topP: 0.5,
});
const RESEARCH_RECOMMENDED_DEFAULTS = Object.freeze({
  threshold: 0.3,
  maxChunks: 8,
  // Same as _DEFAULT_CHAT_ANSWER_LENGTH in src/app_settings.py.
  chatAnswerLength: 'auto',
  rerankerEnabled: true,
  rerankerProfile: 'auto',
  rerankerTranslateFrench: true,
  chunkSize: 1000,
});

function _setLlmGenerationValues(values) {
  [
    ['llm-temperature', 'llm-temperature-val', values.temperature],
    ['llm-repeat-penalty', 'llm-repeat-penalty-val', values.repeatPenalty],
    ['llm-top-p', 'llm-top-p-val', values.topP],
  ].forEach(([inputId, pillId, rawValue]) => {
    const value = Number(rawValue);
    const input = document.getElementById(inputId);
    const pill = document.getElementById(pillId);
    if (input && Number.isFinite(value)) input.value = String(value);
    if (pill && Number.isFinite(value)) pill.textContent = value.toFixed(2);
  });
}

async function resetLlmGenerationSettings() {
  const button = document.getElementById('llm-generation-reset-btn');
  const previousValues = {
    temperature: Number(document.getElementById('llm-temperature')?.value),
    repeatPenalty: Number(document.getElementById('llm-repeat-penalty')?.value),
    topP: Number(document.getElementById('llm-top-p')?.value),
  };
  _setLlmGenerationValues({
    ...LLM_GENERATION_RECOMMENDED_DEFAULTS,
    repeatPenalty: isOllamaMode(activeProviderMode()) ? LLM_GENERATION_RECOMMENDED_DEFAULTS.repeatPenalty : previousValues.repeatPenalty,
  });
  if (button) button.disabled = true;
  try {
    const saved = await syncAppSettings({
      silent: false,
      successMessage: _ti('settings.recommendedValuesRestored'),
    });
    if (!saved) _setLlmGenerationValues(previousValues);
  } finally {
    if (button) button.disabled = false;
  }
}

function _setResearchSettingsValues(values) {
  [
    ['threshold', 'threshold-val', values.threshold, 2],
    ['max-chunks', 'max-chunks-val', values.maxChunks, 0],
    ['chunk-size', 'chunk-size-val', values.chunkSize, 0],
  ].forEach(([inputId, pillId, rawValue, digits]) => {
    const value = Number(rawValue);
    const input = document.getElementById(inputId);
    const pill = document.getElementById(pillId);
    if (input && Number.isFinite(value)) input.value = String(value);
    if (pill && Number.isFinite(value)) pill.textContent = value.toFixed(digits);
  });
  const answerLength = document.getElementById('chat-answer-length');
  if (answerLength && values.chatAnswerLength) answerLength.value = String(values.chatAnswerLength);
  const reranker = document.getElementById('reranker-enabled');
  if (reranker && typeof values.rerankerEnabled === 'boolean') {
    reranker.checked = values.rerankerEnabled;
  }
  const profile = document.getElementById('reranker-profile');
  if (profile && values.rerankerProfile) profile.value = String(values.rerankerProfile);
  const translateFrench = document.getElementById('reranker-translate-french');
  if (translateFrench && typeof values.rerankerTranslateFrench === 'boolean') {
    translateFrench.checked = values.rerankerTranslateFrench;
  }
  updateRerankerSettingsUi();
}

async function resetResearchSettings() {
  const button = document.getElementById('research-settings-reset-btn');
  const previousValues = {
    threshold: Number(document.getElementById('threshold')?.value),
    maxChunks: Number(document.getElementById('max-chunks')?.value),
    chatAnswerLength: String(document.getElementById('chat-answer-length')?.value || RESEARCH_RECOMMENDED_DEFAULTS.chatAnswerLength),
    rerankerEnabled: Boolean(document.getElementById('reranker-enabled')?.checked),
    rerankerProfile: String(document.getElementById('reranker-profile')?.value || 'auto'),
    rerankerTranslateFrench: Boolean(document.getElementById('reranker-translate-french')?.checked),
    chunkSize: Number(document.getElementById('chunk-size')?.value),
  };
  _setResearchSettingsValues(RESEARCH_RECOMMENDED_DEFAULTS);
  if (button) button.disabled = true;
  try {
    const appSaved = await syncAppSettings({ silent: false });
    if (!appSaved) {
      _setResearchSettingsValues(previousValues);
      await Promise.all([loadAppSettings(), loadRagSettings()]);
      return;
    }
    refreshRerankerStatusPill();

    const ragSaved = await syncRagSettings({ silent: false });
    if (!ragSaved) {
      _setResearchSettingsValues(previousValues);
      const appRolledBack = await syncAppSettings({ silent: true });
      if (!appRolledBack) await loadAppSettings();
      else refreshRerankerStatusPill();
      await loadRagSettings();
      return;
    }

    _showSettingSaved(_ti('settings.researchValuesRestored'));
  } finally {
    if (button) button.disabled = false;
  }
}

// ---------------------------------------------
//  THÈME (sombre / clair / auto)
// ---------------------------------------------
function applyThemePreference() {
  const pref = localStorage.getItem(LS_PREFIX + 'theme') || 'dark';
  const light = pref === 'light'
    || (pref === 'auto' && window.matchMedia('(prefers-color-scheme: light)').matches);
  if (light) document.documentElement.setAttribute('data-theme', 'light');
  else document.documentElement.removeAttribute('data-theme');
  document.querySelectorAll('[data-theme-choice]').forEach((btn) => {
    const active = btn.getAttribute('data-theme-choice') === pref;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}

function setTheme(pref) {
  const normalized = ['dark', 'light', 'auto'].includes(pref) ? pref : 'dark';
  localStorage.setItem(LS_PREFIX + 'theme', normalized);
  applyThemePreference();
}
window.setTheme = setTheme;
window.matchMedia('(prefers-color-scheme: light)')
  .addEventListener('change', () => applyThemePreference());
document.addEventListener('DOMContentLoaded', applyThemePreference);

// ---------------------------------------------
//  PERFORMANCE (auto / normal / léger)
// ---------------------------------------------
const PERFORMANCE_MODE_STORAGE_KEY = LS_PREFIX + 'performance-mode';

function getPerformanceModePreference() {
  const value = String(localStorage.getItem(PERFORMANCE_MODE_STORAGE_KEY) || 'auto').trim();
  return ['auto', 'normal', 'light'].includes(value) ? value : 'auto';
}

function getAutomaticLightModeReasons() {
  const reasons = [];
  const cpuCount = Number(navigator.hardwareConcurrency || 0);
  const memoryGb = Number(navigator.deviceMemory || 0);
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) reasons.push('reduced-motion');
  if (navigator.connection?.saveData === true) reasons.push('save-data');
  if (cpuCount > 0 && cpuCount <= 4) reasons.push('cpu');
  if (memoryGb > 0 && memoryGb <= 4) reasons.push('memory');
  return reasons;
}

function applyPerformanceModePreference() {
  const preference = getPerformanceModePreference();
  const autoReasons = getAutomaticLightModeReasons();
  const light = preference === 'light' || (preference === 'auto' && autoReasons.length > 0);
  const root = document.documentElement;
  root.setAttribute('data-performance-preference', preference);
  root.setAttribute('data-performance-mode', light ? 'light' : 'normal');

  const select = document.getElementById('performance-mode-select');
  if (select) select.value = preference;
  const status = document.getElementById('performance-mode-status');
  if (status) {
    const key = light ? 'settings.performanceLightActive' : 'settings.performanceNormalActive';
    const translate = window.BibliobrainI18n?.t;
    const effectiveLabel = translate ? translate(key) : (light ? 'Mode effectif : léger' : 'Mode effectif : normal');
    const automaticLabel = translate ? translate('settings.performanceAutoDetection') : 'détection automatique';
    const automatic = preference === 'auto' ? ` · ${automaticLabel}` : '';
    status.textContent = `${effectiveLabel}${automatic}`;
  }
  window.dispatchEvent(new CustomEvent('bibliobrain:performancechange', {
    detail: { preference, mode: light ? 'light' : 'normal', autoReasons },
  }));
  return light ? 'light' : 'normal';
}

function setPerformanceMode(value) {
  const preference = ['auto', 'normal', 'light'].includes(String(value)) ? String(value) : 'auto';
  localStorage.setItem(PERFORMANCE_MODE_STORAGE_KEY, preference);
  applyPerformanceModePreference();
}

window.setPerformanceMode = setPerformanceMode;
const reducedMotionPerformanceQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
const refreshAutomaticPerformanceMode = () => {
  if (getPerformanceModePreference() === 'auto') applyPerformanceModePreference();
};
if (typeof reducedMotionPerformanceQuery.addEventListener === 'function') {
  reducedMotionPerformanceQuery.addEventListener('change', refreshAutomaticPerformanceMode);
} else if (typeof reducedMotionPerformanceQuery.addListener === 'function') {
  reducedMotionPerformanceQuery.addListener(refreshAutomaticPerformanceMode);
}
document.addEventListener('DOMContentLoaded', applyPerformanceModePreference);
window.addEventListener('bibliobrain:languagechange', applyPerformanceModePreference);

const OLLAMA_DEFAULT_URL = 'http://localhost:11434';
const GPU_STATUS_REFRESH_INTERVAL_MS = 60000;
let _gpuStatusRefreshPromise = null;
let _gpuStatusRefreshTimer = null;
let _gpuStatusLastRefreshAt = 0;

function normalizeProviderMode(value) {
  const mode = String(value || '').trim().toLowerCase();
  if (mode === 'local') return 'ollama';
  if (mode === 'lmstudio') return 'lmstudio';
  if (mode === 'openai' || mode === 'openai_compatible') return 'remote';
  if (mode === 'remote') return 'remote';
  return 'ollama';
}

function isRemoteMode(mode) {
  return normalizeProviderMode(mode) === 'remote';
}

function isOllamaMode(mode) {
  return normalizeProviderMode(mode) === 'ollama';
}

function isLmStudioMode(mode) {
  return normalizeProviderMode(mode) === 'lmstudio';
}

function providerDisplayName(mode) {
  const normalized = normalizeProviderMode(mode);
  if (normalized === 'remote') return _ti('settings.providerRemoteName');
  if (normalized === 'lmstudio') return 'LM Studio';
  return 'Ollama';
}

function activeProviderMode() {
  return normalizeProviderMode(state.providerInfo?.effective_mode || state.providerInfo?.mode || 'ollama');
}

function embeddingProviderMode() {
  return normalizeProviderMode(state.providerInfo?.embedding_provider || 'ollama');
}

function activeLocalProviderMode() {
  const mode = activeProviderMode();
  return isLmStudioMode(mode) ? 'lmstudio' : 'ollama';
}

function providerUrlForUi(info = state.providerInfo) {
  const mode = normalizeProviderMode(info?.effective_mode || info?.mode || 'ollama');
  const embeddingMode = normalizeProviderMode(info?.embedding_provider || 'ollama');
  if (isRemoteMode(mode)) {
    if (embeddingMode === 'ollama') return String(info?.ollama_base_url || OLLAMA_DEFAULT_URL).trim();
    if (embeddingMode === 'lmstudio') return String(info?.lmstudio_base_url || 'http://127.0.0.1:1234').trim();
    return String(info?.base_url || '').trim();
  }
  if (isLmStudioMode(mode)) {
    return String(info?.lmstudio_base_url || 'http://127.0.0.1:1234').trim();
  }
  return String(info?.ollama_base_url || OLLAMA_DEFAULT_URL).trim();
}

let _providerUiRevision = 0;
let _providerInfoRequest = 0;
let _chatModelsRequest = 0;
let _embeddingModelsRequest = 0;
let _computeVerificationRequest = 0;
let _providerChangePending = false;

function settingsProviderChoice() {
  return state.settingsProviderChoice || activeProviderMode();
}

// Where the AI runs (this computer, another computer, an online API) is a
// separate question from the software serving the models (Ollama, LM Studio).
// The location of a local service is read from its address, never stored.
const LMSTUDIO_DEFAULT_URL = 'http://127.0.0.1:1234';

function defaultLocalProviderUrl(mode) {
  return isLmStudioMode(mode) ? LMSTUDIO_DEFAULT_URL : OLLAMA_DEFAULT_URL;
}

function isThisComputerUrl(url) {
  try {
    const host = new URL(String(url || '').trim()).hostname.toLowerCase().replace(/^\[|\]$/g, '');
    return host === 'localhost' || host === '::1' || /^127\./.test(host) || host.endsWith('.docker.internal');
  } catch {
    return false;
  }
}

function savedLocalProviderUrl(mode, info = state.providerInfo) {
  const saved = isLmStudioMode(mode) ? info?.lmstudio_base_url : info?.ollama_base_url;
  return String(saved || defaultLocalProviderUrl(mode)).trim();
}

function providerLocationFor(mode, info = state.providerInfo) {
  if (isRemoteMode(mode)) return 'remote';
  return isThisComputerUrl(savedLocalProviderUrl(mode, info)) ? 'local' : 'network';
}

function activeProviderLocation() {
  return providerLocationFor(activeProviderMode());
}

function settingsLocationChoice() {
  const mode = settingsProviderChoice();
  if (isRemoteMode(mode)) return 'remote';
  return state.settingsLocationChoice || providerLocationFor(mode);
}

// "Another computer" is chosen but its address is not known yet: nothing is applied.
function networkAddressPending() {
  const mode = settingsProviderChoice();
  return !isRemoteMode(mode)
    && settingsLocationChoice() === 'network'
    && isThisComputerUrl(savedLocalProviderUrl(mode));
}

function settingsConnectionPending() {
  return settingsProviderChoice() !== activeProviderMode()
    || settingsLocationChoice() !== activeProviderLocation();
}

// Scan reading and figure analysis: on this computer's Ollama unless Expert moves them.
function refreshDocumentToolsSourceUi(info = state.providerInfo) {
  const select = document.getElementById('document-tools-source-select');
  if (!select) return;
  const source = info?.document_tools_source === 'chat' ? 'chat' : 'local';
  const ollamaUrl = String(info?.ollama_base_url || OLLAMA_DEFAULT_URL).trim();
  const otherComputer = !isThisComputerUrl(ollamaUrl);
  const remoteOption = select.querySelector('option[value="chat"]');
  if (remoteOption) {
    remoteOption.textContent = otherComputer
      ? _ti('settings.tools.sourceChat', { url: ollamaUrl })
      : _ti('settings.tools.sourceChatUnset');
    // Without the address of another Ollama there is nowhere else to send the pages.
    remoteOption.disabled = !otherComputer && source !== 'chat';
  }
  if (document.activeElement !== select) select.value = source;
}

async function setDocumentToolsSource(source) {
  const select = document.getElementById('document-tools-source-select');
  const statusEl = document.getElementById('document-tools-source-status');
  const current = state.providerInfo?.document_tools_source === 'chat' ? 'chat' : 'local';
  const next = source === 'chat' ? 'chat' : 'local';
  if (next === current) return state.providerInfo;
  if (select) select.disabled = true;
  try {
    const r = await fetch('/api/set-document-tools-source', {
      method: 'POST', signal: AbortSignal.timeout(15000),
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: next }),
    });
    const d = await r.json();
    if (r.status === 409) {
      if (select) select.value = current;
      setMiniStatus(statusEl, _ti('settings.tools.sourceBusy'), 'red');
      return null;
    }
    if (!r.ok || !d?.ok || !d.info) throw new Error('document_tools_source_not_applied');
    state.providerInfo = d.info;
    refreshDocumentToolsSourceUi(d.info);
    setMiniStatus(
      statusEl,
      _ti(next === 'chat' ? 'settings.tools.sourceSavedChat' : 'settings.tools.sourceSavedLocal', {
        url: String(d.info.document_tools_base_url || ''),
      }),
      'cyan',
    );
    // Both tools list the models of the Ollama they now use.
    window.refreshOcrReadiness?.();
    void window.loadVisionModels?.({ silent: true });
    return d.info;
  } catch (error) {
    if (select) select.value = current;
    setMiniStatus(statusEl, _ti('settings.tools.sourceFailed'), 'red');
    return null;
  } finally {
    if (select) select.disabled = false;
  }
}
window.setDocumentToolsSource = setDocumentToolsSource;

function refreshProviderUrlUi(info = state.providerInfo) {
  const mode = settingsProviderChoice();
  const remoteMode = isRemoteMode(mode);
  const location = settingsLocationChoice();
  const active = activeProviderMode();
  const localRow = document.getElementById('settings-local-connection');
  if (localRow) localRow.hidden = remoteMode;
  const remoteFields = document.getElementById('settings-remote-connection');
  if (remoteFields) remoteFields.hidden = !remoteMode;
  if (remoteMode && (!info?.can_use_remote || settingsDraftIsDirty('remote'))) {
    const details = document.getElementById('settings-remote-options');
    if (details) details.open = true;
  }
  const title = document.getElementById('provider-url-title');
  const desc = document.getElementById('provider-url-desc');
  if (title) title.textContent = _ti('settings.serviceAddress', { provider: providerDisplayName(mode) });
  if (desc) {
    const software = isLmStudioMode(mode) ? 'Lmstudio' : 'Ollama';
    desc.textContent = _ti(`settings.ai.address${location === 'network' ? 'Network' : 'Local'}${software}`);
  }
  const urlInput = document.getElementById('ollama-url');
  if (urlInput) {
    urlInput.disabled = remoteMode || _providerChangePending;
    urlInput.placeholder = location === 'network'
      ? `http://192.168.1.20:${isLmStudioMode(mode) ? 1234 : 11434}`
      : defaultLocalProviderUrl(mode);
    urlInput.title = '';
  }
  const defaultButton = document.getElementById('ollama-default-url-btn');
  if (defaultButton) {
    // The default address only makes sense for a service on this computer.
    defaultButton.hidden = location !== 'local';
    defaultButton.disabled = remoteMode || _providerChangePending;
  }
  const providerSelect = document.getElementById('provider-mode-select');
  if (providerSelect) { providerSelect.value = mode; providerSelect.disabled = _providerChangePending; }
  const activeLabel = document.getElementById('settings-active-provider');
  if (activeLabel) {
    const activeLocation = activeProviderLocation();
    activeLabel.textContent = activeLocation === 'remote'
      ? _ti('settings.activeService', { provider: providerDisplayName(active) })
      : _ti(`settings.ai.active${activeLocation === 'network' ? 'Network' : 'Local'}`, { provider: providerDisplayName(active) });
  }
  const chatRow = document.getElementById('compute-mode-verification-row');
  if (chatRow) chatRow.hidden = settingsConnectionPending() || _providerChangePending;
  const testButton = document.getElementById('verify-compute-btn');
  if (testButton) testButton.disabled = _providerChangePending;
  // Indexing is a separate service. Never present its local URL as the chat connection.
  const embeddingMode = normalizeProviderMode(info?.embedding_provider || 'ollama');
  const embeddingProvider = providerDisplayName(embeddingMode);
  const embeddingTitle = document.getElementById('embedding-model-title');
  if (embeddingTitle) embeddingTitle.textContent = _ti('settings.indexingModelFor', { provider: embeddingProvider });
  const embeddingDesc = document.getElementById('embedding-model-desc');
  if (embeddingDesc) embeddingDesc.textContent = _ti('settings.indexingIndependent');
  const indexing = document.getElementById('settings-indexing-provider');
  if (indexing) {
    const embeddingUrl = String(info?.embedding_base_url || '').trim();
    const where = embeddingMode === 'remote'
      ? 'Remote'
      : (isThisComputerUrl(embeddingUrl || OLLAMA_DEFAULT_URL) ? 'Local' : 'Network');
    indexing.textContent = _ti(`settings.indexing.service${where}`, { provider: embeddingProvider, url: embeddingUrl });
  }
  refreshDocumentToolsSourceUi(info);
  const sourceSelect = document.getElementById('embedding-source-select');
  if (sourceSelect && document.activeElement !== sourceSelect) {
    sourceSelect.value = info?.embedding_source === 'chat' ? 'chat' : 'local';
  }
  document.getElementById('settings-indexing')?.classList.toggle('settings-remote-indexing', embeddingMode === 'remote');
  document.getElementById('ollama-context-length-row')?.toggleAttribute('hidden', !isOllamaMode(active));
  document.querySelectorAll('[data-settings-chat-provider]').forEach((node) => {
    node.hidden = mode !== active || node.dataset.settingsChatProvider !== active;
  });
  refreshSettingsDefaults();
  refreshSettingsOverview();
}

// ---------------------------------------------
//  API CREDENTIALS (server-side encrypted store)
// ---------------------------------------------
const API_CREDENTIAL_FIELDS = Object.freeze({
  remote_llm_api_key: 'remote-llm-api-key',
  openalex_api_key: 'openalex-api-key',
  semantic_scholar_api_key: 'semantic-scholar-api-key',
  ollama_api_key: 'ollama-api-key',
  ollama_auth_header: 'ollama-auth-header',
  huggingface_token: 'huggingface-token',
  bugsink_dsn: 'bugsink-dsn',
});

function _apiCredentialState(payload, key) {
  if (key === 'remote_llm_base_url') return payload?.remote_llm_base_url || {};
  return payload?.secrets?.[key] || {};
}

function _renderApiCredentialStatus(payload) {
  const available = Boolean(payload?.available);
  const storageStatus = document.getElementById('api-secret-storage-status');
  if (storageStatus) {
    storageStatus.textContent = available
      ? _ti('settings.apiSecureStorageReady')
      : (String(payload?.error || '').trim() || _ti('settings.apiSecureStorageUnavailable'));
    storageStatus.className = `settings-status ${available ? 'status-green' : 'status-red'}`;
  }

  const remoteUrlInput = document.getElementById('remote-llm-base-url');
  if (remoteUrlInput && !settingsDraftIsDirty('remote') && document.activeElement !== remoteUrlInput) {
    remoteUrlInput.value = String(payload?.remote_llm_base_url?.value || '');
  }

  const fields = { remote_llm_base_url: 'remote-llm-base-url', ...API_CREDENTIAL_FIELDS };
  Object.entries(fields).forEach(([key, inputId]) => {
    const input = document.getElementById(inputId);
    const meta = _apiCredentialState(payload, key);
    const configured = Boolean(meta?.configured);
    const source = String(meta?.source || '');
    const status = document.querySelector(`[data-api-secret-status="${key}"]`);
    const clearButton = document.querySelector(`[data-api-secret-clear="${key}"]`);

    if (input) {
      input.disabled = !available;
      if (key !== 'remote_llm_base_url') {
        if (!input.closest('[data-settings-draft]')?.matches('[data-dirty="true"]')) input.value = '';
        input.placeholder = configured
          ? _ti('settings.apiCredentialConfiguredPlaceholder')
          : _ti('settings.apiCredentialNewPlaceholder');
      }
    }
    if (status) {
      status.textContent = configured
        ? (source === 'secure_store'
          ? _ti('settings.apiCredentialStoredSecurely')
          : _ti('settings.apiCredentialFromEnvironment'))
        : _ti('settings.apiCredentialNotConfigured');
      status.className = `api-secret-field-status ${configured ? (source === 'secure_store' ? 'configured' : 'environment') : ''}`;
    }
    if (clearButton) {
      clearButton.disabled = !available || source !== 'secure_store';
      clearButton.title = source === 'environment'
        ? _ti('settings.apiCredentialEnvironmentClearHint')
        : '';
    }
  });

  for (const id of ['api-secrets-save-btn', 'remote-connection-save']) {
    const saveButton = document.getElementById(id);
    if (saveButton) saveButton.disabled = !available;
  }
  const remoteStorageStatus = document.getElementById('remote-secret-storage-status');
  if (remoteStorageStatus) {
    remoteStorageStatus.hidden = available;
    remoteStorageStatus.textContent = available ? '' : _ti('settings.apiSecureStorageUnavailable');
  }
  if (typeof refreshSettingsOverview === 'function') refreshSettingsOverview();
}

async function loadApiSecrets() {
  try {
    const response = await fetch('/api/api-secrets', { cache: 'no-store' });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload?.detail || 'api_credentials_unavailable');
    _renderApiCredentialStatus(payload);
    return payload;
  } catch (error) {
    _renderApiCredentialStatus({ available: false, error: _ti('settings.apiSecureStorageUnavailable') });
    return null;
  }
}

async function saveApiSecrets(scope = 'credentials') {
  const saveButton = document.getElementById(scope === 'remote' ? 'remote-connection-save' : 'api-secrets-save-btn');
  const payload = {};
  const remoteUrl = String(document.getElementById('remote-llm-base-url')?.value || '').trim();
  if (scope === 'remote' && remoteUrl) payload.remote_llm_base_url = remoteUrl;
  Object.entries(API_CREDENTIAL_FIELDS).forEach(([key, inputId]) => {
    const value = String(document.getElementById(inputId)?.value || '').trim();
    if (value && (scope === 'remote') === (key === 'remote_llm_api_key')) payload[key] = value;
  });
  if (!Object.keys(payload).length) {
    showToast(_ti('settings.apiCredentialNothingToSave'), 3200);
    return null;
  }

  const group = document.querySelector(`[data-settings-draft="${scope}"]`);
  const inputs = Array.from(group?.querySelectorAll('input') || []);
  const submittedValues = inputs.map(input => input.value);

  if (saveButton) saveButton.disabled = true;
  try {
    const response = await fetch('/api/api-secrets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok || !result?.ok) {
      throw new Error(String(result?.detail || _ti('settings.apiCredentialSaveFailed')));
    }
    const changedDuringSave = inputs.some((input, index) => input.value !== submittedValues[index]);
    setSettingsDraft(scope, changedDuringSave);
    _renderApiCredentialStatus(result);
    if (scope === 'remote' && !changedDuringSave) {
      const details = document.getElementById('settings-remote-options');
      if (details) details.open = false;
    }
    await loadProviderInfo();
    showToast(_ti('settings.apiCredentialSaved'), 3600, 'success');
    return result;
  } catch (error) {
    showToast(String(error?.message || _ti('settings.apiCredentialSaveFailed')), 5200, 'error');
    return null;
  } finally {
    if (saveButton) saveButton.disabled = false;
  }
}

async function clearApiSecret(key) {
  if (!Object.prototype.hasOwnProperty.call(
    { remote_llm_base_url: true, ...API_CREDENTIAL_FIELDS },
    key,
  )) return;
  if (!window.confirm(_ti('settings.apiCredentialClearConfirm'))) return;
  try {
    const response = await fetch('/api/api-secrets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clear: [key] }),
    });
    const result = await response.json();
    if (!response.ok || !result?.ok) {
      throw new Error(String(result?.detail || _ti('settings.apiCredentialClearFailed')));
    }
    _renderApiCredentialStatus(result);
    await loadProviderInfo();
    showToast(_ti('settings.apiCredentialCleared'), 3200, 'success');
  } catch (error) {
    showToast(String(error?.message || _ti('settings.apiCredentialClearFailed')), 5200, 'error');
  }
}

function saveSettings() {
  PERSIST_FIELDS.forEach(({ id, type }) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (type === 'checkbox') {
      localStorage.setItem(LS_PREFIX + id, el.checked ? '1' : '0');
      return;
    }
    localStorage.setItem(LS_PREFIX + id, el.value);
  });
  const model = document.getElementById('model-select').value;
  if (model && model !== 'Chargement…') localStorage.setItem(LS_PREFIX + 'model', model);
  localStorage.setItem(LS_PREFIX + 'sidebar-collapsed',
    document.getElementById('sidebar').classList.contains('collapsed') ? '1' : '0');
}

function loadSettings() {
  PERSIST_FIELDS.forEach(({ id, type }) => {
    const saved = localStorage.getItem(LS_PREFIX + id);
    if (saved === null) return;
    const el = document.getElementById(id);
    if (!el) return;
    if (type === 'checkbox') {
      el.checked = (saved === '1' || saved === 'true');
      return;
    }
    el.value = saved;
    // Update pill display for range inputs
    if (type === 'range') {
      const pill = document.getElementById(id + '-val');
      if (pill) {
        if (id === 'threshold') pill.textContent = parseFloat(saved).toFixed(2);
        else pill.textContent = saved;
      }
    }
  });
  // Sidebar collapsed state. Mobile uses a temporary drawer state instead.
  if (!isMobileNavViewport() && localStorage.getItem(LS_PREFIX + 'sidebar-collapsed') === '1') {
    document.getElementById('sidebar').classList.add('collapsed');
    document.getElementById('app').classList.add('sidebar-collapsed');
    document.getElementById('sidebar-toggle').title = _tr('Agrandir la sidebar');
  }
}

function _hookPersistence() {
  PERSIST_FIELDS.forEach(({ id }) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', saveSettings);
  });
  document.getElementById('embedding-model-select')?.addEventListener('change', async (e) => {
    if (state.ragInitBusy || state.startupAutoInitRunning) {
      const current = String(state.providerInfo?.required_embedding_model || '').trim();
      if (current) e.target.value = current;
      showToast("L'indexation RAG est en cours. Changer d'embedding demande d'arrêter puis de reconstruire l'index.", 5600, 'error');
      return;
    }
    const label = String(e.target.selectedOptions?.[0]?.textContent || '').trim();
    await setEmbeddingModel(e.target.value, {
      provider: embeddingProviderMode(),
      successMessage: `Modèle d'embedding sélectionné : ${label || '-'}`,
    });
  });
  document.getElementById('document-tools-source-select')?.addEventListener('change', (e) => {
    void setDocumentToolsSource(e.target.value);
  });
  document.getElementById('embedding-source-select')?.addEventListener('change', (e) => {
    void setEmbeddingSource(e.target.value);
  });
  document.getElementById('remote-embedding-enabled')?.addEventListener('change', async (e) => {
    const enabled = Boolean(e.target.checked);
    const modelRow = document.getElementById('remote-embedding-model-row');
    if (state.ragInitBusy || state.startupAutoInitRunning) {
      e.target.checked = embeddingProviderMode() === 'remote';
      if (modelRow) modelRow.hidden = !e.target.checked;
      showToast("L'indexation RAG est en cours. Attendez sa fin avant de changer d'espace vectoriel.", 5600, 'error');
      return;
    }
    if (modelRow) modelRow.hidden = !enabled;
    if (enabled) {
      await loadRemoteEmbeddingModels({ silent: false });
      return;
    }
    if (embeddingProviderMode() === 'remote') {
      await setEmbeddingModel('', {
        provider: 'remote',
        useLocal: true,
        statusElementId: 'remote-embedding-model-status',
        successMessage: 'Embeddings locaux réactivés.',
      });
    } else {
      await loadProviderInfo();
    }
  });
  document.getElementById('remote-embedding-model-select')?.addEventListener('change', async (e) => {
    const value = String(e.target.value || '').trim();
    if (!value) return;
    const useLocal = value === '__local__';
    if (state.ragInitBusy || state.startupAutoInitRunning) {
      showToast("L'indexation RAG est en cours. Changer d'embedding demande d'arrêter puis de reconstruire l'index.", 5600, 'error');
      await loadRemoteEmbeddingModels({ silent: true });
      return;
    }
    await setEmbeddingModel(useLocal ? '' : value, {
      provider: 'remote',
      useLocal,
      statusElementId: 'remote-embedding-model-status',
      successMessage: useLocal
        ? 'Embeddings locaux réactivés.'
        : `Modèle d'embedding distant sélectionné : ${value}`,
    });
    await loadRemoteEmbeddingModels({ silent: true });
  });
  document.getElementById('model-select').addEventListener('change', async () => {
    saveSettings();
    const selectedModel = optionalSelectedModel();
    if (selectedModel) {
      rememberSelectedChatModel(selectedModel);
      warmChatModel(selectedModel);
      await syncAppSettings({ silent: false });
      await refreshOllamaContextStatus();
    }
    _showSettingSaved(`Modèle principal sélectionné : ${document.getElementById('model-select')?.value || '-'}`);
    syncSettingsChatModelUi();
    _updateNbContext();
    updateModelWarning();
    if (!selectedModel) return;
    const modelWarning = getUnreliableChatModelWarning(selectedModel);
    if (modelWarning) {
      showToast(modelWarning, 6200, 'error');
    }
    // The chat model is selected per request. Changing it must not restart RAG:
    // the vector index depends on the embedding model, and a needless restart
    // can race with a chat request and turn a healthy "ready" state into an error.
  });
}

// ---------------------------------------------
//  INIT
// ---------------------------------------------
async function loadCapabilities() {
  try {
    const response = await fetch('/api/capabilities', { cache: 'no-store' });
    const payload = await response.json();
    state.capabilities = {
      rag_connections: payload?.rag_connections !== false,
      rag_retrieval_graph: payload?.rag_retrieval_graph !== false,
    };
    const versionBadge = document.getElementById('app-version-badge');
    if (versionBadge) {
      const version = String(payload?.version || '').trim();
      versionBadge.textContent = version ? `v${version}` : '';
      versionBadge.hidden = !version;
    }
  } catch (_) {
    state.capabilities = { rag_connections: true, rag_retrieval_graph: true };
  }
  return state.capabilities;
}

window.addEventListener('DOMContentLoaded', async () => {
  loadSettings();
  // The shell is usable while backend/model/library probes continue. Keeping
  // the full-screen splash until those optional calls finish makes a healthy
  // first launch look frozen on large libraries or a cold Ollama process.
  hideStartupBootSplash();
  syncMobileNavigationState();
  _hookPersistence();
  document.getElementById('api-secrets-save-btn')?.addEventListener('click', () => saveApiSecrets());
  document.querySelectorAll('[data-api-secret-clear]').forEach((button) => {
    button.addEventListener('click', () => clearApiSecret(String(button.dataset.apiSecretClear || '')));
  });
  initTextFieldPasteGuards();
  _initKnowledgeGraphColumnLinks();
  loadDiscoveryHistory();
  document.getElementById('allow-external-discovery')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: e.target.checked
        ? 'Confidentialité API externe activée.'
        : 'Confidentialité API externe désactivée.',
    });
    updateDiscoveryPrivacyUi();
  });
  document.getElementById('research-scope-enabled')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: e.target.checked
        ? 'Cartographie auteur / laboratoire activée.'
        : 'Cartographie auteur / laboratoire désactivée et cache effacé.',
    });
  });
  document.getElementById('auto-enrich-pdf-metadata')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: e.target.checked
        ? 'Enrichissement automatique des métadonnées activé.'
        : 'Enrichissement automatique des métadonnées désactivé.',
    });
  });
  document.getElementById('index-standalone-pdfs')?.addEventListener('change', async () => {
    await syncAppSettings({ silent: true });
    // The library scan picks the change up: offer the PDFs to add or remove.
    if (typeof checkRagRefreshNeeded === 'function') void checkRagRefreshNeeded(true);
  });
  document.getElementById('discovery-query-planner')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: e.target.checked
        ? 'Mode query planner experimental active.'
        : 'Mode query planner expérimental désactivé.',
    });
  });
  document.getElementById('discovery-candidate-multiplier')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: `Pool candidat Discovery enregistre : x${parseInt(e.target.value || '0', 10)}`,
    });
  });
  document.getElementById('discovery-validation-multiplier')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: `Fenetre de reranking LLM enregistree : x${parseInt(e.target.value || '0', 10)}`,
    });
  });
  document.getElementById('ollama-url')?.addEventListener('change', (e) => {
    const providerName = providerDisplayName(isRemoteMode(settingsProviderChoice()) ? activeLocalProviderMode() : settingsProviderChoice());
    syncOllamaBaseUrl({
      silent: true,
      successMessage: `URL ${providerName} enregistrée : ${String(e.target.value || '').trim()}`,
    });
  });
  document.getElementById('llm-thinking')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: e.target.checked ? 'Mode thinking activé.' : 'Mode thinking désactivé.',
    });
  });
  document.getElementById('force-thinking-small-models')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: e.target.checked
        ? 'Thinking forcé sur les petits modèles (debug).'
        : 'Auto-désactivation du thinking restaurée pour les petits modèles.',
    });
  });
  document.getElementById('debug-metrics')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: e.target.checked ? 'Mode debug activé.' : 'Mode debug désactivé.',
    });
    updateDebugSettingsVisibility();
  });
  document.getElementById('reranker-enabled')?.addEventListener('change', async (e) => {
    await syncAppSettings({
      silent: true,
      successMessage: e.target.checked ? 'Reranker activé.' : 'Reranker désactivé.',
    });
    updateRerankerSettingsUi();
  });
  document.getElementById('reranker-profile')?.addEventListener('change', async () => {
    await syncAppSettings({ silent: false, successMessage: 'Profil du reranker enregistré.' });
    updateRerankerSettingsUi();
    refreshRerankerStatusPill();
  });
  document.getElementById('reranker-translate-french')?.addEventListener('change', async () => {
    await syncAppSettings({ silent: true, successMessage: 'Traduction du reranker enregistrée.' });
  });
  document.getElementById('visual-analysis-mode')?.addEventListener('change', async () => {
    updateVisualAnalysisUi();
    await syncAppSettings({ silent: false, successMessage: 'Mode d’analyse visuelle enregistré.' });
    if (document.getElementById('visual-analysis-mode')?.value !== 'off') {
      const detection = await loadVisionModels({ silent: true, applyDefaults: true });
      if (detection?.defaultsApplied) {
        await syncAppSettings({
          silent: true,
          successMessage: 'Modèles vision recommandés enregistrés.',
        });
      }
    }
  });
  document.getElementById('vision-model-select')?.addEventListener('change', async () => {
    await syncAppSettings({ silent: false, successMessage: 'Modèle OCR enregistré.' });
    updateVisualAnalysisUi();
  });
  document.getElementById('visual-description-model-select')?.addEventListener('change', async () => {
    await syncAppSettings({ silent: false, successMessage: 'Modèle de compréhension visuelle enregistré.' });
    updateVisualAnalysisUi();
  });
  document.getElementById('visual-analysis-auto-chat')?.addEventListener('change', async (event) => {
    await syncAppSettings({
      silent: false,
      successMessage: event.target.checked
        ? 'Lecture automatique des figures activée.'
        : 'Lecture automatique des figures désactivée.',
    });
  });
  document.getElementById('llm-temperature')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: `Température LLM enregistrée : ${Number(e.target.value || 0).toFixed(2)}`,
    });
  });
  document.getElementById('llm-repeat-penalty')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: `Repeat penalty enregistrée : ${Number(e.target.value || 0).toFixed(2)}`,
    });
  });
  document.getElementById('llm-top-p')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: `Top-p enregistré : ${Number(e.target.value || 0).toFixed(2)}`,
    });
  });
  document.getElementById('ollama-context-length')?.addEventListener('change', async (e) => {
    const value = parseInt(e.target.value || '0', 10) || 0;
    await syncAppSettings({
      silent: false,
      successMessage: value ? `Contexte Ollama enregistré : ${value.toLocaleString()} tokens` : 'Contexte Ollama automatique enregistré.',
    });
    await refreshOllamaContextStatus();
    if (typeof _draftingUpdateCounters === 'function') _draftingUpdateCounters();
  });
  document.getElementById('llm-generation-reset-btn')?.addEventListener('click', resetLlmGenerationSettings);
  document.getElementById('chunk-size')?.addEventListener('change', (e) => {
    syncRagSettings({
      silent: true,
      successMessage: `Taille des chunks enregistrée : ${parseInt(e.target.value || '0', 10)} caractères`,
    });
  });
  document.getElementById('threshold')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: `Seuil RAG enregistré : ${Number(e.target.value || 0).toFixed(2)}`,
    });
  });
  document.getElementById('max-chunks')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: `Nombre max de chunks enregistré : ${parseInt(e.target.value || '0', 10)}`,
    });
  });
  document.getElementById('chat-answer-length')?.addEventListener('change', (e) => {
    syncAppSettings({
      silent: true,
      successMessage: `Longueur des réponses enregistrée : ${e.target.selectedOptions?.[0]?.textContent || e.target.value}`,
    });
  });
  document.getElementById('research-settings-reset-btn')?.addEventListener('click', resetResearchSettings);
  document.getElementById('zotero-path')?.addEventListener('change', () => {
    const path = String(document.getElementById('zotero-path')?.value || '').trim();
    if (typeof setMetadataSource === 'function') {
      setMetadataSource(inferMetadataSource(path), path);
    }
    syncAppSettings({ silent: true, successMessage: 'Chemin Zotero enregistré.' });
  });
  const settingsDataPathInput = document.getElementById('data-path');
  const startupDataPathInput = document.getElementById('startup-data-path');
  settingsDataPathInput?.addEventListener('input', () => {
    if (startupDataPathInput) startupDataPathInput.value = settingsDataPathInput.value;
  });
  startupDataPathInput?.addEventListener('input', () => {
    if (settingsDataPathInput) settingsDataPathInput.value = startupDataPathInput.value;
  });
  document.getElementById('unpaywall-email')?.addEventListener('change', () => {
    syncAppSettings({ silent: true, successMessage: 'Email Unpaywall enregistré.' });
  });
  window.addEventListener('bibliobrain:languagechange', (event) => {
    const language = String(event?.detail?.language || '').trim();
    if (state.appSettings && language) state.appSettings.ui_language = language;
    if (typeof updateDiscoveryPrivacyUi === 'function') updateDiscoveryPrivacyUi();
    if (typeof updateFirstInitButtonVisibility === 'function') updateFirstInitButtonVisibility();
  });
  document.getElementById('discovery-mode')?.addEventListener('change', onDiscoveryModeChange);
  onDiscoveryModeChange();
  updateDiscoveryPrivacyUi();
  await Promise.all([
    loadCapabilities(),
    loadProviderInfo(),
    loadRagSettings(),
    loadAppSettings(),
  ]);
  void Promise.all([
    loadEmbeddingModels({ silent: true }),
    loadApiSecrets(),
  ]).catch(() => null);
  _renderCitationPanel(null);
  document.getElementById('chat-tags-filter')?.addEventListener('input', _updateChatFilterHint);
  document.getElementById('chat-collections-filter')?.addEventListener('input', _updateChatFilterHint);
  document.getElementById('article-select')?.addEventListener('change', (e) => {
    const relPath = e.target.value || '';
    const idx = state.libPaths.indexOf(relPath);
    if (idx >= 0 && idx !== state.selectedRow) {
      selectRow(idx, { preserveTab: true, skipScroll: true });
      return;
    }
    state.currentSummaryPath = relPath;
    loadItemCitations(relPath);
    _updateLibraryNotebookAction();
    refreshAnalysisForSelection();
  });
  document.getElementById('analysis-type-select')?.addEventListener('change', () => {
    const relPath = _currentSelectedArticlePath();
    const analysisType = document.getElementById('analysis-type-select')?.value || '';
    if (relPath && analysisType) {
      const bucket = _ensureArticleAnalysisState(relPath);
      bucket.lastView = 'analysis';
      _rememberArticleAnalysisView(relPath, 'analysis');
      bucket.lastType = analysisType;
    }
    refreshAnalysisForSelection();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeKgNotebookModal();
    if (e.key === 'Enter' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey) {
      const isOpen = document.getElementById('kg-modal')?.classList.contains('open');
      if (isOpen) {
        const tag = (document.activeElement?.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'select' || tag === 'button' || tag === 'textarea') {
          e.preventDefault();
          submitKgNotebookFromModal();
        }
      }
    }
  });
  _updateChatFilterHint();
  const bootPromise = bootApplicationAuto();
  await loadChatHistory();
  const preferredChat = localStorage.getItem(LS_PREFIX + 'active-chat-id') || '';
  if (preferredChat && state.chatSessions.some(c => c.id === preferredChat)) {
    await openChatSession(preferredChat, true);
  } else if (state.chatSessions.length > 0) {
    await openChatSession(state.chatSessions[0].id, true);
  } else {
    startNewChat();
  }
  // Restaure le dernier onglet actif de la session précédente.
  const savedTab = localStorage.getItem(LS_PREFIX + 'active-tab') || '';
  if (savedTab && savedTab !== 'chat') switchTab(savedTab);
  _setSendButtonIdle();
  void refreshKgNotebookTargets().catch(() => null);
  _updateKnowledgeFocusActions();
  initLayoutResizers();
  window.addEventListener('resize', () => {
    window.requestAnimationFrame(refreshLayoutResizers);
    syncMobileNavigationState();
  });
  _setLibraryDetailEmptyState();
  await bootPromise;
});

// ---------------------------------------------
//  SIDEBAR COLLAPSE
// ---------------------------------------------
function isMobileNavViewport() {
  return window.matchMedia('(max-width: 760px)').matches;
}

function setMobileNavOpen(open) {
  const app = document.getElementById('app');
  const btn = document.getElementById('mobile-nav-toggle');
  const nextOpen = Boolean(open) && isMobileNavViewport();
  app?.classList.toggle('mobile-nav-open', nextOpen);
  document.body.classList.toggle('mobile-nav-open', nextOpen);
  if (btn) {
    btn.setAttribute('aria-expanded', nextOpen ? 'true' : 'false');
    btn.setAttribute('aria-label', nextOpen ? 'Fermer la navigation' : 'Ouvrir la navigation');
  }
}

function syncMobileNavigationState() {
  const sidebar = document.getElementById('sidebar');
  const app = document.getElementById('app');
  const btn = document.getElementById('sidebar-toggle');
  if (isMobileNavViewport()) {
    setMobileNavOpen(app?.classList.contains('mobile-nav-open'));
    if (btn) btn.title = _tr('Fermer la navigation');
    return;
  }
  setMobileNavOpen(false);
  const collapsed = localStorage.getItem(LS_PREFIX + 'sidebar-collapsed') === '1';
  sidebar?.classList.toggle('collapsed', collapsed);
  app?.classList.toggle('sidebar-collapsed', collapsed);
  if (btn) btn.title = collapsed ? _tr('Agrandir la sidebar') : _tr('Réduire la sidebar');
}

function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const app     = document.getElementById('app');
  const btn     = document.getElementById('sidebar-toggle');
  if (isMobileNavViewport()) {
    setMobileNavOpen(!app.classList.contains('mobile-nav-open'));
    return;
  }
  const collapsed = sidebar.classList.toggle('collapsed');
  app.classList.toggle('sidebar-collapsed', collapsed);
  btn.title = collapsed ? _tr('Agrandir la sidebar') : _tr('Réduire la sidebar');
  localStorage.setItem(LS_PREFIX + 'sidebar-collapsed', collapsed ? '1' : '0');
  window.requestAnimationFrame(refreshLayoutResizers);
}

// ---------------------------------------------
//  NAV / TABS
// ---------------------------------------------
function switchTab(name, btn) {
  const targetBtn = btn || document.querySelector(`.nav-item[data-tab="${name}"]`);
  const targetPanel = document.getElementById('tab-' + name);
  if (!targetBtn || !targetPanel) return;
  if (targetPanel.classList.contains('active')) {
    if (isMobileNavViewport()) setMobileNavOpen(false);
    return;
  }

  document.querySelectorAll('.nav-item').forEach((b) => {
    b.classList.remove('active');
    b.removeAttribute('aria-current');
  });
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  targetBtn.classList.add('active');
  targetBtn.setAttribute('aria-current', 'page');
  targetPanel.classList.add('active');
  localStorage.setItem(LS_PREFIX + 'active-tab', name);
  if (name === 'notebooks' && !_nb.id) loadNotebooks();
  if (name === 'discovery') {
    loadDiscoveryNotebookOptions();
    loadDiscoveryHistory();
  }
  if (name === 'connections' && (state.knowledgeGraphDirty || !state.knowledgeGraph)) {
    scheduleKnowledgeGraphReload(80);
  }
  if (name === 'connections') {
    if (typeof setCartographyView === 'function') {
      setCartographyView(document.getElementById('kg-view-mode')?.value || 'articles', { load: false });
    }
    refreshKgNotebookTargets().then(_updateKnowledgeFocusActions);
  }
  window.requestAnimationFrame(refreshLayoutResizers);
  if (isMobileNavViewport()) setMobileNavOpen(false);
  window.dispatchEvent(new CustomEvent('bibliobrain:tabchange', { detail: { name } }));
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && document.getElementById('app')?.classList.contains('mobile-nav-open')) {
    setMobileNavOpen(false);
  }
});

// ---------------------------------------------
//  MODELS
// ---------------------------------------------
function setMiniStatus(el, msg, tone = 'green') {
  if (!el) return;
  el.className = `status-mini status-${tone}`;
  el.textContent = msg ? _tr(msg) : '';
  el.style.display = msg ? 'block' : 'none';
}

function setComputeModeStatus(message, tone = 'cyan') {
  const statusEl = document.getElementById('compute-mode-status');
  if (!statusEl) return;
  statusEl.textContent = String(message || '').trim();
  statusEl.className = `settings-status status-${tone}`;
  statusEl.style.display = message ? '' : 'none';
}

// The chat model, or '' while the list loads or is empty (the placeholder
// options have no value attribute or an empty one).
function currentChatModelName() {
  const option = document.getElementById('model-select')?.selectedOptions[0];
  return option?.hasAttribute('value') ? String(option.value || '').trim() : '';
}

// The composer's model chip mirrors the hidden #model-select: its dot is green
// when the model is usable, amber with a reliability warning, red when the
// engine is stopped or no model is available; the tooltip says why.
function renderModelChip() {
  const chip = document.getElementById('model-chip');
  if (!chip) return;
  const select = document.getElementById('model-select');
  const name = currentChatModelName();
  const placeholder = String(select?.selectedOptions[0]?.textContent || '').trim();
  const warning = String(document.getElementById('model-warning')?.textContent || '').trim();
  const statusEl = document.getElementById('model-status');
  const statusError = statusEl?.classList.contains('status-red')
    ? String(statusEl.textContent || '').trim()
    : '';
  let tone = 'ok';
  let reason = '';
  if (typeof statusTaskTone === 'function' && statusTaskTone('ollama') === 'blocked') {
    tone = 'error';
    reason = _ti('status.engineStopped');
  } else if (!name || statusError) {
    tone = name || !select?.disabled ? 'error' : 'loading';
    reason = statusError || placeholder;
  } else if (warning) {
    tone = 'warn';
    reason = warning;
  }
  chip.dataset.tone = tone;
  const label = document.getElementById('model-chip-name');
  if (label) label.textContent = name ? name.replace(/:latest$/, '') : (placeholder || _ti('chat.modelNone'));
  const title = [_ti('chat.modelChipTitle'), name, reason].filter(Boolean).join(' — ');
  chip.title = title;
  chip.setAttribute('aria-label', title);
}

function _renderModelMenu(menu) {
  const select = document.getElementById('model-select');
  const current = currentChatModelName();
  const options = Array.from(select?.options || []).filter((option) => option.hasAttribute('value') && option.value);
  const items = options.map((option) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'composer-menu-item';
    item.setAttribute('role', 'menuitemradio');
    item.setAttribute('aria-checked', String(option.value === current));
    const label = document.createElement('span');
    label.textContent = option.textContent || option.value;
    item.appendChild(label);
    item.title = label.textContent;
    if (isUnreliableChatModel(option.value)) {
      item.classList.add('is-warning');
      item.title = `${label.textContent} — ${getUnreliableChatModelWarning(option.value)}`;
    }
    item.addEventListener('click', () => {
      closeComposerMenu({ focus: true });
      if (option.value !== currentChatModelName()) selectSettingsChatModel(option.value);
    });
    return item;
  });
  if (!items.length) {
    const empty = document.createElement('p');
    empty.className = 'composer-menu-empty';
    empty.textContent = String(select?.selectedOptions[0]?.textContent || '').trim() || _ti('chat.modelNone');
    items.push(empty);
  }
  const refresh = document.createElement('button');
  refresh.type = 'button';
  refresh.className = 'composer-menu-item is-secondary';
  refresh.setAttribute('role', 'menuitem');
  refresh.textContent = _ti('nav.refreshModels');
  refresh.addEventListener('click', () => {
    closeComposerMenu({ focus: true });
    void loadModels({ forceRefresh: true });
  });
  const separator = document.createElement('div');
  separator.className = 'composer-menu-separator';
  separator.setAttribute('role', 'separator');
  menu.replaceChildren(...items, separator, refresh);
}

function toggleModelMenu() {
  toggleComposerMenu('model-chip', 'model-menu', _renderModelMenu);
}
window.toggleModelMenu = toggleModelMenu;
window.renderModelChip = renderModelChip;

function syncSettingsChatModelUi() {
  renderModelChip();
  renderWorkspaceModelChips();
  const source = document.getElementById('model-select');
  const target = document.getElementById('settings-model-select');
  if (!source || !target) return;
  target.innerHTML = Array.from(source.options || [])
    .map((option) => optionHtml(option.value, option.textContent || option.value))
    .join('');
  target.value = source.value;
  target.disabled = source.disabled || !String(source.value || '').trim();
}

function selectSettingsChatModel(modelName = '') {
  const source = document.getElementById('model-select');
  if (!source) return;
  const nextModel = String(modelName || '').trim();
  if (!Array.from(source.options || []).some((option) => option.value === nextModel)) {
    syncSettingsChatModelUi();
    return;
  }
  source.value = nextModel;
  source.dispatchEvent(new Event('change', { bubbles: true }));
  syncSettingsChatModelUi();
}

async function verifyComputeSettings(options = {}) {
  const revision = _providerUiRevision;
  const request = ++_computeVerificationRequest;
  const mode = activeProviderMode();
  if (_providerChangePending || settingsConnectionPending()) return { ok: false };
  const button = document.getElementById('verify-compute-btn');
  if (button) button.disabled = true;
  setComputeModeStatus(_ti('settings.computeChecking'), 'cyan');
  try {
    const chat = await loadModels({ silent: true, forceRefresh: options.forceRefresh !== false });
    if (revision !== _providerUiRevision || request !== _computeVerificationRequest || chat?.stale) return { ok: false, stale: true };
    // Local embedding health is reported under Preparation, independently of remote chat.
    const ready = Boolean(chat?.models?.length) && !chat?.error;
    state.settingsChatConnection = { mode, ok: ready };
    const endpoint = isRemoteMode(mode)
      ? String(state.providerInfo?.provider_url || state.providerInfo?.base_url || '')
      : providerUrlForUi();
    setComputeModeStatus(_ti(ready ? 'settings.connectionReady' : 'settings.connectionFailed', {
      provider: providerDisplayName(mode), count: chat?.models?.length || 0,
      endpoint: endpoint ? ` · ${endpoint}` : '',
    }), ready ? 'green' : 'red');
    refreshSettingsOverview();
    return { ok: ready, chat };
  } catch (error) {
    if (revision === _providerUiRevision && request === _computeVerificationRequest) {
      setComputeModeStatus(_ti('settings.connectionFailed', { provider: providerDisplayName(mode), endpoint: '' }), 'red');
    }
    return { ok: false, error };
  } finally {
    if (button && revision === _providerUiRevision && request === _computeVerificationRequest) button.disabled = false;
  }
}

async function resetOllamaBaseUrl() {
  const urlInput = document.getElementById('ollama-url');
  const defaultUrl = defaultLocalProviderUrl(settingsProviderChoice());
  if (urlInput) urlInput.value = defaultUrl;
  const result = await syncOllamaBaseUrl({
    silent: false,
    successMessage: _ti('settings.ai.defaultAddressRestored', { url: defaultUrl }),
  });
  if (result) await verifyComputeSettings({ forceRefresh: true });
  return result;
}

window.selectSettingsChatModel = selectSettingsChatModel;
window.verifyComputeSettings = verifyComputeSettings;
window.resetOllamaBaseUrl = resetOllamaBaseUrl;

function optionHtml(value, label = value) {
  return `<option value="${escapeAttr(value)}">${escapeHtml(label)}</option>`;
}

function setInitButtonsAvailability(enabled) {
  const initBtn = document.getElementById('init-btn');
  const reloadBtn = document.getElementById('reload-btn');
  if (initBtn) initBtn.disabled = !enabled;
  if (reloadBtn) reloadBtn.disabled = !enabled;
  if (state.ragInitAvailable !== Boolean(enabled)) {
    state.ragInitAvailable = Boolean(enabled);
    if (typeof renderRagStatus === 'function') renderRagStatus();
  }
}

function hasCompletedFirstRagInit() {
  return Boolean(state.appSettings?.first_rag_init_done);
}

// While no index is loaded, the status pill offers "Préparer" (or "Réparer")
// as its action; a running load gives the pill back to its progress.
function updateFirstInitButtonVisibility() {
  let action = '';
  if (state.startupStatus) {
    const startupAction = String(state.startupStatus?.recommended_action || '').trim();
    const busy = Boolean(state.ragInitBusy || state.startupAutoInitRunning);
    if (!state.ragReady && startupAction !== 'configure' && !busy) {
      action = startupAction === 'manual_rebuild' ? 'repair' : 'prepare';
    }
  }
  state.ragFirstInitAction = action;
  if (typeof renderRagStatus === 'function') renderRagStatus();
}

async function persistFirstRagInitDone() {
  if (hasCompletedFirstRagInit()) return state.appSettings;
  try {
    const d = await postJsonOrThrow(
      '/api/app-settings', { first_rag_init_done: true, onboarding_done_v1: true },
      'Sauvegarde de la préparation impossible',
    );
    if (d?.ok) {
      state.appSettings = d;
      updateFirstInitButtonVisibility();
      return d;
    }
  } catch (e) {
  }
  return null;
}

async function runFirstRagInit() {
  // On first run the PDF folder has not been chosen yet: open the assistant
  // instead of indexing whatever folder happens to be configured.
  if (typeof isStartupGuideNeeded === 'function' && isStartupGuideNeeded()) {
    openStartupAssistant();
    return null;
  }
  if (typeof runStartupAutoInit === 'function') return runStartupAutoInit(true);
  return initRAG(false);
}

function _isInvalidModelSelection(value) {
  const normalized = String(value || '').trim().toLowerCase().replace(/…/g, '...');
  return (
    !normalized
    || normalized === '-'
    || normalized === '—'
    || normalized === 'none'
    || normalized === 'null'
    || normalized === 'undefined'
    || normalized === 'chargement...'
    || normalized === 'chargement…'
    || normalized === 'aucun modele'
    || normalized === 'aucun modèle detecte'
    || normalized === 'aucun modèle détecté'
    || normalized === 'erreur connexion'
  );
}

// Discovery, the notebook chat and Writing can each use another model than
// the Chat. The choice stays in this browser; "Default" follows the Chat
// model, and a model that disappears from the list falls back to it.
const WORKSPACE_MODEL_SCOPES = ['discovery', 'notebook', 'drafting'];

function _workspaceModelKey(scope) {
  return `${LS_PREFIX}workspace-model-${scope}`;
}

function workspaceModelOverride(scope) {
  if (!WORKSPACE_MODEL_SCOPES.includes(scope)) return '';
  let value = '';
  try { value = localStorage.getItem(_workspaceModelKey(scope)) || ''; } catch (_) {}
  const options = Array.from(document.getElementById('model-select')?.options || []);
  return options.some((option) => option.hasAttribute('value') && option.value === value) ? value : '';
}

function setWorkspaceModel(scope, model = '') {
  if (!WORKSPACE_MODEL_SCOPES.includes(scope)) return;
  try {
    if (model) localStorage.setItem(_workspaceModelKey(scope), model);
    else localStorage.removeItem(_workspaceModelKey(scope));
  } catch (_) {}
  renderWorkspaceModelChips();
}

function _modelForScope(scope = '') {
  return (scope && workspaceModelOverride(scope)) || document.getElementById('model-select')?.value || '';
}

function renderWorkspaceModelChips() {
  const chatModel = currentChatModelName();
  document.querySelectorAll('.workspace-model-chip[data-model-scope]').forEach((chip) => {
    const scope = chip.dataset.modelScope;
    const override = workspaceModelOverride(scope);
    const name = override || chatModel;
    const label = chip.querySelector('.workspace-model-name');
    if (label) label.textContent = name ? name.replace(/:latest$/, '') : _ti('chat.modelNone');
    chip.dataset.tone = !name ? 'error' : (isUnreliableChatModel(name) ? 'warn' : 'ok');
    chip.classList.toggle('is-override', Boolean(override));
    const title = override
      ? _ti('model.workspaceOverride', { model: name })
      : _ti('model.workspaceDefault', { model: name || _ti('chat.modelNone') });
    chip.title = title;
    chip.setAttribute('aria-label', title);
  });
}

function _renderWorkspaceModelMenu(menu, scope) {
  const current = workspaceModelOverride(scope);
  const chatModel = currentChatModelName();
  const select = document.getElementById('model-select');
  const item = (text, value, checked, warning = '') => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'composer-menu-item';
    button.setAttribute('role', 'menuitemradio');
    button.setAttribute('aria-checked', String(checked));
    const span = document.createElement('span');
    span.textContent = text;
    button.appendChild(span);
    button.title = warning ? `${text} — ${warning}` : text;
    if (warning) button.classList.add('is-warning');
    button.addEventListener('click', () => {
      if (typeof closeComposerMenu === 'function') closeComposerMenu({ focus: true });
      setWorkspaceModel(scope, value);
    });
    return button;
  };
  const items = [item(_ti('model.workspaceDefaultItem', { model: chatModel || _ti('chat.modelNone') }), '', !current)];
  Array.from(select?.options || [])
    .filter((option) => option.hasAttribute('value') && option.value)
    .forEach((option) => {
      items.push(item(option.textContent || option.value, option.value, option.value === current,
        isUnreliableChatModel(option.value) ? getUnreliableChatModelWarning(option.value) : ''));
    });
  menu.replaceChildren(...items);
}

document.addEventListener('click', (event) => {
  const chip = event.target.closest?.('.workspace-model-chip[data-model-scope]');
  if (!chip || typeof toggleComposerMenu !== 'function') return;
  const menuId = chip.getAttribute('aria-controls');
  toggleComposerMenu(chip.id, menuId, (menu) => _renderWorkspaceModelMenu(menu, chip.dataset.modelScope));
});
window.renderWorkspaceModelChips = renderWorkspaceModelChips;

function requireSelectedModel(scope = '') {
  const model = _modelForScope(scope);
  if (_isInvalidModelSelection(model)) {
    const installed = Array.from(document.getElementById('model-select')?.options || [])
      .some((option) => option.value);
    showToast(installed ? 'Sélectionnez un modèle valide (Actualiser les modèles).' : _ti('model.noneInstalledToast'), 6000);
    return null;
  }
  return model;
}

function optionalSelectedModel(scope = '') {
  const model = _modelForScope(scope);
  return _isInvalidModelSelection(model) ? null : model;
}

function isUnreliableChatModel(modelName = '') {
  const model = String(modelName || '').trim().toLowerCase();
  return model.startsWith('qwen3.5:0.8b') || model.startsWith('qwen3.5-0.8b');
}

// The installer's fallback when nothing better fits the computer: usable, but
// the user is told once that poor answers come from the model's size.
function isSmallChatModel(modelName = '') {
  const model = String(modelName || '').trim().toLowerCase();
  return isUnreliableChatModel(model)
    || model.startsWith('qwen3.5:2b')
    || model.startsWith('qwen3.5-2b')
    || model.startsWith('gemma4:e2b');
}

function getUnreliableChatModelWarning(modelName = '') {
  if (isUnreliableChatModel(modelName)) {
    return "Erreur : ce modele est considere comme non fiable dans Bibliobrain. Il est sujet aux hallucinations et ne doit pas etre utilise pour le chat ou le RAG sauf test volontaire.";
  }
  return isSmallChatModel(modelName) ? _ti('model.smallWarning') : '';
}

function _smallModelNoticeKey(modelName) {
  return `${LS_PREFIX}smallModelNotice:${String(modelName || '').trim().toLowerCase()}`;
}

// Shown once per model, never over the setup assistant (its closing calls
// this again).
function maybeShowSmallModelNotice(modelName = '') {
  const model = String(modelName || currentChatModelName() || '').trim();
  const dialog = document.getElementById('small-model-dialog');
  if (!model || !dialog || dialog.open || !isSmallChatModel(model)) return false;
  if (state.startupHelpShown) return false;
  try {
    if (localStorage.getItem(_smallModelNoticeKey(model))) return false;
  } catch (_) {}
  const body = document.getElementById('small-model-body');
  if (body) body.textContent = _ti('model.smallBody', { model: model.replace(/:latest$/, '') });
  dialog.dataset.model = model;
  dialog.showModal();
  document.getElementById('small-model-understood')?.focus();
  return true;
}

function _closeSmallModelNotice({ openApiSettings = false } = {}) {
  const dialog = document.getElementById('small-model-dialog');
  if (!dialog) return;
  try {
    localStorage.setItem(_smallModelNoticeKey(dialog.dataset.model), '1');
  } catch (_) {}
  if (dialog.open) dialog.close();
  if (openApiSettings) _openAiSettings();
}

document.getElementById('small-model-understood')
  ?.addEventListener('click', () => _closeSmallModelNotice());
document.getElementById('small-model-connect-api')
  ?.addEventListener('click', () => _closeSmallModelNotice({ openApiSettings: true }));
document.getElementById('small-model-dialog')
  ?.addEventListener('cancel', () => _closeSmallModelNotice());
window.maybeShowSmallModelNotice = maybeShowSmallModelNotice;

function getPersistedChatModelPreference() {
  const fromSettings = String(state.appSettings?.selected_model || '').trim();
  if (fromSettings) return fromSettings;
  return String(localStorage.getItem(LS_PREFIX + 'model') || '').trim();
}

function rememberSelectedChatModel(modelName = '') {
  const normalized = String(modelName || '').trim();
  if (!normalized) return '';
  localStorage.setItem(LS_PREFIX + 'model', normalized);
  state.appSettings = {
    ...(state.appSettings || {}),
    selected_model: normalized,
  };
  return normalized;
}

// Loads the chat model in Ollama before the first question, so it does not
// pay the load (about 3 s). The server runs one warm-up per model at a time.
function warmChatModel(modelName = '') {
  const model = String(modelName || '').trim();
  if (!model) return;
  fetch('/api/chat/warmup', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model }),
  }).catch(() => {});
}

function resolvePreferredChatModel(models = [], savedModel = '') {
  const installed = Array.isArray(models)
    ? models.map((value) => String(value || '').trim()).filter(Boolean)
    : [];
  const preferredSaved = String(savedModel || '').trim();
  if (preferredSaved && installed.includes(preferredSaved) && !isUnreliableChatModel(preferredSaved)) {
    return preferredSaved;
  }
  // The list is sorted by size: a very small model only by default when it is
  // all there is.
  return installed.find((model) => !isSmallChatModel(model))
    || installed.find((model) => !isUnreliableChatModel(model))
    || installed[0]
    || '';
}

function updateModelWarning(modelName = '') {
  const warningEl = document.getElementById('model-warning');
  if (!warningEl) return;
  const model = String(modelName || document.getElementById('model-select')?.value || '').trim();
  const warning = getUnreliableChatModelWarning(model);
  setMiniStatus(warningEl, warning, isUnreliableChatModel(model) ? 'red' : (warning ? 'amber' : 'green'));
  renderModelChip();
  maybeShowSmallModelNotice(model);
  if (typeof _syncChatEmptyState === 'function') _syncChatEmptyState();
}

function _formatContextLength(value) {
  const parsed = Number(value || 0);
  if (!Number.isFinite(parsed) || parsed <= 0) return 'inconnu';
  return parsed >= 1024 ? `${Math.round(parsed / 1024)}k` : String(Math.round(parsed));
}

async function refreshOllamaContextStatus() {
  const row = document.getElementById('ollama-context-length-row');
  const select = document.getElementById('ollama-context-length');
  const status = document.getElementById('ollama-context-status');
  if (!row || !select || !status) return null;
  const mode = String(state.providerInfo?.effective_mode || state.providerInfo?.provider || '').toLowerCase();
  const isOllama = !mode || mode === 'ollama';
  select.disabled = !isOllama;
  if (!isOllama) {
    status.textContent = 'Ce réglage s’applique uniquement au provider Ollama.';
    status.style.display = 'block';
    return null;
  }
  const model = optionalSelectedModel() || String(state.appSettings?.selected_model || '');
  if (!model) {
    status.textContent = 'Sélectionnez un modèle pour afficher son contexte maximal.';
    status.style.display = 'block';
    return null;
  }
  try {
    const response = await fetch(`/api/ollama-runtime?model=${encodeURIComponent(model)}`, { cache: 'no-store' });
    const runtime = await response.json();
    const configured = Number(state.appSettings?.ollama_context_length || select.value || 0);
    const parts = [configured ? `réglé : ${_formatContextLength(configured)}` : 'réglé : Auto'];
    if (runtime?.model_context_length) parts.push(`maximum du modèle : ${_formatContextLength(runtime.model_context_length)}`);
    if (runtime?.active_context_length) parts.push(`chargé : ${_formatContextLength(runtime.active_context_length)}`);
    else parts.push('contexte chargé visible après une génération');
    status.textContent = parts.join(' · ');
    status.style.display = 'block';
    return runtime;
  } catch (_error) {
    status.textContent = 'Impossible de lire le contexte Ollama pour le moment.';
    status.style.display = 'block';
    return null;
  }
}



async function loadRagSettings() {
  try {
    const r = await fetch('/api/rag-settings');
    const d = await r.json();
    const chunkInput = document.getElementById('chunk-size');
    const chunkPill = document.getElementById('chunk-size-val');
    if (chunkInput && Number.isFinite(Number(d?.chunk_size))) {
      const value = String(parseInt(d.chunk_size, 10));
      chunkInput.value = value;
      if (chunkPill) chunkPill.textContent = value;
      saveSettings();
    }
    return d;
  } catch (e) {
    return null;
  }
}

// Debug-only settings visibility.

function isGpuDebugPanelEnabled() {
  return Boolean(document.getElementById('debug-metrics')?.checked);
}

function clearGpuStatus() {
  const statusEl = document.getElementById('gpu-status');
  if (!statusEl) return;
  statusEl.textContent = '';
  statusEl.removeAttribute('title');
}

function clearDiscoveryDebug() {
  const debugEl = document.getElementById('discovery-debug');
  if (!debugEl) return;
  debugEl.style.display = 'none';
  debugEl.innerHTML = '';
}

function updateDebugSettingsVisibility() {
  const enabled = isGpuDebugPanelEnabled();
  document.querySelectorAll('[data-debug-only]').forEach((node) => {
    const display = String(node.getAttribute('data-debug-only') || 'block').trim() || 'block';
    node.style.display = enabled ? display : 'none';
  });
  if (!enabled) {
    clearGpuStatus();
    clearDiscoveryDebug();
  }
}


async function loadAppSettings() {
  try {
    const r = await fetch('/api/app-settings');
    const d = await r.json();
    state.appSettings = d;
    if (typeof setMetadataSource === 'function') {
      setMetadataSource(d?.metadata_source, d?.zotero_path || '');
    }
    const persistedModel = String(d?.selected_model || '').trim();
    if (persistedModel) {
      rememberSelectedChatModel(persistedModel);
      const modelSelect = document.getElementById('model-select');
      if (modelSelect && Array.from(modelSelect.options || []).some((opt) => opt.value === persistedModel)) {
        modelSelect.value = persistedModel;
      }
    }

    const applyCheckbox = (id, value) => {
      const el = document.getElementById(id);
      if (el && typeof value === 'boolean') el.checked = Boolean(value);
    };
    const applyValue = (id, value) => {
      const el = document.getElementById(id);
      if (el && value !== undefined && value !== null) el.value = String(value);
    };

    applyCheckbox('llm-thinking', d?.llm_thinking);
    applyCheckbox('reranker-enabled', d?.reranker_enabled);
    applyValue('reranker-profile', d?.reranker_profile || 'auto');
    applyCheckbox('reranker-translate-french', d?.reranker_translate_french);
    applyValue('visual-analysis-mode', d?.visual_analysis_mode || 'off');
    applyValue('ollama-context-length', d?.ollama_context_length ?? 0);
    applyCheckbox('visual-analysis-auto-chat', d?.visual_analysis_auto_chat);
    applyValue('chat-answer-length', d?.chat_answer_length || 'auto');
    applyCheckbox('debug-metrics', d?.debug_metrics);
    applyCheckbox('force-thinking-small-models', d?.debug_force_thinking_small_models);
    applyCheckbox('allow-external-discovery', d?.allow_external_api_requests ?? d?.allow_external_discovery);
    applyCheckbox('research-scope-enabled', d?.research_scope_enabled !== false);
    if (typeof setResearchScopeFeatureEnabled === 'function') {
      setResearchScopeFeatureEnabled(d?.research_scope_enabled !== false);
    }
    applyCheckbox('auto-enrich-pdf-metadata', d?.auto_enrich_pdf_metadata);
    applyCheckbox('index-standalone-pdfs', d?.index_standalone_pdfs !== false);
    applyCheckbox('discovery-query-planner', d?.discovery_query_planner);
    applyValue('discovery-candidate-multiplier', d?.discovery_candidate_multiplier ?? 4);
    applyValue('discovery-validation-multiplier', d?.discovery_llm_validation_multiplier ?? 3);
    applyValue('unpaywall-email', d?.unpaywall_email || '');
    applyValue('zotero-path', d?.zotero_path || '');
    if (typeof updatePdfMetadataEnrichmentUi === 'function') {
      updatePdfMetadataEnrichmentUi();
    }
    if (typeof syncPdfPathInputs === 'function') {
      syncPdfPathInputs(d?.data_path || '');
    } else {
      applyValue('data-path', d?.data_path || '');
      applyValue('startup-data-path', d?.data_path || '');
    }
    if (window.BibliobrainI18n?.normalizeLanguage(d?.ui_language)) {
      window.BibliobrainI18n.setLanguageFromSettings(d.ui_language);
    }

    if (Number.isFinite(Number(d?.temperature))) {
      const value = Number(d.temperature);
      const input = document.getElementById('llm-temperature');
      const pill = document.getElementById('llm-temperature-val');
      if (input) input.value = String(value);
      if (pill) pill.textContent = value.toFixed(2);
    }

    if (Number.isFinite(Number(d?.repeat_penalty))) {
      const value = Number(d.repeat_penalty);
      const input = document.getElementById('llm-repeat-penalty');
      const pill = document.getElementById('llm-repeat-penalty-val');
      if (input) input.value = String(value);
      if (pill) pill.textContent = value.toFixed(2);
    }

    if (Number.isFinite(Number(d?.top_p))) {
      const value = Number(d.top_p);
      const input = document.getElementById('llm-top-p');
      const pill = document.getElementById('llm-top-p-val');
      if (input) input.value = String(value);
      if (pill) pill.textContent = value.toFixed(2);
    }

    if (Number.isFinite(Number(d?.threshold))) {
      const value = Number(d.threshold);
      const input = document.getElementById('threshold');
      const pill = document.getElementById('threshold-val');
      if (input) input.value = String(value);
      if (pill) pill.textContent = value.toFixed(2);
    }

    if (Number.isFinite(Number(d?.max_chunks))) {
      const value = parseInt(d.max_chunks, 10);
      const input = document.getElementById('max-chunks');
      const pill = document.getElementById('max-chunks-val');
      if (input) input.value = String(value);
      if (pill) pill.textContent = String(value);
    }

    updateDebugSettingsVisibility();
    updateRerankerSettingsUi();
    updateDiscoveryPrivacyUi();
    updateFirstInitButtonVisibility();
    refreshRerankerStatusPill();
    requestRerankerStartupWarmup();
    if (String(d?.visual_analysis_mode || 'off') !== 'off') {
      await loadVisionModels({
        silent: true,
        preferredModel: d?.vision_model || '',
        preferredAnalysisModel: d?.visual_description_model || '',
      });
    } else {
      const visionSelect = document.getElementById('vision-model-select');
      const analysisSelect = document.getElementById('visual-description-model-select');
      const installButton = document.getElementById('vision-model-install-btn');
      if (visionSelect) {
        visionSelect.disabled = true;
        visionSelect.innerHTML = '<option value="">Analyse visuelle désactivée</option>';
      }
      if (analysisSelect) {
        analysisSelect.disabled = true;
        analysisSelect.innerHTML = '<option value="">Compréhension visuelle désactivée</option>';
      }
      if (installButton) installButton.style.display = 'none';
      _setVisionModelStatus('');
    }
    updateVisualAnalysisUi();
    refreshOllamaContextStatus();
    return d;
  } catch (e) {
    updateFirstInitButtonVisibility();
    return null;
  }
}

async function syncRagSettings(options = {}) {
  const silent = false;
  const ticket = beginSettingsSave(options);
  const successMessage = String(options.successMessage || '').trim();
  const chunkInput = document.getElementById('chunk-size');
  if (!chunkInput) return null;
  const nextChunkSize = parseInt(chunkInput.value, 10);
  if (!Number.isFinite(nextChunkSize) || nextChunkSize <= 0) return null;
  try {
    const d = await settingsWrite(
      '/api/rag-settings', { chunk_size: nextChunkSize },
      'Impossible de sauvegarder la taille des chunks.',
    );
    if (d?.ok) {
      finishSettingsSave(ticket, true);
      if (successMessage && !ticket) _showSettingSaved(successMessage);
      return d;
    }
    if (!silent) showToast(d?.msg || 'Impossible de sauvegarder la taille des chunks.', 4200, 'error');
  } catch (e) {
    if (!silent) showToast(e?.message || 'Impossible de sauvegarder la taille des chunks.', 4200, 'error');
  }
  finishSettingsSave(ticket, false, () => syncRagSettings(options));
  return null;
}


async function syncAppSettings(options = {}) {
  const silent = false;
  const ticket = beginSettingsSave(options);
  const successMessage = String(options.successMessage || '').trim();
  try {
    const selectedModel = optionalSelectedModel() || '';
    const candidateMultiplier = parseInt(
      document.getElementById('discovery-candidate-multiplier')?.value || '4',
      10,
    );
    const validationMultiplier = parseInt(
      document.getElementById('discovery-validation-multiplier')?.value || '3',
      10,
    );
    const payload = {
      llm_thinking: Boolean(document.getElementById('llm-thinking')?.checked),
      reranker_enabled: Boolean(document.getElementById('reranker-enabled')?.checked),
      reranker_profile: String(document.getElementById('reranker-profile')?.value || 'auto'),
      reranker_translate_french: Boolean(document.getElementById('reranker-translate-french')?.checked),
      debug_metrics: Boolean(document.getElementById('debug-metrics')?.checked),
      debug_force_thinking_small_models: Boolean(document.getElementById('force-thinking-small-models')?.checked),
      temperature: parseFloat(document.getElementById('llm-temperature')?.value || '0.1'),
      repeat_penalty: parseFloat(document.getElementById('llm-repeat-penalty')?.value || '1.0'),
      top_p: parseFloat(document.getElementById('llm-top-p')?.value || '0.5'),
      ollama_context_length: parseInt(document.getElementById('ollama-context-length')?.value || '0', 10),
      selected_model: selectedModel,
      visual_analysis_mode: String(document.getElementById('visual-analysis-mode')?.value || 'off'),
      vision_model: String(document.getElementById('vision-model-select')?.value || '').trim(),
      visual_description_model: String(document.getElementById('visual-description-model-select')?.value || '').trim(),
      visual_analysis_auto_chat: Boolean(document.getElementById('visual-analysis-auto-chat')?.checked),
      threshold: parseFloat(document.getElementById('threshold')?.value || '0.3'),
      max_chunks: parseInt(document.getElementById('max-chunks')?.value || '8', 10),
      chat_answer_length: String(document.getElementById('chat-answer-length')?.value || 'auto'),
      zotero_path: String(document.getElementById('zotero-path')?.value || '').trim(),
      metadata_source: state.metadataSource
        || inferMetadataSource(document.getElementById('zotero-path')?.value || ''),
      auto_enrich_pdf_metadata: Boolean(document.getElementById('auto-enrich-pdf-metadata')?.checked),
      index_standalone_pdfs: Boolean(document.getElementById('index-standalone-pdfs')?.checked),
      allow_external_api_requests: Boolean(document.getElementById('allow-external-discovery')?.checked),
      allow_external_discovery: Boolean(document.getElementById('allow-external-discovery')?.checked),
      research_scope_enabled: Boolean(document.getElementById('research-scope-enabled')?.checked),
      discovery_query_planner: Boolean(document.getElementById('discovery-query-planner')?.checked),
      discovery_candidate_multiplier: Number.isFinite(candidateMultiplier) ? candidateMultiplier : 4,
      discovery_llm_validation_multiplier: Number.isFinite(validationMultiplier) ? validationMultiplier : 3,
      unpaywall_email: String(document.getElementById('unpaywall-email')?.value || '').trim(),
      ui_language: window.BibliobrainI18n?.getLanguage?.() || undefined,
    };
    const d = await settingsWrite(
      '/api/app-settings', payload, 'Impossible de sauvegarder les réglages.',
    );
    if (d?.ok) {
      state.appSettings = d;
      const metadataEnrichmentInput = document.getElementById('auto-enrich-pdf-metadata');
      if (metadataEnrichmentInput && typeof d?.auto_enrich_pdf_metadata === 'boolean') {
        metadataEnrichmentInput.checked = Boolean(d.auto_enrich_pdf_metadata);
      }
      if (typeof updatePdfMetadataEnrichmentUi === 'function') {
        updatePdfMetadataEnrichmentUi();
      }
      if (d?.selected_model) rememberSelectedChatModel(d.selected_model);
      const contextSelect = document.getElementById('ollama-context-length');
      if (contextSelect && Number.isFinite(Number(d?.ollama_context_length))) {
        contextSelect.value = String(Number(d.ollama_context_length));
      }
      if (typeof setResearchScopeFeatureEnabled === 'function') {
        setResearchScopeFeatureEnabled(d?.research_scope_enabled !== false);
      }
      finishSettingsSave(ticket, true);
      if (successMessage && !ticket) _showSettingSaved(successMessage);
      return d;
    }
    if (!silent) showToast(d?.msg || 'Impossible de sauvegarder les réglages.', 4200, 'error');
  } catch (e) {
    if (!silent) showToast(e?.message || 'Impossible de sauvegarder les réglages.', 4200, 'error');
  }
  finishSettingsSave(ticket, false, () => syncAppSettings(options));
  return null;
}

function _setVisionModelStatus(message, kind = 'cyan') {
  const status = document.getElementById('vision-model-status');
  if (!status) return;
  status.className = `settings-status status-${kind}`;
  status.textContent = String(message || '');
  status.style.display = message ? 'block' : 'none';
}

function updateVisualAnalysisUi() {
  const mode = String(document.getElementById('visual-analysis-mode')?.value || 'off');
  const select = document.getElementById('vision-model-select');
  const analysisSelect = document.getElementById('visual-description-model-select');
  const installButton = document.getElementById('vision-model-install-btn');
  const autoChat = document.getElementById('visual-analysis-auto-chat');
  const indexWarning = document.getElementById('visual-index-warning');
  const enabled = mode !== 'off';
  const section = document.getElementById('settings-figures');
  section?.querySelectorAll('.settings-row').forEach((row, index) => {
    if (index > 0) row.hidden = !enabled;
  });
  if (indexWarning) indexWarning.style.display = mode === 'index' ? 'block' : 'none';
  if (select) select.disabled = !enabled || select.options.length <= 1;
  if (analysisSelect) analysisSelect.disabled = !enabled || analysisSelect.options.length <= 1;
  if (!enabled && installButton) installButton.style.display = 'none';
  const ocrModel = String(select?.value || state.appSettings?.vision_model || '').trim();
  const analysisModel = String(
    analysisSelect?.value || state.appSettings?.visual_description_model || ''
  ).trim();
  const hasVisualModel = Boolean(ocrModel || analysisModel);
  if (autoChat) autoChat.disabled = !enabled || !hasVisualModel;
  const analysisButton = document.getElementById('visual-analysis-btn');
  if (analysisButton) {
    analysisButton.hidden = !enabled;
    analysisButton.disabled = !hasVisualModel;
    analysisButton.title = analysisButton.disabled
      ? 'Sélectionnez d’abord un modèle vision dans les paramètres.'
      : 'Transcrire et comprendre les figures avec les modèles sélectionnés.';
  }
  if (state.appSettings) {
    state.appSettings.visual_analysis_mode = mode;
    state.appSettings.vision_model = ocrModel;
    state.appSettings.visual_description_model = analysisModel;
    state.appSettings.visual_analysis_auto_chat = Boolean(autoChat?.checked);
  }
  if (typeof updateChatVisualForceUi === 'function') updateChatVisualForceUi();
}

async function loadVisionModels(options = {}) {
  const silent = Boolean(options.silent);
  const applyDefaults = Boolean(options.applyDefaults);
  const select = document.getElementById('vision-model-select');
  const analysisSelect = document.getElementById('visual-description-model-select');
  const installButton = document.getElementById('vision-model-install-btn');
  const refreshButton = document.getElementById('vision-model-refresh-btn');
  if (!select || !analysisSelect) return null;
  const preferred = String(options.preferredModel || select.value || state.appSettings?.vision_model || '').trim();
  const preferredAnalysis = String(
    options.preferredAnalysisModel
      || analysisSelect.value
      || state.appSettings?.visual_description_model
      || ''
  ).trim();
  select.disabled = true;
  analysisSelect.disabled = true;
  if (refreshButton) {
    refreshButton.disabled = true;
    refreshButton.setAttribute('aria-busy', 'true');
  }
  select.innerHTML = '<option value="">Détection des modèles vision…</option>';
  analysisSelect.innerHTML = '<option value="">Détection des modèles vision…</option>';
  _setVisionModelStatus(_ti('settings.visionModelsDetecting'));
  try {
    const response = await fetch('/api/vision-models', { cache: 'no-store' });
    const payload = await response.json();
    const models = Array.isArray(payload?.models) ? payload.models : [];
    const generalVisionModels = models.filter((item) => !item.specialized_ocr);
    const recommendedOcr = String(payload?.recommended_model || '').trim();
    const recommendedAnalysis = String(payload?.recommended_analysis_model || '').trim();
    const vramLabel = String(payload?.gpu_vram_label || '').trim();
    select.innerHTML = '<option value="">Sélectionnez un modèle vision</option>' + models.map((item) => {
      const suffix = item.specialized_ocr ? ' · OCR spécialisé' : '';
      return `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)} · ${escapeHtml(item.size_label || '')}${suffix}</option>`;
    }).join('');
    analysisSelect.innerHTML = '<option value="">Compréhension visuelle désactivée</option>' + generalVisionModels.map((item) => {
      const suffix = item.name === recommendedAnalysis
        ? ` · ${escapeHtml(_ti('settings.visionModelRecommendedForGpu', { vram: vramLabel || _ti('settings.visionModelUnknownVram') }))}`
        : '';
      return `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)} · ${escapeHtml(item.size_label || '')}${suffix}</option>`;
    }).join('');
    if (preferred && models.some((item) => item.name === preferred)) select.value = preferred;
    if (preferredAnalysis && generalVisionModels.some((item) => item.name === preferredAnalysis)) {
      analysisSelect.value = preferredAnalysis;
    }
    let defaultsApplied = false;
    if (applyDefaults && !preferred && models.some((item) => item.name === recommendedOcr)) {
      select.value = recommendedOcr;
      defaultsApplied = true;
    }
    if (
      applyDefaults
      && !preferredAnalysis
      && generalVisionModels.some((item) => item.name === recommendedAnalysis)
    ) {
      analysisSelect.value = recommendedAnalysis;
      defaultsApplied = true;
    }
    const glmInstalled = models.some((item) => String(item.name || '').split(':')[0] === 'glm-ocr');
    if (installButton) installButton.style.display = glmInstalled ? 'none' : '';
    const lightButton = document.getElementById('visual-light-model-install-btn');
    if (lightButton) lightButton.style.display = payload?.light_analysis_suggested ? '' : 'none';
    const detectedMessage = payload?.ok === false
      ? (payload?.msg || '')
      : _ti(
        models.length === 1
          ? 'settings.visionModelsDetectedOne'
          : 'settings.visionModelsDetectedMany',
        { count: models.length },
      );
    const recommendationMessage = recommendedAnalysis
      ? ` ${_ti('settings.visionModelRecommendation', {
        model: recommendedAnalysis,
        vram: vramLabel || _ti('settings.visionModelUnknownVram'),
      })}`
      : '';
    _setVisionModelStatus(`${detectedMessage}${recommendationMessage}`, models.length ? 'green' : 'amber');
    if (state.appSettings) {
      state.appSettings.vision_model = String(select.value || '').trim();
      state.appSettings.visual_description_model = String(analysisSelect.value || '').trim();
    }
    updateVisualAnalysisUi();
    return { ...payload, defaultsApplied };
  } catch (error) {
    select.innerHTML = '<option value="">Modèles vision indisponibles</option>';
    analysisSelect.innerHTML = '<option value="">Modèles vision indisponibles</option>';
    if (installButton) installButton.style.display = '';
    const lightButton = document.getElementById('visual-light-model-install-btn');
    if (lightButton) lightButton.style.display = 'none';
    _setVisionModelStatus('Impossible de contacter Ollama pour détecter les modèles vision.', 'red');
    updateVisualAnalysisUi();
    if (!silent) showToast('Détection des modèles vision impossible.', 4200, 'error');
    return null;
  } finally {
    if (refreshButton) {
      refreshButton.disabled = false;
      refreshButton.removeAttribute('aria-busy');
    }
  }
}

async function installRecommendedVisionModel() {
  const approved = window.confirm(
    'Télécharger et installer glm-ocr:q8_0 avec Ollama ?\n\n' +
    'Téléchargement estimé : 1,6 Go. Aucun autre modèle ne sera installé.'
  );
  if (!approved) return null;
  const button = document.getElementById('vision-model-install-btn');
  if (button) {
    button.disabled = true;
    button.textContent = 'Installation de GLM-OCR…';
  }
  _setVisionModelStatus('Téléchargement de GLM-OCR en cours. Cette opération peut prendre plusieurs minutes…');
  try {
    const response = await fetch('/api/vision-models/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'glm-ocr:q8_0', confirmed: true }),
    });
    const payload = await response.json();
    if (!response.ok || !payload?.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
    await loadVisionModels({ preferredModel: payload.model });
    await syncAppSettings({ silent: true, successMessage: 'GLM-OCR installé et sélectionné.' });
    showToast('GLM-OCR installé et sélectionné.', 4200, 'success');
    return payload;
  } catch (error) {
    _setVisionModelStatus(`Installation impossible : ${error?.message || error}`, 'red');
    showToast('Installation de GLM-OCR impossible.', 5200, 'error');
    return null;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = 'Installer GLM-OCR · 1,6 Go';
    }
  }
}

async function installLightVisualModel() {
  const approved = window.confirm(
    'Télécharger et installer minicpm-v4.6 avec Ollama ?\n\n' +
    'Téléchargement estimé : 1,6 Go. Modèle de compréhension visuelle léger pour les machines ' +
    'modestes : moins précis que qwen3.5:4b, mais il tient sur une petite carte graphique. ' +
    'Aucun autre modèle ne sera installé.'
  );
  if (!approved) return null;
  const button = document.getElementById('visual-light-model-install-btn');
  const idleLabel = button?.textContent || '';
  if (button) {
    button.disabled = true;
    button.textContent = 'Installation de MiniCPM-V 4.6…';
  }
  _setVisionModelStatus('Téléchargement de MiniCPM-V 4.6 en cours. Cette opération peut prendre plusieurs minutes…');
  try {
    const response = await fetch('/api/vision-models/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: 'minicpm-v4.6', confirmed: true }),
    });
    const payload = await response.json();
    if (!response.ok || !payload?.ok) throw new Error(payload?.error || `HTTP ${response.status}`);
    await loadVisionModels({ preferredAnalysisModel: payload.model });
    await syncAppSettings({ silent: true, successMessage: 'MiniCPM-V 4.6 installé et sélectionné.' });
    showToast('MiniCPM-V 4.6 installé et sélectionné pour la compréhension visuelle.', 4200, 'success');
    return payload;
  } catch (error) {
    _setVisionModelStatus(`Installation impossible : ${error?.message || error}`, 'red');
    showToast('Installation de MiniCPM-V 4.6 impossible.', 5200, 'error');
    return null;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = idleLabel;
    }
  }
}

window.loadVisionModels = loadVisionModels;
window.installRecommendedVisionModel = installRecommendedVisionModel;
window.installLightVisualModel = installLightVisualModel;



function _scoreEmbeddingModelName(name) {
  const value = String(name || '').toLowerCase();
  if (!value) return 0;
  const normalized = value.trim().split(':', 1)[0];
  if (normalized === 'embeddinggemma') return 100;
  if (/qwen3[-_]?embedding/.test(normalized)) return 90;
  if (/nomic/.test(normalized)) return 80;
  if (/(embed|embedding|bge|e5|gte|minilm|mxbai)/.test(normalized)) return 70;
  return 1;
}

function _normalizeOllamaModelName(name) {
  return String(name || '').trim().toLowerCase().split(':', 1)[0];
}

function _resolveInstalledEmbeddingModel(requested, models) {
  const wanted = String(requested || '').trim();
  if (!wanted) return '';
  if (models.includes(wanted)) return wanted;
  const wantedNorm = _normalizeOllamaModelName(wanted);
  if (!wantedNorm) return '';
  const compatible = models.find(model => _normalizeOllamaModelName(model) === wantedNorm);
  return compatible || '';
}



async function setEmbeddingModel(model, options = {}) {
  const statusEl = document.getElementById(
    options.statusElementId || 'embedding-model-status'
  );
  const value = String(model || '').trim();
  const provider = String(options.provider || '').trim();
  const useLocal = Boolean(options.useLocal);
  const silent = Boolean(options.silent);
  const successMessage = String(options.successMessage || '').trim();
  if (!value && !useLocal) return null;
  try {
    const r = await fetch('/api/set-embedding-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: value,
        provider: provider || null,
        use_local: useLocal,
      }),
    });
    const d = await r.json();
    if (!d.ok) {
      if (statusEl) {
        statusEl.textContent = d.msg || _tr('Impossible d\'enregistrer le modèle d\'embedding.');
        statusEl.className = 'settings-status status-red';
        statusEl.style.display = '';
      }
      return null;
    }
    state.providerInfo = d.info;
    state.ragReady = false;
    state.startupStatus = await fetchStartupStatus();
    renderStartupStatus(state.startupStatus);
    updateFirstInitButtonVisibility();
    const vectorStatus = await fetchVectorDbStatus();
    if (statusEl) {
      const vectorWarning = getVectorDbEmbeddingWarning(vectorStatus);
      if (d.index_reused) {
        setMiniStatus(
          statusEl,
          `Index existant réactivé pour ${d.model}. Aucune reconstruction nécessaire.`,
          'green',
        );
      } else if (vectorWarning) {
        const tone = vectorStatus?.warning_level === 'info' ? 'cyan' : 'red';
        setMiniStatus(statusEl, vectorWarning, tone);
      } else {
        statusEl.textContent = _tr(
          `Modèle d'embedding actif : ${d.model}. Un index sera construit lors de la prochaine initialisation.`
        );
        statusEl.className = 'settings-status status-cyan';
        statusEl.style.display = '';
      }
    }
    await loadProviderInfo();
    await checkOllamaHealth(false);
    if (!silent) {
      _showSettingSaved(successMessage || _tr(`Modèle d'embedding enregistré : ${d.model}`));
    }
    return d;
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = _tr('Impossible d\'enregistrer le modèle d\'embedding.');
      statusEl.className = 'settings-status status-red';
      statusEl.style.display = '';
    }
    return null;
  }
}

// Where the index is computed: Ollama on this computer, or the service that answers.
async function setEmbeddingSource(source) {
  const select = document.getElementById('embedding-source-select');
  const statusEl = document.getElementById('embedding-source-status');
  const current = state.providerInfo?.embedding_source === 'chat' ? 'chat' : 'local';
  const next = source === 'chat' ? 'chat' : 'local';
  if (next === current) return state.providerInfo;
  if (state.ragInitBusy || state.startupAutoInitRunning) {
    if (select) select.value = current;
    setMiniStatus(statusEl, _ti('settings.indexing.sourceBusy'), 'red');
    return null;
  }
  if (select) select.disabled = true;
  try {
    const r = await fetch('/api/set-embedding-source', {
      method: 'POST', signal: AbortSignal.timeout(60000),
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source: next }),
    });
    const d = await r.json();
    if (!r.ok || !d?.ok || !d.info) throw new Error('embedding_source_not_applied');
    state.providerInfo = d.info;
    state.ragReady = false;
    state.startupStatus = await fetchStartupStatus();
    renderStartupStatus(state.startupStatus);
    updateFirstInitButtonVisibility();
    setMiniStatus(
      statusEl,
      _ti(d.index_reused ? 'settings.indexing.sourceReused' : 'settings.indexing.sourceToBuild'),
      d.index_reused ? 'green' : 'cyan',
    );
    await loadProviderInfo();
    await loadEmbeddingModels({ silent: true });
    await checkOllamaHealth(false);
    return d.info;
  } catch (error) {
    if (select) select.value = current;
    setMiniStatus(statusEl, _ti('settings.indexing.sourceFailed'), 'red');
    return null;
  } finally {
    if (select) select.disabled = false;
  }
}
window.setEmbeddingSource = setEmbeddingSource;

async function loadRemoteEmbeddingModels(options = {}) {
  const silent = Boolean(options.silent);
  const section = document.getElementById('remote-embedding-settings');
  const sel = document.getElementById('remote-embedding-model-select');
  const statusEl = document.getElementById('remote-embedding-model-status');
  const refreshBtn = document.getElementById('remote-embedding-model-refresh-btn');
  const enabledToggle = document.getElementById('remote-embedding-enabled');
  const modelRow = document.getElementById('remote-embedding-model-row');
  if (!sel || !isRemoteMode(activeProviderMode())) {
    if (section) section.hidden = true;
    return { ok: false, models: [] };
  }

  if (section) section.hidden = false;
  const enabled = Boolean(enabledToggle?.checked);
  if (modelRow) modelRow.hidden = !enabled;
  if (!enabled) {
    sel.disabled = true;
    return { ok: true, models: [], inactive: true };
  }
  sel.disabled = true;
  if (refreshBtn) refreshBtn.disabled = true;
  sel.innerHTML = '<option value="">Détection via l’API…</option>';
  try {
    const r = await fetch('/api/remote-embedding-models');
    const d = await r.json();
    const models = Array.from(new Set(
      (Array.isArray(d?.models) ? d.models : [])
        .map((model) => String(model || '').trim())
        .filter(Boolean)
    )).sort((a, b) => {
      const scoreDiff = _scoreEmbeddingModelName(b) - _scoreEmbeddingModelName(a);
      if (scoreDiff) return scoreDiff;
      return a.localeCompare(b);
    });
    const active = String(
      state.providerInfo?.embedding_provider === 'remote'
        ? state.providerInfo?.remote_embedding_model || ''
        : ''
    ).trim();
    const choices = active && !models.includes(active) ? [active, ...models] : models;

    if (!choices.length) {
      sel.innerHTML = '<option value="">Aucun modèle d’embedding annoncé</option>';
      if (statusEl) {
        statusEl.textContent = d?.status || 'Aucun modèle d’embedding n’est annoncé par cette API.';
        statusEl.className = 'settings-status status-red';
        statusEl.style.display = '';
      }
      return { ok: false, models, payload: d };
    }

    const placeholder = active ? '' : '<option value="" selected>Choisir un modèle distant…</option>';
    sel.innerHTML = placeholder + choices.map((model) => {
      const suffix = model === active ? ' (actif)' : '';
      return `<option value="${escapeAttr(model)}">${escapeHtml(model + suffix)}</option>`;
    }).join('');
    sel.value = active || '';
    if (statusEl) {
      statusEl.textContent = active
        ? `Modèle distant actif : ${active}.`
        : `Choisissez un modèle pour créer un index distant distinct. ${d?.status || `${models.length} modèle(s) disponible(s).`}`;
      statusEl.className = 'settings-status status-cyan';
      statusEl.style.display = '';
    }
    return { ok: true, models, payload: d };
  } catch (e) {
    sel.innerHTML = '<option value="">API distante inaccessible</option>';
    if (statusEl) {
      statusEl.textContent = 'Impossible de détecter les modèles d’embedding de l’API.';
      statusEl.className = 'settings-status status-red';
      statusEl.style.display = '';
    }
    if (!silent) showToast('Impossible de détecter les embeddings de l’API.', 4200, 'error');
    return { ok: false, models: [], error: e };
  } finally {
    sel.disabled = !enabledToggle?.checked;
    if (refreshBtn) refreshBtn.disabled = false;
  }
}






// ---------------------------------------------
//  GPU
// ---------------------------------------------
function shouldAutoRefreshGpuStatus() {
  if (!isGpuDebugPanelEnabled()) return false;
  const statusEl = document.getElementById('gpu-status');
  if (!statusEl || document.hidden) return false;
  if (statusEl.offsetParent === null) return false;
  return true;
}

function setGpuRefreshBusy(isBusy) {
  const btn = document.getElementById('gpu-refresh-btn');
  if (!btn) return;
  btn.disabled = Boolean(isBusy);
  btn.setAttribute('aria-busy', isBusy ? 'true' : 'false');
}

// Affiche le statut GPU en chips compactes ; le texte brut reste en tooltip.
function renderGpuStatus(statusText) {
  const statusEl = document.getElementById('gpu-status');
  if (!statusEl) return;
  const raw = String(statusText || '').trim();
  statusEl.textContent = '';
  statusEl.title = raw;
  if (!raw) return;
  const modelLines = raw.split('\n').map((l) => l.trim()).filter((l) => l.startsWith('[GPU]'));
  if (!modelLines.length) {
    const chip = document.createElement('span');
    chip.className = 'gpu-chip gpu-chip-muted';
    chip.textContent = raw.split('\n')[0];
    statusEl.appendChild(chip);
    return;
  }
  modelLines.forEach((line) => {
    const text = line.replace(/^\[GPU\]\s*/, '');
    const vram = text.match(/\(([^)]*(?:VRAM|RAM)[^)]*)\)/i);
    const model = text.split(' - ')[0].trim();
    const chip = document.createElement('span');
    chip.className = 'gpu-chip';
    chip.textContent = vram ? `${model} · ${vram[1]}` : model;
    statusEl.appendChild(chip);
  });
}

async function checkOllamaHealth(showToastOnError = false) {
  const statusEl = document.getElementById('model-status');
  const providerName = providerDisplayName(embeddingProviderMode());
  try {
    // The index has its own service; the server knows its address.
    const r = await fetch('/api/ollama-health');
    const d = await r.json();
    renderOllamaStatus(d);
    if (!d.required) {
      setInitButtonsAvailability(true);
      return d;
    }
    if (!d.ok) {
      setMiniStatus(statusEl, d.ollama_ok === false ? '' : d.msg, 'red');
      setInitButtonsAvailability(false);
      if (showToastOnError) showToast(d.msg, 5200);
    } else {
      setInitButtonsAvailability(true);
    }
    return d;
  } catch (e) {
    const msg = window.isBackendConnectionError?.(e)
      ? await window.describeBackendConnectionError('sidebar.ollamaCheckFailed')
      : `Impossible de vérifier ${providerName}.`;
    renderOllamaStatus({ required: true, ok: false, ollama_ok: false, msg });
    setMiniStatus(statusEl, msg, 'red');
    setInitButtonsAvailability(false);
    if (showToastOnError) showToast(msg, 5200);
    return { required: true, ok: false, msg };
  }
}

function _ollamaRuntimeText(key) {
  return window.BibliobrainI18n?.t(key) || key;
}

function _openAiSettings() {
  closeStatusPanel();
  if (!document.getElementById('settings-panel')?.classList.contains('open')) toggleSettings();
  switchSettingsTab('assistant');
}

// "Sur cet ordinateur · qwen3:8b", or the provider for LM Studio / online.
function _engineDetailText() {
  const model = currentChatModelName();
  const mode = activeProviderMode();
  if (normalizeProviderMode(mode) === 'ollama') return _ti('status.engineLocal', { model: model || '—' });
  return [providerDisplayName(mode), model].filter(Boolean).join(' · ');
}

// The AI engine as a status-pill task: ready while it answers, blocked when
// the local Ollama is stopped ("Démarrer" in the pill, "Réessayer" after a
// failed start), working while it starts, needs when it runs but cannot
// serve the library (e.g. the embedding model is missing).
function renderOllamaStatus(health, options = {}) {
  if (typeof setStatusTask !== 'function') return;
  if (health && !options.state) state.ollamaHealth = health;
  const engine = _ti('status.engine');
  const starting = options.state === 'starting';
  const failed = options.state === 'error';
  const offline = Boolean(health?.required) && health?.ollama_ok === false;
  let task = null;
  if (starting) {
    const detail = options.message || _ollamaRuntimeText('sidebar.ollamaStartingDesc');
    task = {
      tone: 'working',
      text: _ollamaRuntimeText('sidebar.ollamaStarting'),
      title: detail,
      view: () => ({ title: engine, detail }),
    };
  } else if (offline || failed) {
    const detail = options.message
      || _ollamaRuntimeText(failed ? 'sidebar.ollamaStartFailed' : 'sidebar.ollamaOfflineDesc');
    const action = failed
      ? { label: _ollamaRuntimeText('sidebar.retry'), onClick: () => checkOllamaHealth(true) }
      : { label: _ti('status.engineStart'), onClick: () => startLocalOllama() };
    task = {
      tone: 'blocked',
      text: _ti('status.engineStopped'),
      title: detail,
      action,
      view: () => ({ title: engine, detail, actions: [{ ...action, primary: true }] }),
    };
  } else if (health?.required && health.ok === false) {
    const detail = String(health.msg || '').trim();
    const action = { label: _ti('status.engineCheck'), onClick: _openAiSettings };
    task = {
      tone: 'needs',
      text: _ti('status.engineProblem'),
      title: detail,
      action,
      view: () => ({ title: engine, detail, actions: [{ ...action, primary: true }] }),
    };
  } else if (health) {
    task = {
      tone: 'ready',
      text: engine,
      title: _engineDetailText(),
      view: () => ({ title: engine, detail: _engineDetailText() }),
    };
  }
  setStatusTask('ollama', task);
  renderModelChip();
}

function _delay(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function startLocalOllama() {
  renderOllamaStatus(
    { required: true, ollama_ok: false },
    { forceVisible: true, state: 'starting' },
  );
  try {
    const response = await fetch('/api/ollama/start', { method: 'POST' });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const result = await response.json();
    if (!result.ok) {
      renderOllamaStatus(
        { required: true, ollama_ok: false },
        { forceVisible: true, state: 'error', message: result.msg },
      );
      showToast(result.msg || _ollamaRuntimeText('sidebar.ollamaStartFailed'), 5200, 'error');
      return result;
    }

    for (let attempt = 0; attempt < 16; attempt += 1) {
      await _delay(attempt === 0 ? 450 : 700);
      const health = await checkOllamaHealth(false);
      if (health?.ollama_ok) {
        showToast(_ollamaRuntimeText('sidebar.ollamaReady'), 3000, 'success');
        await Promise.all([
          loadModels({ silent: true, forceRefresh: true }),
          loadEmbeddingModels({ silent: true }),
          Promise.resolve(loadGPU()).catch(() => null),
        ]);
        return { ...result, health };
      }
      renderOllamaStatus(
        { required: true, ollama_ok: false },
        { forceVisible: true, state: 'starting' },
      );
    }

    const message = _ollamaRuntimeText('sidebar.ollamaStartTimeout');
    renderOllamaStatus(
      { required: true, ollama_ok: false },
      { forceVisible: true, state: 'error', message },
    );
    return { ...result, ok: false, code: 'start_timeout', msg: message };
  } catch (error) {
    const message = window.isBackendConnectionError?.(error)
      ? await window.describeBackendConnectionError('sidebar.ollamaStartFailed')
      : (String(error?.message || '').trim() || _ollamaRuntimeText('sidebar.ollamaStartFailed'));
    renderOllamaStatus(
      { required: true, ollama_ok: false },
      { forceVisible: true, state: 'error', message },
    );
    showToast(message, 5200, 'error');
    return { ok: false, code: 'request_failed', msg: message };
  }
}

window.startLocalOllama = startLocalOllama;

async function loadProviderInfo() {
  const revision = _providerUiRevision;
  const request = ++_providerInfoRequest;
  try {
    const r = await fetch('/api/provider-info', { signal: AbortSignal.timeout(12000) });
    const d = await r.json();
    if (!r.ok) throw new Error(d?.msg || _ti('settings.backendUnavailable'));
    if (revision !== _providerUiRevision || request !== _providerInfoRequest) return null;
    state.providerInfo = d;

    const mode = normalizeProviderMode(d?.effective_mode || d?.mode);
    const embeddingMode = normalizeProviderMode(d?.embedding_provider || 'ollama');
    const localEmbeddingsRequired = typeof d?.local_embeddings_required === 'boolean'
      ? Boolean(d.local_embeddings_required)
      : embeddingMode !== 'remote';
    const providerName = providerDisplayName(mode);
    const embeddingProviderName = providerDisplayName(embeddingMode);
    const urlInput = document.getElementById('ollama-url');
    const gpuBtn = document.getElementById('gpu-refresh-btn');
    const gpuStatus = document.getElementById('gpu-status');
    const modelStatus = document.getElementById('model-status');
    const embeddingSelect = document.getElementById('embedding-model-select');
    const embeddingRefreshBtn = document.getElementById('embedding-model-refresh-btn');
    const embeddingStatus = document.getElementById('embedding-model-status');
    const remoteEmbeddingSection = document.getElementById('remote-embedding-settings');
    const remoteEmbeddingSelect = document.getElementById('remote-embedding-model-select');
    const remoteEmbeddingEnabled = document.getElementById('remote-embedding-enabled');
    const remoteEmbeddingModelRow = document.getElementById('remote-embedding-model-row');
    const providerRow = document.getElementById('provider-mode-row');
    const providerSel = document.getElementById('provider-mode-select');

    refreshProviderUrlUi(d);
    if (providerRow) providerRow.style.display = '';
    if (providerSel) {
      providerSel.value = settingsProviderChoice();
      const remoteOption = Array.from(providerSel.options || []).find((opt) => opt.value === 'remote');
      if (remoteOption) {
        remoteOption.disabled = false;
        remoteOption.title = d?.can_use_remote
          ? ''
          : 'La configuration de l’API sera vérifiée lors de la sélection.';
      }
    }
    if (urlInput && document.activeElement !== urlInput && !networkAddressPending()) {
      urlInput.value = providerUrlForUi(d);
    }
    if (remoteEmbeddingSection) {
      remoteEmbeddingSection.hidden = !isRemoteMode(mode);
    }
    const remoteEmbeddingsActive = embeddingMode === 'remote';
    if (remoteEmbeddingEnabled) {
      remoteEmbeddingEnabled.checked = remoteEmbeddingsActive;
    }
    if (remoteEmbeddingModelRow) {
      remoteEmbeddingModelRow.hidden = !remoteEmbeddingsActive;
    }
    if (remoteEmbeddingSelect && isRemoteMode(mode)) {
      const activeRemoteModel = String(d?.remote_embedding_model || '').trim();
      remoteEmbeddingSelect.disabled = !remoteEmbeddingsActive;
      if (activeRemoteModel) {
        remoteEmbeddingSelect.innerHTML = `<option value="${escapeAttr(activeRemoteModel)}">${escapeHtml(activeRemoteModel)} (actif)</option>`;
      } else {
        remoteEmbeddingSelect.innerHTML = '<option value="">Détecter les modèles disponibles…</option>';
      }
    }

    if (isRemoteMode(mode)) {
      if (urlInput) {
        urlInput.disabled = true;
        urlInput.title = 'Adresse locale en lecture seule lorsque le provider API est actif.';
      }
      if (embeddingSelect) {
        embeddingSelect.disabled = !localEmbeddingsRequired;
        embeddingSelect.title = localEmbeddingsRequired
          ? `Le modèle d'embedding ${embeddingProviderName} reste configurable.`
          : 'Embeddings distants actifs côté serveur';
      }
      if (embeddingRefreshBtn) {
        embeddingRefreshBtn.disabled = !localEmbeddingsRequired;
        embeddingRefreshBtn.title = localEmbeddingsRequired
          ? `Actualiser les modèles d'embedding ${embeddingProviderName}`
          : 'Actualisation non applicable avec des embeddings distants';
      }
      if (gpuBtn) {
        gpuBtn.disabled = embeddingMode !== 'ollama';
        gpuBtn.title = embeddingMode === 'ollama'
          ? _ti('settings.refreshOllamaGpu')
          : _ti('settings.gpuUnavailableProvider');
      }
      if (gpuStatus) {
        renderGpuStatus(embeddingMode === 'ollama'
          ? _tr('GPU local : utilisé pour les embeddings Ollama.')
          : _tr('GPU local : non applicable en mode distant.'));
      }
      if (modelStatus) {
        setMiniStatus(
          modelStatus,
          localEmbeddingsRequired
            ? `Provider distant actif (${d.provider || 'remote'}). Chat distant OK, embeddings via ${embeddingProviderName} (${providerUrlForUi(d) || 'URL non définie'}).`
            : `Provider distant actif (${d.provider || 'remote'}). Chat + embeddings côté serveur.`,
          'cyan'
        );
      }
    } else {
      if (urlInput) {
        urlInput.disabled = false;
        urlInput.title = '';
      }
      if (embeddingSelect) {
        embeddingSelect.disabled = false;
        embeddingSelect.title = '';
      }
      if (embeddingRefreshBtn) {
        embeddingRefreshBtn.disabled = false;
        embeddingRefreshBtn.title = `Actualiser les modèles d'embedding ${embeddingProviderName}`;
      }
      if (gpuBtn) {
        gpuBtn.disabled = !isOllamaMode(mode);
        gpuBtn.title = isOllamaMode(mode)
          ? _ti('settings.refreshOllamaGpu')
          : _ti('settings.gpuUnavailableProvider');
      }
      if (modelStatus) {
        setMiniStatus(
          modelStatus,
          _ti('settings.providerActive', {
            provider: providerName,
            embeddingProvider: embeddingProviderName,
          }),
          'cyan'
        );
      }
    }

    if (embeddingSelect && d?.required_embedding_model) {
      embeddingSelect.value = d.required_embedding_model;
    }
    if (embeddingStatus) {
      const vectorWarning = getVectorDbEmbeddingWarning();
      if (vectorWarning) {
        setMiniStatus(
          embeddingStatus,
          vectorWarning,
          state.vectorDbStatus?.warning_level === 'info' ? 'cyan' : 'red',
        );
      } else {
        embeddingStatus.textContent = localEmbeddingsRequired
          ? `Modèle actif : ${d?.required_embedding_model || 'non défini'} (via ${embeddingProviderName})`
          : `Embeddings distants actifs côté serveur : ${d?.remote_embedding_model || 'configurés'}`;
        embeddingStatus.className = 'settings-status status-cyan';
        embeddingStatus.style.display = '';
      }
    }
    refreshProviderUrlUi(d);
    syncSettingsChatModelUi();
    return d;
  } catch (e) {
    return null;
  }
}

async function syncOllamaBaseUrl(options = {}) {
  const silent = false;
  if (_providerChangePending) return null;
  const successMessage = String(options.successMessage || '').trim();
  const skipGpuRefresh = Boolean(options.skipGpuRefresh);
  const urlInput = document.getElementById('ollama-url');
  const nextUrl = String(urlInput?.value || '').trim();
  if (!nextUrl) return null;
  if (settingsConnectionPending()) {
    // The address completes a choice that is not applied yet (another computer, other software).
    const choice = settingsProviderChoice();
    if (isRemoteMode(choice)) return null;
    const applied = await setProviderMode(choice, { url: nextUrl });
    return applied?.ok ? state.providerInfo : null;
  }

  const mode = activeProviderMode();
  const embeddingMode = embeddingProviderMode();
  if (isRemoteMode(mode) && embeddingMode === 'remote') {
    return state.providerInfo;
  }

  const targetProvider = isRemoteMode(mode) ? embeddingMode : activeLocalProviderMode();
  const currentUrl = String(
    targetProvider === 'lmstudio'
      ? state.providerInfo?.lmstudio_base_url || ''
      : state.providerInfo?.ollama_base_url || ''
  ).trim();
  if (currentUrl === nextUrl) return state.providerInfo;

  const ticket = beginSettingsSave(options);
  _providerChangePending = true;
  ++_providerUiRevision;
  state.settingsChatConnection = null;
  setComputeModeStatus('');
  refreshProviderUrlUi();
  try {
    const r = await fetch('/api/set-ollama-url', {
      method: 'POST', signal: AbortSignal.timeout(15000),
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: nextUrl, provider: targetProvider }),
    });
    const d = await r.json();
    if (d?.ok && d?.info) {
      state.providerInfo = d.info;
      if (d.changed === false) {
        if (urlInput) urlInput.value = d.url || nextUrl;
        finishSettingsSave(ticket, true);
        return d.info;
      }
      state.ragReady = false;
      state.startupStatus = await fetchStartupStatus();
      renderStartupStatus(state.startupStatus);
      updateFirstInitButtonVisibility();
      await loadProviderInfo();
      await loadEmbeddingModels({ silent: true });
      await loadModels({ silent: true });
      if (!skipGpuRefresh) await loadGPU();
      if (successMessage) _showSettingSaved(successMessage);
      finishSettingsSave(ticket, true);
      return d.info;
    }
    if (!silent) showToast(d?.msg || `Impossible de synchroniser l'URL ${providerDisplayName(targetProvider)}.`, 4200, 'error');
  } catch (e) {
    if (!silent) showToast(`Impossible de synchroniser l'URL ${providerDisplayName(targetProvider)}.`, 4200, 'error');
  } finally {
    _providerChangePending = false;
    refreshProviderUrlUi();
  }
  finishSettingsSave(ticket, false, () => syncOllamaBaseUrl(options));
  return null;
}

async function loadEmbeddingModels(options = {}) {
  const revision = _providerUiRevision;
  const request = ++_embeddingModelsRequest;
  const current = () => revision === _providerUiRevision && request === _embeddingModelsRequest;
  const silent = Boolean(options.silent);
  const sel = document.getElementById('embedding-model-select');
  const statusEl = document.getElementById('embedding-model-status');
  if (!sel) return { ok: false, models: [] };

  sel.innerHTML = '<option value="">Chargement…</option>';
  try {
    const r = await fetch('/api/ollama-local-models');
    const d = await r.json();
    if (!current()) return { stale: true, ok: false };
    const providerMode = normalizeProviderMode(d?.provider || embeddingProviderMode());
    const providerName = providerDisplayName(providerMode);
    if (providerMode === 'remote') {
      sel.innerHTML = '<option value="">Embeddings distants actifs</option>';
      if (statusEl) {
        statusEl.textContent = d.status || _tr('Embeddings distants actifs côté serveur.');
        statusEl.className = 'settings-status status-cyan';
        statusEl.style.display = '';
      }
      return { ok: true, models: [], payload: d };
    }

    const rawModels = Array.isArray(d.models) ? d.models : [];
    const models = [...rawModels].sort((a, b) => {
      const scoreDiff = _scoreEmbeddingModelName(b) - _scoreEmbeddingModelName(a);
      if (scoreDiff) return scoreDiff;
      return String(a).localeCompare(String(b));
    });

    if (!models.length) {
      const ollamaMissing = providerMode === 'ollama';
      sel.innerHTML = ollamaMissing
        ? '<option value="">EmbeddingGemma — recommandé, à installer</option>'
        : `<option value="">Aucun modèle ${providerName} détecté</option>`;
      if (statusEl) {
        const detectedStatus = d.status || `Aucun modèle d'embedding ${providerName} détecté.`;
        statusEl.textContent = ollamaMissing
          ? `${detectedStatus} Installez le modèle recommandé avec : ollama pull embeddinggemma`
          : detectedStatus;
        statusEl.className = 'settings-status status-red';
        statusEl.style.display = '';
      }
      return { ok: false, models, payload: d };
    }

    const selected = state.providerInfo?.required_embedding_model || d.required_embedding_model || '';
    const resolved = _resolveInstalledEmbeddingModel(selected, models);
    const backendRecommended = _resolveInstalledEmbeddingModel(
      d.recommended_embedding_model || '',
      models,
    );
    const recommended = resolved || backendRecommended || models[0] || '';
    sel.innerHTML = models.map((model) => {
      const suffix = resolved && model === resolved
        ? ' (actif)'
        : (!resolved && model === recommended ? ' (recommandé)' : '');
      return `<option value="${escapeAttr(model)}">${escapeHtml(model + suffix)}</option>`;
    }).join('');
    if (resolved) {
      sel.value = resolved;
    } else if (
      providerMode === 'ollama'
      && _normalizeOllamaModelName(backendRecommended) === 'embeddinggemma'
      && !state.ragInitBusy
      && !state.startupAutoInitRunning
    ) {
      sel.value = backendRecommended;
      const applied = await setEmbeddingModel(backendRecommended, {
        provider: 'ollama',
        silent: true,
      });
      if (applied?.ok) {
        const activeOption = Array.from(sel.options).find((option) => option.value === backendRecommended);
        if (activeOption) activeOption.textContent = `${backendRecommended} (actif)`;
      }
    } else if (selected) {
      sel.innerHTML = `<option value="${escapeAttr(selected)}">${escapeHtml(selected)} (non installé)</option>` + sel.innerHTML;
      sel.value = selected;
    } else if (recommended) {
      sel.value = recommended;
    }

    if (statusEl) {
      const vectorWarning = getVectorDbEmbeddingWarning();
      if (vectorWarning) {
        setMiniStatus(
          statusEl,
          vectorWarning,
          state.vectorDbStatus?.warning_level === 'info' ? 'cyan' : 'red',
        );
      } else {
        statusEl.textContent = `Liste chargée via ${providerName} (${state.providerInfo?.embedding_base_url || providerUrlForUi(state.providerInfo)}).`;
        statusEl.className = 'settings-status status-cyan';
        statusEl.style.display = '';
      }
    }
    return { ok: true, models, payload: d };
  } catch (e) {
    if (!current()) return { stale: true, ok: false };
    const providerName = providerDisplayName(embeddingProviderMode());
    sel.innerHTML = `<option value="">${providerName} inaccessible</option>`;
    if (statusEl) {
      statusEl.textContent = `Impossible de charger les modèles d'embedding via ${providerName}.`;
      statusEl.className = 'settings-status status-red';
      statusEl.style.display = '';
    }
    if (!silent) showToast('Impossible de charger les modèles d\'embedding.', 4200, 'error');
    return { ok: false, models: [], error: e };
  }
}

async function chooseProviderLocation(location, software = '') {
  if (_providerChangePending) return { ok: false };
  const mode = isLmStudioMode(software || activeLocalSoftwareChoice()) ? 'lmstudio' : 'ollama';
  state.settingsLocationChoice = null;
  if (location === 'remote') return setProviderMode('remote');
  const saved = savedLocalProviderUrl(mode);
  if (location === 'local') {
    // Keep a custom local port; otherwise come back to the software's default address.
    return setProviderMode(mode, { url: isThisComputerUrl(saved) ? saved : defaultLocalProviderUrl(mode) });
  }
  if (!isThisComputerUrl(saved)) return setProviderMode(mode);
  // No address for the other computer yet: ask for it, apply nothing.
  state.settingsProviderChoice = mode;
  state.settingsLocationChoice = 'network';
  ++_providerUiRevision;
  state.settingsChatConnection = null;
  setComputeModeStatus('');
  const urlInput = document.getElementById('ollama-url');
  if (urlInput) urlInput.value = '';
  refreshProviderUrlUi();
  const statusEl = document.getElementById('provider-mode-status');
  if (statusEl) {
    statusEl.textContent = _ti('settings.ai.networkAddressNeeded', { provider: providerDisplayName(mode) });
    statusEl.className = 'settings-status status-cyan';
    statusEl.style.display = '';
  }
  urlInput?.focus();
  return { ok: false, needsConfiguration: true };
}

function activeLocalSoftwareChoice() {
  const choice = settingsProviderChoice();
  if (!isRemoteMode(choice)) return choice;
  // Coming from the online service: reuse the software that serves the index.
  return isLmStudioMode(embeddingProviderMode()) ? 'lmstudio' : 'ollama';
}

function chooseLocalSoftware(software) {
  const location = settingsLocationChoice();
  return chooseProviderLocation(location === 'network' ? 'network' : 'local', software);
}

window.chooseProviderLocation = chooseProviderLocation;
window.chooseLocalSoftware = chooseLocalSoftware;

async function setProviderMode(mode, options = {}) {
  if (_providerChangePending) return { ok: false };
  const normalizedMode = normalizeProviderMode(mode);
  const previousMode = activeProviderMode();
  // LM Studio shows its address with "/v1"; the server expects the bare address.
  const requestedUrl = isRemoteMode(normalizedMode)
    ? ''
    : String(options.url || '').trim().replace(/\/+$/, '').replace(/\/v1$/i, '');
  const urlChanges = Boolean(requestedUrl) && requestedUrl !== savedLocalProviderUrl(normalizedMode).replace(/\/+$/, '');
  const pendingLocation = state.settingsLocationChoice || null;
  state.settingsProviderChoice = normalizedMode;
  // While an address is being applied, the cards show where it points.
  state.settingsLocationChoice = urlChanges ? (isThisComputerUrl(requestedUrl) ? 'local' : 'network') : null;
  ++_providerUiRevision;
  state.settingsChatConnection = null;
  const statusEl = document.getElementById('provider-mode-status');
  const showStatus = (key, tone = 'cyan') => {
    if (statusEl) {
      statusEl.textContent = _ti(key, { provider: providerDisplayName(normalizedMode) });
      statusEl.className = `settings-status status-${tone}`;
      statusEl.style.display = '';
    }
  };
  if (statusEl) { statusEl.textContent = ''; statusEl.style.display = 'none'; }
  setComputeModeStatus('');
  refreshProviderUrlUi();
  if (isRemoteMode(normalizedMode) && !state.providerInfo?.can_use_remote) {
    showStatus('settings.configureRemoteFirst');
    await loadApiSecrets();
    return { ok: false, needsConfiguration: true };
  }
  // Returning from an unconfigured choice to the already active service is a read-only check.
  if (normalizedMode === previousMode && !urlChanges) {
    const input = document.getElementById('ollama-url');
    if (input && !isRemoteMode(normalizedMode)) input.value = savedLocalProviderUrl(normalizedMode);
    return verifyComputeSettings({ forceRefresh: true });
  }
  _providerChangePending = true;
  refreshProviderUrlUi();
  showStatus('settings.computeApplying');
  let applied = false;
  let invalidAddress = false;
  try {
    const r = await fetch('/api/set-provider', {
      method: 'POST', signal: AbortSignal.timeout(30000),
      headers: { 'Content-Type': 'application/json' },
      // The address travels with the choice: switching software never moves it to another machine.
      body: JSON.stringify(requestedUrl ? { mode: normalizedMode, url: requestedUrl } : { mode: normalizedMode }),
    });
    const d = await r.json();
    const effectiveMode = normalizeProviderMode(d?.effective_mode || d?.info?.effective_mode || d?.info?.mode);
    if (r.status === 422 && requestedUrl) invalidAddress = true;
    if (!r.ok || !d.ok || d.applied === false || effectiveMode !== normalizedMode) throw new Error('provider_not_applied');
    state.providerInfo = d.info;
    state.settingsProviderChoice = normalizedMode;
    state.settingsLocationChoice = null;
    state.ragReady = false;
    applied = true;
    const input = document.getElementById('ollama-url');
    if (input) input.value = providerUrlForUi(d.info);
    if (statusEl) { statusEl.textContent = ''; statusEl.style.display = 'none'; }
  } catch (error) {
    // The active service is confirmed separately, including after an uncertain timeout.
    await loadProviderInfo();
    // A refused address leaves the choice open so it can be corrected.
    state.settingsLocationChoice = invalidAddress ? pendingLocation : null;
    showStatus(invalidAddress ? 'settings.ai.addressInvalid' : 'settings.serviceChangeFailed', 'red');
  } finally {
    _providerChangePending = false;
    refreshProviderUrlUi();
  }
  if (!applied) return { ok: false };
  const revision = _providerUiRevision;
  const verification = await verifyComputeSettings({ forceRefresh: true });
  if (revision !== _providerUiRevision) return { ok: false, stale: true };
  // Read the indexing status without folding its failure into chat connectivity.
  state.startupStatus = await fetchStartupStatus();
  if (revision !== _providerUiRevision) return { ok: false, stale: true };
  renderStartupStatus(state.startupStatus);
  updateFirstInitButtonVisibility();
  loadEmbeddingModels({ silent: true });
  return verification;
}

async function loadModels(options = {}) {
  const revision = _providerUiRevision;
  const request = ++_chatModelsRequest;
  const current = () => revision === _providerUiRevision && request === _chatModelsRequest;
  const silent = Boolean(options.silent);
  const forceRefresh = Boolean(options.forceRefresh);
  const url = document.getElementById('ollama-url')?.value || providerUrlForUi();
  const sel = document.getElementById('model-select');
  const statusEl = document.getElementById('model-status');
  const markMissing = (missing) => {
    sel?.classList.toggle('model-missing', Boolean(missing));
  };
  const providerName = providerDisplayName(activeProviderMode());

  sel.disabled = true;
  sel.innerHTML = '<option value="">Chargement…</option>';
  syncSettingsChatModelUi();
  markMissing(false);
  try {
    const params = new URLSearchParams({ url });
    if (forceRefresh) params.set('refresh', 'true');
    const r = await fetch('/api/models?' + params.toString(), { signal: AbortSignal.timeout(15000) });
    const d = await r.json();
    if (!current()) return { stale: true, ok: false };
    if (!r.ok) throw new Error(d?.msg || 'models_unavailable');
    const models = Array.isArray(d.models) ? d.models : [];
    if (!models.length) {
      sel.innerHTML = '<option value="">Aucun modèle détecté</option>';
      markMissing(true);
    } else {
      sel.innerHTML = models.map((model) => optionHtml(model)).join('');
      const savedModel = getPersistedChatModelPreference();
      const preferredModel = resolvePreferredChatModel(models, savedModel);
      if (preferredModel && Array.from(sel.options).some((opt) => opt.value === preferredModel)) {
        sel.value = preferredModel;
      }
      if (preferredModel && preferredModel !== savedModel) {
        rememberSelectedChatModel(preferredModel);
      }
      markMissing(false);
    }
    updateModelWarning(sel?.value || '');

    const health = d.health || await checkOllamaHealth(false);
    if (!current()) return { stale: true, ok: false };
    state.settingsChatConnection = { mode: activeProviderMode(), ok: models.length > 0 };
    refreshSettingsOverview();
    renderOllamaStatus(health);
    if (health && health.required && !health.ok) {
      const detailedMsg = isRemoteMode(activeProviderMode())
        ? `Modèles distants OK, mais embeddings via ${providerDisplayName(embeddingProviderMode())} requis : ${health.msg}`
        : health.msg;
      setMiniStatus(statusEl, health.ollama_ok === false ? '' : detailedMsg, 'red');
      if (!silent) showToast(health.msg, 5200);
      return { ok: false, models, health, payload: d };
    }

    if (models.length) {
      if (isRemoteMode(activeProviderMode())) {
        const remoteEmbeddingModel = String(state.providerInfo?.remote_embedding_model || '').trim();
        setMiniStatus(
          statusEl,
          remoteEmbeddingModel
            ? 'Provider distant actif. Chat + embeddings côté serveur.'
            : `Provider distant actif. Modèles distants OK, embeddings via ${providerDisplayName(embeddingProviderMode())}.`,
          'cyan'
        );
      } else {
        setMiniStatus(statusEl, _ti('settings.providerReady', { provider: providerName }), 'green');
      }
    } else {
      setMiniStatus(statusEl, `Aucun modèle détecté sur ${providerName}.`, 'red');
    }
    updateModelWarning(sel?.value || '');
    // The index health was checked above; preparing the documents does not
    // need a chat model.
    setInitButtonsAvailability(true);
    return { ok: models.length > 0, models, health, payload: d };
  } catch (e) {
    if (!current()) return { stale: true, ok: false };
    state.settingsChatConnection = { mode: activeProviderMode(), ok: false };
    refreshSettingsOverview();
    sel.innerHTML = '<option value="">Aucun modèle détecté</option>';
    markMissing(true);
    setMiniStatus(statusEl, `${providerName} inaccessible`, 'red');
    updateModelWarning('');
    setInitButtonsAvailability(false);
    if (!silent) showToast(`${providerName} inaccessible`, 5200);
    return { ok: false, models: [], health: null, error: e };
  } finally {
    if (current()) {
      sel.disabled = !String(sel.value || '').trim();
      syncSettingsChatModelUi();
    }
  }
}

// Module-level state for the reranker loading watcher.
const _rerankerWatcher = {
  pollHandle: null,
  bannerDismissed: false,
  startupWarmupRequested: false,
};

function updateRerankerSettingsUi() {
  const enabled = Boolean(document.getElementById('reranker-enabled')?.checked);
  const profile = document.getElementById('reranker-profile');
  const translateFrench = document.getElementById('reranker-translate-french');
  if (profile) profile.disabled = !enabled;
  if (translateFrench) {
    const selectedOption = profile?.selectedOptions?.[0];
    translateFrench.disabled = !enabled || selectedOption?.dataset?.englishOnly === 'false';
  }
}

async function fetchRerankerStatus() {
  try {
    const r = await fetch('/api/reranker-status');
    return await r.json();
  } catch (e) {
    return null;
  }
}

function _applyRerankerStatusPill(status) {
  const pill = document.getElementById('reranker-status-pill');
  if (!pill) return;
  if (!status) {
    pill.textContent = '-';
    pill.title = '';
    return;
  }
  const modelName = String(status.model_name || '').split('/').pop();
  const deviceName = String(status.device || '').toUpperCase();
  pill.title = [modelName, deviceName].filter(Boolean).join(' · ');
  if (!status.enabled) {
    pill.textContent = 'désactivé';
    pill.title = '';
    pill.style.color = '';
    return;
  }
  if (status.loading) {
    const elapsed = Number(status.load_elapsed_seconds || 0);
    pill.textContent = elapsed > 0 ? `chargement… (${elapsed.toFixed(0)}s)` : 'chargement…';
    pill.title = 'Téléchargement / initialisation du modèle en cours.';
    pill.style.color = 'var(--accent, #4a8cff)';
    return;
  }
  if (status.unload_after_use && !status.loaded) {
    pill.textContent = 'activé · libère la VRAM';
    pill.title = 'Le reranker est chargé seulement pendant le reranking, puis déchargé pour laisser la VRAM au LLM.';
    pill.style.color = '';
    return;
  }
  if (status.loaded && status.predict_error) {
    pill.textContent = 'chargé · predict indisponible';
    pill.title = String(status.predict_error);
    pill.style.color = 'var(--red, #c33)';
  } else if (status.loaded) {
    pill.textContent = `actif · ${modelName}${deviceName ? ` · ${deviceName}` : ''}`;
    pill.title = `Modèle chargé: ${status.model_name}${deviceName ? ` (${deviceName})` : ''}`;
    pill.style.color = 'var(--green, #2a8c3a)';
  } else if (status.load_error) {
    pill.textContent = 'erreur de chargement';
    pill.title = String(status.load_error);
    pill.style.color = 'var(--red, #c33)';
  } else {
    pill.textContent = 'activé · pas encore chargé';
    pill.title = 'Le modèle se chargera à la première requête.';
    pill.style.color = '';
  }
}

function _updateRerankerWarmupButton(status) {
  const btn = document.getElementById('reranker-warmup-btn');
  if (!btn) return;
  if (!status || !status.enabled) {
    btn.disabled = true;
    btn.textContent = _tr('Précharger maintenant');
    return;
  }
  if (status.unload_after_use) {
    btn.disabled = true;
    btn.textContent = _tr('Mode VRAM: déchargement auto');
    return;
  }
  if (status.loaded) {
    btn.disabled = true;
    btn.textContent = _tr('Modèle déjà chargé');
    return;
  }
  if (status.loading) {
    btn.disabled = true;
    btn.textContent = _tr('Chargement en cours…');
    return;
  }
  if (status.load_error) {
    btn.disabled = false;
    btn.textContent = _tr('Réessayer le chargement');
    return;
  }
  btn.disabled = false;
  btn.textContent = _tr('Précharger maintenant');
}

// The loading reranker is one of the sidebar status tasks, like the RAG load
// and the OCR pass. It reports no progress, only the time spent.
let _rerankerPillStatus = null;

function _rerankerDetailView() {
  const status = _rerankerPillStatus || {};
  const elapsed = Number(status.load_elapsed_seconds || 0);
  return {
    title: _ti('settings.reranker'),
    detail: _ti('settings.rerankerLoading'),
    percent: null,
    facts: [
      { term: _ti('settings.rerankerDetailModel'), value: String(status.model_name || '').split('/').pop() },
      { term: _ti('settings.rerankerDetailElapsed'), value: elapsed > 0 ? `${elapsed.toFixed(0)} s` : '' },
      { term: _ti('settings.rerankerDetailNote'), value: _ti('settings.rerankerDetailNoteText') },
    ],
    actions: [{
      label: _ti('settings.rerankerDetailSettings'),
      onClick: () => { closeStatusPanel(); _openRerankerSettings(); },
    }],
  };
}

function _openRerankerSettings() {
  if (!document.getElementById('settings-panel')?.classList.contains('open')) toggleSettings();
  switchSettingsTab('expert-search');
  window.setTimeout(() => {
    document.getElementById('reranker-warmup-btn')?.scrollIntoView?.({ block: 'center', behavior: preferredScrollBehavior() });
  }, 80);
}

function _updateRerankerBanner(status) {
  if (typeof setStatusTask !== 'function') return;
  const loading = Boolean(status && status.enabled && status.loading);
  if (!loading) {
    _rerankerPillStatus = null;
    activityClear('reranker');
    setStatusTask('reranker', null);
    return;
  }
  _rerankerPillStatus = status;
  const elapsed = Number(status.load_elapsed_seconds || 0);
  const label = _ti('settings.rerankerLoading');
  // Shown in the sidebar pill only when nothing else runs; otherwise it is a
  // "+1" there and a section of the status card.
  setStatusTask('reranker', {
    tone: 'working',
    text: elapsed > 0 ? `${label} · ${elapsed.toFixed(0)} s` : label,
    title: String(status.model_name || '').split('/').pop() || label,
    view: _rerankerDetailView,
  });
  // The collapsed sidebar hides the pill and shows this activity row instead.
  activitySet('reranker', label, _ti('settings.rerankerDetailNoteText'), null, { onClick: toggleStatusPanel });
}

function _applyRerankerStatusUi(status) {
  _applyRerankerStatusPill(status);
  _updateRerankerWarmupButton(status);
  _updateRerankerBanner(status);
}

function _updateRerankerInlineProgress(status) {
  const progress = document.getElementById('reranker-warmup-progress');
  const progressText = document.getElementById('reranker-warmup-progress-text');
  if (!progress) return;
  if (!status || !status.loading) {
    progress.style.display = 'none';
    return;
  }
  const elapsed = Number(status.load_elapsed_seconds || 0);
  progress.style.display = 'inline-flex';
  if (progressText) {
    progressText.textContent =
      elapsed > 0
        ? `Chargement… ${elapsed.toFixed(0)}s écoulées`
        : _tr('Chargement…');
  }
}

function _stopRerankerPolling() {
  if (_rerankerWatcher.pollHandle) {
    clearInterval(_rerankerWatcher.pollHandle);
    _rerankerWatcher.pollHandle = null;
  }
}

function _ensureRerankerPolling(status) {
  if (status && status.loading) {
    if (_rerankerWatcher.pollHandle) return;
    _rerankerWatcher.pollHandle = window.setInterval(async () => {
      const fresh = await fetchRerankerStatus();
      _applyRerankerStatusUi(fresh);
      _updateRerankerInlineProgress(fresh);
      if (!fresh || !fresh.loading) {
        _stopRerankerPolling();
        if (fresh && fresh.loaded) {
          showToast(_ti('settings.rerankerReadyToast'), 3500, 'success');
        } else if (fresh && fresh.load_error) {
          showToast(`Reranker: ${fresh.load_error}`, 6000, 'error');
        }
      }
    }, 1500);
  } else {
    _stopRerankerPolling();
  }
}

async function refreshRerankerStatusPill() {
  const status = await fetchRerankerStatus();
  _applyRerankerStatusUi(status);
  _updateRerankerInlineProgress(status);
  _ensureRerankerPolling(status);
}

async function requestRerankerStartupWarmup() {
  if (_rerankerWatcher.startupWarmupRequested) return;
  _rerankerWatcher.startupWarmupRequested = true;
  const status = await fetchRerankerStatus();
  _applyRerankerStatusUi(status);
  _updateRerankerInlineProgress(status);
  _ensureRerankerPolling(status);
  if (
    !status
    || !status.enabled
    || status.loaded
    || status.loading
    || status.unload_after_use
    || status.load_error
  ) {
    return;
  }
  _rerankerWatcher.bannerDismissed = false;
  try {
    const r = await fetch('/api/reranker-warmup', { method: 'POST' });
    const d = await r.json();
    const pollingStatus = (d?.warmup_started || d?.warmup_running) ? { ...d, loading: true } : d;
    _applyRerankerStatusUi(pollingStatus);
    _updateRerankerInlineProgress(pollingStatus);
    _ensureRerankerPolling(pollingStatus);
  } catch (e) {
    refreshRerankerStatusPill();
  }
}

async function warmupReranker() {
  const btn = document.getElementById('reranker-warmup-btn');
  const progress = document.getElementById('reranker-warmup-progress');
  const progressText = document.getElementById('reranker-warmup-progress-text');
  let keepProgressVisible = false;
  _rerankerWatcher.bannerDismissed = false;
  if (btn) {
    btn.disabled = true;
    btn.textContent = _tr('Chargement en cours…');
  }
  if (progress) progress.style.display = 'inline-flex';
  if (progressText) progressText.textContent = _tr('Chargement…');

  // Affiche immédiatement la pastille ; le poller mettra à jour le temps
  // écoulé dès que le chargement backend démarre.
  _updateRerankerBanner({ enabled: true, loading: true });

  // Start polling for live status updates while the warmup runs.
  _ensureRerankerPolling({ enabled: true, loading: true });

  try {
    const r = await fetch('/api/reranker-warmup', { method: 'POST' });
    const d = await r.json();
    const pollingStatus = (d?.warmup_started || d?.warmup_running) ? { ...d, loading: true } : d;
    keepProgressVisible = Boolean(pollingStatus?.loading);
    _applyRerankerStatusUi(pollingStatus);
    _updateRerankerInlineProgress(pollingStatus);
    _ensureRerankerPolling(pollingStatus);
    if (d?.loaded) {
      showToast(_ti('settings.rerankerReadyToast'), 3500, 'success');
    } else if (d?.load_error) {
      showToast(`Reranker: ${d.load_error}`, 6000, 'error');
    } else if (d?.warmup_skipped) {
      showToast(`Préchargement ignoré: ${d.warmup_skipped}`, 4500, 'error');
    }
  } catch (e) {
    _stopRerankerPolling();
    showToast(`Préchargement impossible: ${e.message}`, 5000, 'error');
  } finally {
    if (progress && !keepProgressVisible) progress.style.display = 'none';
    refreshRerankerStatusPill();
  }
}

document.addEventListener('change', (event) => {
  const target = event.target;
  if (target && target.id === 'reranker-enabled') {
    refreshRerankerStatusPill();
  }
});

// Expose to global so app.js (sendMessage finally) can call them.
window.refreshRerankerStatusPill = refreshRerankerStatusPill;
window.warmupReranker = warmupReranker;

async function loadGPU(options = {}) {
  if (!isGpuDebugPanelEnabled()) {
    clearGpuStatus();
    return null;
  }
  const isAuto = Boolean(options.auto);
  if (isAuto && !shouldAutoRefreshGpuStatus()) return null;
  if (_gpuStatusRefreshPromise) return _gpuStatusRefreshPromise;
  const refreshPromise = (async () => {
    const providerName = providerDisplayName(activeProviderMode());
    const statusEl = document.getElementById('gpu-status');
    if (!statusEl) return null;
    setGpuRefreshBusy(true);
    try {
      const url = document.getElementById('ollama-url')?.value || providerUrlForUi();
      const r = await fetch('/api/gpu?url=' + encodeURIComponent(url));
      const d = await r.json();
      renderGpuStatus(d.status);
      _gpuStatusLastRefreshAt = Date.now();
      return d;
    } catch (e) {
      renderGpuStatus(`${providerName} inaccessible`);
      return null;
    } finally {
      setGpuRefreshBusy(false);
    }
  })();
  _gpuStatusRefreshPromise = refreshPromise;
  try {
    return await refreshPromise;
  } finally {
    if (_gpuStatusRefreshPromise === refreshPromise) _gpuStatusRefreshPromise = null;
  }
}

function startGpuStatusAutoRefresh() {
  if (_gpuStatusRefreshTimer) return;
  _gpuStatusRefreshTimer = window.setInterval(() => {
    loadGPU({ auto: true }).catch(() => null);
  }, GPU_STATUS_REFRESH_INTERVAL_MS);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  const elapsed = Date.now() - _gpuStatusLastRefreshAt;
  if (elapsed >= GPU_STATUS_REFRESH_INTERVAL_MS) {
    loadGPU({ auto: true }).catch(() => null);
  }
});

startGpuStatusAutoRefresh();
