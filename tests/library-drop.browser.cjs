// Run with Node and Playwright already available in NODE_PATH. No live backend is used.
// Optional argument: a path where a screenshot of the drop overlay is written.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const screenshotPath = process.argv[2] || '';

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
    // The checks read French text: pin the language, CI browsers run in English.
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, locale: 'fr-FR' });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));

    let target = { ok: true, target: 'folder', extensions: ['.docx', '.pdf', '.txt'], max_bytes: 1024 * 1024 };
    let replies = {};
    const uploads = [];
    let outboxReplies = [];
    let outboxCollects = 0;
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/library/upload-target') {
        await route.fulfill({ status: 200, json: target });
        return;
      }
      if (url.pathname === '/api/library/zotero-outbox/collect') {
        outboxCollects += 1;
        await route.fulfill({ status: 200, json: outboxReplies.shift() || { ok: true, pending: 0, delivered: [] } });
        return;
      }
      if (url.pathname === '/api/library/upload') {
        const filename = url.searchParams.get('filename');
        uploads.push({
          filename,
          method: route.request().method(),
          body: route.request().postDataBuffer()?.toString('utf8') ?? '',
        });
        const reply = replies[filename] || { status: 200, json: { ok: true, status: 'added', filename } };
        await route.fulfill(reply);
        return;
      }
      await route.fulfill({ status: 200, json: { ok: true } });
    });

    const startUrl = `http://127.0.0.1:${server.address().port}/`;
    await page.goto(startUrl);
    await page.evaluate(() => {
      document.getElementById('startup-boot-splash')?.remove();
      window.state = { ragReady: true, ragInitBusy: false };
      window.toasts = [];
      window.reloads = 0;
      window.activity = [];
      window.showToast = (message, _duration, type) => { window.toasts.push({ message, type }); };
      window.activitySet = (id, label, detail) => { window.activity.push(`${id}|${label}|${detail}`); };
      window.activityClear = (id) => { window.activity.push(`${id}|cleared`); };
      window.formatCount = (value) => String(value);
      window._ti = (key, params) => window.BibliobrainI18n.t(key, params || undefined);
      window.closeLibraryMoreMenu = () => { document.getElementById('lib-more-popover').hidden = true; };
      window.toggleLibraryMoreMenu = () => { document.getElementById('lib-more-popover').hidden = false; };
      window.reloadLibraryRag = async () => { window.reloads += 1; return { ok: true }; };
    });
    for (const script of ['event-bindings.js', 'i18n.js', 'app/library-drop.js', 'static-event-bindings.js']) {
      await page.addScriptTag({ url: `/static/js/${script}` });
    }

    const overlayVisible = () => page.locator('#library-drop-overlay').isVisible();
    const drag = async (type, selector, files) => {
      const dataTransfer = await page.evaluateHandle((items) => {
        const transfer = new DataTransfer();
        for (const [name, content] of items) transfer.items.add(new File([content], name));
        return transfer;
      }, files);
      await page.dispatchEvent(selector, type, { dataTransfer });
    };
    const reset = () => {
      uploads.length = 0;
      replies = {};
      return page.evaluate(() => { window.toasts = []; window.reloads = 0; window.activity = []; });
    };
    const settled = () => page.waitForFunction(() => window.toasts.length > 0);
    const lastToast = () => page.evaluate(() => window.toasts.at(-1));

    // The overlay follows a file drag in and out of the window.
    assert.equal(await overlayVisible(), false);
    await drag('dragenter', 'body', [['paper.pdf', '%PDF-1.4']]);
    assert.equal(await overlayVisible(), true);
    await page.waitForFunction(() => document.getElementById('library-drop-hint').dataset.i18n === 'library.dropHintFolder');
    assert.equal(await page.locator('.library-drop-card strong').textContent(), 'Déposez pour ajouter à la bibliothèque');
    assert.equal(
      await page.evaluate(() => document.elementFromPoint(640, 400).id),
      'library-drop-overlay',
      'The card must not be a drop target of its own',
    );
    if (screenshotPath) await page.screenshot({ path: screenshotPath });
    await drag('dragleave', '#library-drop-overlay', [['paper.pdf', '%PDF-1.4']]);
    assert.equal(await overlayVisible(), false);

    // Text dragged inside the app is not a file drop.
    await page.dispatchEvent('body', 'dragenter', { dataTransfer: await page.evaluateHandle(() => {
      const transfer = new DataTransfer();
      transfer.setData('text/plain', 'title');
      return transfer;
    }) });
    assert.equal(await overlayVisible(), false);

    // An image dragged from the page carries a file but started inside the app.
    await page.dispatchEvent('body', 'dragstart');
    await drag('dragenter', 'body', [['figure.png', 'png']]);
    assert.equal(await overlayVisible(), false);
    await drag('drop', 'body', [['figure.png', 'png']]);
    await page.dispatchEvent('body', 'dragend');
    assert.deepEqual(uploads, []);

    // Folder library: each accepted file is sent raw, then the RAG reloads once.
    replies['copie.pdf'] = { status: 200, json: { ok: true, status: 'duplicate', filename: 'copie.pdf', rel_path: 'imports/paper.pdf' } };
    await drag('dragenter', 'body', [['x', 'x']]);
    await drag('drop', '#library-drop-overlay', [
      ['Étude énergie.pdf', '%PDF-1.4 étude'],
      ['outil.exe', 'MZ'],
      ['copie.pdf', '%PDF-1.4 copie'],
      ['vide.txt', ''],
      ['gros.pdf', 'x'.repeat(1024 * 1024 + 1)],
    ]);
    assert.equal(await overlayVisible(), false);
    await page.waitForFunction(() => window.reloads === 1);
    assert.equal(page.url(), startUrl, 'A dropped file must not replace the app');
    assert.deepEqual(uploads, [
      { filename: 'Étude énergie.pdf', method: 'POST', body: '%PDF-1.4 étude' },
      { filename: 'copie.pdf', method: 'POST', body: '%PDF-1.4 copie' },
    ]);
    const folderToast = await page.evaluate(() => window.toasts[0]);
    assert.equal(folderToast.type, 'success');
    assert.deepEqual(folderToast.message.split('\n'), [
      '1 document(s) ajouté(s).',
      'Déjà dans la bibliothèque : « copie.pdf »',
      'Format non pris en charge : « outil.exe »',
      'Fichier vide : « vide.txt »',
      'Trop volumineux (max. 1 Mo) : « gros.pdf »',
    ]);
    // The reload's own toast must not bury what still needs attention.
    await page.waitForFunction(() => window.toasts.length === 2);
    assert.equal((await lastToast()).message.split('\n')[0], 'Déjà dans la bibliothèque : « copie.pdf »');
    assert.deepEqual(await page.evaluate(() => window.activity), [
      'library-drop|Ajout à la bibliothèque|1 sur 2 · Étude énergie.pdf',
      'library-drop|Ajout à la bibliothèque|2 sur 2 · copie.pdf',
      'library-drop|cleared',
    ]);

    // Nothing but refusals: an error, and no reload.
    await reset();
    await drag('drop', 'body', [['outil.exe', 'MZ']]);
    await settled();
    assert.equal((await lastToast()).type, 'error');
    assert.equal(await page.evaluate(() => window.reloads), 0);

    // Index not loaded yet: the file is stored, the reload is left to the user.
    await reset();
    await page.evaluate(() => { state.ragReady = false; });
    await drag('drop', 'body', [['paper.pdf', '%PDF-1.4']]);
    await settled();
    assert.deepEqual((await lastToast()).message.split('\n'), [
      '1 document(s) ajouté(s).',
      'Préparez la bibliothèque pour pouvoir les interroger.',
    ]);
    assert.equal(await page.evaluate(() => window.reloads), 0);

    // An indexing run is in progress: same, with the matching explanation.
    await reset();
    await page.evaluate(() => { state.ragReady = true; state.ragInitBusy = true; });
    await drag('drop', 'body', [['paper.pdf', '%PDF-1.4']]);
    await settled();
    assert.equal((await lastToast()).message.split('\n')[1], 'Ils seront indexés à la prochaine actualisation du RAG.');
    assert.equal(await page.evaluate(() => window.reloads), 0);
    await page.evaluate(() => { state.ragInitBusy = false; });

    // Zotero library: PDFs only, sent to Zotero; a PDF without a record is
    // indexed all the same, under its file name.
    await reset();
    target = { ok: true, target: 'zotero', extensions: ['.pdf'], max_bytes: 1024 * 1024, zotero_running: true };
    replies['article.pdf'] = { status: 200, json: { ok: true, status: 'zotero_added', filename: 'article.pdf', title: 'Article' } };
    replies['scan.pdf'] = { status: 200, json: { ok: true, status: 'zotero_unrecognized', filename: 'scan.pdf', title: '' } };
    await drag('dragenter', 'body', [['x', 'x']]);
    await page.waitForFunction(() => document.getElementById('library-drop-hint').dataset.i18n === 'library.dropHintZotero');
    await drag('drop', '#library-drop-overlay', [['article.pdf', '%PDF-1.4 a'], ['notes.docx', 'PK'], ['scan.pdf', '%PDF-1.4 s']]);
    await page.waitForFunction(() => window.reloads === 1);
    assert.deepEqual(uploads.map(upload => upload.filename), ['article.pdf', 'scan.pdf']);
    const zoteroToast = await page.evaluate(() => window.toasts[0]);
    assert.deepEqual(zoteroToast.message.split('\n'), [
      '2 PDF ajouté(s) à Zotero.',
      'Zotero n’a pas trouvé de fiche pour « scan.pdf » : le nom du fichier sert de titre.',
      'Avec une bibliothèque Zotero, seuls les PDF peuvent être ajoutés : « notes.docx »',
    ]);
    assert.equal(zoteroToast.type, 'success');
    assert.equal(await page.evaluate(() => window.activity[0]), 'library-drop|Envoi à Zotero|1 sur 2 · article.pdf');

    // Zotero closed: the server starts it during the first upload, and the page says so.
    await reset();
    target = { ...target, zotero_running: false };
    replies['a.pdf'] = { status: 200, json: { ok: true, status: 'zotero_added', filename: 'a.pdf', title: 'A' } };
    replies['b.pdf'] = { status: 200, json: { ok: true, status: 'zotero_added', filename: 'b.pdf', title: 'B' } };
    await drag('drop', 'body', [['a.pdf', '%PDF-1.4 a'], ['b.pdf', '%PDF-1.4 b']]);
    await page.waitForFunction(() => window.reloads === 1);
    assert.deepEqual(await page.evaluate(() => window.activity), [
      'library-drop|Démarrage de Zotero…|1 sur 2 · a.pdf',
      'library-drop|Envoi à Zotero|2 sur 2 · b.pdf',
      'library-drop|cleared',
    ]);
    assert.equal((await page.evaluate(() => window.toasts[0])).message, '2 PDF ajouté(s) à Zotero.');

    // Zotero still does not answer (held at its update prompt): the PDFs wait
    // on the server and nothing has to be dropped again.
    await reset();
    await page.evaluate(() => {
      const realSetInterval = window.setInterval;
      window.setInterval = (callback, delay) => realSetInterval(callback, Math.min(delay, 40));
    });
    replies['a.pdf'] = { status: 200, json: { ok: true, status: 'zotero_queued', filename: 'a.pdf' } };
    replies['b.pdf'] = { status: 200, json: { ok: true, status: 'zotero_queued', filename: 'b.pdf' } };
    outboxReplies = [
      { ok: true, pending: 2, delivered: [] },
      { ok: true, pending: 2, delivered: [] },
      { ok: true, pending: 0, delivered: [
        { filename: 'a.pdf', status: 'zotero_added' },
        { filename: 'b.pdf', status: 'zotero_unrecognized' },
      ] },
    ];
    await drag('drop', 'body', [['a.pdf', '%PDF-1.4 a'], ['b.pdf', '%PDF-1.4 b']]);
    await settled();
    assert.deepEqual(uploads.map(upload => upload.filename), ['a.pdf', 'b.pdf']);
    assert.deepEqual(await page.evaluate(() => window.toasts[0]), {
      message: '2 PDF en attente de Zotero : ils lui seront envoyés dès qu’il répondra, sans rien refaire.',
      type: 'info',
    });
    // The user answers Zotero; the server delivers; the page reports and indexes.
    await page.waitForFunction(() => window.reloads === 1);
    assert.deepEqual(await page.evaluate(() => window.toasts[1]), {
      message: '2 PDF ajouté(s) à Zotero.\nZotero n’a pas trouvé de fiche pour « b.pdf » : le nom du fichier sert de titre.',
      type: 'success',
    });
    const collectsWhenEmpty = outboxCollects;
    assert.equal(collectsWhenEmpty, 3);
    await page.waitForTimeout(250);
    assert.equal(outboxCollects, collectsWhenEmpty, 'Nothing is waiting: the page stops asking');

    // Sending to Zotero switched off in the configuration: stop at the first file.
    await reset();
    replies['a.pdf'] = { status: 503, json: { ok: false, error: 'zotero_disabled', msg: '' } };
    await drag('drop', 'body', [['a.pdf', '%PDF-1.4 a'], ['b.pdf', '%PDF-1.4 b']]);
    await settled();
    assert.deepEqual(uploads.map(upload => upload.filename), ['a.pdf']);
    assert.deepEqual(await lastToast(), {
      message: 'L’envoi vers Zotero est désactivé dans la configuration.',
      type: 'error',
    });
    assert.equal(await page.evaluate(() => window.reloads), 0);
    // A server refusal the page could not foresee is still reported by name.
    await reset();
    replies['faux.pdf'] = { status: 415, json: { ok: false, error: 'content_mismatch', msg: '' } };
    replies['panne.pdf'] = { status: 500, json: { ok: false, error: 'storage_failed', msg: '' } };
    await drag('drop', 'body', [['faux.pdf', 'MZ'], ['panne.pdf', '%PDF-1.4']]);
    await settled();
    assert.deepEqual((await lastToast()).message.split('\n'), [
      'Contenu illisible ou extension incorrecte : « faux.pdf »',
      'Ajout impossible : « panne.pdf »',
    ]);

    // Keyboard path: the Library menu opens a file picker limited to what the library takes.
    await reset();
    target = { ok: true, target: 'folder', extensions: ['.docx', '.pdf', '.txt'], max_bytes: 1024 * 1024 };
    await page.evaluate(() => {
      switchTabForTest();
      function switchTabForTest() {
        document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.remove('active'));
        document.getElementById('tab-library').classList.add('active');
        document.getElementById('lib-more-popover').hidden = false;
      }
    });
    const chooser = page.waitForEvent('filechooser');
    await page.click('#library-add-files-btn');
    const fileChooser = await chooser;
    assert.equal(fileChooser.isMultiple(), true);
    assert.equal(await page.getAttribute('#library-file-input', 'accept'), '.docx,.pdf,.txt');
    assert.equal(await page.locator('#lib-more-popover').isVisible(), false);
    await fileChooser.setFiles([{ name: 'choisi.pdf', mimeType: 'application/pdf', buffer: Buffer.from('%PDF-1.4 choisi') }]);
    await page.waitForFunction(() => window.reloads === 1);
    assert.deepEqual(uploads, [{ filename: 'choisi.pdf', method: 'POST', body: '%PDF-1.4 choisi' }]);

    // English wording comes from the same keys.
    await page.evaluate(() => window.BibliobrainI18n.setLanguage('en'));
    await reset();
    await drag('drop', 'body', [['tool.exe', 'MZ']]);
    await settled();
    assert.equal((await lastToast()).message, 'Unsupported format: « tool.exe »');

    assert.deepEqual(errors, []);
    console.log('library-drop browser test: OK');
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => { console.error(error); process.exit(1); });
