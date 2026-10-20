// Run with Node and Playwright already available in NODE_PATH. No live backend is used.
// The sidebar card "Rester à jour ?" must answer the click at once, even when the
// network check that follows is slow (as it is while the library is being indexed).
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

    const baseStatus = {
      current_version: '0.7.34', latest_version: '', update_available: false, checked_at: '',
      last_error: '', install_mode: 'packaged', install_blocker: '', job: { phase: 'idle' }, last_install: null,
    };
    let enabled = null;
    let saveFails = false;
    let releaseCheck = null;
    const checks = [];
    const status = () => ({ ...baseStatus, enabled, ask_consent: enabled === null });
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/update/status') {
        await route.fulfill({ status: 200, json: status() });
        return;
      }
      if (url.pathname === '/api/app-settings') {
        if (saveFails) {
          await route.fulfill({ status: 500, json: { detail: 'disk full' } });
          return;
        }
        enabled = JSON.parse(route.request().postData()).update_check_enabled;
        await route.fulfill({ status: 200, json: { ok: true, update_check_enabled: enabled } });
        return;
      }
      if (url.pathname === '/api/update/check') {
        checks.push(url.search);
        // Held until the test releases it: a slow manifest download.
        await new Promise(resolve => { releaseCheck = resolve; });
        await route.fulfill({ status: 200, json: { ...status(), checked_at: '2026-10-15T09:00:00+00:00' } });
        return;
      }
      await route.fulfill({ status: 200, json: { ok: true } });
    });

    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(() => {
      document.getElementById('startup-boot-splash')?.remove();
      window.state = {};
      window.toasts = [];
      window.showToast = (message, _duration, type) => { window.toasts.push({ message, type }); };
      window._ti = (key, params) => window.BibliobrainI18n.t(key, params || undefined);
    });
    for (const script of ['i18n.js', 'app/updates.js']) {
      await page.addScriptTag({ url: `/static/js/${script}` });
    }
    const card = page.locator('#update-consent');
    await card.waitFor({ state: 'visible' });

    // A failed save brings the question back, clickable again.
    saveFails = true;
    await page.click('#update-consent-accept');
    await page.waitForFunction(() => window.toasts.length > 0);
    assert.equal(await card.isVisible(), true);
    assert.equal(await page.locator('#update-consent-accept').isDisabled(), false);
    assert.deepEqual(await page.evaluate(() => window.toasts.at(-1)), { message: 'Réglage non enregistré.', type: 'error' });
    assert.deepEqual(checks, [], 'Nothing may be fetched before the choice is saved');

    // Accepting closes the card and confirms before the check has answered.
    saveFails = false;
    await page.evaluate(() => { window.toasts = []; });
    await page.click('#update-consent-accept');
    await card.waitFor({ state: 'hidden' });
    await page.waitForFunction(() => window.toasts.length > 0);
    assert.deepEqual(await page.evaluate(() => window.toasts.at(-1)), { message: 'Vérification quotidienne activée.', type: 'success' });
    assert.equal(await page.locator('#update-check-enabled').isChecked(), true);
    assert.equal(enabled, true);
    for (let i = 0; i < 50 && !releaseCheck; i += 1) await page.waitForTimeout(100);
    assert.deepEqual(checks, [''], 'The first check is the ordinary due check, not a forced one');
    releaseCheck();
    await page.waitForFunction(() => document.getElementById('update-status-text').textContent.includes('0.7.34'));
    assert.equal(await card.isVisible(), false);

    assert.deepEqual(errors, []);
    console.log('update-consent browser test passed');
  } finally {
    await browser?.close();
    server.close();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
