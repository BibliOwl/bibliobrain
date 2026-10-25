/* Independent OCR progress survives closing the settings pane or refreshing. */
(() => {
  let dirty = false;
  let advisoryModel = null;
  let advisoryAt = 0;
  let advisoryBusy = false;
  let actionBusy = false;
  let lastStatus = null;
  let lastReadiness = null;
  let advisoryFailed = false;
  let pollTimer = 0;
  let rescheduleStatusPoll = () => {};
  let visibleErrors = 20;
  let errorsSignature = '';
  const defaultModel = 'ovisocr2-benchmark:q8_0';
  const byId = id => document.getElementById(id);
  const tr = (key, params) => window.t(`ocr.${key}`, params);
  const statusKeys = {
    idle: 'statusIdle', scanning: 'statusScanning', running: 'statusRunning',
    pausing: 'statusPausing', paused: 'statusPaused', interrupted: 'statusInterrupted',
    error: 'statusError', completed: 'statusCompleted', completed_with_review: 'statusCompletedReview',
  };
  // Plain-language explanation of each failure the OCR pass records.
  const issueKeys = {
    PdfStreamError: 'unreadablePdf', PdfReadError: 'unreadablePdf',
    FileNotDecryptedError: 'protectedPdf', FileNotFoundError: 'missingFile',
    PermissionError: 'accessDenied', TimeoutError: 'timeout',
    incomplete_stream: 'incompleteText', truncated: 'incompleteText',
    repetition: 'repetition', no_readable_text: 'noText', incomplete_table: 'incompleteText',
  };
  const readinessReasonKeys = {
    model_missing: 'readinessModelMissing',
  };
  // The backend ships French prose; rebuild it from its codes so it follows the UI language.
  function readinessText(data) {
    let label = tr('readinessUnknown');
    let detail;
    if (data.installed !== true) {
      detail = tr(readinessReasonKeys[data.reason] || 'readinessOllamaDown');
    } else if (data.remote) {
      detail = tr('readinessRemote');
    } else if (data.level === 'slow') {
      label = tr('readinessSlow');
      detail = tr('readinessSlowDetail');
    } else if (data.level === 'recommended') {
      label = tr('readinessRecommended');
      detail = tr('readinessRecommendedDetail');
    } else {
      detail = tr('readinessInconclusive');
    }
    const memory = [];
    if (data.ram_bytes) memory.push(tr('memoryRam', { gb: Math.round(data.ram_bytes / 1024 ** 3) }));
    if (data.gpu_bytes) memory.push(tr('memoryVram', { gb: Math.round(data.gpu_bytes / 1024 ** 3) }));
    return `${label}${memory.length ? ` · ${memory.join(' · ')}` : ''} — ${detail}`;
  }
  function renderReadiness() {
    const target = byId('ocr-readiness');
    if (!target) return;
    if (lastReadiness) target.textContent = readinessText(lastReadiness);
    else if (!advisoryBusy) target.textContent = tr(advisoryFailed ? 'readinessUnavailable' : 'readinessInitial');
  }
  function renderInstallLabel() {
    const button = byId('ocr-install');
    if (button && !actionBusy) button.textContent = tr('installModel');
  }
  // The explanation card forces a fresh check: memory and install state decide its steps.
  async function refreshReadiness(options = {}) {
    const pane = ['settings-pane-documents', 'settings-pane-expert-indexing']
      .some(id => byId(id)?.classList.contains('active'));
    if (!byId('settings-panel')?.classList.contains('open') || !pane || advisoryBusy) return;
    const model = byId('ocr-model').value.trim();
    if (options?.force !== true && model === advisoryModel && Date.now() - advisoryAt < 60000) return;
    advisoryBusy = true;
    advisoryModel = model;
    advisoryAt = Date.now();
    const target = byId('ocr-readiness');
    // Keep a previous result on screen while checking: swapping the text moves
    // the Save button between mousedown and mouseup when the model field blurs.
    if (!lastReadiness) target.textContent = tr('readinessChecking');
    target.setAttribute('aria-busy', 'true');
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 4000);
    try {
      const response = await fetch(`/api/ocr/readiness?model=${encodeURIComponent(model)}`, { signal: controller.signal });
      if (!response.ok) throw new Error(`OCR (${response.status})`);
      const data = await response.json();
      if (byId('ocr-model').value.trim() !== model) return;
      const installButton = byId('ocr-install');
      if (installButton) installButton.hidden = model !== defaultModel || data.installed === true;
      lastReadiness = data;
      advisoryFailed = false;
      renderReadiness();
    } catch {
      lastReadiness = null;
      advisoryFailed = true;
      target.textContent = tr('readinessUnavailable');
    } finally {
      window.clearTimeout(timeout);
      advisoryBusy = false;
      target.removeAttribute('aria-busy');
      renderQuickFix();
    }
  }
  function remainingTime(seconds) {
    const minutes = Math.max(1, Math.ceil(seconds / 60));
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    return `${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
  }
  function updateControls() {
    const data = lastStatus;
    const active = !!data?.active;
    const locked = actionBusy || !data || active;
    ['ocr-enabled', 'ocr-model', 'ocr-save', 'ocr-install'].forEach(id => {
      if (byId(id)) byId(id).disabled = locked;
    });
    byId('ocr-start').hidden = active;
    byId('ocr-start').disabled = locked || dirty || !data?.settings.enabled;
    byId('ocr-start').textContent = tr(['paused', 'interrupted', 'error'].includes(data?.status) ? 'resume' : 'start');
    byId('ocr-pause').hidden = !active;
    byId('ocr-pause').disabled = actionBusy || !active || data?.status === 'pausing';
    byId('ocr-retry').disabled = locked || dirty || !data?.settings.enabled || !data?.errors?.length;
    byId('ocr-save').textContent = tr(actionBusy ? 'working' : 'save');
    byId('ocr-action-help').textContent = tr(dirty ? 'saveFirst' : active ? 'pauseFirst' : data?.settings.enabled ? 'readyHelp' : 'enableFirst');
    renderQuickFix();
  }
  function renderErrors() {
    const errors = lastStatus?.errors || [];
    const signature = JSON.stringify([errors, visibleErrors, window.getLanguage?.()]);
    if (signature === errorsSignature) return;
    errorsSignature = signature;
    const list = byId('ocr-errors');
    const rows = errors.slice(0, visibleErrors).map(error => {
      const li = document.createElement('li');
      const file = document.createElement('span');
      file.className = 'ocr-error-file';
      file.textContent = `${error.file}${error.page ? ` · p. ${error.page}` : ''}`;
      const message = document.createElement('span');
      message.className = 'ocr-error-message';
      message.textContent = tr(issueKeys[error.issue] || 'unknownError');
      const code = document.createElement('code');
      code.className = 'ocr-error-code';
      code.textContent = error.issue || 'review';
      li.append(file, message, code);
      return li;
    });
    const scrollTop = list.scrollTop;
    list.replaceChildren(...rows);
    list.scrollTop = scrollTop;
    byId('ocr-errors-summary').textContent = tr('reviewCount', { count: errors.length });
    byId('ocr-errors-more').hidden = errors.length <= visibleErrors;
    byId('ocr-errors-more').textContent = tr('showMoreCount', { count: Math.min(20, errors.length - visibleErrors) });
  }
  // The OCR pass runs in the background for minutes and blocks "Update RAG":
  // show it in a sidebar status pill, from any tab, like the RAG load. The
  // collapsed sidebar hides the pills and shows the activity row instead.
  const pillLabels = {
    scanning: 'sidebarScanning', running: 'statusRunning', pausing: 'sidebarPausing',
    paused: 'pillPaused', interrupted: 'pillInterrupted',
  };
  function openOcrSettings() {
    if (!byId('settings-panel')?.classList.contains('open')) toggleSettings();
    switchSettingsTab('documents');
    window.setTimeout(() => {
      byId('settings-scans')?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
    }, 80);
  }
  function progressOf(data) {
    const scanning = data.status === 'scanning';
    const total = scanning ? data.documents_total : data.candidates;
    const done = scanning ? data.documents_scanned : data.completed;
    return { scanning, total, done: done || 0, fraction: total ? Math.min(1, (done || 0) / total) : null };
  }
  function currentPage(data) {
    if (!data.current_file) return '';
    const name = String(data.current_file).split(/[\\/]/).pop();
    return `${name}${data.current_page ? ` · p. ${data.current_page}` : ''}`;
  }
  function detailView() {
    const data = lastStatus || {};
    const { scanning, total, done, fraction } = progressOf(data);
    const results = [];
    if (data.indexed_pages) results.push(tr('indexedPages', { count: data.indexed_pages }));
    if (data.blank) results.push(tr('blankPages', { count: data.blank }));
    if (data.review) results.push(tr('toReview', { count: data.review }));
    if (data.skipped_documents) results.push(tr('skippedDocs', { count: data.skipped_documents }));
    // Shown only when the server measured a rate: never an invented estimate.
    const eta = data.active && data.status === 'running' && typeof data.estimated_remaining_seconds === 'number'
      ? window.t('status.timeLeft', { time: remainingTime(data.estimated_remaining_seconds) })
      : '';
    const label = pillLabels[data.status] || statusKeys[data.status];
    let detail = label ? tr(label) : String(data.status || '');
    if (total && data.active) {
      detail = scanning
        ? tr('progressScanned', { done, total })
        : window.t('status.ocrDetail', { i: formatCount(done), n: formatCount(total) });
    }
    return {
      title: window.t('status.ocrTitle'),
      detail,
      eta,
      percent: fraction === null ? null : Math.round(fraction * 100),
      facts: [
        { term: tr('detailCurrent'), value: data.active ? currentPage(data) : '' },
        { term: tr('detailResults'), value: results.join(' · ') },
        { term: tr('detailError'), value: data.error || '' },
      ],
      actions: [
        canResume(data) ? { label: tr('resume'), onClick: toggleOcrPause, primary: data.status === 'interrupted' }
          : canPause(data) ? { label: tr('pause'), onClick: toggleOcrPause } : null,
        { label: tr('detailSettings'), onClick: () => { closeStatusPanel(); openOcrSettings(); } },
      ],
    };
  }
  const canResume = (data) => data?.status === 'paused' || data?.status === 'interrupted';
  const canPause = (data) => data?.status === 'running' || data?.status === 'scanning';
  // Pause while it works, resume once paused or interrupted.
  function toggleOcrPause() {
    if (canResume(lastStatus)) action('start');
    else if (canPause(lastStatus)) action('pause');
  }
  // One sidebar pill shows the work that matters most (setStatusTask in
  // app.js). A running OCR is 'working' (pause it from the card); an
  // interrupted one needs the user (Resume in the pill), unless other work
  // is in progress: then that work keeps the pill and the OCR waits in "+N";
  // a paused one only appears in the card, with its resume action.
  function renderOcrStatus(data) {
    if (typeof setStatusTask !== 'function' || typeof activitySet !== 'function') return;
    const shown = Boolean(data?.settings?.enabled && pillLabels[data.status]);
    if (!shown) {
      activityClear('ocr');
      setStatusTask('ocr', null);
      return;
    }
    const { fraction } = progressOf(data);
    const percent = fraction === null ? null : Math.round(fraction * 100);
    const label = tr(pillLabels[data.status]);
    let text = percent === null ? label : `${label} · ${percent}%`;
    if (data.status === 'running' && percent !== null) text = window.t('status.ocrRunning', { pct: percent });
    let tone = 'ready';
    if (data.active) tone = 'working';
    else if (data.status === 'interrupted') tone = 'needs';
    setStatusTask('ocr', {
      tone,
      text,
      title: currentPage(data) || label,
      percent,
      action: tone === 'needs' ? { label: tr('resume'), onClick: toggleOcrPause } : null,
      yieldsToWork: true,
      view: detailView,
    });
    if (data.active) {
      activitySet('ocr', label, [currentPage(data), tr('sidebarOpen')].filter(Boolean).join('\n'), fraction, {
        paused: data.status === 'pausing', onClick: toggleStatusPanel,
      });
    } else {
      activityClear('ocr');
    }
  }
  window.toggleOcrPause = toggleOcrPause;
  function renderStatusLine() {
    const data = lastStatus;
    if (!data) return;
    const scanning = data.status === 'scanning';
    const total = scanning ? data.documents_total : data.candidates;
    const done = scanning ? data.documents_scanned : data.completed;
    const parts = [statusKeys[data.status] ? tr(statusKeys[data.status]) : data.status];
    if (total) parts.push(tr(scanning ? 'progressScanned' : 'progressPages', { done: done || 0, total }));
    if (data.indexed_pages) parts.push(tr('indexedPages', { count: data.indexed_pages }));
    if (data.skipped_documents) parts.push(tr('skippedDocs', { count: data.skipped_documents }));
    if (data.blank) parts.push(tr('blankPages', { count: data.blank }));
    if (data.review) parts.push(tr('toReview', { count: data.review }));
    if (data.active && data.status === 'running') {
      parts.push(typeof data.estimated_remaining_seconds === 'number'
        ? tr('etaApprox', { time: remainingTime(data.estimated_remaining_seconds) }) : tr('etaPending'));
    }
    if (data.active && data.current_file) parts.push(`${data.current_file}${data.current_page ? ` · p. ${data.current_page}` : ''}`);
    if (data.error) parts.push(data.error);
    byId('ocr-status').textContent = parts.join(' · ');
  }
  async function refresh() {
    try {
      const response = await fetch('/api/ocr/status', { cache: 'no-store' });
      if (!response.ok) throw new Error(`OCR (${response.status})`);
      const data = await response.json();
      lastStatus = data;
      renderOcrStatus(data);
      if (!dirty) {
        byId('ocr-enabled').checked = data.settings.enabled;
        byId('ocr-model').value = data.settings.model;
      }
      updateControls();
      const scanning = data.status === 'scanning';
      const total = scanning ? data.documents_total : data.candidates;
      const done = scanning ? data.documents_scanned : data.completed;
      const percent = total ? Math.max(0, Math.min(100, 100 * (done || 0) / total)) : 0;
      byId('ocr-progress-bar').style.width = `${percent}%`;
      byId('ocr-progress').setAttribute('aria-valuenow', String(Math.round(percent)));
      renderStatusLine();
      byId('ocr-ignored-files').hidden = !data.ignored_non_pdf_errors;
      byId('ocr-ignored-files').textContent = tr('ignoredFiles', { count: data.ignored_non_pdf_errors || 0 });
      renderErrors();
      renderQuickFix();
      refreshReadiness();
    } catch (error) {
      byId('ocr-status').textContent = tr('trackingUnavailable', { message: error.message });
    }
  }
  async function action(endpoint, body, method = 'POST') {
    if (actionBusy) return;
    actionBusy = true;
    updateControls();
    try {
      const response = await fetch(`/api/ocr/${endpoint}`, {
        method, headers: { 'Content-Type': 'application/json' },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.msg || data.detail || `OCR (${response.status})`);
      if (endpoint === 'settings') {
        dirty = byId('ocr-enabled').checked !== body.enabled || byId('ocr-model').value !== body.model;
        setSettingsDraft('ocr', dirty);
      }
      await refresh();
      rescheduleStatusPoll();
    } catch (error) {
      byId('ocr-status').textContent = error.message;
      showToast(error.message, 6000, 'error');
    } finally {
      actionBusy = false;
      updateControls();
    }
  }
  async function installDefaultModel() {
    if (actionBusy) return;
    const approved = window.confirm(tr('installConfirm'));
    if (!approved) return;
    actionBusy = true;
    updateControls();
    const button = byId('ocr-install');
    if (button) {
      button.disabled = true;
      button.textContent = tr('installing');
    }
    byId('ocr-readiness').textContent = tr('installDownloading');
    try {
      const response = await fetch('/api/ocr/install', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: defaultModel, confirmed: true }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || `OCR (${response.status})`);
      byId('ocr-model').value = defaultModel;
      advisoryModel = null;
      await refreshReadiness();
      showToast(tr('installed'), 4200, 'success');
    } catch (error) {
      byId('ocr-readiness').textContent = tr('installError', { message: error.message });
      showToast(tr('installFailed'), 5200, 'error');
    } finally {
      actionBusy = false;
      updateControls();
      renderInstallLabel();
    }
  }
  // "Fix this" (My documents) chains what used to be four manual steps:
  // install the model, turn OCR on, save, start. Each failure gets a card that
  // says why and what to do, instead of a toast.
  let inventory = null;
  let inventoryTimer = 0;
  let quickPhase = '';
  let quickExpanded = false;
  let quickBlocked = null;
  const blockedCodes = new Set(['ollama_down', 'install_failed', 'index_busy', 'library_not_loaded']);
  const formatCount = value => Number(value || 0).toLocaleString(window.getLanguage?.() === 'en' ? 'en-GB' : 'fr-FR');
  const GIB = 1024 ** 3;

  function quickFailure() {
    if (quickBlocked) return quickBlocked;
    if (lastStatus?.settings?.enabled && lastStatus.status === 'error' && !lastStatus.active) {
      return { code: 'failed', message: lastStatus.error || '' };
    }
    return null;
  }
  function quickFixView() {
    if (quickFailure()) return 'blocked';
    if (quickPhase) return 'working';
    const data = lastStatus;
    if (data?.settings?.enabled) {
      if (data.active || ['scanning', 'running', 'pausing', 'paused', 'interrupted'].includes(data.status)) return 'working';
      if (['completed', 'completed_with_review'].includes(data.status)) return 'done';
    }
    if (quickExpanded) return 'confirm';
    if (inventory?.active) return 'checking';
    if (inventory?.status === 'completed') return inventory.documents > 0 ? 'offer' : 'none';
    return 'check';
  }
  window.ocrQuickFixSummary = () => {
    const view = quickFixView();
    const fraction = lastStatus && !quickPhase ? progressOf(lastStatus).fraction : null;
    return {
      view,
      percent: view === 'working' && fraction !== null ? Math.round(fraction * 100) : null,
      documents: inventory?.documents || 0,
      pages: inventory?.pages || 0,
    };
  };
  function setText(id, text) {
    const node = byId(id);
    if (node) node.textContent = text;
  }
  function setBar(id, fraction) {
    const bar = byId(id);
    if (!bar) return;
    bar.classList.toggle('is-indeterminate', fraction === null);
    bar.querySelector('span').style.width = fraction === null ? '' : `${Math.round(fraction * 100)}%`;
    if (fraction === null) bar.removeAttribute('aria-valuenow');
    else bar.setAttribute('aria-valuenow', String(Math.round(fraction * 100)));
  }
  function memoryLine() {
    const data = lastReadiness;
    const gb = data?.ram_bytes ? Math.round(data.ram_bytes / GIB) : 0;
    // Same thresholds as the backend advisory, applied before the model exists.
    const level = data?.level && data.level !== 'unknown' ? data.level
      : !data?.ram_bytes ? 'unknown' : data.ram_bytes >= 16 * GIB ? 'recommended' : data.ram_bytes < 8 * GIB ? 'slow' : 'unknown';
    if (level === 'unknown') return null;
    return { level, text: tr(level === 'slow' ? 'quickfix.memorySlow' : 'quickfix.memoryOk', { gb }) };
  }
  function renderBlocked(failure) {
    const code = blockedCodes.has(failure.code) ? failure.code : 'failed';
    setText('ocr-quickfix-blocked-title', tr(`quickfix.${code}Title`));
    setText('ocr-quickfix-blocked-lead', code === 'failed' && failure.message ? failure.message : tr(`quickfix.${code}Lead`));
    byId('ocr-quickfix-blocked-steps')?.replaceChildren(...['Step1', 'Step2'].map((step) => {
      const item = document.createElement('li');
      item.textContent = tr(`quickfix.${code}${step}`);
      return item;
    }));
    const link = byId('ocr-quickfix-blocked-link');
    if (link) {
      link.hidden = code !== 'library_not_loaded';
      link.textContent = tr('quickfix.openLibrary');
    }
  }
  function renderWorking() {
    const data = lastStatus || {};
    const { scanning, total, done, fraction } = progressOf(data);
    const paused = ['paused', 'interrupted'].includes(data.status);
    const steps = {
      download: quickPhase === 'download' ? 'active' : 'done',
      read: quickPhase === 'download' ? 'todo' : 'active',
      ready: 'todo',
    };
    document.querySelectorAll('[data-ocr-step]').forEach((node) => { node.dataset.state = steps[node.dataset.ocrStep]; });
    let text;
    if (quickPhase === 'download') text = tr('quickfix.downloading');
    else if (quickPhase) text = tr('quickfix.starting');
    else if (!total) text = tr(paused ? 'statusPaused' : 'sidebarScanning');
    else if (scanning) text = tr('quickfix.scanningDocs', { done: formatCount(done), total: formatCount(total) });
    else text = tr(paused ? 'quickfix.pausedAt' : 'quickfix.readingPage', { done: formatCount(done), total: formatCount(total) });
    setText('ocr-quickfix-progress-text', text);
    setText('ocr-quickfix-remaining', data.active && data.status === 'running' && typeof data.estimated_remaining_seconds === 'number'
      ? tr('sidebarRemaining', { time: remainingTime(data.estimated_remaining_seconds) }) : '');
    setBar('ocr-quickfix-bar', quickPhase || fraction === null ? null : fraction);
    setText('ocr-quickfix-now', data.active && !quickPhase && data.current_file ? tr('quickfix.now', { file: currentPage(data) }) : '');
    const pause = byId('ocr-quickfix-pause');
    if (pause) {
      pause.textContent = tr(canResume(data) ? 'resume' : 'pause');
      pause.hidden = Boolean(quickPhase);
      pause.disabled = actionBusy || data.status === 'pausing' || !(canResume(data) || canPause(data));
    }
  }
  function renderDone() {
    const data = lastStatus || {};
    setText('ocr-quickfix-done-title', tr('quickfix.doneTitle', { count: formatCount(data.indexed_pages) }));
    const errors = data.errors || [];
    const files = new Set(errors.map(error => error.file));
    const issue = byId('ocr-quickfix-issue');
    if (issue) issue.hidden = !files.size;
    if (!files.size) return;
    setText('ocr-quickfix-issue-count', tr(files.size === 1 ? 'quickfix.issueOne' : 'quickfix.issueMany', { count: formatCount(files.size) }));
    const kinds = new Set(errors.map(error => issueKeys[error.issue] || 'unknownError'));
    setText('ocr-quickfix-issue-reason', kinds.size === 1 ? tr([...kinds][0]) : '');
  }
  function renderQuickFix() {
    const card = byId('settings-scans');
    if (!card) return;
    const view = quickFixView();
    card.hidden = view === 'none';
    card.dataset.ocrView = view;
    card.querySelectorAll('[data-ocr-panel]').forEach((panel) => { panel.hidden = panel.dataset.ocrPanel !== view; });
    // Failures stay listed while a pass is paused, not only once it ends.
    const errorsWrap = byId('ocr-errors-wrap');
    if (errorsWrap) errorsWrap.hidden = !(['working', 'done'].includes(view) && lastStatus?.errors?.length);
    const installed = lastReadiness?.installed === true;
    const documents = formatCount(inventory?.documents);
    if (view === 'checking') {
      const scanned = inventory?.documents_scanned || 0;
      const total = inventory?.documents_total || 0;
      setText('ocr-quickfix-checking-text', tr('quickfix.checking', { done: formatCount(scanned), total: total ? formatCount(total) : '…' }));
      setBar('ocr-quickfix-checking-bar', total ? scanned / total : null);
    } else if (view === 'offer') {
      setText('ocr-quickfix-offer-text', tr('quickfix.offerText', { count: documents }));
      setText('ocr-quickfix-open', tr(installed ? 'quickfix.turnOn' : 'quickfix.turnOnDownload'));
    } else if (view === 'confirm') {
      const pages = inventory?.pages || 0;
      setText('ocr-quickfix-confirm-title', tr('quickfix.confirmTitle', { count: documents }));
      byId('ocr-quickfix-step-download').hidden = installed;
      setText('ocr-quickfix-step-read', tr(pages ? 'quickfix.stepRead' : 'quickfix.stepReadUnknown', { pages: formatCount(pages) }));
      const rate = Number(lastStatus?.seconds_per_page);
      setText('ocr-quickfix-step-time', rate > 0 && pages
        ? tr('quickfix.stepTime', { time: remainingTime(rate * pages) }) : tr('quickfix.stepTimeUnknown'));
      const memory = memoryLine();
      const line = byId('ocr-quickfix-memory');
      if (line) {
        line.hidden = !memory;
        line.dataset.level = memory?.level || '';
        line.textContent = memory?.text || '';
      }
    } else if (view === 'working') renderWorking();
    else if (view === 'done') renderDone();
    else if (view === 'blocked') renderBlocked(quickFailure());
    ['ocr-quickfix-start', 'ocr-quickfix-retry', 'ocr-quickfix-open', 'ocr-quickfix-check'].forEach((id) => {
      if (byId(id)) byId(id).disabled = actionBusy;
    });
    if (typeof window.refreshSettingsOverview === 'function') window.refreshSettingsOverview();
  }

  async function quickFetch(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: options.body ? { 'Content-Type': 'application/json' } : undefined,
    });
    let data = {};
    try { data = await response.json(); } catch { /* an empty body is a failure below */ }
    return { response, data };
  }
  function blocked(code, detail = '') {
    return Object.assign(new Error(code), { code, detail });
  }
  function installFailureCode(error) {
    const code = String(error || '');
    if (code.startsWith('ollama_unavailable')) return 'ollama_down';
    return 'install_failed';
  }
  // Pressing Start is the consent the old window.confirm() asked for.
  async function runQuickFix() {
    if (actionBusy) return;
    actionBusy = true;
    quickBlocked = null;
    quickExpanded = false;
    updateControls();
    try {
      let model = lastStatus?.settings?.model || defaultModel;
      const check = async name => quickFetch(`/api/ocr/readiness?model=${encodeURIComponent(name)}`);
      let readiness = await check(model);
      if (readiness.response.ok && readiness.data.installed !== true && model !== defaultModel) {
        model = defaultModel;
        readiness = await check(model);
      }
      if (readiness.response.ok) lastReadiness = readiness.data;
      if (readiness.data.installed !== true) {
        quickPhase = 'download';
        renderQuickFix();
        const install = await quickFetch('/api/ocr/install', {
          method: 'POST', body: JSON.stringify({ model: defaultModel, confirmed: true }),
        });
        if (!install.response.ok || !install.data.ok) throw blocked(installFailureCode(install.data.error));
        advisoryModel = null;
      }
      quickPhase = 'starting';
      renderQuickFix();
      const saved = await quickFetch('/api/ocr/settings', {
        method: 'PUT', body: JSON.stringify({ enabled: true, model }),
      });
      // 409: a pass already runs, which is what the user asked for.
      if (!saved.response.ok && saved.response.status !== 409) throw blocked('failed', saved.data.msg);
      dirty = false;
      setSettingsDraft('ocr', false);
      const started = await quickFetch('/api/ocr/start', { method: 'POST' });
      if (!started.response.ok || !started.data.ok) throw blocked(started.data.code || 'failed', started.data.msg);
    } catch (error) {
      quickBlocked = { code: error.code || 'failed', message: error.detail || '' };
    } finally {
      quickPhase = '';
      actionBusy = false;
      await refresh();
      rescheduleStatusPoll();
    }
  }
  async function refreshInventory() {
    window.clearTimeout(inventoryTimer);
    try {
      const response = await fetch('/api/ocr/inventory', { cache: 'no-store' });
      if (response.ok) inventory = await response.json();
    } catch { /* keep the last known count */ }
    renderQuickFix();
    if (inventory?.active) inventoryTimer = window.setTimeout(refreshInventory, 1500);
  }
  async function startOcrInventory() {
    if (inventory?.active) return;
    const button = byId('ocr-quickfix-check');
    if (button) button.disabled = true;
    try {
      const { response, data } = await quickFetch('/api/ocr/inventory', { method: 'POST' });
      if (response.ok) inventory = { ...(inventory || {}), active: true, documents_scanned: 0 };
      else if (data.code === 'no_pdfs') inventory = { status: 'completed', documents: 0, pages: 0 };
      else if (data.code !== 'ocr_running') showToast(tr('quickfix.checkFailed'), 5200, 'error');
    } catch {
      showToast(tr('quickfix.checkFailed'), 5200, 'error');
    } finally {
      if (button) button.disabled = false;
    }
    renderQuickFix();
    if (inventory?.active) inventoryTimer = window.setTimeout(refreshInventory, 800);
  }
  function focusQuickFix() {
    window.setTimeout(() => {
      const card = byId('settings-scans');
      card?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
      const start = byId('ocr-quickfix-start');
      (start && !start.closest('[hidden]') ? start : card?.querySelector('button:not([hidden])'))?.focus();
    }, 80);
  }
  function openOcrQuickFix() {
    if (typeof openSettingsAt === 'function') openSettingsAt('documents');
    if (['offer', 'check'].includes(quickFixView())) quickExpanded = true;
    renderQuickFix();
    refreshReadiness({ force: true });
    focusQuickFix();
  }
  window.openOcrQuickFix = openOcrQuickFix;
  window.refreshOcrReadiness = () => refreshReadiness({ force: true });
  window.startOcrInventory = startOcrInventory;
  function bindQuickFix() {
    byId('ocr-quickfix-check')?.addEventListener('click', startOcrInventory);
    byId('ocr-quickfix-open')?.addEventListener('click', () => {
      quickExpanded = true;
      renderQuickFix();
      refreshReadiness({ force: true });
      focusQuickFix();
    });
    byId('ocr-quickfix-dismiss')?.addEventListener('click', () => { quickExpanded = false; renderQuickFix(); });
    byId('ocr-quickfix-start')?.addEventListener('click', runQuickFix);
    byId('ocr-quickfix-retry')?.addEventListener('click', runQuickFix);
    byId('ocr-quickfix-pause')?.addEventListener('click', toggleOcrPause);
    byId('ocr-quickfix-blocked-dismiss')?.addEventListener('click', () => { quickBlocked = null; renderQuickFix(); });
    byId('ocr-quickfix-blocked-link')?.addEventListener('click', () => {
      quickBlocked = null;
      closeSettings();
      if (typeof switchTab === 'function') switchTab('library');
    });
    byId('ocr-quickfix-see')?.addEventListener('click', () => {
      const wrap = byId('ocr-errors-wrap');
      if (!wrap) return;
      wrap.open = true;
      wrap.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
      byId('ocr-errors')?.focus();
    });
    refreshInventory();
  }

  function init() {
    if (!byId('ocr-enabled')) return;
    bindQuickFix();
    ['ocr-enabled', 'ocr-model'].forEach(id => byId(id).addEventListener('input', () => { dirty = true; updateControls(); }));
    byId('ocr-errors-more').addEventListener('click', () => { visibleErrors += 20; renderErrors(); });
    window.addEventListener('bibliobrain:languagechange', () => {
      updateControls(); renderErrors(); renderStatusLine(); renderReadiness(); renderInstallLabel();
    });
    byId('ocr-model').addEventListener('change', refreshReadiness);
    const observer = new MutationObserver(refreshReadiness);
    ['settings-panel', 'settings-pane-documents', 'settings-pane-expert-indexing'].forEach(id => {
      if (byId(id)) observer.observe(byId(id), { attributes: true, attributeFilter: ['class'] });
    });
    byId('ocr-save').addEventListener('click', () => action('settings', {
      enabled: byId('ocr-enabled').checked, model: byId('ocr-model').value,
    }, 'PUT'));
    byId('ocr-start').addEventListener('click', () => action('start'));
    byId('ocr-install')?.addEventListener('click', installDefaultModel);
    byId('ocr-pause').addEventListener('click', () => action('pause'));
    byId('ocr-retry').addEventListener('click', () => action('start?retry_failed=true'));
    updateControls();
    renderInstallLabel();
    byId('ocr-status').textContent = tr('disabled');
    // Poll quickly only while OCR works; an idle queue was polled every 3 s
    // for the whole session.
    const schedule = () => {
      window.clearTimeout(pollTimer);
      const busy = Boolean(lastStatus?.active) || ['scanning', 'running'].includes(lastStatus?.status);
      pollTimer = window.setTimeout(async () => {
        if (!document.hidden) await refresh();
        schedule();
      }, busy ? 3000 : 30000);
    };
    rescheduleStatusPoll = schedule;
    // A RAG load may start or pause the OCR: don't wait for the 30 s idle poll.
    window.refreshOcrStatus = () => refresh().finally(schedule);
    window.addEventListener('bibliobrain:languagechange', () => renderOcrStatus(lastStatus));
    refresh().finally(schedule);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
