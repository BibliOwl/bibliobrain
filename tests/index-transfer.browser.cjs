// Run with Node and Playwright already available in NODE_PATH. No live backend is used.
// Covers the simple index import: summary, folded details, options, one-click
// download + import, and the hand-back to the startup assistant.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');

function report(overrides = {}) {
  const embedding = {
    action: 'download', model: 'embeddinggemma', model_name: 'embeddinggemma', provider: 'ollama',
    provider_label: 'Ollama', current_model: 'nomic-embed-text', current_model_name: 'nomic-embed-text',
    current_provider: 'ollama', current_provider_label: 'Ollama', same_as_current: false,
    installed: false, provider_reachable: true, local_model_name: '', digest_matches: null,
    can_download: true, install_command: 'ollama pull embeddinggemma',
    equivalence: { status: 'different', similarity: 0.1, detail: '' },
    ...(overrides.embedding || {}),
  };
  return {
    archive: { path: 'C:/x/Bibliobrain-index.zip', name: 'Bibliobrain-index.zip', bytes: 1000, created_at: '2026-10-20T10:00:00Z', application_version: '1.0' },
    index: { embedding_model: 'embeddinggemma', embedding_model_name: 'embeddinggemma', chunk_size: 1200, document_count: 12, chunk_count: 300 },
    chunk_size: { index: 1200, current: 1200, same: true },
    library: { exported_source: 'none', current_source: 'none', same_account: null, exported_item_count: 0, current_item_count: 0, exported_storage_layout: false, current_storage_layout: false },
    documents: { data_path: 'D:/Articles', data_path_exists: true, total: 12, found: 12, missing_count: 0, missing: [], content_changed_count: 0, content_changed: [], metadata_changed_count: 0, metadata_changed: [], visual_changed_count: 0, reindex_count: 2, unchanged_count: 10, new_local_count: 0 },
    existing_index: { exists: false, document_count: 0 },
    documents_copy: { mode: 'none', target: '', set_data_path: false, included_count: 0, included_bytes: 0, copy_count: 0, copy_bytes: 0, free_bytes: -1, disk_ok: true },
    disk: { required_bytes: 1, free_bytes: 10, ok: true },
    issues: [
      { level: 'warning', code: 'model_missing', message: 'Le modèle embeddinggemma n’est pas installé (test).' },
      { level: 'info', code: 'documents_changed', message: '2 fichiers diffèrent (test).' },
    ],
    verdict: 'attention',
    can_import: true,
    ...overrides,
    embedding,
  };
}

async function main() {
  const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    if (pathname === '/') {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(fs.readFileSync(path.join(root, 'static/index.html'), 'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, ''));
    } else if (pathname.startsWith('/static/')) {
      const target = path.resolve(root, '.' + pathname);
      if (!target.startsWith(root + path.sep) || !fs.existsSync(target)) { res.writeHead(404).end(); return; }
      res.setHeader('Content-Type', pathname.endsWith('.css') ? 'text/css' : 'text/javascript');
      res.end(fs.readFileSync(target));
    } else res.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, locale: 'fr-FR' });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const dialogs = [];
    page.on('dialog', dialog => { dialogs.push(dialog.message()); dialog.accept(); });
    const writes = [];
    let importedDocuments = { copied: 0, skipped: 0, target: '', data_path_set: false };
    let progress = { ok: true, active: false };
    let importDelay = 0;
    let inspect = { ok: true, cancelled: false, token: 'tok', report: report() };
    let pull = {
      ok: true,
      report: report({
        embedding: { action: 'switch', installed: true, can_download: false, local_model_name: 'embeddinggemma:latest' },
        issues: [{ level: 'info', code: 'model_switch', message: 'Bibliobrain passera à embeddinggemma (test).' }],
        verdict: 'ready',
      }),
    };
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const body = route.request().postDataJSON();
      let payload = { ok: true };
      if (route.request().method() !== 'GET') writes.push({ pathname: url.pathname, body });
      if (url.pathname === '/api/index-transfer/inspect') payload = inspect;
      if (url.pathname === '/api/index-transfer/pull-model') payload = pull;
      if (url.pathname === '/api/index-transfer/progress') payload = progress;
      if (url.pathname === '/api/index-transfer/import' && importDelay) {
        await new Promise(resolve => setTimeout(resolve, importDelay));
      }
      if (url.pathname === '/api/index-transfer/import') {
        payload = { ok: true, active: true, document_count: 12, adopted: { chunk_size: false, embedding_model: true }, documents: importedDocuments, embedding: pull.report.embedding, vector_db: {} };
      }
      if (url.pathname === '/api/index-transfer/export-estimate') {
        payload = { ok: true, document_count: 12, documents_found: 3, documents_bytes: 3 * 1024 ** 3, index_bytes: 200 * 1024 ** 2 };
      }
      if (url.pathname === '/api/index-transfer/export') {
        payload = body.include_documents
          ? { ok: true, cancelled: false, archive: 'C:/x/out.zip', bytes: 5 * 1024 ** 3, document_count: 12, documents_included: 3 }
          : { ok: true, cancelled: false, archive: 'C:/x/out.zip', bytes: 200 * 1024 ** 2, document_count: 12, documents_included: 0 };
      }
      await route.fulfill({ status: 200, json: payload });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(() => {
      document.getElementById('startup-boot-splash')?.remove();
      window.state = { appSettings: {}, providerInfo: {}, startupStatus: { library_configured: true, rag: { ready: false } }, chatSessions: [] };
      window.escapeHtml = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.escapeAttr = window.escapeHtml;
      window.showToast = () => {};
      window.postJsonOrThrow = async (url, body) => {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const d = await r.json(); if (!r.ok) throw new Error(d.msg); return d;
      };
      window.openStartupAssistant = () => { window.startupReopened = (window.startupReopened || 0) + 1; };
      window.persistStartupOnboardingDone = async () => { window.onboardingDone = true; };
      window._rememberStartupGuideDone = () => { window.guideRemembered = true; };
    });
    for (const script of ['boot.js', 'event-bindings.js', 'i18n.js', 'app/settings-panel.js', 'settings.js', 'static-event-bindings.js']) {
      await page.addScriptTag({ url: `/static/js/${script}` });
    }
    await page.evaluate(() => {
      clearInterval(_gpuStatusRefreshTimer);
      // Collaborators of the post-import refresh, outside this test's scope.
      window.fetchStartupStatus = async () => state.startupStatus;
      window.renderStartupStatus = () => {};
      window._resetRagStatusUI = () => {};
      window.updateFirstInitButtonVisibility = () => {};
      window.scheduleKnowledgeGraphReload = () => {};
      window.initRAG = () => { window.ragStarted = true; };
      window.syncPdfPathInputs = (folder) => { window.pdfFolderShown = folder; };
      document.getElementById('settings-panel').classList.add('open');
      switchSettingsTab('documents');
    });

    // The card lives in the everyday Documents area, no longer in Expert > Indexing.
    assert.equal(await page.locator('#settings-pane-documents #settings-index-transfer').count(), 1);
    assert.equal(await page.locator('#settings-pane-expert-indexing #settings-index-transfer').count(), 0);
    assert.equal(await page.locator('#index-import-btn').isVisible(), true);
    assert.equal(await page.locator('#index-transfer-options').getAttribute('open'), null, 'Options start folded');

    // Simple view: the planned download answers the "model missing" warning.
    await page.click('#index-import-btn');
    await page.waitForSelector('#index-transfer-report:not([hidden]) [data-index-transfer-action=import]');
    const confirmButton = page.locator('[data-index-transfer-action=import]');
    assert.equal((await confirmButton.textContent()).trim(), 'Télécharger embeddinggemma et importer');
    const plan = await page.locator('.index-transfer-plan li').allTextContents();
    assert.equal(plan.length, 4, plan.join(' | '));
    assert.match(plan[0], /Télécharger embeddinggemma/);
    assert.match(plan[1], /12 documents/);
    assert.match(plan[2], /Utiliser embeddinggemma/);
    assert.match(plan[3], /Réindexer 2 documents/);
    assert.equal(await page.locator('#index-transfer-report > .index-transfer-issues').count(), 0, 'No warning left to show');
    assert.match(await page.textContent('#index-transfer-report > .settings-status'), /Tout est prêt/);
    const detail = page.locator('.index-transfer-detail');
    assert.equal(await detail.getAttribute('open'), null, 'Details start folded');
    assert.equal(await detail.locator('.index-transfer-card').count(), 3);
    assert.equal(await detail.locator('[data-index-transfer-action=pull]').count(), 0, 'No second download button');
    assert.match(await detail.textContent(), /2 fichiers diffèrent/, 'Informative notes stay in the details');

    // Without the automatic download, the warning and the manual button come back.
    await page.click('#index-transfer-options > summary');
    await detail.locator('summary').click();
    await page.uncheck('#index-transfer-auto-download');
    assert.equal((await confirmButton.textContent()).trim(), 'Importer l’index');
    assert.match(await page.textContent('#index-transfer-report > .index-transfer-issues'), /pas installé/);
    assert.match(await page.textContent('#index-transfer-report > .settings-status'), /lisez les points/);
    assert.equal(await page.locator('.index-transfer-detail').getAttribute('open'), '', 'Re-render keeps the details open');
    assert.equal(await page.locator('.index-transfer-detail [data-index-transfer-action=pull]').count(), 1);
    await page.check('#index-transfer-auto-download');

    // One click: download, then import, without any confirmation dialog.
    await page.click('[data-index-transfer-action=import]');
    await page.waitForFunction(() => window.ragStarted === true);
    const transferWrites = writes.filter(write => write.pathname.startsWith('/api/index-transfer/')).map(write => write.pathname);
    assert.deepEqual(transferWrites, ['/api/index-transfer/inspect', '/api/index-transfer/pull-model', '/api/index-transfer/import']);
    assert.deepEqual(writes.find(write => write.pathname === '/api/index-transfer/import').body, { token: 'tok', confirm: true, adopt_settings: true });
    assert.deepEqual(dialogs, [], 'The button label is the consent: no extra dialog');
    assert.equal(await page.locator('#index-transfer-report').isHidden(), true);
    assert.match(await page.textContent('#index-transfer-status'), /Index importé : 12 documents/);
    assert.equal(await page.evaluate(() => Boolean(window.onboardingDone)), false, 'Not started from the assistant');

    // Replacing an index that exists here still asks; keeping settings is passed on.
    inspect = { ok: true, cancelled: false, token: 'tok2', report: report({ existing_index: { exists: true, document_count: 9 }, embedding: { action: 'ready', installed: true, can_download: false, same_as_current: true }, issues: [], verdict: 'ready' }) };
    await page.click('#index-import-btn');
    await page.waitForSelector('#index-transfer-report:not([hidden]) [data-index-transfer-action=import]');
    assert.equal((await page.locator('.index-transfer-plan li').allTextContents()).length, 2);
    await page.uncheck('#index-transfer-adopt');
    await page.click('[data-index-transfer-action=import]');
    await page.waitForFunction(() => document.getElementById('index-transfer-report').hidden);
    assert.equal(dialogs.length, 1);
    assert.match(dialogs[0], /existe déjà/);
    assert.equal(writes.at(-1).body.adopt_settings, false);
    await page.check('#index-transfer-adopt');

    // A blocked report cannot be imported.
    inspect = { ok: true, cancelled: false, token: 'tok3', report: report({ issues: [{ level: 'error', code: 'disk_space', message: 'Espace disque insuffisant (test).' }], verdict: 'blocked', can_import: false }) };
    await page.click('#index-import-btn');
    await page.waitForSelector('#index-transfer-report:not([hidden]) [data-index-transfer-action=import]');
    assert.equal(await page.locator('[data-index-transfer-action=import]').isDisabled(), true);
    assert.equal(await page.locator('.index-transfer-plan').count(), 0);
    assert.match(await page.textContent('#index-transfer-report > .index-transfer-issues'), /Espace disque/);

    // From the startup assistant: cancelling the file picker goes back to it,
    // and a successful import ends the assistant.
    inspect = { ok: true, cancelled: true };
    await page.evaluate(() => startIndexImportFromStartup());
    assert.equal(await page.evaluate(() => window.startupReopened), 1);
    inspect = { ok: true, cancelled: false, token: 'tok4', report: report() };
    await page.evaluate(() => startIndexImportFromStartup());
    await page.click('[data-index-transfer-action=cancel]');
    assert.equal(await page.evaluate(() => window.startupReopened), 2, 'Cancel in the report returns too');
    await page.evaluate(() => { window.ragStarted = false; });
    await page.evaluate(() => startIndexImportFromStartup());
    await page.click('[data-index-transfer-action=import]');
    await page.waitForFunction(() => window.ragStarted === true);
    assert.equal(await page.evaluate(() => window.onboardingDone && window.guideRemembered), true);
    assert.equal(await page.evaluate(() => window.startupReopened), 2);

    // PDFs carried by the archive: the plan names the copy and the new folder.
    const folder = 'C:/Users/test/Documents/Bibliobrain PDF';
    inspect = { ok: true, cancelled: false, token: 'tok5', report: report({
      embedding: { action: 'ready', installed: true, can_download: false, same_as_current: true },
      issues: [{ level: 'info', code: 'documents_copied', message: '12 documents de l’archive seront copiés (test).' }],
      documents: { ...report().documents, data_path: folder, data_path_exists: false, found: 0, copied_count: 12, reindex_count: 0, unchanged_count: 12 },
      documents_copy: { mode: 'new_folder', target: folder, set_data_path: true, included_count: 12, included_bytes: 60 * 1024 ** 2, copy_count: 12, copy_bytes: 60 * 1024 ** 2, free_bytes: -1, disk_ok: true },
    }) };
    inspect.report.records = { count: 12, use: true };
    importedDocuments = { copied: 12, skipped: 0, target: folder, data_path_set: true };
    await page.click('#index-import-btn');
    await page.waitForSelector('#index-transfer-report:not([hidden]) [data-index-transfer-action=import]');
    const copyPlan = await page.locator('.index-transfer-plan li').allTextContents();
    assert.match(copyPlan[0], /Copier 12 PDF \(60 Mo\) dans C:\/Users\/test\/Documents\/Bibliobrain PDF, qui devient le dossier de vos PDF/);
    assert.match(copyPlan[2], /Utiliser les 12 notices de l’archive/);
    assert.match(await page.locator('.index-transfer-card').nth(2).textContent(), /Les 12 notices de l’archive seront utilisées/);
    const copyCard = await page.locator('.index-transfer-card').nth(1).textContent();
    assert.match(copyCard, /12 copiés depuis l’archive/);
    assert.doesNotMatch(copyCard, /non configuré/, 'The new folder is not reported as missing');
    await page.evaluate(() => { window.ragStarted = false; });
    // The server's progress shows while the import request runs.
    progress = { ok: true, active: true, operation: 'import', phase: 'copy', done: 30 * 1024 ** 2, total: 60 * 1024 ** 2, label: 'a.pdf' };
    importDelay = 1500;
    await page.click('[data-index-transfer-action=import]');
    await page.waitForFunction(() => !document.getElementById('index-transfer-progress').hidden);
    assert.equal(await page.textContent('#index-transfer-progress-text'), 'Copie des PDF : 30 Mo sur 60 Mo (50 %)');
    assert.equal(await page.getAttribute('#index-transfer-progress-bar', 'aria-valuenow'), '50');
    progress = { ok: true, active: true, operation: 'import', phase: 'install', done: 0, total: 0, label: '' };
    await page.waitForFunction(() => document.getElementById('index-transfer-progress-bar').classList.contains('is-indeterminate'));
    assert.equal(await page.textContent('#index-transfer-progress-text'), 'Installation de l’index…');
    await page.waitForFunction(() => window.ragStarted === true);
    progress = { ok: true, active: false };
    importDelay = 0;
    assert.equal(await page.locator('#index-transfer-progress').isHidden(), true, 'Hidden once the request ends');
    assert.match(await page.textContent('#index-transfer-status'), /12 PDF copiés dans C:\/Users\/test\/Documents\/Bibliobrain PDF/);
    assert.equal(await page.evaluate(() => window.pdfFolderShown), folder);
    assert.equal(await page.evaluate(() => state.appSettings.data_path), folder);

    // Export: the option shows the estimate; the PDFs travel unless unchecked.
    await page.evaluate(() => { document.getElementById('index-transfer-options').open = false; });
    await page.click('#index-transfer-options > summary');
    await page.waitForFunction(() => document.getElementById('index-transfer-estimate').textContent.includes('3 PDF'));
    assert.match(await page.textContent('#index-transfer-estimate'), /\(3 PDF, environ 3,0 Go\)/);
    await page.click('#index-export-btn');
    await page.waitForFunction(() => document.getElementById('index-transfer-status').textContent.includes('exportés'));
    assert.deepEqual(writes.filter(write => write.pathname === '/api/index-transfer/export').at(-1).body, { include_documents: true });
    const exported = await page.textContent('#index-transfer-status');
    assert.match(exported, /Index et 3 PDF exportés : 5,0 Go/);
    assert.match(exported, /FAT32/, 'Files of 4 GB or more get the USB stick warning');
    await page.uncheck('#index-transfer-include-documents');
    await page.click('#index-export-btn');
    await page.waitForFunction(() => document.getElementById('index-transfer-status').textContent.includes('Index exporté'));
    assert.deepEqual(writes.filter(write => write.pathname === '/api/index-transfer/export').at(-1).body, { include_documents: false });
    assert.doesNotMatch(await page.textContent('#index-transfer-status'), /FAT32/);

    // The startup assistant offers the import as a quiet link at the end of step 1.
    assert.equal(await page.locator('#startup-panel-library #startup-import-index-btn').count(), 1);

    assert.deepEqual(errors, []);
    console.log('index-transfer browser checks passed');
  } finally {
    await browser?.close();
    server.close();
  }
}

main().catch(error => { console.error(error); process.exit(1); });
