//  BUG REPORT
// ---------------------------------------------
function _setBugReportStatus(message = '', type = '') {
  const status = document.getElementById('bug-report-status');
  if (!status) return;
  status.textContent = message;
  status.classList.toggle('is-success', type === 'success');
}

function _setBugReportDisclosureState(groupId, included) {
  const group = document.getElementById(groupId);
  if (!group) return;
  group.classList.toggle('is-included', included);
  group.classList.toggle('is-excluded', !included);
  const includedStatus = group.querySelector('[data-bug-report-state="included"]');
  const excludedStatus = group.querySelector('[data-bug-report-state="excluded"]');
  if (includedStatus) includedStatus.hidden = !included;
  if (excludedStatus) excludedStatus.hidden = included;
}

function _updateBugReportDataDisclosure() {
  _setBugReportDisclosureState(
    'bug-report-data-diagnostics',
    Boolean(document.getElementById('bug-report-include-diagnostics')?.checked),
  );
  _setBugReportDisclosureState(
    'bug-report-data-chat',
    Boolean(document.getElementById('bug-report-include-chat')?.checked && state.activeChatId),
  );
  _setBugReportDisclosureState(
    'bug-report-data-screenshot',
    Boolean(state.bugReportScreenshotDataUrl),
  );
}

function _bugReportDataUrlBytes(dataUrl) {
  const encoded = String(dataUrl || '').split(',', 2)[1] || '';
  if (!encoded) return 0;
  const padding = encoded.endsWith('==') ? 2 : (encoded.endsWith('=') ? 1 : 0);
  return Math.max(0, Math.floor((encoded.length * 3) / 4) - padding);
}

function clearBugReportScreenshot() {
  state.bugReportScreenshotDataUrl = '';
  state.bugReportScreenshotMeta = null;
  const preview = document.getElementById('bug-report-screenshot-preview');
  const image = document.getElementById('bug-report-screenshot-image');
  const meta = document.getElementById('bug-report-screenshot-meta');
  if (preview) preview.hidden = true;
  if (image) image.removeAttribute('src');
  if (meta) meta.textContent = '';
  _updateBugReportDataDisclosure();
}

function _setBugReportScreenshot(dataUrl, width, height) {
  const bytes = _bugReportDataUrlBytes(dataUrl);
  state.bugReportScreenshotDataUrl = dataUrl;
  state.bugReportScreenshotMeta = { width, height, bytes };
  const preview = document.getElementById('bug-report-screenshot-preview');
  const image = document.getElementById('bug-report-screenshot-image');
  const meta = document.getElementById('bug-report-screenshot-meta');
  if (image) image.src = dataUrl;
  if (meta) meta.textContent = `${width}×${height} · ${Math.ceil(bytes / 1024)} Kio`;
  if (preview) preview.hidden = false;
  _updateBugReportDataDisclosure();
}

function _compressBugReportImage(source, sourceWidthValue, sourceHeightValue) {
  const maxBytes = 285 * 1024;
  const sourceWidth = Math.max(1, Number(sourceWidthValue || 0));
  const sourceHeight = Math.max(1, Number(sourceHeightValue || 0));
  let scale = Math.min(1, 1280 / sourceWidth, 800 / sourceHeight);
  const qualities = [0.82, 0.72, 0.62, 0.52, 0.42];
  let best = null;
  for (let resizeAttempt = 0; resizeAttempt < 5; resizeAttempt += 1) {
    const width = Math.max(320, Math.round(sourceWidth * scale));
    const height = Math.max(180, Math.round(sourceHeight * scale));
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d', { alpha: false });
    if (!context) throw new Error('capture_canvas_unavailable');
    context.drawImage(source, 0, 0, width, height);
    for (const quality of qualities) {
      const dataUrl = canvas.toDataURL('image/jpeg', quality);
      best = { dataUrl, width, height };
      if (_bugReportDataUrlBytes(dataUrl) <= maxBytes) return best;
    }
    scale *= 0.78;
  }
  if (!best || _bugReportDataUrlBytes(best.dataUrl) > maxBytes) {
    throw new Error('capture_too_large');
  }
  return best;
}

function openBugReportScreenshotFilePicker() {
  document.getElementById('bug-report-screenshot-file')?.click();
}

async function _attachBugReportImageFile(file) {
  if (!(file instanceof Blob) || !String(file.type || '').startsWith('image/') || file.size > 20 * 1024 * 1024) {
    _setBugReportStatus(_ti('bugReport.captureInvalidImage'));
    return false;
  }
  const objectUrl = URL.createObjectURL(file);
  try {
    const source = new Image();
    source.src = objectUrl;
    if (typeof source.decode === 'function') {
      await source.decode();
    } else {
      await new Promise((resolve, reject) => {
        source.onload = resolve;
        source.onerror = reject;
      });
    }
    if (!source.naturalWidth || !source.naturalHeight) throw new Error('capture_invalid_image');
    const screenshot = _compressBugReportImage(source, source.naturalWidth, source.naturalHeight);
    _setBugReportScreenshot(screenshot.dataUrl, screenshot.width, screenshot.height);
    _setBugReportStatus(_ti('bugReport.captureReady'), 'success');
    return true;
  } catch (_) {
    _setBugReportStatus(_ti('bugReport.captureInvalidImage'));
    return false;
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

async function handleBugReportScreenshotFile(event) {
  const input = event?.currentTarget || document.getElementById('bug-report-screenshot-file');
  const file = input?.files?.[0] || null;
  if (file) await _attachBugReportImageFile(file);
  if (input) input.value = '';
}

document.addEventListener('paste', (event) => {
  const modal = document.getElementById('bug-report-modal');
  if (!modal || modal.hidden) return;
  const items = Array.from(event.clipboardData?.items || []);
  const imageItem = items.find((item) => item.kind === 'file' && item.type.startsWith('image/'));
  const file = imageItem?.getAsFile?.();
  if (!file) return;
  event.preventDefault();
  _attachBugReportImageFile(file);
});

async function captureBugReportScreenshot() {
  if (!navigator.mediaDevices?.getDisplayMedia) {
    _setBugReportStatus(_ti(
      window.isSecureContext === false
        ? 'bugReport.captureNeedsSecureContext'
        : 'bugReport.captureUnsupported',
    ));
    openBugReportScreenshotFilePicker();
    return;
  }
  const button = document.getElementById('bug-report-capture-screenshot');
  const modal = document.getElementById('bug-report-modal');
  const overlay = document.getElementById('bug-report-overlay');
  let stream = null;
  if (button) button.disabled = true;
  _setBugReportStatus(_ti('bugReport.capturePicking'));
  try {
    if (modal) modal.hidden = true;
    if (overlay) overlay.hidden = true;
    await new Promise((resolve) => window.requestAnimationFrame(resolve));
    stream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: false,
      preferCurrentTab: true,
      selfBrowserSurface: 'include',
    });
    const video = document.createElement('video');
    video.muted = true;
    video.srcObject = stream;
    await video.play();
    await new Promise((resolve) => window.requestAnimationFrame(resolve));
    if (!video.videoWidth || !video.videoHeight) throw new Error('capture_invalid_image');
    const screenshot = _compressBugReportImage(video, video.videoWidth, video.videoHeight);
    _setBugReportScreenshot(screenshot.dataUrl, screenshot.width, screenshot.height);
    _setBugReportStatus(_ti('bugReport.captureReady'), 'success');
  } catch (error) {
    _setBugReportStatus(
      _ti(error?.message === 'capture_too_large' ? 'bugReport.captureTooLarge' : 'bugReport.captureFailed'),
    );
  } finally {
    for (const track of stream?.getTracks?.() || []) track.stop();
    if (modal) modal.hidden = false;
    if (overlay) overlay.hidden = false;
    if (button) button.disabled = false;
    button?.focus();
  }
}

async function openBugReport() {
  const modal = document.getElementById('bug-report-modal');
  const overlay = document.getElementById('bug-report-overlay');
  if (!modal || !overlay) return;
  if (typeof isMobileNavViewport === 'function' && isMobileNavViewport()) {
    setMobileNavOpen(false);
  }
  state.bugReportReturnFocus = document.activeElement;
  modal.hidden = false;
  overlay.hidden = false;
  document.getElementById('bug-report-btn')?.classList.add('is-open');
  document.body.classList.add('bug-report-open');
  const chatCheckbox = document.getElementById('bug-report-include-chat');
  if (chatCheckbox) {
    chatCheckbox.checked = false;
    chatCheckbox.disabled = !state.activeChatId;
    chatCheckbox.title = state.activeChatId ? '' : _ti('bugReport.chatUnavailable');
  }
  _updateBugReportDataDisclosure();
  _setBugReportStatus('');
  window.setTimeout(() => document.getElementById('bug-report-description')?.focus(), 20);

  try {
    const response = await fetch('/api/bug-reports/status');
    const payload = await response.json();
    if (!payload?.configured) {
      _setBugReportStatus(_ti('bugReport.notConfigured'));
    }
  } catch (_) {
    _setBugReportStatus(_ti('bugReport.statusUnavailable'));
  }
}

function closeBugReport() {
  const modal = document.getElementById('bug-report-modal');
  const overlay = document.getElementById('bug-report-overlay');
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  overlay.hidden = true;
  document.getElementById('bug-report-btn')?.classList.remove('is-open');
  document.body.classList.remove('bug-report-open');
  clearBugReportScreenshot();
  const restore = state.bugReportReturnFocus;
  state.bugReportReturnFocus = null;
  if (restore && typeof restore.focus === 'function') restore.focus();
}

async function submitBugReport(event) {
  event?.preventDefault();
  const description = String(document.getElementById('bug-report-description')?.value || '').trim();
  if (description.length < 10) {
    _setBugReportStatus(_ti('bugReport.descriptionTooShort'));
    document.getElementById('bug-report-description')?.focus();
    return;
  }
  const includeDiagnostics = Boolean(document.getElementById('bug-report-include-diagnostics')?.checked);
  const includeChat = Boolean(document.getElementById('bug-report-include-chat')?.checked && state.activeChatId);
  const submit = document.getElementById('bug-report-submit');
  if (submit) submit.disabled = true;
  _setBugReportStatus(_ti('bugReport.sending'));
  const activeView = document.querySelector('.tab-panel.active')?.id || '';
  try {
    const response = await fetch('/api/bug-reports', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        description,
        steps: String(document.getElementById('bug-report-steps')?.value || '').trim(),
        contact: String(document.getElementById('bug-report-contact')?.value || '').trim(),
        include_diagnostics: includeDiagnostics,
        include_chat: includeChat,
        active_chat_id: includeChat ? state.activeChatId : '',
        current_model: String(
          state.activeStreamModel || document.getElementById('model-select')?.value || '',
        ).trim(),
        screenshot_data_url: state.bugReportScreenshotDataUrl || '',
        client_logs: includeDiagnostics ? state.recentClientErrors : [],
        client_context: {
          language: document.documentElement.lang || 'fr',
          view: activeView,
        },
      }),
    });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* handled below */ }
    if (!response.ok || !payload?.ok) {
      const errorMessages = {
        bug_report_not_configured: 'bugReport.notConfigured',
        bug_report_invalid_url: 'bugReport.invalidConfiguration',
        bug_report_invalid_dsn: 'bugReport.invalidConfiguration',
        bug_report_invalid_screenshot: 'bugReport.invalidScreenshot',
        bug_report_delivery_failed: 'bugReport.deliveryFailed',
        bug_report_rate_limited: 'bugReport.rateLimited',
        bug_report_internal_error: 'bugReport.deliveryFailed',
      };
      throw new Error(_ti(errorMessages[payload?.error] || 'bugReport.deliveryFailed'));
    }
    if (payload.delivery === 'unconfirmed') {
      // Still in flight on the server side: neither sent nor failed. The form is
      // kept so the user decides whether to send it again.
      _setBugReportStatus(_ti('bugReport.deliveryUnconfirmed'));
      return;
    }
    document.getElementById('bug-report-form')?.reset();
    const diagnosticsCheckbox = document.getElementById('bug-report-include-diagnostics');
    if (diagnosticsCheckbox) diagnosticsCheckbox.checked = true;
    const chatCheckbox = document.getElementById('bug-report-include-chat');
    if (chatCheckbox) chatCheckbox.checked = false;
    closeBugReport();
    showToast(_ti('bugReport.sent'), 4200, 'success');
  } catch (error) {
    _setBugReportStatus(error?.message || _ti('bugReport.deliveryFailed'));
  } finally {
    if (submit) submit.disabled = false;
  }
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !document.getElementById('bug-report-modal')?.hidden) {
    event.preventDefault();
    closeBugReport();
  }
});

