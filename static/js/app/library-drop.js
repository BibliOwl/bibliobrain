// ---------------------------------------------
//  LIBRARY: add documents by dropping them on the window
// ---------------------------------------------
// While files are dragged over the window an overlay covers it, so the drop
// lands on the overlay and never on the page below: the Writing editor has its
// own drop handling, and a browser left alone opens a dropped PDF in place of
// the app. The server copies each file into the documents folder or, when the
// library is Zotero's storage, hands it to Zotero; the usual incremental
// reload then indexes it.
(function initLibraryDrop() {
  const REFUSALS = [
    'unsupported_format', 'zotero_pdf_only', 'file_too_large',
    'content_mismatch', 'empty_file', 'zotero_rejected',
  ];
  // Nothing can reach Zotero: the remaining files would fail the same way.
  const ZOTERO_STOPS = ['zotero_disabled'];
  const MAX_NAMES = 3;
  const OUTBOX_POLL_MS = 5000;

  let uploadTarget = null;
  let adding = false;
  let outboxTimer = 0;
  let collecting = false;
  // An image dragged from inside the page also carries a file.
  let dragFromPage = false;

  const byId = (id) => document.getElementById(id);
  const carriesFiles = (event) => !dragFromPage
    && Array.from(event.dataTransfer?.types || []).includes('Files');

  async function loadUploadTarget() {
    try {
      const response = await fetch('/api/library/upload-target', { cache: 'no-store' });
      if (response.ok) uploadTarget = await response.json();
    } catch (_) {
      // The server checks every file anyway.
    }
    return uploadTarget;
  }

  function renderHint() {
    const hint = byId('library-drop-hint');
    if (!hint) return;
    const key = uploadTarget?.target === 'zotero' ? 'library.dropHintZotero' : 'library.dropHintFolder';
    hint.dataset.i18n = key;
    hint.textContent = _ti(key);
  }

  function showOverlay() {
    const overlay = byId('library-drop-overlay');
    if (!overlay || !overlay.hidden) return;
    overlay.hidden = false;
    void loadUploadTarget().then(renderHint);
  }

  function hideOverlay() {
    const overlay = byId('library-drop-overlay');
    if (overlay) overlay.hidden = true;
  }

  function listNames(files) {
    const names = files.slice(0, MAX_NAMES).map((file) => `« ${file.name} »`).join(', ');
    const more = files.length - MAX_NAMES;
    return more > 0 ? `${names} ${_ti('library.ragDetailMore', { count: more })}` : names;
  }

  // What the server would refuse, without sending the file first.
  function refusalBeforeUpload(file, target) {
    if (!target) return '';
    const extension = (/\.[^.]+$/.exec(file.name)?.[0] || '').toLowerCase();
    if (!target.extensions.includes(extension)) {
      return target.target === 'zotero' && extension !== '.pdf' ? 'zotero_pdf_only' : 'unsupported_format';
    }
    if (file.size > target.max_bytes) return 'file_too_large';
    if (!file.size) return 'empty_file';
    return '';
  }

  async function uploadOne(file) {
    try {
      const response = await fetch(`/api/library/upload?filename=${encodeURIComponent(file.name)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: file,
      });
      const data = await response.json().catch(() => ({}));
      if (response.ok && data.ok) return data;
      const error = response.status === 413 ? 'file_too_large' : String(data.error || '');
      const known = REFUSALS.includes(error) || ZOTERO_STOPS.includes(error);
      return { status: 'error', error: known ? error : 'other' };
    } catch (_) {
      return { status: 'error', error: 'other' };
    }
  }

  async function addFilesToLibrary(files) {
    if (!files.length) return;
    if (adding) {
      showToast(_ti('library.dropBusy'), 3200, 'info');
      return;
    }
    adding = true;
    const byOutcome = new Map();
    const record = (outcome, file) => {
      if (!byOutcome.has(outcome)) byOutcome.set(outcome, []);
      byOutcome.get(outcome).push(file);
    };
    try {
      const target = await loadUploadTarget();
      const toZotero = target?.target === 'zotero';
      const label = _ti(toZotero ? 'library.dropSendingZotero' : 'library.dropUploading');
      const accepted = files.filter((file) => {
        const refusal = refusalBeforeUpload(file, target);
        if (refusal) record(refusal, file);
        return !refusal;
      });
      // The server starts a closed Zotero while it handles the first file.
      let startingZotero = toZotero && target.zotero_running === false;
      for (const [index, file] of accepted.entries()) {
        const detail = _ti('library.dropProgress', { i: index + 1, n: accepted.length, name: file.name });
        activitySet('library-drop', startingZotero ? _ti('library.dropStartingZotero') : label, detail, index / accepted.length);
        const result = await uploadOne(file);
        startingZotero = false;
        record(result.status === 'error' ? result.error : result.status, file);
        if (ZOTERO_STOPS.includes(result.error)) break;
      }
      await reportAndIndex(byOutcome, target);
    } finally {
      activityClear('library-drop');
      adding = false;
    }
  }

  async function reportAndIndex(byOutcome, target) {
    const files = (outcome) => byOutcome.get(outcome) || [];
    const added = files('added').length;
    // A PDF Zotero found no record for is indexed all the same, titled after its file.
    const noRecord = files('zotero_unrecognized');
    const zoteroAdded = files('zotero_added').length + noRecord.length;
    const queued = files('zotero_queued').length;
    const done = [];
    const warnings = [];

    if (added) done.push(_ti('library.dropAdded', { n: formatCount(added) }));
    if (zoteroAdded) done.push(_ti('library.dropZoteroAdded', { n: formatCount(zoteroAdded) }));
    if (noRecord.length) done.push(_ti('library.dropZoteroNoRecord', { names: listNames(noRecord) }));
    if (queued) done.push(_ti('library.dropZoteroQueued', { n: formatCount(queued) }));
    if (files('duplicate').length) {
      warnings.push(_ti('library.dropDuplicate', { names: listNames(files('duplicate')) }));
    }
    for (const stop of ZOTERO_STOPS) {
      if (byOutcome.has(stop)) warnings.push(_ti(`library.dropErr.${stop}`));
    }
    const megabytes = Math.floor((target?.max_bytes || 0) / (1024 * 1024));
    for (const [outcome, refused] of byOutcome) {
      if (REFUSALS.includes(outcome)) {
        warnings.push(_ti(`library.dropErr.${outcome}`, { names: listNames(refused), max: megabytes }));
      } else if (outcome === 'other') {
        warnings.push(_ti('library.dropErr.other', { names: listNames(refused) }));
      }
    }

    const toIndex = added + zoteroAdded;
    const canIndexNow = toIndex > 0 && state.ragReady && !state.ragInitBusy;
    if (toIndex && !canIndexNow) {
      done.push(_ti(state.ragInitBusy ? 'library.dropIndexLater' : 'library.dropPrepareFirst'));
    }
    const failed = !toIndex && !queued && warnings.length > 0;
    showToast([...done, ...warnings].join('\n'), warnings.length || queued ? 9000 : 4200, failed ? 'error' : (toIndex ? 'success' : 'info'));
    if (queued) watchOutbox();
    if (!canIndexNow) return;

    await reloadLibraryRag();
    // The reload reports its own result: bring back what still needs attention.
    if (warnings.length) showToast(warnings.join('\n'), 9000, 'info');
  }

  // PDFs kept for a Zotero that was not answering: the server sends them when
  // it does; report and index them then, with nothing to drop again.
  function watchOutbox() {
    if (outboxTimer) return;
    outboxTimer = window.setInterval(async () => {
      if (collecting) return;
      collecting = true;
      try {
        const response = await fetch('/api/library/zotero-outbox/collect', { method: 'POST' });
        const data = await response.json();
        if (!(data.pending > 0)) {
          window.clearInterval(outboxTimer);
          outboxTimer = 0;
        }
        const byOutcome = new Map();
        for (const item of data.delivered || []) {
          if (!byOutcome.has(item.status)) byOutcome.set(item.status, []);
          byOutcome.get(item.status).push({ name: item.filename });
        }
        if (byOutcome.size) await reportAndIndex(byOutcome, uploadTarget);
      } catch (_) {
        // Server restarting: the next tick asks again.
      } finally {
        collecting = false;
      }
    }, OUTBOX_POLL_MS);
  }

  function pickLibraryFiles() {
    closeLibraryMoreMenu();
    const input = byId('library-file-input');
    if (!input) return;
    input.value = '';
    void loadUploadTarget().then((target) => {
      input.accept = (target?.extensions || []).join(',');
      input.click();
    });
  }
  window.pickLibraryFiles = pickLibraryFiles;

  document.addEventListener('dragstart', () => { dragFromPage = true; });
  document.addEventListener('dragend', () => { dragFromPage = false; });
  window.addEventListener('dragenter', (event) => {
    if (carriesFiles(event)) showOverlay();
  });
  window.addEventListener('dragover', (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  });
  // The overlay fills the window and its content ignores the pointer, so
  // leaving the overlay is leaving the window.
  document.addEventListener('dragleave', (event) => {
    if (event.target === byId('library-drop-overlay')) hideOverlay();
  });
  window.addEventListener('drop', (event) => {
    const fromPage = dragFromPage;
    dragFromPage = false;
    if (fromPage || !Array.from(event.dataTransfer?.types || []).includes('Files')) return;
    event.preventDefault();
    hideOverlay();
    void addFilesToLibrary(Array.from(event.dataTransfer.files || []));
  });

  function bind() {
    byId('library-file-input')?.addEventListener('change', (event) => {
      void addFilesToLibrary(Array.from(event.target.files || []));
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
