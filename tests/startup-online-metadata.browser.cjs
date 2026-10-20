// Run with Node and Playwright already available in NODE_PATH. No live backend is used.
// Without Zotero, the first-run assistant asks whether missing metadata may be looked
// up online, and saves the answer before preparation starts.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');

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

    const saves = [];
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const body = route.request().postData() ? JSON.parse(route.request().postData()) : null;
      if (url.pathname === '/api/app-settings' && body) {
        saves.push(body);
        await route.fulfill({ status: 200, json: { ok: true, ...body } });
        return;
      }
      if (url.pathname === '/api/set-data-path') {
        await route.fulfill({ status: 200, json: { ok: true, document_count: 3 } });
        return;
      }
      if (url.pathname === '/api/zotero-load') {
        await route.fulfill({ status: 200, json: { ok: true, path: body.path } });
        return;
      }
      await route.fulfill({ status: 200, json: { ok: true } });
    });

    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(() => {
      document.getElementById('startup-boot-splash')?.remove();
      window.state = { appSettings: { metadata_source: 'none' }, providerInfo: {}, startupStatus: { rag: { ready: false } }, chatSessions: [] };
      window.showToast = () => {};
      window._showSettingSaved = () => {};
      window.populateLibrary = () => {};
      window._applyZoteroFacets = () => {};
      window.refreshRenderedSourceZoteroLinks = () => {};
      window.clearChatFilters = () => {};
      window.onNbCreateModeChange = () => {};
      window.refreshSettingsOverview = () => {};
      window._updateNbCollectionRefreshAction = () => {};
      window.optionalSelectedModel = () => '';
      window.rememberSelectedChatModel = () => {};
      window.privacyUiUpdates = 0;
      window.updateDiscoveryPrivacyUi = () => { window.privacyUiUpdates += 1; };
      window._ti = (key, params) => window.BibliobrainI18n.t(key, params || undefined);
      window._tr = text => text;
    });
    for (const script of ['i18n.js', 'app/startup.js']) {
      await page.addScriptTag({ url: `/static/js/${script}` });
    }
    // Only the saving half of settings.js is needed; its init would start the workspace.
    await page.addScriptTag({ content: `
      function beginSettingsSave() { return null; }
      function finishSettingsSave() {}
      async function settingsWrite(url, payload) {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        return r.json();
      }
    ` });
    const settingsSource = fs.readFileSync(path.join(root, 'static/js/settings.js'), 'utf8');
    const start = settingsSource.indexOf('async function syncAppSettings(');
    const end = settingsSource.indexOf('\nfunction _setVisionModelStatus(', start);
    assert.ok(start > 0 && end > start, 'syncAppSettings not found in settings.js');
    await page.addScriptTag({ content: settingsSource.slice(start, end) });

    const choice = page.locator('#startup-online-metadata-choice');
    const box = page.locator('#startup-online-metadata');
    await page.evaluate(() => {
      state.startupZoteroAutoDetect = 'done';
      document.getElementById('startup-help-overlay').classList.add('open');
      document.getElementById('startup-help-modal').classList.add('open');
      initStartupGuide();
      document.getElementById('startup-data-path').value = 'D:\\Articles';
    });

    // No Zotero: the question is shown and starts unticked.
    assert.equal(await choice.isVisible(), true);
    assert.equal(await box.isChecked(), false);
    assert.match(await choice.innerText(), /Crossref/);

    // Leaving it unticked never enables any web request.
    assert.equal(await page.evaluate(() => saveStartupLibrary()), true);
    assert.equal(saves.at(-1).auto_enrich_pdf_metadata, false);
    assert.equal(saves.at(-1).allow_external_api_requests, false);

    // Ticking it turns on both switches the lookup needs, in the same save.
    await box.check();
    assert.equal(await page.evaluate(() => saveStartupLibrary()), true);
    assert.equal(saves.at(-1).auto_enrich_pdf_metadata, true);
    assert.equal(saves.at(-1).allow_external_api_requests, true);
    assert.equal(await page.locator('#auto-enrich-pdf-metadata').isChecked(), true);
    assert.equal(await page.locator('#allow-external-discovery').isChecked(), true);
    assert.ok(await page.evaluate(() => window.privacyUiUpdates) > 0);

    // Unticking later turns the lookup off but leaves the web switch alone.
    await box.uncheck();
    assert.equal(await page.evaluate(() => saveStartupLibrary()), true);
    assert.equal(saves.at(-1).auto_enrich_pdf_metadata, false);
    assert.equal(saves.at(-1).allow_external_api_requests, true);

    // Zotero provides the metadata: the question disappears and is not applied.
    await page.evaluate(() => {
      document.getElementById('startup-online-metadata').checked = true;
      document.getElementById('auto-enrich-pdf-metadata').checked = false;
      setStartupMetadataSource('zotero');
      document.getElementById('startup-zotero-path').value = 'C:\\Zotero\\zotero.sqlite';
    });
    assert.equal(await choice.isVisible(), false);
    assert.equal(await page.evaluate(() => saveStartupLibrary()), true);
    assert.equal(saves.at(-1).metadata_source, 'zotero');
    assert.equal(saves.at(-1).auto_enrich_pdf_metadata, false);

    // Back to no source: the question comes back.
    await page.evaluate(() => setStartupMetadataSource('none'));
    assert.equal(await choice.isVisible(), true);

    // English label.
    await page.evaluate(() => { window.BibliobrainI18n.setLanguage('en'); });
    assert.match(await choice.innerText(), /Look up missing titles/);

    assert.deepEqual(errors, []);
    console.log('startup-online-metadata browser test passed');
  } finally {
    await browser?.close();
    server.close();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
